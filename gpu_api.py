from flask import Flask, request, jsonify
import requests
import os
import time
import uuid
import secrets
import threading

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
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
PUBLIC_API_URL = os.environ.get("PUBLIC_API_URL")

SESSION_SECONDS = int(
    os.environ.get("SESSION_SECONDS", "900")
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
    "User-Agent": "GPU-Session-API"
}

# ============================================================
# STORAGE
# ============================================================

sessions = {}
sessions_lock = threading.Lock()


# ============================================================
# GITHUB REQUEST
# ============================================================

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


# ============================================================
# PUBLIC SESSION
# ============================================================

def public_session(session):
    result = dict(session)

    result.pop("worker_token", None)

    return result


# ============================================================
# FIND WORKFLOW RUN
# ============================================================

def find_workflow_run(session_id):

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
            "per_page": 50
        }
    )

    if response is None:
        return None

    if response.status_code != 200:
        print(
            "Workflow list error:",
            response.status_code,
            response.text[:500]
        )
        return None

    try:
        runs = response.json().get(
            "workflow_runs",
            []
        )
    except Exception:
        return None

    for run in runs:

        values = [
            run.get("display_title") or "",
            run.get("name") or "",
            run.get("run_name") or ""
        ]

        if any(
            session_id in value
            for value in values
        ):
            return run

    return None


# ============================================================
# GET WORKFLOW RUN
# ============================================================

def get_workflow_run(run_id):

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
        return None

    if response.status_code != 200:
        return None

    try:
        return response.json()
    except Exception:
        return None


# ============================================================
# MONITOR SESSION
# ============================================================

def monitor_session(session_id):

    print("=" * 60)
    print("SESSION MONITOR")
    print("Session:", session_id)
    print("=" * 60)

    run_id = None

    # --------------------------------------------------------
    # Find GitHub run
    # --------------------------------------------------------

    for attempt in range(60):

        with sessions_lock:
            session = sessions.get(session_id)

            if not session:
                return

            if time.time() >= session["expires_at"]:
                session["status"] = "expired"
                return

            run_id = session.get("run_id")

        if run_id:
            break

        run = find_workflow_run(session_id)

        if run:

            run_id = run.get("id")

            with sessions_lock:

                session = sessions.get(session_id)

                if session:

                    session["run_id"] = run_id
                    session["github_status"] = run.get(
                        "status"
                    )
                    session["github_conclusion"] = run.get(
                        "conclusion"
                    )

            print("GitHub run found:", run_id)

            break

        time.sleep(2)

    if not run_id:

        with sessions_lock:

            session = sessions.get(session_id)

            if session:

                session["status"] = "error"
                session["error"] = (
                    "GitHub workflow run not found"
                )

        return

    # --------------------------------------------------------
    # Monitor
    # --------------------------------------------------------

    while True:

        with sessions_lock:

            session = sessions.get(session_id)

            if not session:
                return

            # IMPORTANT:
            # Don't expire an already READY worker.
            if (
                time.time() >= session["expires_at"]
                and session["status"] != "active"
            ):
                session["status"] = "expired"
                return

        run = get_workflow_run(run_id)

        if not run:
            time.sleep(3)
            continue

        status = run.get("status")
        conclusion = run.get("conclusion")

        with sessions_lock:

            session = sessions.get(session_id)

            if not session:
                return

            session["github_status"] = status
            session["github_conclusion"] = conclusion

        print(
            "GitHub:",
            status,
            conclusion
        )

        # ----------------------------------------------------
        # Workflow completed
        # ----------------------------------------------------

        if status == "completed":

            with sessions_lock:

                session = sessions.get(session_id)

                if not session:
                    return

                # If worker-ready already arrived,
                # keep the session active.
                if session["status"] == "active":
                    print(
                        "Worker already READY."
                    )
                    return

                if conclusion == "success":

                    session["status"] = "error"

                    session["error"] = (
                        "Workflow completed before "
                        "GPU worker became ready"
                    )

                else:

                    session["status"] = "error"

                    session["error"] = (
                        "GitHub workflow failed: "
                        + str(conclusion)
                    )

            return

        time.sleep(3)


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

    now = time.time()

    session = {

        "session_id":
            session_id,

        "status":
            "starting",

        "created_at":
            now,

        "expires_at":
            now + SESSION_SECONDS,

        "run_id":
            None,

        "github_status":
            None,

        "github_conclusion":
            None,

        "gpu":
            None,

        "compute_capability":
            None,

        "worker_ready_at":
            None,

        "results":
            {},

        "error":
            None,

        "worker_token":
            worker_token
    }

    with sessions_lock:
        sessions[session_id] = session

    # --------------------------------------------------------
    # Dispatch GitHub
    # --------------------------------------------------------

    url = (
        f"{GITHUB_API}/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}/"
        f"actions/workflows/"
        f"{GITHUB_WORKFLOW}/dispatches"
    )

    payload = {

        "ref":
            GITHUB_BRANCH,

        "inputs": {

            "session_id":
                session_id,

            "api_url":
                PUBLIC_API_URL,

            "worker_token":
                worker_token
        }
    }

    print("=" * 60)
    print("START GPU SESSION")
    print("Session:", session_id)
    print("=" * 60)

    response = github_request(
        "POST",
        url,
        json=payload
    )

    if response is None:

        with sessions_lock:

            session["status"] = "error"
            session["error"] = (
                "Could not connect to GitHub"
            )

        return jsonify(
            public_session(session)
        ), 500

    if response.status_code not in (
        200,
        201,
        202,
        204
    ):

        with sessions_lock:

            session["status"] = "error"

            session["error"] = (
                "GitHub dispatch failed: "
                f"{response.status_code} "
                f"{response.text}"
            )

        return jsonify(
            public_session(session)
        ), 500

    threading.Thread(
        target=monitor_session,
        args=(session_id,),
        daemon=True
    ).start()

    return jsonify(
        public_session(session)
    ), 202


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

    token = request.headers.get(
        "X-Worker-Token"
    )

    with sessions_lock:

        session = sessions.get(session_id)

        if not session:

            return jsonify({
                "error":
                    "Unknown session"
            }), 404

        if token != session["worker_token"]:

            return jsonify({
                "error":
                    "Invalid worker token"
            }), 403

        session["status"] = "active"

        session["gpu"] = data.get(
            "gpu"
        )

        session[
            "compute_capability"
        ] = data.get(
            "compute_capability"
        )

        session[
            "worker_ready_at"
        ] = time.time()

        session[
            "results"
        ] = data.get(
            "results",
            {}
        )

        session["error"] = None

    print("=" * 60)
    print("GPU WORKER READY")
    print("Session:", session_id)
    print("GPU:", data.get("gpu"))
    print(
        "Compute:",
        data.get("compute_capability")
    )
    print("=" * 60)

    return jsonify({
        "status":
            "ok",

        "session_id":
            session_id,

        "worker_status":
            "active"
    })


# ============================================================
# SESSION STATUS
# ============================================================

@app.route(
    "/gpu/session/<session_id>",
    methods=["GET"]
)
def session_status(session_id):

    with sessions_lock:

        session = sessions.get(
            session_id
        )

        if not session:

            return jsonify({
                "error":
                    "Unknown session"
            }), 404

        remaining = max(
            0,
            int(
                session["expires_at"]
                - time.time()
            )
        )

        if (
            remaining <= 0
            and session["status"]
            not in (
                "active",
                "error",
                "stopped"
            )
        ):

            session["status"] = "expired"

        output = public_session(
            session
        )

        output[
            "remaining_seconds"
        ] = remaining

    return jsonify(output)


# ============================================================
# STOP SESSION
# ============================================================

@app.route(
    "/gpu/session/<session_id>/stop",
    methods=["POST"]
)
def stop_session(session_id):

    with sessions_lock:

        session = sessions.get(
            session_id
        )

        if not session:

            return jsonify({
                "error":
                    "Unknown session"
            }), 404

        run_id = session.get(
            "run_id"
        )

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
        "session_id":
            session_id,

        "status":
            "stopped"
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

        "status":
            "ok",

        "service":
            "GPU Session API",

        "github":
            f"{GITHUB_OWNER}/{GITHUB_REPO}",

        "workflow":
            GITHUB_WORKFLOW,

        "session_seconds":
            SESSION_SECONDS
    })


# ============================================================
# ROOT
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def root():

    return jsonify({

        "service":
            "GPU Session API",

        "status":
            "online",

        "session_seconds":
            SESSION_SECONDS
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
    print("=" * 60)

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True
                    )
