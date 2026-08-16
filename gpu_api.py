from flask import Flask, request, jsonify
import requests
import os, time, uuid, secrets, threading
from datetime import datetime, timezone

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

GITHUB_OWNER    = os.environ.get("GITHUB_OWNER",    "forgotenmywin")
GITHUB_REPO     = os.environ.get("GITHUB_REPO",     "CRD_Win")
GITHUB_WORKFLOW = os.environ.get("GITHUB_WORKFLOW", "gpu-session.yml")
GITHUB_BRANCH   = os.environ.get("GITHUB_BRANCH",   "main")
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN")
PUBLIC_API_URL  = os.environ.get("PUBLIC_API_URL")
SESSION_SECONDS = int(os.environ.get("SESSION_SECONDS", "1200"))
GITHUB_API      = "https://api.github.com"

# How long to wait for Kaggle worker after GitHub workflow completes
# Kaggle GPU allocation can take 3-10 min → wait 15 min
WORKER_WAIT_AFTER_GITHUB = int(os.environ.get("WORKER_WAIT_SECONDS", "900"))

if not GITHUB_TOKEN:
    raise RuntimeError("GITHUB_TOKEN env var is missing")
if not PUBLIC_API_URL:
    raise RuntimeError("PUBLIC_API_URL env var is missing")

PUBLIC_API_URL = PUBLIC_API_URL.rstrip("/")

GH_HEADERS = {
    "Accept":               "application/vnd.github+json",
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
    "Content-Type":         "application/json",
    "User-Agent":           "GPU-Session-API",
}

# ═══════════════════════════════════════════════════════════
# IN-MEMORY SESSION STORE
# ═══════════════════════════════════════════════════════════

sessions      = {}
sessions_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def gh(method, url, **kw):
    try:
        return requests.request(method, url, headers=GH_HEADERS, timeout=30, **kw)
    except Exception as e:
        print(f"[GH] {method} {url} error: {e}")
        return None

def expired(s):
    return time.time() >= s["expires_at"]

def pub(s):
    d = {k: v for k, v in s.items() if k not in ("worker_token", "commands")}
    d["remaining_seconds"] = max(0, int(s["expires_at"] - time.time()))
    return d

# ═══════════════════════════════════════════════════════════
# GITHUB HELPERS
# ═══════════════════════════════════════════════════════════

def find_run(session_id):
    url  = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW}/runs"
    resp = gh("GET", url, params={"event": "workflow_dispatch",
                                   "branch": GITHUB_BRANCH, "per_page": 50})
    if not resp or resp.status_code != 200:
        return None
    for run in resp.json().get("workflow_runs", []):
        for f in ("display_title", "run_name", "name"):
            if session_id in (run.get(f) or ""):
                return run
    return None

def get_run(run_id):
    resp = gh("GET", f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}")
    if resp and resp.status_code == 200:
        try:
            return resp.json()
        except Exception:
            pass
    return None

# ═══════════════════════════════════════════════════════════
# MONITOR  (background thread per session)
# ═══════════════════════════════════════════════════════════

def monitor_session(session_id):
    print(f"[MON] {session_id} started")
    run_id = None

    # ── Find GitHub run (up to 3 min) ──────────────────────
    for attempt in range(60):
        with sessions_lock:
            s = sessions.get(session_id)
            if not s:
                return
            run_id = s.get("run_id")
        if run_id:
            break

        run = find_run(session_id)
        if run:
            run_id = run["id"]
            with sessions_lock:
                s = sessions.get(session_id)
                if s:
                    s["run_id"]            = run_id
                    s["github_status"]     = run.get("status")
                    s["github_conclusion"] = run.get("conclusion")
            print(f"[MON] GitHub run found: {run_id}")
            break

        print(f"[MON] Waiting for GitHub run {attempt+1}/60")
        time.sleep(3)

    if not run_id:
        with sessions_lock:
            s = sessions.get(session_id)
            if s:
                s["status"] = "error"
                s["error"]  = "GitHub workflow run not found within 3 min"
        return

    # ── Poll GitHub + watch for worker ─────────────────────
    gh_completed_at = None   # time when GitHub workflow finished

    while True:
        with sessions_lock:
            s = sessions.get(session_id)
            if not s or expired(s):
                if s:
                    s["status"] = "expired"
                return
            if s["status"] in ("stopped",):
                return

        run    = get_run(run_id)
        status = (run or {}).get("status")
        conc   = (run or {}).get("conclusion")

        with sessions_lock:
            s = sessions.get(session_id)
            if not s:
                return
            s["github_status"]     = status
            s["github_conclusion"] = conc
            worker_ready = bool(s.get("worker_ready_at"))
            if worker_ready and not expired(s):
                s["status"] = "active"

        print(f"[MON] github={status}/{conc} worker_ready={worker_ready}")

        # worker already connected → done
        if worker_ready:
            return

        # GitHub workflow finished
        if status == "completed":
            if gh_completed_at is None:
                gh_completed_at = time.time()
                print(f"[MON] GitHub completed. Waiting up to {WORKER_WAIT_AFTER_GITHUB}s for Kaggle worker...")

            waited = time.time() - gh_completed_at
            if waited >= WORKER_WAIT_AFTER_GITHUB:
                # Still no worker → error
                with sessions_lock:
                    s = sessions.get(session_id)
                    if s and not s.get("worker_ready_at"):
                        s["status"] = "error"
                        s["error"]  = (
                            "Kaggle GPU worker never connected. "
                            "Check Kaggle kernel logs for errors."
                            if conc == "success"
                            else f"GitHub workflow failed: {conc}"
                        )
                return

        time.sleep(5)

# ═══════════════════════════════════════════════════════════
# START SESSION
# ═══════════════════════════════════════════════════════════

@app.route("/gpu/session/start", methods=["POST"])
def start_session():
    sid   = f"session-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    token = secrets.token_urlsafe(32)
    now   = time.time()

    s = {
        "session_id":         sid,
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
        "worker_token":       token,
    }

    with sessions_lock:
        sessions[sid] = s

    url  = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW}/dispatches"
    body = {"ref": GITHUB_BRANCH, "inputs": {
        "session_id":   sid,
        "api_url":      PUBLIC_API_URL,
        "worker_token": token,
    }}

    print(f"[START] {sid}")
    resp = gh("POST", url, json=body)

    if not resp:
        with sessions_lock:
            s["status"] = "error"
            s["error"]  = "Cannot connect to GitHub"
        return jsonify(pub(s)), 500

    if resp.status_code not in (200, 201, 202, 204):
        with sessions_lock:
            s["status"] = "error"
            s["error"]  = f"GitHub dispatch failed: {resp.status_code} {resp.text[:200]}"
        return jsonify(pub(s)), 500

    threading.Thread(target=monitor_session, args=(sid,), daemon=True).start()

    result = pub(s)
    result["message"] = (
        f"Session started. Kaggle GPU is being provisioned. "
        f"Poll GET /gpu/session/{sid} — takes 3-10 min to become active."
    )
    return jsonify(result), 202

# ═══════════════════════════════════════════════════════════
# SESSION STATUS
# ═══════════════════════════════════════════════════════════

@app.route("/gpu/session/<sid>", methods=["GET"])
def get_session(sid):
    with sessions_lock:
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "Unknown session", "session_id": sid}), 404
        if expired(s) and s["status"] not in ("error", "stopped"):
            s["status"] = "expired"
        return jsonify(pub(s))

# ═══════════════════════════════════════════════════════════
# LIST SESSIONS
# ═══════════════════════════════════════════════════════════

@app.route("/gpu/sessions", methods=["GET"])
def list_sessions():
    with sessions_lock:
        rows = [{"session_id": s["session_id"], "status": s["status"],
                 "gpu": s["gpu"],
                 "remaining_seconds": max(0, int(s["expires_at"] - time.time()))}
                for s in sessions.values()]
    return jsonify({"sessions": rows, "count": len(rows)})

# ═══════════════════════════════════════════════════════════
# WORKER → READY
# ═══════════════════════════════════════════════════════════

@app.route("/gpu/session/<sid>/worker-ready", methods=["POST"])
def worker_ready(sid):
    data  = request.get_json(silent=True) or {}
    token = request.headers.get("X-Worker-Token")

    with sessions_lock:
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "Unknown session"}), 404
        if token != s["worker_token"]:
            return jsonify({"error": "Unauthorized"}), 403

        s["status"]             = "active"
        s["expires_at"]         = time.time() + SESSION_SECONDS  # timer resets here
        s["gpu"]                = data.get("gpu")
        s["compute_capability"] = data.get("compute_capability")
        s["cuda_available"]     = data.get("cuda_available")
        s["worker_ready_at"]    = utc_now()
        s["error"]              = None

    print(f"[READY] {sid} | GPU: {data.get('gpu')}")
    return jsonify({"status": "ok", "session_id": sid, "expires_in": SESSION_SECONDS})

# ═══════════════════════════════════════════════════════════
# WORKER → HEARTBEAT
# ═══════════════════════════════════════════════════════════

@app.route("/gpu/session/<sid>/heartbeat", methods=["POST"])
def worker_heartbeat(sid):
    token = request.headers.get("X-Worker-Token")

    with sessions_lock:
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "Unknown session"}), 404
        if token != s["worker_token"]:
            return jsonify({"error": "Unauthorized"}), 403
        if expired(s):
            s["status"] = "expired"
            return jsonify({"status": "expired"}), 410
        s["expires_at"] = time.time() + SESSION_SECONDS
        s["status"]     = "active"

    return jsonify({"status": "ok", "remaining_seconds": SESSION_SECONDS})

# ═══════════════════════════════════════════════════════════
# WORKER → GET COMMAND
# ═══════════════════════════════════════════════════════════

@app.route("/internal/session/<sid>/command", methods=["GET"])
def worker_get_command(sid):
    token = request.headers.get("X-Worker-Token")

    with sessions_lock:
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "Unknown session"}), 404
        if token != s["worker_token"]:
            return jsonify({"error": "Unauthorized"}), 403
        if expired(s):
            s["status"] = "expired"
            return jsonify({"command": None, "expired": True})
        if not s["commands"]:
            return jsonify({"command": None, "expired": False})
        cmd = s["commands"].pop(0)
        return jsonify({"command": cmd})

# ═══════════════════════════════════════════════════════════
# WORKER → POST RESULT
# ═══════════════════════════════════════════════════════════

@app.route("/internal/session/<sid>/result", methods=["POST"])
def worker_result(sid):
    token      = request.headers.get("X-Worker-Token")
    data       = request.get_json(silent=True) or {}
    command_id = data.get("command_id")

    with sessions_lock:
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "Unknown session"}), 404
        if token != s["worker_token"]:
            return jsonify({"error": "Unauthorized"}), 403
        if not command_id:
            return jsonify({"error": "command_id missing"}), 400
        s["results"][command_id] = data

    return jsonify({"status": "received", "command_id": command_id})

# ═══════════════════════════════════════════════════════════
# USER → QUEUE COMMAND
# ═══════════════════════════════════════════════════════════

VALID_OPS = {"execute_python", "nvidia_smi", "shell", "info"}

@app.route("/gpu/session/<sid>/command", methods=["POST"])
def queue_command(sid):
    data = request.get_json(silent=True) or {}
    op   = data.get("operation")

    if not op:
        return jsonify({"error": "operation is required",
                        "valid": sorted(VALID_OPS)}), 400
    if op not in VALID_OPS:
        return jsonify({"error": f"Unknown operation: {op}",
                        "valid": sorted(VALID_OPS)}), 400

    with sessions_lock:
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "Unknown session"}), 404
        if expired(s):
            s["status"] = "expired"
            return jsonify({"error": "Session expired"}), 410

        # "info" is answered instantly from cached data
        if op == "info":
            return jsonify({
                "status":             "completed",
                "operation":          "info",
                "session_id":         sid,
                "session_status":     s["status"],
                "gpu":                s.get("gpu"),
                "compute_capability": s.get("compute_capability"),
                "cuda_available":     s.get("cuda_available"),
                "worker_ready_at":    s.get("worker_ready_at"),
                "remaining_seconds":  max(0, int(s["expires_at"] - time.time())),
                "github_status":      s.get("github_status"),
                "github_conclusion":  s.get("github_conclusion"),
            })

        if s["status"] != "active":
            return jsonify({
                "error":  "Session not active yet",
                "status": s["status"],
                "hint":   f"Poll GET /gpu/session/{sid} and wait for status=active",
            }), 409

        cmd_id = "cmd-" + uuid.uuid4().hex[:12]
        s["commands"].append({
            "command_id": cmd_id,
            "operation":  op,
            "parameters": data,
            "created_at": time.time(),
        })

    return jsonify({"status": "queued", "command_id": cmd_id}), 202

# ═══════════════════════════════════════════════════════════
# USER → GET RESULT
# ═══════════════════════════════════════════════════════════

@app.route("/gpu/session/<sid>/result/<cmd_id>", methods=["GET"])
def get_result(sid, cmd_id):
    with sessions_lock:
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "Unknown session"}), 404
        r = s["results"].get(cmd_id)
        if r is None:
            return jsonify({"status": "pending", "command_id": cmd_id}), 202
        return jsonify({"status": "completed", "command_id": cmd_id, "result": r})

# ═══════════════════════════════════════════════════════════
# STOP SESSION
# ═══════════════════════════════════════════════════════════

@app.route("/gpu/session/<sid>/stop", methods=["POST"])
def stop_session(sid):
    with sessions_lock:
        s = sessions.get(sid)
        if not s:
            return jsonify({"error": "Unknown session"}), 404
        run_id       = s.get("run_id")
        s["status"]     = "stopped"
        s["expires_at"] = time.time()

    if run_id:
        gh("POST", f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}/cancel")

    return jsonify({"session_id": sid, "status": "stopped"})

# ═══════════════════════════════════════════════════════════
# HEALTH / ROOT
# ═══════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    with sessions_lock:
        active = sum(1 for s in sessions.values() if s["status"] == "active")
        total  = len(sessions)
    return jsonify({
        "status":              "ok",
        "sessions_active":     active,
        "sessions_total":      total,
        "session_seconds":     SESSION_SECONDS,
        "worker_wait_seconds": WORKER_WAIT_AFTER_GITHUB,
    })

@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "GPU Session API",
        "endpoints": {
            "start":   "POST /gpu/session/start",
            "status":  "GET  /gpu/session/<id>",
            "list":    "GET  /gpu/sessions",
            "command": "POST /gpu/session/<id>/command",
            "result":  "GET  /gpu/session/<id>/result/<cmd_id>",
            "stop":    "POST /gpu/session/<id>/stop",
        }
    })

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print("=" * 60)
    print(f"GPU SESSION API  —  port {port}")
    print(f"GitHub : {GITHUB_OWNER}/{GITHUB_REPO}")
    print(f"API URL: {PUBLIC_API_URL}")
    print(f"Session timeout : {SESSION_SECONDS}s")
    print(f"Worker wait max : {WORKER_WAIT_AFTER_GITHUB}s")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
