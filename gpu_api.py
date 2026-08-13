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

GITHUB_OWNER = os.environ["GITHUB_OWNER"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
GITHUB_WORKFLOW = os.environ.get(
    "GITHUB_WORKFLOW",
    "gpu-session.yml"
)

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

API = "https://api.github.com"

HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "X-GitHub-Api-Version": "2026-03-10",
    "Content-Type": "application/json",
}

SESSION_SECONDS = 600

sessions = {}


# ============================================================
# GITHUB
# ============================================================

def github_request(method, url, **kwargs):
    return requests.request(
        method,
        url,
        headers=HEADERS,
        timeout=30,
        **kwargs
    )


# ============================================================
# START SESSION
# ============================================================

@app.route("/gpu/session/start", methods=["POST"])
def start_session():

    session_id = (
        "session-"
        + str(int(time.time()))
        + "-"
        + uuid.uuid4().hex[:8]
    )

    worker_token = secrets.token_urlsafe(32)

    created_at = time.time()
    expires_at = created_at + SESSION_SECONDS

    session = {
        "session_id": session_id,
        "worker_token": worker_token,

        "status": "starting",

        "created_at": created_at,
        "expires_at": expires_at,

        "run_id": None,
        "github_status": None,
        "github_conclusion": None,

        "commands": [],
        "results": [],

        "error": None,
    }

    sessions[session_id] = session

    # --------------------------------------------------------
    # GitHub workflow dispatch
    # --------------------------------------------------------

    url = (
        f"{API}/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}/"
        f"actions/workflows/"
        f"{GITHUB_WORKFLOW}/dispatches"
    )

    payload = {
        "ref": "main",
        "inputs": {
            "session_id": session_id,
        }
    }

    response = github_request(
        "POST",
        url,
        json=payload
    )

    print(
        "GitHub dispatch:",
        response.status_code,
        response.text
    )

    if response.status_code not in (200, 204):

        session["status"] = "error"

        session["error"] = (
            f"GitHub dispatch failed: "
            f"{response.status_code} "
            f"{response.text}"
        )

        return jsonify(session), 500

    # Start monitor
    threading.Thread(
        target=monitor_session,
        args=(session_id,),
        daemon=True
    ).start()

    # Do NOT expose worker_token publicly
    public_session = dict(session)
    public_session.pop("worker_token", None)

    return jsonify(public_session), 202


# ============================================================
# GET SESSION
# ============================================================

@app.route("/gpu/session/<session_id>", methods=["GET"])
def get_session(session_id):

    session = sessions.get(session_id)

    if session is None:

        return jsonify({
            "error": "Unknown session"
        }), 404

    update_expiration(session)

    public_session = dict(session)

    # Never expose this
    public_session.pop("worker_token", None)

    return jsonify(public_session)


# ============================================================
# SESSION EXPIRATION
# ============================================================

def update_expiration(session):

    if (
        session["status"]
        not in ("expired", "error")
        and time.time() >= session["expires_at"]
    ):

        session["status"] = "expired"


# ============================================================
# MONITOR GITHUB
# ============================================================

def monitor_session(session_id):

    session = sessions.get(session_id)

    if not session:
        return

    # Wait for GitHub run to appear
    for _ in range(30):

        if time.time() >= session["expires_at"]:
            session["status"] = "expired"
            return

        url = (
            f"{API}/repos/"
            f"{GITHUB_OWNER}/"
            f"{GITHUB_REPO}/"
            f"actions/runs"
        )

        response = github_request(
            "GET",
            url,
            params={
                "event": "workflow_dispatch",
                "per_page": 20,
            }
        )

        if response.status_code == 200:

            runs = response.json().get(
                "workflow_runs",
                []
            )

            for run in runs:

                title = (
                    run.get("display_title")
                    or ""
                )

                if session_id in title:

                    session["run_id"] = run["id"]

                    break

        if session["run_id"]:
            break

        time.sleep(3)

    if not session["run_id"]:

        session["status"] = "error"

        session["error"] = (
            "GitHub workflow run not found"
        )

        return

    # --------------------------------------------------------
    # Monitor run
    # --------------------------------------------------------

    while True:

        if time.time() >= session["expires_at"]:

            session["status"] = "expired"

            return

        url = (
            f"{API}/repos/"
            f"{GITHUB_OWNER}/"
            f"{GITHUB_REPO}/"
            f"actions/runs/"
            f"{session['run_id']}"
        )

        response = github_request(
            "GET",
            url
        )

        if response.status_code != 200:

            session["status"] = "error"

            session["error"] = response.text

            return

        run = response.json()

        session["github_status"] = run.get(
            "status"
        )

        session["github_conclusion"] = run.get(
            "conclusion"
        )

        if run.get("status") == "completed":

            if run.get("conclusion") == "success":

                session["status"] = "running"

            else:

                session["status"] = "error"

                session["error"] = (
                    "GitHub workflow failed: "
                    + str(
                        run.get("conclusion")
                    )
                )

            return

        session["status"] = "starting"

        time.sleep(5)


# ============================================================
# HEALTH
# ============================================================

@app.route("/health", methods=["GET"])
def health():

    return jsonify({
        "status": "ok",
        "service": "GPU Session API"
    })


# ============================================================
# ROOT
# ============================================================

@app.route("/", methods=["GET"])
def root():

    return jsonify({
        "service": "GPU Session API",
        "status": "online"
    })


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    print("=" * 50)
    print("        GPU SESSION API")
    print("=" * 50)
    print("Port:", port)

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
)
