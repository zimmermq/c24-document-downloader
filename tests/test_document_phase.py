"""Document listing + download — locks the per-account subfolder layout,
the skip-if-exists optimisation, and the per-doc failure tolerance."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from requests.exceptions import HTTPError

from c24_client import (
    SessionState,
    Status,
    _download_document,
    _list_documents,
    submit_code_and_download,
)


@pytest.fixture
def state(tmp_path: Path) -> SessionState:
    return SessionState(
        token="t", output_dir=tmp_path, session=requests.Session(),
        web_login_uuid="u", access_token="JWT.X.Y",
    )


@pytest.fixture
def b64_pdf_response():
    r = MagicMock()
    r.status_code = 200
    r.raise_for_status.return_value = None
    r.content = base64.b64encode(b"%PDF-1.7\nfake")
    return r


# ----------------------------------------------------- _download_document

class TestDownloadDocument:
    DOC = {
        "document_id": "abc",
        "url": "/api/v1/account/X/statement/2026/05/",
        "subtitle": "C24 Smartkonto",
        "created_at": "2026-05-30 10:00:00",
        "mimetype": "application/pdf",
        "download_name": "Kontoauszug_2026-05_C24_Smartkonto",
    }

    def test_writes_under_per_account_subfolder(self, state, b64_pdf_response):
        with patch.object(state.session, "get", return_value=b64_pdf_response):
            fetched = _download_document(state, self.DOC)
        assert fetched is True
        files = list(state.output_dir.rglob("*.pdf"))
        assert len(files) == 1
        assert files[0].parent.name == "C24 Smartkonto"
        assert files[0].name == "2026-05-30_Kontoauszug_2026-05_C24_Smartkonto.pdf"

    def test_returns_false_when_file_already_exists(self, state, b64_pdf_response):
        with patch.object(state.session, "get", return_value=b64_pdf_response):
            _download_document(state, self.DOC)  # first call writes
            fetched = _download_document(state, self.DOC)  # second call skips
        assert fetched is False
        assert state.files[-1].endswith("(existed)")

    def test_rejects_unknown_url_shape(self, state):
        bad = {**self.DOC, "url": "https://attacker.example.com/file.pdf"}
        with pytest.raises(RuntimeError, match="Unexpected url field"):
            _download_document(state, bad)

    def test_flat_mode_writes_into_output_dir_root(self, state, b64_pdf_response):
        state.flat = True
        with patch.object(state.session, "get", return_value=b64_pdf_response):
            _download_document(state, self.DOC)
        files = list(state.output_dir.rglob("*.pdf"))
        assert len(files) == 1
        # Flat: directly under output_dir, no per-account subfolder.
        assert files[0].parent == state.output_dir
        assert files[0].name == "2026-05-30_Kontoauszug_2026-05_C24_Smartkonto.pdf"
        # state.files display has no folder prefix.
        assert "/" not in state.files[-1]


# ------------------------------------------------------- _list_documents

class TestListDocuments:
    def test_walks_only_years_filters_returns(self, state):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            r = MagicMock(status_code=200); r.raise_for_status.return_value = None
            if url.endswith("filters/"):
                r.json.return_value = {"years": ["2025", "2026"]}
            else:
                r.json.return_value = [{"document_id": "x"}]
            return r
        with patch.object(state.session, "get", side_effect=fake_get):
            docs = _list_documents(state)
        # 1 filter call + 2 year calls
        assert len(calls) == 3
        assert any("filters/" in u for u in calls)
        assert sum("/year/" in u for u in calls) == 2
        assert len(docs) == 2  # one per year

    def test_newest_year_first(self, state):
        seen_years = []

        def fake_get(url, **kw):
            r = MagicMock(status_code=200); r.raise_for_status.return_value = None
            if url.endswith("filters/"):
                r.json.return_value = {"years": ["2023", "2024", "2025", "2026"]}
            else:
                # Capture which year was requested
                seen_years.append(url.split("/year/")[1].rstrip("/"))
                r.json.return_value = []
            return r
        with patch.object(state.session, "get", side_effect=fake_get):
            _list_documents(state)
        assert seen_years == ["2026", "2025", "2024", "2023"]

    def test_404_per_year_is_skipped(self, state):
        def fake_get(url, **kw):
            r = MagicMock(); r.raise_for_status.return_value = None
            if url.endswith("filters/"):
                r.status_code = 200
                r.json.return_value = {"years": ["2025", "2026"]}
            elif "/year/2025/" in url:
                r.status_code = 404
            else:
                r.status_code = 200
                r.json.return_value = [{"document_id": "x"}]
            return r
        with patch.object(state.session, "get", side_effect=fake_get):
            docs = _list_documents(state)
        # 2025 returned 404, only 2026's one doc counts
        assert len(docs) == 1


# --------------------- submit_code_and_download (failure-tolerant loop)

class TestRunIsFailureTolerant:
    """A 401 on one doc must not abort the whole run."""

    DOCS = [
        {"document_id": "a", "url": "/x/1", "subtitle": "C24 Smartkonto",
         "created_at": "2026-01-01 00:00:00", "mimetype": "application/pdf",
         "download_name": "ok-1"},
        {"document_id": "b", "url": "/x/2", "subtitle": "C24 Smartkonto",
         "created_at": "2026-02-01 00:00:00", "mimetype": "application/pdf",
         "download_name": "fails-with-401"},
        {"document_id": "c", "url": "/x/3", "subtitle": "C24 Smartkonto",
         "created_at": "2026-03-01 00:00:00", "mimetype": "application/pdf",
         "download_name": "ok-3"},
    ]

    def test_one_failure_doesnt_abort_loop(self, state, b64_pdf_response):
        # Stub login phase so we go straight to documents.
        state.status = Status.DOWNLOADING
        call_count = [0]

        def fake_get(url, **kw):
            r = MagicMock(); r.raise_for_status.return_value = None
            if url.endswith("filters/"):
                r.status_code = 200; r.json.return_value = {"years": ["2026"]}
                return r
            if "/year/" in url:
                r.status_code = 200; r.json.return_value = self.DOCS
                return r
            # Doc downloads: 2nd one fails.
            call_count[0] += 1
            if call_count[0] == 2:
                r.status_code = 401
                r.raise_for_status.side_effect = HTTPError("401 Unauthorized")
                return r
            r.status_code = 200
            r.content = base64.b64encode(b"%PDF-1.7\nfake")
            return r

        with patch.object(state.session, "get", side_effect=fake_get), \
             patch("c24_client._poll_for_authorization", return_value="u"), \
             patch("c24_client._submit_code"):
            submit_code_and_download(state, "123456")

        assert state.status == Status.DONE
        assert state.total_count == 3
        assert state.downloaded_count == 2
        assert state.failed_count == 1
        # Failed doc shows up with the ✗ marker in the file list.
        assert any(f.startswith("✗") for f in state.files)
