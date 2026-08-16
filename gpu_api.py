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

GITHUB_OWNER    = os.environ.get("GITHUB_OWNER",    "forgotenmywin")
GITHUB_REPO     = os.environ.get("GITHUB_REPO",     "CRD_Win")
GITHUB_WORKFLOW = os.environ.get("GITHUB_WORKFLOW", "gpu-session.yml")
GITHUB_BRANCH   = os.environ.get("GITHUB_BRANCH",   "main")
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN")
PUBLIC_API_URL  = os.environ.get("PUBLIC_API_URL")
SESSION_SECONDS = int(os.environ.get("SESSION_SECONDS", "1200"))
GITHUB_API      = "https://api.github.com"

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

sessions      = {}
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
    data["remaining_seconds"] = max(0, int(session["expires_at"] - time.time()))
    return data

# ============================================================
# FIND / GET WORKFLOW RUN
# ============================================================

def find_workflow_run(session_id):
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW}/runs"
    resp = github_request("GET", url, params={
        "event": "workflow_dispatch",
        "branch": GITHUB_BRANCH,
        "per_page": 50
    })
    if not resp or resp.status_code != 200:
        return None
    try:
        runs = resp.json().get("workflow_runs", [])
    except Exception:
        return None
    for run in runs:
        for field in ("display_title", "run_name", "name"):
            if session_id in (run.get(field) or ""):
                return run
    return None

def get_workflow_run(run_id):
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}"
    resp = github_request("GET", url)
    if not resp or resp.status_code != 200:
        return None
    try:
        return resp.json()
    except Exception:
        return None

# ============================================================
# MONITOR GITHUB (background thread)
# ============================================================

def monitor_session(session_id):
    print(f"[MONITOR] Session {session_id} started")
    run_id = None

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
                s = sessions.get(session_id)
                if s:
                    s["run_id"]           = run_id
                    s["github_status"]    = run.get("status")
                    s["github_conclusion"]= run.get("conclusion")
            print(f"[MONITOR] GitHub run found: {run_id}")
            break

        print(f"[MONITOR] Waiting for GitHub run {attempt+1}/60")
        time.sleep(3)

    if not run_id:
        with sessions_lock:
            s = sessions.get(session_id)
            if s:
                s["status"] = "error"
                s["error"]  = "GitHub workflow run not found within timeout"
        return

    while True:
        with sessions_lock:
            session = sessions.get(session_id)
            if not session:
                return
            if expired(session):
                session["status"] = "expired"
                return

        run    = get_workflow_run(run_id)
        status = (run or {}).get("status")
        conc   = (run or {}).get("conclusion")

        with sessions_lock:
            s = sessions.get(session_id)
            if not s:
                return
            s["github_status"]     = status
            s["github_conclusion"] = conc
            if s.get("worker_ready_at"):
                s["status"] = "active"

        print(f"[MONITOR] GitHub: {status} | {conc} | worker_ready: {bool((session or {}).get('worker_ready_at'))}")

        if status == "completed":
            with sessions_lock:
                s = sessions.get(session_id)
                if not s:
                    return
                if s.get("worker_ready_at"):
                    if not expired(s) and s["status"] != "stopped":
                        s["status"] = "active"
                    return

            for _ in range(36):
                time.sleep(5)
                with sessions_lock:
                    s = sessions.get(session_id)
                    if not s:
                        return
                    if s.get("worker_ready_at"):
                        s["status"] = "active"
                        return
                    if s["status"] in ("error", "stopped", "expired"):
                        return

            with sessions_lock:
                s = sessions.get(session_id)
                if s:
                    s["status"] = "error"
                    s["error"]  = (
                        "Workflow completed but GPU worker never became ready"
                        if conc == "success"
                        else f"GitHub workflow failed: {conc}"
                    )
            return

        time.sleep(5)

# ============================================================
# START SESSION
# ============================================================

@app.route("/gpu/session/start", methods=["POST"])
def start_session():
    session_id   = f"session-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    worker_token = secrets.token_urlsafe(32)
    now          = time.time()

    session = {
        "session_id":         session_id,
        "status":             "starting",
        "created_at":         utc_now(),
        "expires_at":         now + SESSION_SECONDS,
        "run_id":             None,
        "github_status":      None,
        "github_conclusion":  None,
        "gpu":                None,
        "compute_capability": None,
        "cuda_available":     None,
        "worker_ready_at":    None,
        "results":            {},
        "commands":           [],
        "error":              None,
        "worker_token":       worker_token
    }

    with sessions_lock:
        sessions[session_id] = session

    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW}/dispatches"
    payload = {
        "ref": GITHUB_BRANCH,
        "inputs": {
            "session_id":   session_id,
            "api_url":      PUBLIC_API_URL,
            "worker_token": worker_token
        }
    }

    print(f"[START] Session {session_id}")
    resp = github_request("POST", url, json=payload)

    if resp is None:
        with sessions_lock:
            session["status"] = "error"
            session["error"]  = "Could not connect to GitHub"
        return jsonify(public_session(session)), 500

    print(f"[START] GitHub HTTP: {resp.status_code}")

    if resp.status_code not in (200, 201, 202, 204):
        with sessions_lock:
            session["status"] = "error"
            session["error"]  = f"GitHub dispatch failed: {resp.status_code} {resp.text[:300]}"
        return jsonify(public_session(session)), 500

    threading.Thread(target=monitor_session, args=(session_id,), daemon=True).start()

    result = public_session(session)
    result["message"] = (
        "Session started. GPU worker is being provisioned via Kaggle. "
        f"Poll GET /gpu/session/{session_id} to check status."
    )
    return jsonify(result), 202

# ============================================================
# GET SESSION STATUS
# ============================================================

@app.route("/gpu/session/<session_id>", methods=["GET"])
def get_session(session_id):
    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Unknown session", "session_id": session_id}), 404

        if expired(session) and session["status"] not in ("error", "stopped"):
            session["status"] = "expired"

        return jsonify(public_session(session))

# ============================================================
# LIST SESSIONS
# ============================================================

@app.route("/gpu/sessions", methods=["GET"])
def list_sessions():
    with sessions_lock:
        result = []
        for s in sessions.values():
            item = {
                "session_id": s["session_id"],
                "status":     s["status"],
                "gpu":        s["gpu"],
                "remaining_seconds": max(0, int(s["expires_at"] - time.time()))
            }
            result.append(item)
        return jsonify({"sessions": result, "count": len(result)})

# ============================================================
# WORKER READY  (called by Kaggle worker)
# ============================================================

@app.route("/gpu/session/<session_id>/worker-ready", methods=["POST"])
def worker_ready(session_id):
    data  = request.get_json(silent=True) or {}
    token = request.headers.get("X-Worker-Token")

    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Unknown session"}), 404
        if token != session["worker_token"]:
            return jsonify({"error": "Unauthorized"}), 403

        session["status"]             = "active"
        session["expires_at"]         = time.time() + SESSION_SECONDS
        session["gpu"]                = data.get("gpu")
        session["compute_capability"] = data.get("compute_capability")
        session["cuda_available"]     = data.get("cuda_available")
        session["worker_ready_at"]    = utc_now()
        session["error"]              = None

    print(f"[READY] Session {session_id} | GPU: {data.get('gpu')}")
    return jsonify({
        "status":      "ok",
        "session_id":  session_id,
        "expires_in":  SESSION_SECONDS
    })

# ============================================================
# WORKER HEARTBEAT  (called by Kaggle worker)
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

        session["expires_at"] = time.time() + SESSION_SECONDS
        session["status"]     = "active"

    return jsonify({"status": "ok", "remaining_seconds": SESSION_SECONDS})

# ============================================================
# WORKER GET COMMAND  (polled by Kaggle worker)
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
# WORKER POST RESULT  (posted by Kaggle worker)
# ============================================================

@app.route("/internal/session/<session_id>/result", methods=["POST"])
def worker_result(session_id):
    token      = request.headers.get("X-Worker-Token")
    data       = request.get_json(silent=True) or {}
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
# QUEUE GPU COMMAND  (called by user)
# ============================================================

VALID_OPERATIONS = {"execute_python", "nvidia_smi", "shell", "info"}

@app.route("/gpu/session/<session_id>/command", methods=["POST"])
def queue_command(session_id):
    data      = request.get_json(silent=True) or {}
    operation = data.get("operation")

    if not operation:
        return jsonify({"error": "operation is required",
                        "valid_operations": sorted(VALID_OPERATIONS)}), 400

    if operation not in VALID_OPERATIONS:
        return jsonify({"error": f"Unknown operation: {operation}",
                        "valid_operations": sorted(VALID_OPERATIONS)}), 400

    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Unknown session"}), 404

        if expired(session):
            session["status"] = "expired"
            return jsonify({"error": "Session expired"}), 410

        # ── "info" is answered instantly from cached session data ──
        if operation == "info":
            return jsonify({
                "status":             "completed",
                "operation":          "info",
                "session_id":         session_id,
                "session_status":     session["status"],
                "gpu":                session.get("gpu"),
                "compute_capability": session.get("compute_capability"),
                "cuda_available":     session.get("cuda_available"),
                "worker_ready_at":    session.get("worker_ready_at"),
                "remaining_seconds":  max(0, int(session["expires_at"] - time.time())),
                "github_status":      session.get("github_status"),
                "github_conclusion":  session.get("github_conclusion"),
            })

        # ── all other operations need an active worker ──
        if session["status"] != "active":
            return jsonify({
                "error":          "Session is not active yet",
                "status":         session["status"],
                "hint":           f"Poll GET /gpu/session/{session_id} and wait for status=active"
            }), 409

        command_id = "cmd-" + uuid.uuid4().hex[:12]
        command = {
            "command_id":  command_id,
            "operation":   operation,
            "parameters":  data,
            "created_at":  time.time()
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

        run_id           = session.get("run_id")
        session["status"]     = "stopped"
        session["expires_at"] = time.time()

    if run_id:
        url  = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}/cancel"
        resp = github_request("POST", url)
        if resp:
            print(f"[STOP] Cancel GitHub run: {resp.status_code}")

    return jsonify({"session_id": session_id, "status": "stopped"})

# ============================================================
# HEALTH / ROOT
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    with sessions_lock:
        active = sum(1 for s in sessions.values() if s["status"] == "active")
        total  = len(sessions)
    return jsonify({
        "service":         "GPU Session API",
        "status":          "ok",
        "workflow":        GITHUB_WORKFLOW,
        "session_seconds": SESSION_SECONDS,
        "sessions_active": active,
        "sessions_total":  total
    })

@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service":         "GPU Session API",
        "status":          "online",
        "session_seconds": SESSION_SECONDS,
        "endpoints": {
            "start":   "POST /gpu/session/start",
            "status":  "GET  /gpu/session/<id>",
            "command": "POST /gpu/session/<id>/command",
            "result":  "GET  /gpu/session/<id>/result/<cmd_id>",
            "stop":    "POST /gpu/session/<id>/stop",
            "list":    "GET  /gpu/sessions"
        }
    })

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print("=" * 60)
    print("GPU SESSION API")
    print("=" * 60)
    print("GitHub  :", f"{GITHUB_OWNER}/{GITHUB_REPO}")
    print("Workflow:", GITHUB_WORKFLOW)
    print("API URL :", PUBLIC_API_URL)
    print("Session :", SESSION_SECONDS, "seconds")
    print("Port    :", port)
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
