"""Flask ``/status`` route — locks both the bookkeeping fields and the
login-phase side-effects (qrtoken rotation + authorized signal)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import _login_phase_extras, _store, app
from c24_client import QRTOKEN_REFRESH_AFTER_SECONDS, SessionState, Status, StatusResult


@pytest.fixture
def client():
    return app.test_client()


@pytest.fixture
def session(tmp_path: Path):
    state = SessionState(
        token="t", output_dir=tmp_path, session=requests.Session(),
        qrtoken_url="https://link.c24.de/web-login/OLD",
        qr_image_data_uri="OLD-QR", deep_link="https://link.c24.de/web-login/OLD",
        qrtoken_fetched_at=time.time() - 3,  # young
        status=Status.AWAITING_CODE,
    )
    _store.put("t", state)
    yield state
    # Best-effort cleanup so tests don't bleed state across runs.
    _store._sessions.pop("t", None)  # noqa: SLF001


class TestStatusRoute:
    def test_unknown_token_returns_404(self, client):
        r = client.get("/status?token=does-not-exist")
        assert r.status_code == 404
        assert r.get_json() == {"session_status": "unknown"}

    def test_known_session_returns_progress_fields(self, client, session: SessionState):
        session.status = Status.DOWNLOADING
        session.total_count = 63
        session.downloaded_count = 12
        session.failed_count = 1
        session.error = None
        r = client.get("/status?token=t")
        assert r.status_code == 200
        body = r.get_json()
        assert body["session_status"] == "downloading"
        assert body["total_count"] == 63
        assert body["downloaded_count"] == 12
        assert body["failed_count"] == 1

    def test_post_login_phases_skip_c24_calls(self, client, session: SessionState):
        session.status = Status.DOWNLOADING
        with patch("app.poll_qrtoken_status") as mock:
            client.get("/status?token=t")
        assert not mock.called


class TestLoginPhaseExtras:
    """Direct tests on the helper — easier than going through the route."""

    def test_authorized_freezes_qrtoken(self, session: SessionState):
        with patch("app.poll_qrtoken_status",
                   return_value=StatusResult("uuid-x", session.qrtoken_url)):
            extras = _login_phase_extras(session)
        assert extras["authorized"] is True
        assert "qr_image_data_uri" not in extras
        assert session.web_login_uuid == "uuid-x"

    def test_rotated_qrtoken_returns_new_qr_and_link(self, session: SessionState):
        new_url = "https://link.c24.de/web-login/NEW"
        with patch("app.poll_qrtoken_status",
                   return_value=StatusResult(None, new_url)), \
             patch("app.adopt_qrtoken") as adopt:
            extras = _login_phase_extras(session)
        assert extras["authorized"] is False
        assert "qr_image_data_uri" in extras
        assert "deep_link" in extras
        assert adopt.called

    def test_stale_qrtoken_falls_back_to_generate(self, session: SessionState):
        session.qrtoken_fetched_at = time.time() - (QRTOKEN_REFRESH_AFTER_SECONDS + 5)
        with patch("app.poll_qrtoken_status",
                   return_value=StatusResult(None, session.qrtoken_url)), \
             patch("app.refresh_qrtoken") as refresh:
            extras = _login_phase_extras(session)
        assert refresh.called
        assert extras["authorized"] is False

    def test_returns_qrtoken_age_seconds_for_debugging(self, session: SessionState):
        with patch("app.poll_qrtoken_status",
                   return_value=StatusResult(None, session.qrtoken_url)):
            extras = _login_phase_extras(session)
        assert "qrtoken_age_seconds" in extras
        assert isinstance(extras["qrtoken_age_seconds"], int)
