"""C24 Bank document downloader — Flask UI for the per-login QR dance.

Visit ``/`` to start a session; the page renders the QR (and a tappable
deep-link fallback if the QR payload turns out to be a URL) plus a code-entry
form. POST the 6 digits to ``/code``; the service forwards them to C24,
scrapes the mailbox, and writes the PDFs into ``C24_OUTPUT_DIR``.

State is held in memory keyed by a short random token in the URL — there are
no stored credentials and nothing persists across pod restarts. That matches
the architecture decision: every C24 login is interactive, so there is no
session to preserve.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path
from threading import Lock, Thread

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from c24_client import (
    QRTOKEN_REFRESH_AFTER_SECONDS,
    SessionState,
    adopt_qrtoken,
    poll_qrtoken_status,
    refresh_qrtoken,
    start_login,
    submit_code_and_download,
)

OUTPUT_DIR = Path(os.environ.get("C24_OUTPUT_DIR", "./downloads"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = Flask(__name__)

# In-memory session store. Each entry is the per-visit ``SessionState`` which
# wraps the underlying ``requests.Session`` (carrying any cookies C24 set on
# the initial GET) plus the QR payload and the live download progress. Lost
# on pod restart; that's fine because every flow is meant to complete in one
# visit anyway.
_sessions: dict[str, SessionState] = {}
_sessions_lock = Lock()


def _new_token() -> str:
    return secrets.token_urlsafe(16)


def _get_state(token: str) -> SessionState | None:
    with _sessions_lock:
        return _sessions.get(token)


@app.route("/")
def index():
    """Open a fresh session, render the QR + code-entry form."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    token = _new_token()
    state = start_login(token, OUTPUT_DIR)
    with _sessions_lock:
        _sessions[token] = state
    return render_template(
        "login.html",
        token=token,
        deep_link=state.deep_link,
        qr_image_data_uri=state.qr_image_data_uri,
    )


@app.route("/code", methods=["POST"])
def code():
    """Kick off login + download in a background thread, redirect to /done.

    The full flow (poll auth → /complete/ → list years → download each PDF)
    can take minutes; running it inline would mean a blank loading screen
    for that whole time. We launch it in a daemon thread instead — done.html's
    meta-refresh picks up the progress via the same /status endpoint.
    """
    token = (request.form.get("token") or "").strip()
    code_value = (request.form.get("code") or "").strip()
    if not token or not code_value:
        abort(400, "Missing token or code")
    state = _get_state(token)
    if state is None:
        abort(404, "No such session (it may have expired or the pod restarted)")

    # Pre-set the status synchronously so /done's first render sees the
    # logging-in state, not a stale "awaiting_code".
    state.status = "logging_in"
    Thread(
        target=submit_code_and_download,
        args=(state, code_value),
        daemon=True,
    ).start()
    return redirect(url_for("done", token=token))


@app.route("/done")
def done():
    token = request.args.get("token", "")
    state = _get_state(token)
    if state is None:
        abort(404)
    return render_template("done.html", state=state)


@app.route("/status")
def status():
    """Unified session-state endpoint polled by both pages.

    Always returns the bookkeeping fields ``session_status`` /
    ``downloaded_count`` / ``total_count`` / ``error`` that ``done.html``
    uses to show progress.

    *Additionally* — while the session is still in the login phase
    (``state.status == "awaiting_code"``) — it does two side-effects the
    login page depends on for a smooth UX:

    1. Calls C24's ``/api/qrtoken/status/`` to check whether the user has
       authorized in the app. If yes, sets ``"authorized": true`` and
       caches the polled ``web_login_uuid`` on the session so ``/code``
       won't have to poll for it again. The page stops swapping QRs once
       authorized, so the qrtoken about to be completed stays stable.
    2. If the current qrtoken is older than ``QRTOKEN_REFRESH_AFTER_SECONDS``
       and the user hasn't authorized yet, fetches a fresh one and returns
       ``qr_image_data_uri`` + ``deep_link`` so the page can swap the QR
       in place. C24's qrtokens expire on the order of a minute; without
       this, scanning slowly leads to silent failure on the app side.
    """
    token = request.args.get("token", "")
    state = _get_state(token)
    if state is None:
        return jsonify({"session_status": "unknown"}), 404

    response = {
        "session_status": state.status,
        "total_count": state.total_count,
        "downloaded_count": state.downloaded_count,
        "failed_count": state.failed_count,
        "error": state.error,
    }

    if state.status == "awaiting_code":
        # Single C24 call returns BOTH the auth signal and the (possibly
        # rotated) qrtoken — same endpoint, same response, no extra round
        # trip. The page polls this once per second (matching the SPA), so
        # rotations land within ~1 s and the QR never goes stale on screen.
        try:
            result = poll_qrtoken_status(state)
        except Exception:
            app.logger.exception("c24 status poll failed")
            result = None

        if result and result.web_login_uuid:
            # Freeze the qrtoken — /code will complete against this same one.
            state.web_login_uuid = result.web_login_uuid
            response["authorized"] = True
        else:
            response["authorized"] = False
            rotated = bool(
                result and result.qrtoken and result.qrtoken != state.qrtoken_url
            )
            if rotated:
                adopt_qrtoken(state, result.qrtoken)
            elif time.time() - state.qrtoken_fetched_at > QRTOKEN_REFRESH_AFTER_SECONDS:
                # Safety net: if C24 somehow never rotated, force /generate/.
                # Shouldn't normally fire given the rotation happens at the
                # C24 server side; logged so we'd notice if it does.
                try:
                    refresh_qrtoken(state)
                    rotated = True
                    app.logger.warning(
                        "qrtoken refresh fell back to /generate/ "
                        "(server didn't rotate within %ds)",
                        QRTOKEN_REFRESH_AFTER_SECONDS,
                    )
                except Exception:
                    app.logger.exception("fallback qrtoken refresh failed")

            if rotated:
                response["qr_image_data_uri"] = state.qr_image_data_uri
                response["deep_link"] = state.deep_link

        response["qrtoken_age_seconds"] = int(
            time.time() - state.qrtoken_fetched_at
        )

    return jsonify(response)


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    # Dev entry point. Production uses gunicorn (see Dockerfile).
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
