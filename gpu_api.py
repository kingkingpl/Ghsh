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
    "GITHUB_TOKEN"
)

PUBLIC_API_URL = os.environ.get(
    "PUBLIC_API_URL"
)

SESSION_SECONDS = int(
    os.environ.get(
        "SESSION_SECONDS",
        "600"
    )
)

GITHUB_API = "https://api.github.com"


# ============================================================
# CONFIG VALIDATION
# ============================================================

if not GITHUB_TOKEN:
    raise RuntimeError(
        "GITHUB_TOKEN environment variable is missing"
    )

if not PUBLIC_API_URL:
    raise RuntimeError(
        "PUBLIC_API_URL environment variable is missing"
    )

PUBLIC_API_URL = PUBLIC_API_URL.rstrip("/")


# ============================================================
# GITHUB HEADERS
# ============================================================

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
# TIME
# ============================================================

def utc_now():
    return datetime.now(
        timezone.utc
    )


# ============================================================
# GITHUB REQUEST
# ============================================================

def github_request(
    method,
    url,
    **kwargs
):
    try:

        return requests.request(
            method,
            url,
            headers=HEADERS,
            timeout=30,
            **kwargs
        )

    except Exception as e:

        print(
            "GitHub request error:",
            repr(e)
        )

        return None


# ============================================================
# PUBLIC SESSION VIEW
# ============================================================

def public_session(session):
    """
    Never expose worker_token.
    """

    result = dict(session)

    result.pop(
        "worker_token",
        None
    )

    return result


# ============================================================
# FIND GITHUB RUN
# ============================================================

def find_workflow_run(
    session_id
):

    url = (
        f"{GITHUB_API}/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}/"
        f"actions/workflows/"
        f"{GITHUB_WORKFLOW}/runs"
    )

    params = {
        "event": "workflow_dispatch",
        "branch": GITHUB_BRANCH,
        "per_page": 50
    }

    response = github_request(
        "GET",
        url,
        params=params
    )

    if response is None:
        return None

    if response.status_code != 200:

        print(
            "List workflow runs failed:",
            response.status_code,
            response.text[:1000]
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

        display_title = (
            run.get("display_title")
            or ""
        )

        name = (
            run.get("name")
            or ""
        )

        run_name = (
            run.get("run_name")
            or ""
        )

        if session_id in display_title:
            return run

        if session_id in name:
            return run

        if session_id in run_name:
            return run

    return None


# ============================================================
# GET GITHUB RUN
# ============================================================

def get_workflow_run(
    run_id
):

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

        print(
            "Get workflow run failed:",
            response.status_code,
            response.text[:500]
        )

        return None

    try:
        return response.json()
    except Exception:
        return None


# ============================================================
# START MONITOR THREAD
# ============================================================

def monitor_session(
    session_id
):

    print("")
    print("=" * 60)
    print("       SESSION MONITOR STARTED")
    print("=" * 60)

    print(
        "Session:",
        session_id
    )

    # --------------------------------------------------------
    # Find Run
    # --------------------------------------------------------

    run_id = None

    for attempt in range(60):

        with sessions_lock:

            session = sessions.get(
                session_id
            )

            if not session:
                return

            if (
                time.time()
                >= session["expires_at"]
            ):

                session["status"] = "expired"

                return

            run_id = session.get(
                "run_id"
            )

        if run_id:
            break

        run = find_workflow_run(
            session_id
        )

        if run:

            run_id = run.get(
                "id"
            )

            with sessions_lock:

                session = sessions.get(
                    session_id
                )

                if session:

                    session["run_id"] = run_id

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

            print(
                "GitHub Run found:",
                run_id
            )

            break

        print(
            f"Waiting for GitHub Run "
            f"{attempt + 1}/60"
        )

        time.sleep(3)

    # --------------------------------------------------------
    # Run not found
    # --------------------------------------------------------

    if not run_id:

        with sessions_lock:

            session = sessions.get(
                session_id
            )

            if session:

                session["status"] = "error"

                session["error"] = (
                    "GitHub workflow run "
                    "not found"
                )

        return

    # --------------------------------------------------------
    # Monitor Run
    # --------------------------------------------------------

    while True:

        with sessions_lock:

            session = sessions.get(
                session_id
            )

            if not session:
                return

            if (
                time.time()
                >= session["expires_at"]
            ):

                session["status"] = "expired"

                print(
                    "Session expired:"
                    ,
                    session_id
                )

                return

        run = get_workflow_run(
            run_id
        )

        if run is None:

            time.sleep(5)

            continue

        status = run.get(
            "status"
        )

        conclusion = run.get(
            "conclusion"
        )

        with sessions_lock:

            session = sessions.get(
                session_id
            )

            if not session:
                return

            session[
                "github_status"
            ] = status

            session[
                "github_conclusion"
            ] = conclusion

        print(
            "GitHub:",
            status,
            "|",
            conclusion
        )

        # ----------------------------------------------------
        # Completed
        # ----------------------------------------------------

        if status == "completed":

            if conclusion == "success":

                with sessions_lock:

                    session = sessions.get(
                        session_id
                    )

                    if session:

                        session[
                            "status"
                        ] = "active"

                        session[
                            "error"
                        ] = None

                print(
                    "GitHub workflow succeeded."
                )

                return

            else:

                with sessions_lock:

                    session = sessions.get(
                        session_id
                    )

                    if session:

                        session[
                            "status"
                        ] = "error"

                        session[
                            "error"
                        ] = (
                            "GitHub workflow "
                            "failed: "
                            + str(
                                conclusion
                            )
                        )

                return

        # ----------------------------------------------------
        # Still running
        # ----------------------------------------------------

        with sessions_lock:

            session = sessions.get(
                session_id
            )

            if session:

                session[
                    "status"
                ] = "starting"

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
        + str(
            int(
                time.time()
            )
        )
        + "-"
        + uuid.uuid4().hex[:8]
    )

    worker_token = secrets.token_urlsafe(
        32
    )

    created_timestamp = time.time()

    expires_timestamp = (
        created_timestamp
        + SESSION_SECONDS
    )

    session = {

        "session_id":
            session_id,

        "status":
            "starting",

        "created_at":
            utc_now().isoformat(),

        "expires_at":
            expires_timestamp,

        "run_id":
            None,

        "github_status":
            None,

        "github_conclusion":
            None,

        "error":
            None,

        "worker_token":
            worker_token
    }

    with sessions_lock:

        sessions[
            session_id
        ] = session

    # ========================================================
    # GITHUB WORKFLOW DISPATCH
    # ========================================================

    dispatch_url = (
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

    print("")
    print("=" * 60)
    print("          STARTING GPU SESSION")
    print("=" * 60)

    print(
        "Session:",
        session_id
    )

    print(
        "API URL:",
        PUBLIC_API_URL
    )

    response = github_request(
        "POST",
        dispatch_url,
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

    print(
        "GitHub HTTP:",
        response.status_code
    )

    print(
        "GitHub response:",
        response.text[:1000]
    )

    # --------------------------------------------------------
    # Success
    # --------------------------------------------------------

    if response.status_code in (
        200,
        201,
        202,
        204
    ):

        # Some environments return a run ID
        # directly.
        try:

            if response.text.strip():

                body = response.json()

                direct_run_id = (
                    body.get(
                        "workflow_run_id"
                    )
                )

                if direct_run_id:

                    with sessions_lock:

                        session[
                            "run_id"
                        ] = direct_run_id

        except Exception:
            pass

        threading.Thread(
            target=monitor_session,
            args=(session_id,),
            daemon=True
        ).start()

        return jsonify(
            public_session(session)
        ), 202

    # --------------------------------------------------------
    # Error
    # --------------------------------------------------------

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


# ============================================================
# SESSION STATUS
# ============================================================

@app.route(
    "/gpu/session/<session_id>",
    methods=["GET"]
)
def session_status(
    session_id
):

    with sessions_lock:

        session = sessions.get(
            session_id
        )

        if not session:

            return jsonify({
                "error":
                    "Unknown session",
                "session_id":
                    session_id
            }), 404

        # Expiration
        remaining = max(
            0,
            int(
                session[
                    "expires_at"
                ]
                - time.time()
            )
        )

        if (
            remaining <= 0
            and session["status"]
            not in (
                "expired",
                "error"
            )
        ):

            session[
                "status"
            ] = "expired"

        output = public_session(
            session
        )

        output[
            "remaining_seconds"
        ] = remaining

    return jsonify(
        output
    )


# ============================================================
# CANCEL SESSION
# ============================================================

@app.route(
    "/gpu/session/<session_id>/stop",
    methods=["POST"]
)
def stop_session(
    session_id
):

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

        session[
            "status"
        ] = "stopped"

    # --------------------------------------------------------
    # Cancel GitHub run if available
    # --------------------------------------------------------

    if run_id:

        cancel_url = (
            f"{GITHUB_API}/repos/"
            f"{GITHUB_OWNER}/"
            f"{GITHUB_REPO}/"
            f"actions/runs/"
            f"{run_id}/cancel"
        )

        response = github_request(
            "POST",
            cancel_url
        )

        if response is not None:

            print(
                "Cancel run HTTP:",
                response.status_code
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
            f"{GITHUB_OWNER}/"
            f"{GITHUB_REPO}",

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
# RAILWAY MAIN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    print("")
    print("=" * 60)
    print("             GPU SESSION API")
    print("=" * 60)

    print(
        "Port:",
        port
    )

    print(
        "GitHub:",
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}"
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
