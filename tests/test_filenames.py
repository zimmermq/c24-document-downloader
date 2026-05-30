"""Pure-function tests for filename / folder sanitization."""

from __future__ import annotations

from c24_client import (
    MAX_FILENAME_LENGTH,
    _build_filename,
    _safe_folder_name,
    _sanitize_filename,
)


class TestSafeFolderName:
    def test_preserves_spaces_and_german_punctuation(self):
        assert _safe_folder_name("Berliner Ring 161b, Bensheim") == \
            "Berliner Ring 161b, Bensheim"
        assert _safe_folder_name("Chrodegangstraße 2 - 4, 64653") == \
            "Chrodegangstraße 2 - 4, 64653"

    def test_strips_path_separators(self):
        assert _safe_folder_name("foo/bar") == "foo_bar"
        assert _safe_folder_name("foo\\bar") == "foo_bar"

    def test_strips_control_chars(self):
        assert _safe_folder_name("foo\x00bar\x07") == "foobar"

    def test_empty_falls_back_to_sonstige(self):
        assert _safe_folder_name("") == "Sonstige"
        assert _safe_folder_name("   ") == "Sonstige"


class TestSanitizeFilename:
    def test_strips_illegal_chars(self):
        assert _sanitize_filename('a<b>c:"d|e?f*g') == "a_b_c_d_e_f_g"

    def test_collapses_whitespace_and_underscores(self):
        assert _sanitize_filename("a   b___c") == "a_b_c"

    def test_strips_trailing_dots_from_stem(self):
        assert _sanitize_filename("foo...pdf").endswith("pdf")

    def test_caps_length(self):
        long = "x" * (MAX_FILENAME_LENGTH + 50) + ".pdf"
        out = _sanitize_filename(long)
        assert len(out) <= MAX_FILENAME_LENGTH
        assert out.endswith(".pdf")


class TestBuildFilename:
    def _doc(self, **overrides):
        d = {
            "document_id": "abc123",
            "download_name": "Kontoauszug_2026-05_C24_Smartkonto",
            "created_at": "2026-05-30 10:35:41",
            "mimetype": "application/pdf",
        }
        d.update(overrides)
        return d

    def test_iso_date_prefix(self):
        assert _build_filename(self._doc()) == \
            "2026-05-30_Kontoauszug_2026-05_C24_Smartkonto.pdf"

    def test_falls_back_to_document_id_without_download_name(self):
        out = _build_filename(self._doc(download_name=None))
        assert "abc123" in out

    def test_no_date_when_created_at_missing(self):
        out = _build_filename(self._doc(created_at=None))
        assert out.startswith("Kontoauszug")  # no YYYY-MM-DD prefix

    def test_no_pdf_extension_when_mimetype_is_not_pdf(self):
        out = _build_filename(self._doc(mimetype="image/png"))
        assert not out.endswith(".pdf")

    def test_sanitizes_german_chars_in_download_name(self):
        out = _build_filename(self._doc(download_name="Kontoauszug_2025-03_Wormser_Str__9,_Bensheim"))
        # Sanitization collapses double-underscores
        assert "__" not in out
