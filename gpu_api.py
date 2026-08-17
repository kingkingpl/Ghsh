import os
import time
import uuid
import secrets
import threading
import requests

from flask import Flask, jsonify, request

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "forgotenmywin")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "CRD_Win")
GITHUB_WORKFLOW = os.environ.get(
    "GITHUB_WORKFLOW",
    "gpu-session.yml"
)
GITHUB_BRANCH = os.environ.get(
    "GITHUB_BRANCH",
    "main"
)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

PUBLIC_API_URL = os.environ.get(
    "PUBLIC_API_URL",
    "https://ghsh-production.up.railway.app"
).rstrip("/")

SESSION_SECONDS = int(
    os.environ.get("SESSION_SECONDS", "1200")
)

# ============================================================
# SESSION STORAGE
# ============================================================

sessions = {}

lock = threading.Lock()


# ============================================================
# HELPERS
# ============================================================

def now():
    return time.time()


def create_session():

    session_id = (
        f"session-{int(now())}-"
        f"{secrets.token_hex(4)}"
    )

    worker_token = secrets.token_urlsafe(32)

    created = now()
    expires = created + SESSION_SECONDS

    data = {
        "session_id": session_id,
        "worker_token": worker_token,

        "created_at": created,
        "expires_at": expires,

        "status": "starting",

        "worker_ready_at": None,

        "gpu": None,
        "cuda_available": None,
        "compute_capability": None,

        "run_id": None,
        "github_status": None,
        "github_conclusion": None,

        "results": {},
        "error": None,

        "last_heartbeat": None,
        "command": None,
    }

    with lock:
        sessions[session_id] = data

    return data


def public_session(data):

    if not data:
        return None

    remaining = max(
        0,
        int(data["expires_at"] - now())
    )

    return {
        "session_id": data["session_id"],
        "created_at": data["created_at"],
        "expires_at": data["expires_at"],
        "remaining_seconds": remaining,

        "status": data["status"],

        "worker_ready_at": data["worker_ready_at"],

        "gpu": data["gpu"],
        "cuda_available": data["cuda_available"],
        "compute_capability": data["compute_capability"],

        "run_id": data["run_id"],
        "github_status": data["github_status"],
        "github_conclusion": data["github_conclusion"],

        "results": data["results"],
        "error": data["error"],
    }


def get_session(session_id):

    with lock:
        return sessions.get(session_id)


# ============================================================
# AUTHENTICATION
# ============================================================

def worker_auth(session):

    if session is None:
        return False

    supplied = request.headers.get(
        "X-Worker-Token",
        ""
    )

    expected = session.get(
        "worker_token",
        ""
    )

    if not supplied:
        print("[AUTH] Missing X-Worker-Token")
        return False

    if not expected:
        print("[AUTH] Session has no worker token")
        return False

    ok = secrets.compare_digest(
        supplied,
        expected
    )

    if not ok:
        print("[AUTH] Invalid worker token")

    return ok


# ============================================================
# GITHUB DISPATCH
# ============================================================

def dispatch_github(session):

    if not GITHUB_TOKEN:
        raise RuntimeError(
            "GITHUB_TOKEN is missing"
        )

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}/actions/workflows/"
        f"{GITHUB_WORKFLOW}/dispatches"
    )

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }

    payload = {
        "ref": GITHUB_BRANCH,
        "inputs": {
            "session_id": session["session_id"],
            "api_url": PUBLIC_API_URL,
            "worker_token": session["worker_token"],
        },
    }

    print("========================================")
    print("GITHUB DISPATCH")
    print("========================================")

    print("URL:", url)
    print("SESSION:", session["session_id"])
    print("API:", PUBLIC_API_URL)

    r = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=30,
    )

    print(
        "GitHub status:",
        r.status_code
    )

    if r.status_code not in (200, 201, 204):

        raise RuntimeError(
            "GitHub workflow dispatch failed: "
            f"{r.status_code} {r.text}"
        )

    return True


# ============================================================
# FIND GITHUB RUN
# ============================================================

def find_github_run(session):

    try:

        url = (
            f"https://api.github.com/repos/"
            f"{GITHUB_OWNER}/"
            f"{GITHUB_REPO}/actions/workflows/"
            f"{GITHUB_WORKFLOW}/runs"
        )

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        r = requests.get(
            url,
            headers=headers,
            params={
                "branch": GITHUB_BRANCH,
                "per_page": 10,
            },
            timeout=20,
        )

        if r.status_code != 200:
            return

        data = r.json()

        runs = data.get(
            "workflow_runs",
            []
        )

        for run in runs:

            created_at = run.get(
                "created_at",
                ""
            )

            # newest run is sufficient here
            if run.get("status") in (
                "queued",
                "in_progress",
                "completed"
            ):

                with lock:

                    if session["run_id"] is None:

                        session["run_id"] = run.get(
                            "id"
                        )

                        session[
                            "github_status"
                        ] = run.get(
                            "status"
                        )

                        session[
                            "github_conclusion"
                        ] = run.get(
                            "conclusion"
                        )

                break

    except Exception as e:

        print(
            "find_github_run error:",
            e
        )


# ============================================================
# START SESSION
# ============================================================

@app.post("/gpu/session/start")
def start_session():

    session = create_session()

    try:

        dispatch_github(session)

    except Exception as e:

        with lock:

            session["status"] = "error"
            session["error"] = str(e)

        return jsonify({
            **public_session(session),
            "message": "Failed to start GPU session",
        }), 500

    # Find run asynchronously
    threading.Thread(
        target=find_github_run,
        args=(session,),
        daemon=True
    ).start()

    return jsonify({
        **public_session(session),
        "message": (
            "Session started. "
            "Kaggle GPU is being provisioned."
        ),
    })


# ============================================================
# SESSION STATUS
# ============================================================

@app.get("/gpu/session/<session_id>")
def session_status(session_id):

    session = get_session(session_id)

    if session is None:

        return jsonify({
            "error": "session_not_found"
        }), 404

    # Expiration
    if now() >= session["expires_at"]:

        with lock:

            if session["status"] not in (
                "completed",
                "error"
            ):

                session["status"] = "expired"

    # Refresh GitHub info
    find_github_run(session)

    return jsonify(
        public_session(session)
    )


# ============================================================
# WORKER READY
# ============================================================

@app.post(
    "/gpu/session/<session_id>/worker-ready"
)
def worker_ready(session_id):

    session = get_session(session_id)

    if session is None:

        return jsonify({
            "error": "session_not_found"
        }), 404

    print(
        "[WORKER-READY]",
        session_id
    )

    if not worker_auth(session):

        print(
            "[WORKER-READY] UNAUTHORIZED"
        )

        return jsonify({
            "error": "unauthorized"
        }), 401

    with lock:

        session["status"] = "active"

        session[
            "worker_ready_at"
        ] = time.time()

    print(
        "[WORKER-READY] ACCEPTED"
    )

    return jsonify({
        "ok": True,
        "status": "active",
    })


# ============================================================
# HEARTBEAT
# ============================================================

@app.post(
    "/gpu/session/<session_id>/heartbeat"
)
def heartbeat(session_id):

    session = get_session(session_id)

    if session is None:

        return jsonify({
            "error": "session_not_found"
        }), 404

    if not worker_auth(session):

        return jsonify({
            "error": "unauthorized"
        }), 401

    remaining = max(
        0,
        int(session["expires_at"] - now())
    )

    if remaining <= 0:

        with lock:
            session["status"] = "expired"

        return jsonify({
            "ok": False,
            "status": "expired",
            "remaining_seconds": 0,
        })

    with lock:

        session[
            "last_heartbeat"
        ] = time.time()

        session["status"] = "active"

    return jsonify({
        "ok": True,
        "status": "active",
        "remaining_seconds": remaining,
        "expires_at": session["expires_at"],
    })


# ============================================================
# COMMAND
# ============================================================

@app.get(
    "/internal/session/<session_id>/command"
)
def command(session_id):

    session = get_session(session_id)

    if session is None:

        return jsonify({
            "error": "session_not_found"
        }), 404

    if not worker_auth(session):

        return jsonify({
            "error": "unauthorized"
        }), 401

    command = None

    with lock:

        command = session.get(
            "command"
        )

        # one-shot command
        session["command"] = None

    return jsonify({
        "command": command
    })


# ============================================================
# SET COMMAND
# ============================================================

@app.post(
    "/gpu/session/<session_id>/command"
)
def set_command(session_id):

    session = get_session(session_id)

    if session is None:

        return jsonify({
            "error": "session_not_found"
        }), 404

    data = request.get_json(
        silent=True
    ) or {}

    command = data.get(
        "command"
    )

    with lock:

        session["command"] = command

    return jsonify({
        "ok": True,
        "command": command,
    })


# ============================================================
# WORKER RESULT
# ============================================================

@app.post(
    "/gpu/session/<session_id>/result"
)
def worker_result(session_id):

    session = get_session(session_id)

    if session is None:

        return jsonify({
            "error": "session_not_found"
        }), 404

    if not worker_auth(session):

        return jsonify({
            "error": "unauthorized"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    with lock:

        session["results"] = data

    return jsonify({
        "ok": True
    })


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
def index():

    return jsonify({
        "ok": True,
        "service": "GPU Session API",
    })


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "8080"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True,
    )
