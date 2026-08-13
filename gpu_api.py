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

GITHUB_OWNER = os.environ.get(
    "GITHUB_OWNER",
    "forgotenmywin"
)

GITHUB_REPO = os.environ.get(
    "GITHUB_REPO",
    "CRD_Win"
)

GITHUB_WORKFLOW = os.environ.get(
    "GITHUB_WORKFLOW",
    "gpu-session.yml"
)

GITHUB_BRANCH = os.environ.get(
    "GITHUB_BRANCH",
    "main"
)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
PUBLIC_API_URL = os.environ.get("PUBLIC_API_URL")

SESSION_SECONDS = int(
    os.environ.get("SESSION_SECONDS", "600")
)

GITHUB_API = "https://api.github.com"


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
    "User-Agent": "GPU-Session-API",
}


# ============================================================
# MEMORY STORAGE
# ============================================================

sessions = {}
lock = threading.Lock()


# ============================================================
# HELPERS
# ============================================================

def now():
    return time.time()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def github_request(method, url, **kwargs):
    try:
        return requests.request(
            method,
            url,
            headers=HEADERS,
            timeout=30,
            **kwargs
        )
    except Exception as e:
        print("GitHub request error:", repr(e))
        return None


def public_session(session):
    data = dict(session)
    data.pop("worker_token", None)
    data.pop("commands", None)
    return data


def session_expired(session):
    return now() >= session["expires_at"]


# ============================================================
# FIND GITHUB RUN
# ============================================================

def find_run(session_id):
    url = (
        f"{GITHUB_API}/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}/"
        f"actions/workflows/"
        f"{GITHUB_WORKFLOW}/runs"
    )

    response = github_request(
        "GET",
        url,
        params={
            "event": "workflow_dispatch",
            "branch": GITHUB_BRANCH,
            "per_page": 50,
        },
    )

    if response is None or response.status_code != 200:
        return None

    try:
        runs = response.json().get(
            "workflow_runs",
            []
        )
    except Exception:
        return None

    for run in runs:
        title = run.get("display_title") or ""
        name = run.get("name") or ""

        if session_id in title or session_id in name:
            return run

    return None


# ============================================================
# MONITOR GITHUB
# ============================================================

def monitor_github(session_id):

    print("Monitoring GitHub:", session_id)

    run_id = None

    for _ in range(60):

        with lock:
            session = sessions.get(session_id)

            if not session:
                return

            run_id = session.get("run_id")

        if run_id:
            break

        run = find_run(session_id)

        if run:
            run_id = run["id"]

            with lock:
                session = sessions.get(session_id)

                if session:
                    session["run_id"] = run_id
                    session["github_status"] = run.get("status")
                    session["github_conclusion"] = run.get("conclusion")

            print("GitHub Run:", run_id)
            break

        time.sleep(3)

    if not run_id:
        with lock:
            session = sessions.get(session_id)
            if session:
                session["status"] = "error"
                session["error"] = "GitHub Run not found"
        return

    # --------------------------------------------------------
    # Monitor until workflow completes
    # --------------------------------------------------------

    while True:

        url = (
            f"{GITHUB_API}/repos/"
            f"{GITHUB_OWNER}/"
            f"{GITHUB_REPO}/"
            f"actions/runs/"
            f"{run_id}"
        )

        response = github_request(
            "GET",
            url
        )

        if response is None:
            time.sleep(5)
            continue

        if response.status_code != 200:
            time.sleep(5)
            continue

        try:
            run = response.json()
        except Exception:
            time.sleep(5)
            continue

        status = run.get("status")
        conclusion = run.get("conclusion")

        with lock:
            session = sessions.get(session_id)

            if not session:
                return

            session["github_status"] = status
            session["github_conclusion"] = conclusion

            # اگر Worker قبلاً آماده شده،
            # active بودن Session را حفظ می‌کنیم.
            if (
                session.get("worker_ready_at")
                and session["status"] != "stopped"
                and not session_expired(session)
            ):
                session["status"] = "active"

        print(
            "GitHub:",
            status,
            "|",
            conclusion
        )

        if status == "completed":

            with lock:
                session = sessions.get(session_id)

                if not session:
                    return

                worker_ready = bool(
                    session.get("worker_ready_at")
                )

                if worker_ready and conclusion in (
                    "success",
                    "cancelled",
                ):
                    # Worker قبلاً GPU را گرفته بود.
                    # پایان workflow به تنهایی Session را خراب نمی‌کند.
                    if not session_expired(session):
                        session["status"] = "active"
                    else:
                        session["status"] = "expired"

                elif conclusion != "success":
                    session["status"] = "error"
                    session["error"] = (
                        "GitHub workflow failed: "
                        + str(conclusion)
                    )

                elif not worker_ready:
                    session["status"] = "error"
                    session["error"] = (
                        "Workflow completed before GPU worker became ready"
                    )

            return

        time.sleep(5)


# ============================================================
# START SESSION
# ============================================================

@app.route(
    "/gpu/session/start",
    methods=["POST"]
)
def start_session():

    session_id = (
        "session-"
        + str(int(time.time()))
        + "-"
        + uuid.uuid4().hex[:8]
    )

    worker_token = secrets.token_urlsafe(32)

    session = {
        "session_id": session_id,
        "status": "starting",

        "created_at": utc_now(),

        # تایمر از زمان START شروع می‌شود.
        # بعداً هنگام worker-ready دوباره تنظیم می‌شود.
        "expires_at": now() + SESSION_SECONDS,

        "run_id": None,

        "github_status": None,
        "github_conclusion": None,

        "gpu": None,
        "compute_capability": None,
        "worker_ready_at": None,

        "commands": [],
        "results": {},

        "error": None,

        "worker_token": worker_token,
    }

    with lock:
        sessions[session_id] = session

    url = (
        f"{GITHUB_API}/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}/"
        f"actions/workflows/"
        f"{GITHUB_WORKFLOW}/dispatches"
    )

    payload = {
        "ref": GITHUB_BRANCH,
        "inputs": {
            "session_id": session_id,
            "api_url": PUBLIC_API_URL,
            "worker_token": worker_token,
        },
    }

    print("=" * 60)
    print("STARTING GPU SESSION")
    print("=" * 60)
    print("Session:", session_id)

    response = github_request(
        "POST",
        url,
        json=payload
    )

    if response is None:
        with lock:
            session["status"] = "error"
            session["error"] = "GitHub connection failed"

        return jsonify(
            public_session(session)
        ), 500

    print(
        "GitHub HTTP:",
        response.status_code
    )

    print(
        "GitHub response:",
        response.text[:500]
    )

    if response.status_code not in (
        200,
        201,
        202,
        204,
    ):
        with lock:
            session["status"] = "error"
            session["error"] = (
                f"GitHub dispatch failed: "
                f"{response.status_code} "
                f"{response.text}"
            )

        return jsonify(
            public_session(session)
        ), 500

    threading.Thread(
        target=monitor_github,
        args=(session_id,),
        daemon=True
    ).start()

    with lock:
        output = public_session(session)

    return jsonify(output), 202


# ============================================================
# SESSION STATUS
# ============================================================

@app.route(
    "/gpu/session/<session_id>",
    methods=["GET"]
)
def get_session(session_id):

    with lock:
        session = sessions.get(session_id)

        if not session:
            return jsonify({
                "error": "Unknown session"
            }), 404

        remaining = max(
            0,
            int(
                session["expires_at"] - now()
            )
        )

        if (
            remaining <= 0
            and session["status"] not in (
                "error",
                "stopped",
                "expired",
            )
        ):
            session["status"] = "expired"

        output = public_session(session)

        output["remaining_seconds"] = remaining

    return jsonify(output)


# ============================================================
# WORKER READY
# ============================================================

@app.route(
    "/gpu/session/<session_id>/worker-ready",
    methods=["POST"]
)
def worker_ready(session_id):

    data = request.get_json(
        silent=True
    ) or {}

    supplied_token = data.get(
        "worker_token"
    )

    with lock:

        session = sessions.get(
            session_id
        )

        if not session:
            return jsonify({
                "error": "Unknown session"
            }), 404

        if supplied_token != session["worker_token"]:
            return jsonify({
                "error": "Unauthorized"
            }), 401

        # از لحظه آماده‌شدن GPU،
        # Session تازه 600 ثانیه وقت دارد.
        session["expires_at"] = (
            now() + SESSION_SECONDS
        )

        session["gpu"] = data.get("gpu")

        session["compute_capability"] = (
            data.get(
                "compute_capability"
            )
        )

        session["worker_ready_at"] = (
            utc_now()
        )

        session["status"] = "active"
        session["error"] = None

    print("=" * 60)
    print("GPU WORKER READY")
    print("=" * 60)
    print("Session:", session_id)
    print("GPU:", data.get("gpu"))

    return jsonify({
        "session_id": session_id,
        "status": "active",
        "gpu": data.get("gpu"),
        "compute_capability": data.get(
            "compute_capability"
        ),
    })


# ============================================================
# QUEUE COMMAND
# ============================================================

@app.route(
    "/gpu/session/<session_id>/command",
    methods=["POST"]
)
def queue_command(session_id):

    data = request.get_json(
        silent=True
    ) or {}

    operation = data.get(
        "operation"
    )

    if not operation:
        return jsonify({
            "error": "operation is required"
        }), 400

    with lock:

        session = sessions.get(
            session_id
        )

        if not session:
            return jsonify({
                "error": "Unknown session"
            }), 404

        if session["status"] != "active":
            return jsonify({
                "error": "Session is not active",
                "status": session["status"]
            }), 409

        if session_expired(session):
            session["status"] = "expired"

            return jsonify({
                "error": "Session expired"
            }), 410

        if operation not in (
            "matrix",
        ):
            return jsonify({
                "error": "Unsupported operation",
                "allowed": [
                    "matrix"
                ]
            }), 400

        command_id = (
            "cmd-"
            + uuid.uuid4().hex[:12]
        )

        command = {
            "command_id": command_id,
            "operation": operation,
            "parameters": data,
            "created_at": now(),
        }

        session["commands"].append(
            command
        )

    return jsonify({
        "status": "queued",
        "command_id": command_id,
    }), 202


# ============================================================
# WORKER GET COMMAND
# ============================================================

@app.route(
    "/internal/session/<session_id>/command",
    methods=["GET"]
)
def worker_get_command(session_id):

    token = request.headers.get(
        "X-Worker-Token"
    )

    with lock:

        session = sessions.get(
            session_id
        )

        if not session:
            return jsonify({
                "error": "Unknown session"
            }), 404

        if token != session["worker_token"]:
            return jsonify({
                "error": "Unauthorized"
            }), 401

        if session_expired(session):
            session["status"] = "expired"

            return jsonify({
                "command": None,
                "expired": True
            })

        if not session["commands"]:
            return jsonify({
                "command": None
            })

        command = session[
            "commands"
        ].pop(0)

    return jsonify({
        "command": command
    })


# ============================================================
# WORKER SEND RESULT
# ============================================================

@app.route(
    "/internal/session/<session_id>/result",
    methods=["POST"]
)
def worker_send_result(session_id):

    token = request.headers.get(
        "X-Worker-Token"
    )

    data = request.get_json(
        silent=True
    ) or {}

    command_id = data.get(
        "command_id"
    )

    with lock:

        session = sessions.get(
            session_id
        )

        if not session:
            return jsonify({
                "error": "Unknown session"
            }), 404

        if token != session["worker_token"]:
            return jsonify({
                "error": "Unauthorized"
            }), 401

        if not command_id:
            return jsonify({
                "error": "command_id missing"
            }), 400

        session[
            "results"
        ][command_id] = data

    print(
        "Result received:",
        command_id
    )

    return jsonify({
        "status": "received",
        "command_id": command_id
    })


# ============================================================
# GET RESULT
# ============================================================

@app.route(
    "/gpu/session/<session_id>/result/<command_id>",
    methods=["GET"]
)
def get_result(
    session_id,
    command_id
):

    with lock:

        session = sessions.get(
            session_id
        )

        if not session:
            return jsonify({
                "error": "Unknown session"
            }), 404

        result = session[
            "results"
        ].get(
            command_id
        )

    if result is None:
        return jsonify({
            "status": "pending",
            "command_id": command_id
        }), 202

    return jsonify({
        "status": "completed",
        "command_id": command_id,
        "result": result,
    })


# ============================================================
# STOP
# ============================================================

@app.route(
    "/gpu/session/<session_id>/stop",
    methods=["POST"]
)
def stop_session(session_id):

    with lock:

        session = sessions.get(
            session_id
        )

        if not session:
            return jsonify({
                "error": "Unknown session"
            }), 404

        run_id = session.get("run_id")

        session["status"] = "stopped"

    if run_id:

        url = (
            f"{GITHUB_API}/repos/"
            f"{GITHUB_OWNER}/"
            f"{GITHUB_REPO}/"
            f"actions/runs/"
            f"{run_id}/cancel"
        )

        github_request(
            "POST",
            url
        )

    return jsonify({
        "session_id": session_id,
        "status": "stopped"
    })


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return jsonify({
        "status": "ok",
        "service": "GPU Session API",
        "session_seconds": SESSION_SECONDS,
        "workflow": GITHUB_WORKFLOW,
    })


@app.route(
    "/",
    methods=["GET"]
)
def root():

    return jsonify({
        "service": "GPU Session API",
        "status": "online",
        "session_seconds": SESSION_SECONDS,
    })


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    print("=" * 60)
    print("GPU SESSION API")
    print("=" * 60)

    print("Port:", port)
    print(
        "GitHub:",
        f"{GITHUB_OWNER}/{GITHUB_REPO}"
    )
    print(
        "Workflow:",
        GITHUB_WORKFLOW
    )
    print(
        "Public API:",
        PUBLIC_API_URL
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True
    )
