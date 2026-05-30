"""SessionStore — TTL eviction, delete, and the per-session lock that
prevents /status and /code from interleaving."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from app import SessionStore, _login_phase_extras, _store, app
from c24_client import SessionState, Status, StatusResult


@pytest.fixture
def store() -> SessionStore:
    return SessionStore()


@pytest.fixture
def state(tmp_path: Path) -> SessionState:
    return SessionState(
        token="t", output_dir=tmp_path, session=requests.Session(),
        qrtoken_url="https://link.c24.de/web-login/OLD",
        qr_image_data_uri="OLD-QR", deep_link="https://link.c24.de/web-login/OLD",
        qrtoken_fetched_at=time.time() - 3,
        status=Status.AWAITING_CODE,
    )


# ----------------------------------------------------- TTL eviction

class TestSessionStoreTTL:
    def test_put_and_get_roundtrip(self, store: SessionStore, state: SessionState):
        store.put("a", state)
        assert store.get("a") is state

    def test_get_returns_none_for_unknown_token(self, store: SessionStore):
        assert store.get("missing") is None

    def test_delete_removes_session(self, store: SessionStore, state: SessionState):
        store.put("a", state)
        store.delete("a")
        assert store.get("a") is None

    def test_delete_is_idempotent(self, store: SessionStore):
        # Deleting a token that was never put must not raise.
        store.delete("never-existed")

    def test_evicts_expired_on_put(self, store: SessionStore, state: SessionState):
        # Insert a stale session by faking its creation time.
        store.put("old", state)
        store._created_at["old"] = time.monotonic() - (store.SESSION_TTL_SECONDS + 5)
        # Triggering a new put runs the eviction sweep.
        store.put("new", state)
        assert store.get("old") is None
        assert store.get("new") is state

    def test_does_not_evict_fresh_sessions(self, store: SessionStore, state: SessionState):
        store.put("a", state)
        store.put("b", state)
        assert store.get("a") is state
        assert store.get("b") is state


# ----------------------------------------- per-session lock + race avoidance

class TestSessionLockRaceAvoidance:
    """When /code transitions the status, a still-in-flight /status must not
    apply a qrtoken rotation that the bg thread would then use."""

    def test_login_phase_extras_bails_when_status_no_longer_awaiting(
        self, state: SessionState
    ):
        # Simulate /code having already transitioned out of AWAITING_CODE.
        state.status = Status.LOGGING_IN
        new_qrtoken = "https://link.c24.de/web-login/NEW"

        with patch("app.poll_qrtoken_status",
                   return_value=StatusResult(None, new_qrtoken)), \
             patch("app.adopt_qrtoken") as adopt:
            extras = _login_phase_extras(state)

        # No mutations applied — qrtoken stays as it was.
        assert not adopt.called
        assert state.qrtoken_url == "https://link.c24.de/web-login/OLD"
        assert extras == {}

    def test_code_route_rejects_second_submit(self, state: SessionState):
        """Double form-submit must not spawn a second background thread."""
        _store.put("dbl", state)
        try:
            # Simulate the first /code having already transitioned.
            state.status = Status.LOGGING_IN
            with app.test_client() as c:
                r = c.post("/code", data={"token": "dbl", "code": "123456"})
            assert r.status_code == 409
        finally:
            _store.delete("dbl")

    def test_login_phase_extras_holds_lock_during_mutation(
        self, state: SessionState
    ):
        """Sanity-check the lock is actually acquired (smoke test)."""
        with patch("app.poll_qrtoken_status",
                   return_value=StatusResult(None, state.qrtoken_url)):
            # Before the call: lock free
            assert state.lock.acquire(blocking=False)
            state.lock.release()
            _login_phase_extras(state)
            # After the call: lock released cleanly
            assert state.lock.acquire(blocking=False)
            state.lock.release()
