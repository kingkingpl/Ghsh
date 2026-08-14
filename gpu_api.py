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
# VALIDATION
# ============================================================

if not GITHUB_TOKEN:
    raise RuntimeError(
        "GITHUB_TOKEN is missing"
    )

if not PUBLIC_API_URL:
    raise RuntimeError(
        "PUBLIC_API_URL is missing"
    )

PUBLIC_API_URL = PUBLIC_API_URL.rstrip("/")


HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
    "Content-Type": "application/json",
    "User-Agent": "GPU-Session-API"
}


# ============================================================
# IN-MEMORY SESSION STORE
# ============================================================

sessions = {}

sessions_lock = threading.Lock()


# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.now(
        timezone.utc
    ).isoformat()


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


def public_session(session):

    result = dict(session)

    result.pop(
        "worker_token",
        None
    )

    result.pop(
        "commands",
        None
    )

    return result


def is_expired(session):

    return (
        time.time()
        >= session["expires_at"]
    )


# ============================================================
# FIND GITHUB RUN
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
            "GitHub run list failed:",
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
# MONITOR GITHUB
# ============================================================

def monitor_session(
    session_id
):

    print("")
    print("=" * 60)
    print("        SESSION MONITOR")
    print("=" * 60)

    print(
        "Session:",
        session_id
    )

    run_id = None

    # --------------------------------------------------------
    # FIND RUN
    # --------------------------------------------------------

    for attempt in range(60):

        with sessions_lock:

            session = sessions.get(
                session_id
            )

            if not session:
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

            run_id = run.get("id")

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
            "Waiting for GitHub Run:",
            attempt + 1,
            "/60"
        )

        time.sleep(3)

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
    # MONITOR
    # --------------------------------------------------------

    while True:

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

            # اگر Worker آماده شده،
            # وضعیت active را حفظ کن.
            if (
                session.get(
                    "worker_ready_at"
                )
                and session["status"]
                not in (
                    "stopped",
                    "expired"
                )
                and not is_expired(
                    session
                )
            ):

                session[
                    "status"
                ] = "active"

        print(
            "GitHub:",
            status,
            "|",
            conclusion
        )

        if status == "completed":

            with sessions_lock:

                session = sessions.get(
                    session_id
                )

                if not session:
                    return

                # Worker قبلاً آماده شده.
                # اتمام GitHub workflow به تنهایی
                # به معنی خطای Session نیست.
                if session.get(
                    "worker_ready_at"
                ):

                    if is_expired(
                        session
                    ):

                        session[
                            "status"
                        ] = "expired"

                    elif session[
                        "status"
                    ] != "stopped":

                        session[
                            "status"
                        ] = "active"

                    return

                # Worker هرگز آماده نشده.
                if conclusion == "success":

                    session[
                        "status"
                    ] = "error"

                    session[
                        "error"
                    ] = (
                        "Workflow completed "
                        "before GPU worker became ready"
                    )

                else:

                    session[
                        "status"
                    ] = "error"

                    session[
                        "error"
                    ] = (
                        "GitHub workflow failed: "
                        + str(conclusion)
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
        + str(
            int(
                time.time()
            )
        )
        + "-"
        + uuid.uuid4().hex[:8]
    )

    worker_token = (
        secrets.token_urlsafe(32)
    )

    created = time.time()

    session = {

        "session_id":
            session_id,

        "status":
            "starting",

        "created_at":
            utc_now(),

        "expires_at":
            created + SESSION_SECONDS,

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

        "commands":
            [],

        "error":
            None,

        "worker_token":
            worker_token
    }

    with sessions_lock:

        sessions[
            session_id
        ] = session

    # --------------------------------------------------------
    # GITHUB DISPATCH
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

    print("")
    print("=" * 60)
    print("          STARTING GPU SESSION")
    print("=" * 60)

    print(
        "Session:",
        session_id
    )

    response = github_request(
        "POST",
        url,
        json=payload
    )

    if response is None:

        with sessions_lock:

            session[
                "status"
            ] = "error"

            session[
                "error"
            ] = (
                "Could not connect to GitHub"
            )

        return jsonify(
            public_session(
                session
            )
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
        204
    ):

        with sessions_lock:

            session[
                "status"
            ] = "error"

            session[
                "error"
            ] = (
                "GitHub dispatch failed: "
                f"{response.status_code} "
                f"{response.text}"
            )

        return jsonify(
            public_session(
                session
            )
        ), 500

    threading.Thread(
        target=monitor_session,
        args=(session_id,),
        daemon=True
    ).start()

    return jsonify(
        public_session(
            session
        )
    ), 202


# ============================================================
# WORKER READY
# ============================================================

@app.route(
    "/gpu/session/<session_id>/worker-ready",
    methods=["POST"]
)
def worker_ready(
    session_id
):

    data = request.get_json(
        silent=True
    ) or {}

    token = request.headers.get(
        "X-Worker-Token"
    )

    with sessions_lock:

        session = sessions.get(
            session_id
        )

        if not session:

            return jsonify({
                "error":
                    "Unknown session"
            }), 404

        if token != session[
            "worker_token"
        ]:

            return jsonify({
                "error":
                    "Unauthorized"
            }), 403

        # مهم:
        # تایمر واقعی از لحظه READY شروع می‌شود.
        session[
            "expires_at"
        ] = (
            time.time()
            + SESSION_SECONDS
        )

        session[
            "status"
        ] = "active"

        session[
            "gpu"
        ] = data.get(
            "gpu"
        )

        session[
            "compute_capability"
        ] = data.get(
            "compute_capability"
        )

        session[
            "worker_ready_at"
        ] = utc_now()

        session[
            "error"
        ] = None

    print("")
    print("=" * 60)
    print("             GPU WORKER READY")
    print("=" * 60)

    print(
        "Session:",
        session_id
    )

    print(
        "GPU:",
        data.get(
            "gpu"
        )
    )

    print(
        "Compute:",
        data.get(
            "compute_capability"
        )
    )

    return jsonify({

        "status":
            "ok",

        "session_id":
            session_id,

        "worker_status":
            "active"
    })


# ============================================================
# WORKER GET COMMAND
# ============================================================

@app.route(
    "/internal/session/<session_id>/command",
    methods=["GET"]
)
def worker_get_command(
    session_id
):

    token = request.headers.get(
        "X-Worker-Token"
    )

    with sessions_lock:

        session = sessions.get(
            session_id
        )

        if not session:

            return jsonify({
                "error":
                    "Unknown session"
            }), 404

        if token != session[
            "worker_token"
        ]:

            return jsonify({
                "error":
                    "Unauthorized"
            }), 403

        if is_expired(
            session
        ):

            session[
                "status"
            ] = "expired"

            return jsonify({
                "command":
                    None,

                "expired":
                    True
            })

        if not session[
            "commands"
        ]:

            return jsonify({
                "command":
                    None
            })

        command = session[
            "commands"
        ].pop(0)

    return jsonify({
        "command":
            command
    })


# ============================================================
# WORKER RESULT
# ============================================================

@app.route(
    "/internal/session/<session_id>/result",
    methods=["POST"]
)
def worker_result(
    session_id
):

    token = request.headers.get(
        "X-Worker-Token"
    )

    data = request.get_json(
        silent=True
    ) or {}

    command_id = data.get(
        "command_id"
    )

    with sessions_lock:

        session = sessions.get(
            session_id
        )

        if not session:

            return jsonify({
                "error":
                    "Unknown session"
            }), 404

        if token != session[
            "worker_token"
        ]:

            return jsonify({
                "error":
                    "Unauthorized"
            }), 403

        if not command_id:

            return jsonify({
                "error":
                    "command_id missing"
            }), 400

        session[
            "results"
        ][
            command_id
        ] = data

    return jsonify({
        "status":
            "received",

        "command_id":
            command_id
    })


# ============================================================
# QUEUE GPU JOB
# ============================================================

@app.route(
    "/gpu/session/<session_id>/command",
    methods=["POST"]
)
def queue_command(
    session_id
):

    data = request.get_json(
        silent=True
    ) or {}

    operation = data.get(
        "operation"
    )

    if operation not in (
        "matrix",
    ):

        return jsonify({

            "error":
                "Unsupported operation",

            "allowed":
                [
                    "matrix"
                ]
        }), 400

    with sessions_lock:

        session = sessions.get(
            session_id
        )

        if not session:

            return jsonify({
                "error":
                    "Unknown session"
            }), 404

        if session[
            "status"
        ] != "active":

            return jsonify({

                "error":
                    "Session is not active",

                "status":
                    session[
                        "status"
                    ]
            }), 409

        if is_expired(
            session
        ):

            session[
                "status"
            ] = "expired"

            return jsonify({
                "error":
                    "Session expired"
            }), 410

        command_id = (
            "cmd-"
            + uuid.uuid4().hex[:12]
        )

        command = {

            "command_id":
                command_id,

            "operation":
                operation,

            "parameters":
                data,

            "created_at":
                time.time()
        }

        session[
            "commands"
        ].append(
            command
        )

    return jsonify({

        "status":
            "queued",

        "command_id":
            command_id
    }), 202


# ============================================================
# GET JOB RESULT
# ============================================================

@app.route(
    "/gpu/session/<session_id>/result/<command_id>",
    methods=["GET"]
)
def get_result(
    session_id,
    command_id
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

        result = session[
            "results"
        ].get(
            command_id
        )

    if result is None:

        return jsonify({

            "status":
                "pending",

            "command_id":
                command_id

        }), 202

    return jsonify({

        "status":
            "completed",

        "command_id":
            command_id,

        "result":
            result
    })


# ============================================================
# STOP
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
    print("             GPU SESSION API")
    print("=" * 60)

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

    print(
        "Session:",
        SESSION_SECONDS,
        "seconds"
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True
        )
