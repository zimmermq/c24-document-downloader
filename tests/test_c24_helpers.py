"""Header construction, JWT decoding, body decoding — pure helpers, easy to
lock so subtle changes (a header rename, base64 padding edge case) don't
slip through."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import requests

from c24_client import (
    SessionState,
    _auth_headers,
    _c24_session_headers,
    _decode_document_body,
    _jwt_subject,
)


@pytest.fixture
def state(tmp_path: Path) -> SessionState:
    return SessionState(
        token="t", output_dir=tmp_path, session=requests.Session(),
        web_login_uuid="79f52f06-405d-4728-9f7a-eb581d0f5bac",
        access_token="JWT.PAYLOAD.SIG",
    )


class TestSessionHeaders:
    def test_fresh_x_c24_guid_per_call(self, state: SessionState):
        h1 = _c24_session_headers(state)
        h2 = _c24_session_headers(state)
        # The SPA generates a fresh UUID per request — we mirror that.
        assert h1["x-c24-guid"] != h2["x-c24-guid"]

    def test_unicorn_guid_tracks_web_login_uuid(self, state: SessionState):
        h = _c24_session_headers(state)
        assert h["x-c24-unicorn-guid"] == state.web_login_uuid
        assert h["x-c24-tracingSession"] == state.web_login_uuid

    def test_timestamp_is_epoch_seconds(self, state: SessionState):
        h = _c24_session_headers(state)
        assert h["x-c24-timestamp"].isdigit()
        assert int(h["x-c24-timestamp"]) > 1_700_000_000  # roughly 2023+

    def test_empty_strings_when_uuid_unset(self, tmp_path: Path):
        s = SessionState(token="t", output_dir=tmp_path,
                         session=requests.Session())
        h = _c24_session_headers(s)
        assert h["x-c24-unicorn-guid"] == ""
        assert h["x-c24-tracingSession"] == ""


class TestAuthHeaders:
    def test_includes_bearer_token(self, state: SessionState):
        h = _auth_headers(state)
        assert h["Authorization"] == "Bearer JWT.PAYLOAD.SIG"

    def test_carries_all_x_c24_headers(self, state: SessionState):
        h = _auth_headers(state)
        assert {"x-c24-guid", "x-c24-timestamp",
                "x-c24-unicorn-guid", "x-c24-tracingSession"} <= set(h)


class TestJwtSubject:
    @staticmethod
    def _make_jwt(payload: dict) -> str:
        b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"hdr.{b64}.sig"

    def test_extracts_sub_claim(self):
        token = self._make_jwt({"sub": "2771712", "scopes": ["WebAppScope"]})
        assert _jwt_subject(token) == "2771712"

    def test_handles_unpadded_base64(self):
        # Real JWTs strip base64 padding; the helper must add it back.
        token = self._make_jwt({"sub": "a"})  # very short payload → padding-sensitive
        assert _jwt_subject(token) == "a"

    def test_none_when_token_missing(self):
        assert _jwt_subject(None) is None
        assert _jwt_subject("") is None

    def test_none_on_malformed_token(self):
        assert _jwt_subject("not-a-jwt") is None
        assert _jwt_subject("hdr.!!!notbase64.sig") is None


class TestDecodeDocumentBody:
    def test_raw_pdf_passes_through(self):
        body = b"%PDF-1.7\nfake pdf content"
        assert _decode_document_body(body, {"document_id": "x"}) == body

    def test_base64_pdf_is_decoded(self):
        pdf = b"%PDF-1.7\nfake pdf content"
        encoded = base64.b64encode(pdf)
        assert _decode_document_body(encoded, {"document_id": "x"}) == pdf

    def test_raises_on_unexpected_body(self):
        with pytest.raises(RuntimeError, match="Unexpected response body"):
            _decode_document_body(b"<html>error</html>", {"document_id": "x"})
