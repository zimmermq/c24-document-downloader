"""C24 Bank document downloader — Flask UI for the per-login QR dance.

Visit ``/`` to start a session; the page renders the QR (and a tappable
deep-link) plus a code-entry form. POST the 6 digits to ``/code``; the
service forwards them to C24, lists the document mailbox, and writes the
PDFs into ``C24_OUTPUT_DIR``.

State is held in memory keyed by a short random token in the URL — there
are no stored credentials and nothing persists across pod restarts. That
matches the architecture decision: every C24 login is interactive, so
there is no session to preserve.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path
from threading import Lock, Thread
from typing import Optional

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
    Status,
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


class SessionStore:
    """Thread-safe in-memory dict of token -> ``SessionState`` with a TTL.

    Each entry is the per-visit state — the underlying ``requests.Session``,
    the live QR payload, the bearer tokens, and the download progress.
    Lost on pod restart; that's fine because every flow is meant to
    complete in one visit anyway.

    Sessions older than ``SESSION_TTL_SECONDS`` are evicted on every
    ``put()`` (cheap O(n) sweep over the few entries we ever hold). This
    keeps the store from growing unbounded across visits.
    """

    SESSION_TTL_SECONDS = 3600  # 1 hour — well past any C24 token lifetime

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._created_at: dict[str, float] = {}
        self._lock = Lock()

    def new_token(self) -> str:
        return secrets.token_urlsafe(16)

    def put(self, token: str, state: SessionState) -> None:
        with self._lock:
            self._evict_expired_locked()
            self._sessions[token] = state
            self._created_at[token] = time.monotonic()

    def get(self, token: str) -> Optional[SessionState]:
        with self._lock:
            return self._sessions.get(token)

    def delete(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)
            self._created_at.pop(token, None)

    def _evict_expired_locked(self) -> None:
        cutoff = time.monotonic() - self.SESSION_TTL_SECONDS
        expired = [t for t, ts in self._created_at.items() if ts < cutoff]
        for t in expired:
            self._sessions.pop(t, None)
            self._created_at.pop(t, None)
        if expired:
            app.logger.info(
                "SessionStore evicted %d expired session(s)", len(expired)
            )


_store = SessionStore()


# ------------------------------------------------------------ routes

@app.route("/")
def index():
    """Open a fresh session, render the QR + code-entry form."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    token = _store.new_token()
    state = start_login(token, OUTPUT_DIR)
    _store.put(token, state)
    return render_template(
        "login.html",
        token=token,
        qr_image_data_uri=state.qr_image_data_uri,
    )


@app.route("/code", methods=["POST"])
def code():
    """Kick off login + download in a background thread, redirect to /done.

    The full flow (poll auth → /complete/ → list years → download each PDF)
    can take ~25 s; running it inline would mean a blank loading screen for
    that whole time. We launch it in a daemon thread instead — done.html's
    meta-refresh picks up the progress via the same /status endpoint.
    """
    token = (request.form.get("token") or "").strip()
    code_value = (request.form.get("code") or "").strip()
    if not token or not code_value:
        abort(400, "Missing token or code")
    state = _store.get(token)
    if state is None:
        abort(404, "No such session (it may have expired or the pod restarted)")

    # Transition out of AWAITING_CODE atomically: a parallel /status
    # request must not still mutate the qrtoken after this point, and a
    # double form-submit must not spawn two background threads.
    with state.lock:
        if state.status != Status.AWAITING_CODE:
            abort(409, "Login already in progress for this session")
        state.status = Status.LOGGING_IN

    Thread(
        target=submit_code_and_download,
        args=(state, code_value),
        daemon=True,
    ).start()
    return redirect(url_for("done", token=token))


@app.route("/done")
def done():
    token = request.args.get("token", "")
    state = _store.get(token)
    if state is None:
        abort(404)
    return render_template("done.html", state=state)


@app.route("/status")
def status():
    """Unified session-state endpoint polled by both pages.

    Always returns the bookkeeping fields (``session_status``,
    ``downloaded_count`` etc.) that ``done.html`` uses for live progress.

    *Additionally* — while the session is still in the login phase — it
    delegates to ``_login_phase_extras`` for the two side-effects the
    login page depends on for a smooth UX: detecting C24-side auth and
    keeping the qrtoken fresh.
    """
    token = request.args.get("token", "")
    state = _store.get(token)
    if state is None:
        return jsonify({"session_status": "unknown"}), 404

    payload = {
        "session_status": state.status,
        "total_count": state.total_count,
        "downloaded_count": state.downloaded_count,
        "failed_count": state.failed_count,
        "error": state.error,
    }
    if state.status == Status.AWAITING_CODE:
        payload.update(_login_phase_extras(state))
    return jsonify(payload)


def _login_phase_extras(state: SessionState) -> dict:
    """Stuff the /status response with the live auth + QR signals.

    Called only while the user is still on the login page. Three outcomes:
    - C24 says authorized → freeze qrtoken, return ``authorized: true``
    - C24 rotated the qrtoken → adopt + return new QR data URI + deep link
    - Nothing changed but our qrtoken is stale → fall back to /generate/
      (rare safety net; logged as WARNING when it fires)

    Mutations are done under ``state.lock`` so a parallel ``/code`` request
    can't transition out of AWAITING_CODE while we're about to apply a
    qrtoken rotation that the background thread would then use. If the
    transition already happened, we bail with an empty extras dict — the
    base ``/status`` payload still goes out unchanged.
    """
    with state.lock:
        if state.status != Status.AWAITING_CODE:
            return {}

        try:
            result = poll_qrtoken_status(state)
        except Exception:
            app.logger.exception("c24 status poll failed")
            result = None

        extras: dict = {
            "qrtoken_age_seconds": int(time.time() - state.qrtoken_fetched_at),
        }

        if result and result.web_login_uuid:
            state.web_login_uuid = result.web_login_uuid
            extras["authorized"] = True
            return extras

        extras["authorized"] = False
        rotated = bool(
            result and result.qrtoken and result.qrtoken != state.qrtoken_url
        )
        if rotated:
            adopt_qrtoken(state, result.qrtoken)
        elif time.time() - state.qrtoken_fetched_at > QRTOKEN_REFRESH_AFTER_SECONDS:
            # Safety net: C24 didn't rotate within the window — force /generate/.
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
            extras["qr_image_data_uri"] = state.qr_image_data_uri
        return extras


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    # Dev entry point. Production uses gunicorn (see Dockerfile).
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
