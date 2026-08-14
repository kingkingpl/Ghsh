from flask import Flask, request, jsonify
import requests
import os
import time
import uuid
import secrets
import threading
from datetime import datetime, timezone

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "forgotenmywin")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "CRD_Win")
GITHUB_WORKFLOW = os.environ.get("GITHUB_WORKFLOW", "gpu-session.yml")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
PUBLIC_API_URL = os.environ.get("PUBLIC_API_URL")

SESSION_SECONDS = int(os.environ.get("SESSION_SECONDS", "1200"))

GITHUB_API = "https://api.github.com"

# ============================================================
# VALIDATION
# ============================================================

if not GITHUB_TOKEN:
    raise RuntimeError("GITHUB_TOKEN is missing")

if not PUBLIC_API_URL:
    raise RuntimeError("PUBLIC_API_URL is missing")

PUBLIC_API_URL = PUBLIC_API_URL.rstrip("/")

HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
    "Content-Type": "application/json",
    "User-Agent": "GPU-Session-API"
}

# ============================================================
# SESSION STORAGE
# ============================================================

sessions = {}
sessions_lock = threading.Lock()

# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def github_request(method, url, **kwargs):
    try:
        return requests.request(method, url, headers=HEADERS, timeout=30, **kwargs)
    except Exception as e:
        print("GitHub request error:", repr(e))
        return None

def expired(session):
    return time.time() >= session["expires_at"]

def public_session(session):
    data = dict(session)
    data.pop("worker_token", None)
    data.pop("commands", None)
    return data

# ============================================================
# FIND WORKFLOW RUN
# ============================================================

def find_workflow_run(session_id):
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW}/runs"
    response = github_request("GET", url, params={
        "event": "workflow_dispatch",
        "branch": GITHUB_BRANCH,
        "per_page": 50
    })
    if response is None:
        return None
    if response.status_code != 200:
        print("GitHub run list failed:", response.status_code, response.text[:500])
        return None
    try:
        runs = response.json().get("workflow_runs", [])
    except Exception:
        return None

    for run in runs:
        display_title = run.get("display_title") or ""
        run_name = run.get("run_name") or ""
        name = run.get("name") or ""
        if session_id in display_title or session_id in run_name or session_id in name:
            return run
    return None

# ============================================================
# GET WORKFLOW RUN
# ============================================================

def get_workflow_run(run_id):
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}"
    response = github_request("GET", url)
    if response is None or response.status_code != 200:
        return None
    try:
        return response.json()
    except Exception:
        return None

# ============================================================
# MONITOR GITHUB
# ============================================================

def monitor_session(session_id):
    print("=" * 60)
    print("SESSION MONITOR")
    print("SESSION:", session_id)
    print("=" * 60)

    run_id = None

    # --- FIND RUN ---
    for attempt in range(60):
        with sessions_lock:
            session = sessions.get(session_id)
            if not session:
                return
            if session.get("run_id"):
                run_id = session["run_id"]
                break

        run = find_workflow_run(session_id)
        if run:
            run_id = run.get("id")
            with sessions_lock:
                session = sessions.get(session_id)
                if session:
                    session["run_id"] = run_id
                    session["github_status"] = run.get("status")
                    session["github_conclusion"] = run.get("conclusion")
            print("GitHub Run:", run_id)
            break

        print("Waiting for GitHub Run:", attempt + 1, "/60")
        time.sleep(3)

    if not run_id:
        with sessions_lock:
            session = sessions.get(session_id)
            if session:
                session["status"] = "error"
                session["error"] = "GitHub workflow run not found"
        return

    # --- MONITOR ---
    while True:
        run = get_workflow_run(run_id)
        if run is None:
            time.sleep(5)
            continue

        status = run.get("status")
        conclusion = run.get("conclusion")

        with sessions_lock:
            session = sessions.get(session_id)
            if not session:
                return

            session["github_status"] = status
            session["github_conclusion"] = conclusion
            worker_ready = bool(session.get("worker_ready_at"))

            if expired(session):
                session["status"] = "expired"
                return

            if worker_ready:
                session["status"] = "active"

        print("GitHub:", status, "|", conclusion, "| worker_ready:", worker_ready)

        # ----------------------------------------------------
        # WORKFLOW COMPLETED
        # ----------------------------------------------------
        if status == "completed":
            with sessions_lock:
                session = sessions.get(session_id)
                if not session:
                    return
                worker_ready = bool(session.get("worker_ready_at"))

                if worker_ready:
                    if expired(session):
                        session["status"] = "expired"
                    elif session["status"] != "stopped":
                        session["status"] = "active"
                    print("Workflow finished but worker was already READY.")
                    return

            # Workflow finished quickly (only pushed kernel). Wait for worker.
            print("Workflow completed. Waiting up to 180s for worker to become ready...")
            for _ in range(36):  # 36 * 5 = 180 seconds
                time.sleep(5)
                with sessions_lock:
                    session = sessions.get(session_id)
                    if not session:
                        return
                    if session.get("worker_ready_at"):
                        session["status"] = "active"
                        print("Worker became ready after workflow completion.")
                        return
                    if session["status"] in ("error", "stopped", "expired"):
                        return

            # Worker never became ready
            with sessions_lock:
                session = sessions.get(session_id)
                if session:
                    session["status"] = "error"
                    if conclusion == "success":
                        session["error"] = "Workflow completed but GPU worker never became ready"
                    else:
                        session["error"] = f"GitHub workflow failed: {conclusion}"
            return

        time.sleep(5)

# ============================================================
# START SESSION
# ============================================================

@app.route("/gpu/session/start", methods=["POST"])
def start_session():
    session_id = f"session-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    worker_token = secrets.token_urlsafe(32)
    now = time.time()

    session = {
        "session_id": session_id,
        "status": "starting",
        "created_at": utc_now(),
        "expires_at": now + SESSION_SECONDS,
        "run_id": None,
        "github_status": None,
        "github_conclusion": None,
        "gpu": None,
        "compute_capability": None,
        "cuda_available": None,
        "worker_ready_at": None,
        "results": {},
        "commands": [],
        "error": None,
        "worker_token": worker_token
    }

    with sessions_lock:
        sessions[session_id] = session

    # --- DISPATCH GITHUB ---
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW}/dispatches"
    payload = {
        "ref": GITHUB_BRANCH,
        "inputs": {
            "session_id": session_id,
            "api_url": PUBLIC_API_URL,
            "worker_token": worker_token
        }
    }

    print("=" * 60)
    print("START GPU SESSION")
    print("SESSION:", session_id)
    print("=" * 60)

    response = github_request("POST", url, json=payload)
    if response is None:
        with sessions_lock:
            session["status"] = "error"
            session["error"] = "Could not connect to GitHub"
        return jsonify(public_session(session)), 500

    print("GitHub HTTP:", response.status_code)
    if response.status_code not in (200, 201, 202, 204):
        with sessions_lock:
            session["status"] = "error"
            session["error"] = f"GitHub dispatch failed: {response.status_code} {response.text}"
        return jsonify(public_session(session)), 500

    threading.Thread(target=monitor_session, args=(session_id,), daemon=True).start()
    return jsonify(public_session(session)), 202

# ============================================================
# GET SESSION STATUS
# ============================================================

@app.route("/gpu/session/<session_id>", methods=["GET"])
def get_session(session_id):
    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Unknown session", "session_id": session_id}), 404

        remaining = max(0, int(session["expires_at"] - time.time()))
        if remaining <= 0 and session["status"] not in ("error", "stopped"):
            session["status"] = "expired"

        result = public_session(session)
        result["remaining_seconds"] = remaining
        return jsonify(result)

# ============================================================
# WORKER READY
# ============================================================

@app.route("/gpu/session/<session_id>/worker-ready", methods=["POST"])
def worker_ready(session_id):
    data = request.get_json(silent=True) or {}
    token = request.headers.get("X-Worker-Token")

    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Unknown session"}), 404
        if token != session["worker_token"]:
            return jsonify({"error": "Unauthorized"}), 403

        session["status"] = "active"
        # Timer starts when GPU actually becomes READY
        session["expires_at"] = time.time() + SESSION_SECONDS
        session["gpu"] = data.get("gpu")
        session["compute_capability"] = data.get("compute_capability")
        session["cuda_available"] = data.get("cuda_available")
        session["worker_ready_at"] = utc_now()
        session["error"] = None

    print("=" * 60)
    print("GPU WORKER READY")
    print("SESSION:", session_id)
    print("GPU:", data.get("gpu"))
    print("COMPUTE:", data.get("compute_capability"))
    print("=" * 60)

    return jsonify({
        "status": "ok",
        "session_id": session_id,
        "worker_status": "active",
        "expires_in": SESSION_SECONDS
    })

# ============================================================
# WORKER HEARTBEAT
# ============================================================

@app.route("/gpu/session/<session_id>/heartbeat", methods=["POST"])
def worker_heartbeat(session_id):
    token = request.headers.get("X-Worker-Token")

    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Unknown session"}), 404
        if token != session["worker_token"]:
            return jsonify({"error": "Unauthorized"}), 403

        if expired(session):
            session["status"] = "expired"
            return jsonify({"status": "expired"}), 410

        # Extend only while worker is alive
        session["expires_at"] = time.time() + SESSION_SECONDS
        session["status"] = "active"

    return jsonify({
        "status": "ok",
        "session_id": session_id,
        "remaining_seconds": SESSION_SECONDS
    })

# ============================================================
# WORKER GET COMMAND
# ============================================================

@app.route("/internal/session/<session_id>/command", methods=["GET"])
def worker_get_command(session_id):
    token = request.headers.get("X-Worker-Token")

    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Unknown session"}), 404
        if token != session["worker_token"]:
            return jsonify({"error": "Unauthorized"}), 403

        if expired(session):
            session["status"] = "expired"
            return jsonify({"command": None, "expired": True})

        if not session["commands"]:
            return jsonify({"command": None, "expired": False})

        command = session["commands"].pop(0)
        return jsonify({"command": command})

# ============================================================
# WORKER RESULT
# ============================================================

@app.route("/internal/session/<session_id>/result", methods=["POST"])
def worker_result(session_id):
    token = request.headers.get("X-Worker-Token")
    data = request.get_json(silent=True) or {}
    command_id = data.get("command_id")

    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Unknown session"}), 404
        if token != session["worker_token"]:
            return jsonify({"error": "Unauthorized"}), 403
        if not command_id:
            return jsonify({"error": "command_id missing"}), 400

        session["results"][command_id] = data
        return jsonify({"status": "received", "command_id": command_id})

# ============================================================
# QUEUE GPU COMMAND
# ============================================================

@app.route("/gpu/session/<session_id>/command", methods=["POST"])
def queue_command(session_id):
    data = request.get_json(silent=True) or {}
    operation = data.get("operation")

    if not operation:
        return jsonify({"error": "operation is required"}), 400

    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Unknown session"}), 404

        if expired(session):
            session["status"] = "expired"
            return jsonify({"error": "Session expired"}), 410

        if session["status"] != "active":
            return jsonify({"error": "Session is not active", "status": session["status"]}), 409

        command_id = "cmd-" + uuid.uuid4().hex[:12]
        command = {
            "command_id": command_id,
            "operation": operation,
            "parameters": data,
            "created_at": time.time()
        }
        session["commands"].append(command)

    return jsonify({"status": "queued", "command_id": command_id}), 202

# ============================================================
# GET RESULT
# ============================================================

@app.route("/gpu/session/<session_id>/result/<command_id>", methods=["GET"])
def get_result(session_id, command_id):
    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Unknown session"}), 404

        result = session["results"].get(command_id)
        if result is None:
            return jsonify({"status": "pending", "command_id": command_id}), 202

        return jsonify({"status": "completed", "command_id": command_id, "result": result})

# ============================================================
# STOP SESSION
# ============================================================

@app.route("/gpu/session/<session_id>/stop", methods=["POST"])
def stop_session(session_id):
    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Unknown session"}), 404

        run_id = session.get("run_id")
        session["status"] = "stopped"
        session["expires_at"] = time.time()

        if run_id:
            url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}/cancel"
            response = github_request("POST", url)
            if response is not None:
                print("Cancel:", response.status_code)

    return jsonify({"session_id": session_id, "status": "stopped"})

# ============================================================
# HEALTH
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "service": "GPU Session API",
        "status": "ok",
        "workflow": GITHUB_WORKFLOW,
        "session_seconds": SESSION_SECONDS
    })

# ============================================================
# ROOT
# ============================================================

@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "GPU Session API",
        "status": "online",
        "session_seconds": SESSION_SECONDS
    })

# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print("=" * 60)
    print("GPU SESSION API")
    print("=" * 60)
    print("GitHub:", f"{GITHUB_OWNER}/{GITHUB_REPO}")
    print("Workflow:", GITHUB_WORKFLOW)
    print("Public API:", PUBLIC_API_URL)
    print("Session:", SESSION_SECONDS)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
