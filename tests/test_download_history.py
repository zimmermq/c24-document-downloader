"""Cross-run download history: a document fetched once is never fetched
again, even after a Paperless consumer has deleted the PDF from the
output folder."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from c24_client import (
    HISTORY_FILENAME,
    SessionState,
    Status,
    _download_document,
    _history_path,
    _load_download_history,
    submit_code_and_download,
)

DOC = {
    "document_id": "abc",
    "url": "/api/v1/account/X/statement/2026/05/",
    "subtitle": "C24 Smartkonto",
    "created_at": "2026-05-30 10:00:00",
    "mimetype": "application/pdf",
    "download_name": "Kontoauszug_2026-05_C24_Smartkonto",
}


def _state(output_dir: Path, **kw) -> SessionState:
    state = SessionState(
        token="t", output_dir=output_dir, session=requests.Session(),
        web_login_uuid="u", access_token="JWT.X.Y", **kw,
    )
    _load_download_history(state)
    return state


def _pdf_response():
    r = MagicMock(status_code=200)
    r.raise_for_status.return_value = None
    r.content = base64.b64encode(b"%PDF-1.7\nfake")
    return r


def _fake_c24(docs):
    """``session.get`` stand-in serving one year of ``docs`` plus PDFs."""
    def fake_get(url, **kw):
        r = MagicMock(status_code=200)
        r.raise_for_status.return_value = None
        if url.endswith("filters/"):
            r.json.return_value = {"years": ["2026"]}
        elif "/year/" in url:
            r.json.return_value = docs
        else:
            return _pdf_response()
        return r
    return fake_get


def _consume_all_pdfs(output_dir: Path) -> None:
    """What the Paperless consumer does: import, then delete the PDF."""
    for pdf in output_dir.rglob("*.pdf"):
        pdf.unlink()


class TestHistoryFile:
    def test_filename_is_paperless_ignored(self, tmp_path):
        assert HISTORY_FILENAME.startswith("._")
        assert _history_path(_state(tmp_path)) == tmp_path / HISTORY_FILENAME

    def test_missing_history_is_empty(self, tmp_path):
        assert _state(tmp_path).history_ids == set()

    def test_download_persists_identity(self, tmp_path):
        state = _state(tmp_path)
        with patch.object(state.session, "get", return_value=_pdf_response()):
            _download_document(state, DOC)
        assert (tmp_path / HISTORY_FILENAME).read_text().splitlines() == [DOC["url"]]
        assert _state(tmp_path).history_ids == {DOC["url"]}


class TestSkipping:
    def test_consumed_file_is_not_fetched_again(self, tmp_path):
        state = _state(tmp_path)
        with patch.object(state.session, "get", return_value=_pdf_response()):
            _download_document(state, DOC)
        _consume_all_pdfs(tmp_path)

        state = _state(tmp_path)
        with patch.object(state.session, "get") as get:
            fetched = _download_document(state, DOC)
        assert fetched is False
        get.assert_not_called()
        assert not list(tmp_path.rglob("*.pdf"))

    def test_distinct_documents_are_not_deduped(self, tmp_path):
        state = _state(tmp_path)
        other = {**DOC, "url": "/api/v1/account/X/statement/2026/06/",
                 "download_name": "Kontoauszug_2026-06_C24_Smartkonto"}
        with patch.object(state.session, "get", return_value=_pdf_response()):
            assert _download_document(state, DOC) is True
            assert _download_document(state, other) is True

    def test_failed_download_is_not_recorded(self, tmp_path):
        state = _state(tmp_path)
        boom = MagicMock(status_code=401)
        boom.raise_for_status.side_effect = requests.HTTPError("401")
        with patch.object(state.session, "get", return_value=boom):
            with pytest.raises(requests.HTTPError):
                _download_document(state, DOC)
        assert not (tmp_path / HISTORY_FILENAME).exists()
        assert _state(tmp_path).history_ids == set()

    def test_file_already_on_disk_is_recorded(self, tmp_path):
        state = _state(tmp_path)
        with patch.object(state.session, "get", return_value=_pdf_response()):
            _download_document(state, DOC)
        (tmp_path / HISTORY_FILENAME).unlink()

        state = _state(tmp_path)
        with patch.object(state.session, "get") as get:
            assert _download_document(state, DOC) is False
        get.assert_not_called()
        assert _state(tmp_path).history_ids == {DOC["url"]}


class TestRunCounts:
    def test_second_run_counts_skips_separately(self, tmp_path):
        state = _state(tmp_path)
        with patch.object(state.session, "get", side_effect=_fake_c24([DOC])), \
             patch("c24_client._submit_code"):
            submit_code_and_download(state, "123456")
        assert (state.downloaded_count, state.skipped_count) == (1, 0)
        _consume_all_pdfs(tmp_path)

        state = _state(tmp_path)
        with patch.object(state.session, "get", side_effect=_fake_c24([DOC])), \
             patch("c24_client._submit_code"):
            submit_code_and_download(state, "123456")
        assert state.status == Status.DONE
        assert (state.downloaded_count, state.skipped_count) == (0, 1)


class TestSeed:
    def test_seed_records_all_without_downloading(self, tmp_path):
        state = _state(tmp_path, seed_history=True)
        with patch.object(state.session, "get", side_effect=_fake_c24([DOC])) as get, \
             patch("c24_client._submit_code"):
            submit_code_and_download(state, "123456")
        assert state.status == Status.DONE
        assert (state.downloaded_count, state.skipped_count) == (0, 1)
        assert not list(tmp_path.rglob("*.pdf"))
        assert not any("/statement/" in c.args[0] for c in get.call_args_list)
        assert _state(tmp_path).history_ids == {DOC["url"]}

    def test_run_after_seed_downloads_only_new(self, tmp_path):
        state = _state(tmp_path, seed_history=True)
        with patch.object(state.session, "get", side_effect=_fake_c24([DOC])), \
             patch("c24_client._submit_code"):
            submit_code_and_download(state, "123456")

        new = {**DOC, "url": "/api/v1/account/X/statement/2026/06/",
               "download_name": "Kontoauszug_2026-06_C24_Smartkonto"}
        state = _state(tmp_path)
        with patch.object(state.session, "get", side_effect=_fake_c24([DOC, new])), \
             patch("c24_client._submit_code"):
            submit_code_and_download(state, "123456")
        assert (state.downloaded_count, state.skipped_count) == (1, 1)
        assert [p.name for p in tmp_path.rglob("*.pdf")] == [
            "2026-05-30_Kontoauszug_2026-06_C24_Smartkonto.pdf"
        ]
