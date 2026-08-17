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

GITHUB_TOKEN = os.environ.get(
    "GITHUB_TOKEN",
    ""
)

PUBLIC_API_URL = os.environ.get(
    "PUBLIC_API_URL",
    "https://ghsh-production.up.railway.app"
).rstrip("/")

SESSION_SECONDS = int(
    os.environ.get(
        "SESSION_SECONDS",
        "1200"
    )
)

WORKER_TOKEN_LENGTH = 32

GITHUB_API = "https://api.github.com"


# ============================================================
# MEMORY STORE
# ============================================================

sessions = {}

sessions_lock = threading.Lock()


# ============================================================
# TIME
# ============================================================

def now():

    return datetime.now(
        timezone.utc
    )


def iso(dt):

    if dt is None:
        return None

    return dt.isoformat()


# ============================================================
# GITHUB HEADERS
# ============================================================

def github_headers():

    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2026-03-10",
        "Content-Type": "application/json",
        "User-Agent": "CRD-Win-GPU-Session",
    }


# ============================================================
# SESSION CREATION
# ============================================================

def create_session():

    created = now()

    session_id = (
        "session-"
        f"{int(created.timestamp())}-"
        f"{secrets.token_hex(4)}"
    )

    worker_token = secrets.token_urlsafe(
        WORKER_TOKEN_LENGTH
    )

    session = {
        "session_id": session_id,

        "created_at": created,

        "expires_at": (
            created.timestamp()
            + SESSION_SECONDS
        ),

        "status": "starting",

        "worker_ready_at": None,

        "github_status": None,
        "github_conclusion": None,
        "run_id": None,

        "gpu": None,
        "cuda_available": None,
        "compute_capability": None,

        "error": None,

        "results": {},

        "commands": [],

        "worker_token": worker_token,

        "last_heartbeat": None,
    }

    with sessions_lock:
        sessions[session_id] = session

    return session


# ============================================================
# GET SESSION
# ============================================================

def get_session(session_id):

    with sessions_lock:
        return sessions.get(session_id)


# ============================================================
# PUBLIC SESSION RESPONSE
# ============================================================

def public_session(session):

    if session is None:
        return None

    remaining = max(
        0,
        int(
            session["expires_at"]
            - time.time()
        )
    )

    return {
        "session_id": session["session_id"],

        "created_at": iso(
            session["created_at"]
        ),

        "expires_at": session["expires_at"],

        "remaining_seconds": remaining,

        "status": session["status"],

        "worker_ready_at": (
            iso(session["worker_ready_at"])
            if session["worker_ready_at"]
            else None
        ),

        "run_id": session["run_id"],

        "github_status": session["github_status"],

        "github_conclusion": session["github_conclusion"],

        "gpu": session["gpu"],

        "cuda_available": session["cuda_available"],

        "compute_capability": session[
            "compute_capability"
        ],

        "error": session["error"],

        "results": session["results"],
    }


# ============================================================
# START GITHUB WORKFLOW
# ============================================================

def start_github_workflow(session):

    if not GITHUB_TOKEN:
        raise RuntimeError(
            "GITHUB_TOKEN is missing"
        )

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
            "session_id": session["session_id"],

            "api_url": PUBLIC_API_URL,

            "worker_token": session["worker_token"],
        },

        # VERY IMPORTANT
        "return_run_details": True,
    }

    print("")
    print("=" * 60)
    print("GITHUB WORKFLOW DISPATCH")
    print("=" * 60)

    print("URL:")
    print(url)

    print("Workflow:")
    print(GITHUB_WORKFLOW)

    print("Branch:")
    print(GITHUB_BRANCH)

    print("Session:")
    print(session["session_id"])

    try:

        response = requests.post(
            url,
            headers=github_headers(),
            json=payload,
            timeout=30,
        )

    except Exception as e:

        raise RuntimeError(
            f"GitHub request failed: {e}"
        )

    print(
        "GitHub status:",
        response.status_code
    )

    print(
        "GitHub response:",
        response.text[:5000]
    )

    # --------------------------------------------------------
    # EXPECTED:
    #
    # 200
    #
    # {
    #   "workflow_run_id": 123,
    #   "run_url": "...",
    #   "html_url": "..."
    # }
    # --------------------------------------------------------

    if response.status_code not in (
        200,
        201,
        202,
        204,
    ):

        raise RuntimeError(
            "GitHub workflow dispatch failed: "
            f"{response.status_code} "
            f"{response.text}"
        )

    # --------------------------------------------------------
    # NEW GITHUB API
    # --------------------------------------------------------

    if response.text.strip():

        try:

            data = response.json()

        except Exception:

            data = {}

    else:

        data = {}

    run_id = data.get(
        "workflow_run_id"
    )

    # --------------------------------------------------------
    # If GitHub returned run ID directly
    # --------------------------------------------------------

    if run_id:

        session["run_id"] = int(run_id)

        print(
            "GitHub RUN ID:",
            session["run_id"]
        )

        return int(run_id)

    # --------------------------------------------------------
    # FALLBACK
    #
    # Some GitHub/API configurations may still return 204.
    # Then search recent runs, but only briefly.
    # --------------------------------------------------------

    print(
        "GitHub did not return workflow_run_id."
    )

    print(
        "Using fallback run discovery..."
    )

    return find_new_run(
        session
    )


# ============================================================
# FIND NEW RUN - FALLBACK ONLY
# ============================================================

def find_new_run(session):

    url = (
        f"{GITHUB_API}/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}/"
        f"actions/workflows/"
        f"{GITHUB_WORKFLOW}/runs"
    )

    # Search for only very recent manual runs.
    params = {
        "branch": GITHUB_BRANCH,
        "event": "workflow_dispatch",
        "per_page": 20,
    }

    start_time = time.time()

    while time.time() - start_time < 60:

        try:

            response = requests.get(
                url,
                headers=github_headers(),
                params=params,
                timeout=20,
            )

        except Exception as e:

            print(
                "Run discovery error:",
                e
            )

            time.sleep(3)

            continue

        print(
            "Run discovery:",
            response.status_code
        )

        if response.status_code != 200:

            time.sleep(3)

            continue

        try:

            data = response.json()

        except Exception:

            time.sleep(3)

            continue

        runs = data.get(
            "workflow_runs",
            []
        )

        for run in runs:

            created_at = run.get(
                "created_at"
            )

            # Ignore old runs.
            if created_at:

                try:

                    created_ts = datetime.fromisoformat(
                        created_at.replace(
                            "Z",
                            "+00:00"
                        )
                    ).timestamp()

                    if (
                        time.time()
                        - created_ts
                        > 120
                    ):
                        continue

                except Exception:
                    pass

            run_id = run.get("id")

            if run_id:

                print(
                    "Fallback found run:",
                    run_id
                )

                session["run_id"] = int(
                    run_id
                )

                return int(run_id)

        time.sleep(3)

    raise RuntimeError(
        "GitHub workflow run could not be identified"
    )


# ============================================================
# MONITOR GITHUB RUN
# ============================================================

def monitor_github(session_id):

    while True:

        session = get_session(
            session_id
        )

        if session is None:
            return

        run_id = session.get(
            "run_id"
        )

        if not run_id:
            return

        url = (
            f"{GITHUB_API}/repos/"
            f"{GITHUB_OWNER}/"
            f"{GITHUB_REPO}/"
            f"actions/runs/"
            f"{run_id}"
        )

        try:

            response = requests.get(
                url,
                headers=github_headers(),
                timeout=20,
            )

            if response.status_code == 200:

                data = response.json()

                session[
                    "github_status"
                ] = data.get(
                    "status"
                )

                session[
                    "github_conclusion"
                ] = data.get(
                    "conclusion"
                )

                print(
                    "GitHub:",
                    data.get("status"),
                    data.get("conclusion")
                )

        except Exception as e:

            print(
                "Monitor error:",
                e
            )

        # ----------------------------------------------------
        # If worker became active, don't kill it.
        # Worker heartbeat controls session.
        # ----------------------------------------------------

        if session["status"] in (
            "stopped",
            "expired",
            "error",
        ):
            return

        time.sleep(10)


# ============================================================
# START
# ============================================================

@app.route(
    "/gpu/session/start",
    methods=["POST"]
)
def start_session():

    session = create_session()

    try:

        run_id = start_github_workflow(
            session
        )

        session["run_id"] = run_id

        print("")
        print(
            "SESSION STARTED:",
            session["session_id"]
        )

        # Start monitor in background.
        thread = threading.Thread(
            target=monitor_github,
            args=(session["session_id"],),
            daemon=True,
        )

        thread.start()

        response = public_session(
            session
        )

        response["message"] = (
            "Session started. "
            "Kaggle GPU is being provisioned."
        )

        response["github_run_url"] = (
            f"https://github.com/"
            f"{GITHUB_OWNER}/"
            f"{GITHUB_REPO}/"
            f"actions/runs/"
            f"{run_id}"
        )

        return jsonify(response), 200

    except Exception as e:

        print(
            "START ERROR:",
            repr(e)
        )

        session["status"] = "error"

        session["error"] = str(e)

        return jsonify(
            public_session(session)
        ), 500


# ============================================================
# SESSION STATUS
# ============================================================

@app.route(
    "/gpu/session/<session_id>",
    methods=["GET"]
)
def session_status(session_id):

    session = get_session(
        session_id
    )

    if session is None:

        return jsonify({
            "error": "session not found",
            "session_id": session_id,
        }), 404

    # --------------------------------------------------------
    # Automatic expiration
    # --------------------------------------------------------

    if (
        time.time()
        >= session["expires_at"]
        and session["status"]
        not in (
            "stopped",
            "expired",
        )
    ):

        session["status"] = "expired"

    return jsonify(
        public_session(session)
    )


# ============================================================
# WORKER READY
# ============================================================

@app.route(
    "/gpu/session/<session_id>/worker-ready",
    methods=["POST"]
)
def worker_ready(session_id):

    session = get_session(
        session_id
    )

    if session is None:

        return jsonify({
            "error": "session not found"
        }), 404

    # --------------------------------------------------------
    # TOKEN CHECK
    # --------------------------------------------------------

    auth = request.headers.get(
        "Authorization",
        ""
    )

    expected = (
        "Bearer "
        + session["worker_token"]
    )

    if not secrets.compare_digest(
        auth,
        expected
    ):

        return jsonify({
            "error": "unauthorized"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    session["gpu"] = data.get(
        "gpu"
    )

    session["cuda_available"] = data.get(
        "cuda_available"
    )

    session[
        "compute_capability"
    ] = data.get(
        "compute_capability"
    )

    session[
        "worker_ready_at"
    ] = now()

    session["status"] = "active"

    session["error"] = None

    session["last_heartbeat"] = now()

    print("")
    print("=" * 60)
    print("GPU WORKER READY")
    print("=" * 60)

    print(
        "Session:",
        session_id
    )

    print(
        "GPU:",
        session["gpu"]
    )

    print(
        "CUDA:",
        session["cuda_available"]
    )

    print(
        "Compute capability:",
        session["compute_capability"]
    )

    return jsonify({
        "ok": True,
        "status": "active",
    })


# ============================================================
# HEARTBEAT
# ============================================================

@app.route(
    "/gpu/session/<session_id>/heartbeat",
    methods=["POST"]
)
def heartbeat(session_id):

    session = get_session(
        session_id
    )

    if session is None:

        return jsonify({
            "error": "session not found"
        }), 404

    auth = request.headers.get(
        "Authorization",
        ""
    )

    expected = (
        "Bearer "
        + session["worker_token"]
    )

    if not secrets.compare_digest(
        auth,
        expected
    ):

        return jsonify({
            "error": "unauthorized"
        }), 401

    session[
        "last_heartbeat"
    ] = now()

    # --------------------------------------------------------
    # Extend expiration.
    #
    # This is the important KEEP-ALIVE part.
    # Every heartbeat gives another SESSION_SECONDS.
    # --------------------------------------------------------

    session[
        "expires_at"
    ] = time.time() + SESSION_SECONDS

    if session["status"] != "active":

        session["status"] = "active"

    return jsonify({
        "ok": True,
        "status": session["status"],
        "expires_at": session["expires_at"],
    })


# ============================================================
# INTERNAL AUTH
# ============================================================

def check_worker_token(session):

    auth = request.headers.get(
        "Authorization",
        ""
    )

    expected = (
        "Bearer "
        + session["worker_token"]
    )

    return secrets.compare_digest(
        auth,
        expected
    )


# ============================================================
# GET COMMAND
# ============================================================

@app.route(
    "/internal/session/<session_id>/command",
    methods=["GET"]
)
def get_command(session_id):

    session = get_session(
        session_id
    )

    if session is None:

        return jsonify({
            "error": "session not found"
        }), 404

    if not check_worker_token(
        session
    ):

        return jsonify({
            "error": "unauthorized"
        }), 401

    if session["commands"]:

        command = session[
            "commands"
        ].pop(0)

        return jsonify(
            command
        )

    return jsonify({})


# ============================================================
# ADD COMMAND
# ============================================================

@app.route(
    "/gpu/session/<session_id>/command",
    methods=["POST"]
)
def add_command(session_id):

    session = get_session(
        session_id
    )

    if session is None:

        return jsonify({
            "error": "session not found"
        }), 404

    data = request.get_json(
        silent=True
    ) or {}

    command = data.get(
        "command"
    )

    if not command:

        return jsonify({
            "error": "command missing"
        }), 400

    session["commands"].append({
        "command": command
    })

    return jsonify({
        "ok": True,
        "queued": True,
    })


# ============================================================
# RESULT
# ============================================================

@app.route(
    "/internal/session/<session_id>/result",
    methods=["POST"]
)
def result(session_id):

    session = get_session(
        session_id
    )

    if session is None:

        return jsonify({
            "error": "session not found"
        }), 404

    if not check_worker_token(
        session
    ):

        return jsonify({
            "error": "unauthorized"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    result_id = str(
        uuid.uuid4()
    )

    session["results"][
        result_id
    ] = data

    return jsonify({
        "ok": True,
        "result_id": result_id,
    })


# ============================================================
# STOP
# ============================================================

@app.route(
    "/gpu/session/<session_id>/stop",
    methods=["POST"]
)
def stop_session(session_id):

    session = get_session(
        session_id
    )

    if session is None:

        return jsonify({
            "error": "session not found"
        }), 404

    session["status"] = "stopped"

    session[
        "expires_at"
    ] = time.time()

    return jsonify({
        "ok": True,
        "status": "stopped",
    })


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def index():

    return jsonify({
        "service": "CRD_Win GPU Session API",
        "status": "online",
        "github_workflow": GITHUB_WORKFLOW,
        "github_repo": (
            f"{GITHUB_OWNER}/{GITHUB_REPO}"
        ),
    })


# ============================================================
# MAIN
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
