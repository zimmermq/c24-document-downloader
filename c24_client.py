"""C24 Bank wire-level client.

C24's web portal at ``banking.c24.de`` is passwordless and device-bound:
each login is a fresh QR challenge that the user scans with the C24 app,
which then shows a 6-digit confirmation code. The flow:

    GET  /api/qrtoken/generate/
        -> {qrtoken: "https://link.c24.de/web-login/<128-hex>"}
           (universal-link the C24 app handles natively)

    POST /api/qrtoken/status/
         body {qrtoken}
        -> {web_login_uuid: null | UUID, qrtoken: <same or rotated>}
           (page polls this ~1 Hz; the ``qrtoken`` field is the rotation
           signal — when C24 hands us a different one, we adopt it)

    POST /api/web-login/complete/
         body {web_login_pin, qrtoken, web_login_uuid}
        -> {access_token, refresh_token, ...}
           (bearer JWT, ~10 min lifetime, for the document phase)

    GET  /api/document-center/filters/
        -> {years: [...], accounts: [...], document_types: [...]}
           (drives which years to enumerate)

    GET  /api/document-center/documents/year/<Y>/
        -> [doc, doc, ...]   each with a per-doc download ``url``

    GET  <doc.url>
        -> PDF body, base64-encoded (starts ``JVBE``); raw ``%PDF`` is
           also accepted in case the API ever stops base64-encoding.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import qrcode
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# --------------------------------------------------------------- endpoints

C24_WEB_BASE = "https://banking.c24.de"
C24_API_BASE = "https://api.c24.de"
C24_QRTOKEN_URL = f"{C24_API_BASE}/api/qrtoken/generate/"
C24_STATUS_URL = f"{C24_API_BASE}/api/qrtoken/status/"
C24_COMPLETE_URL = f"{C24_API_BASE}/api/web-login/complete/"
C24_DOCUMENTS_BY_YEAR_URL = (
    f"{C24_API_BASE}/api/document-center/documents/year/{{year}}/"
)
C24_FILTERS_URL = f"{C24_API_BASE}/api/document-center/filters/"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:151.0) "
        "Gecko/20100101 Firefox/151.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de,en-US;q=0.9,en;q=0.8",
    "Origin": C24_WEB_BASE,
    "Referer": f"{C24_WEB_BASE}/",
}

# ---------------------------------------------------------------- timing

# How long we loop polling /api/qrtoken/status/ on /code submit if the page's
# heartbeat hasn't already cached ``web_login_uuid`` on the session.
POLL_MAX_ATTEMPTS = 10
POLL_INTERVAL_SECONDS = 1.0

# Safety net: if C24's /status/ response never rotates our qrtoken, the
# /status route in app.py falls back to /generate/ after this many seconds.
# Shouldn't normally fire — rotations land within ~1 s in practice.
QRTOKEN_REFRESH_AFTER_SECONDS = 20

# Maximum filename length (Windows-friendly cap).
MAX_FILENAME_LENGTH = 200


# ---------------------------------------------------------------- state

class Status(str, Enum):
    """Lifecycle states for a single user visit.

    Inherits from ``str`` so it compares cleanly with the literal strings
    that the Flask templates and ``/status`` JSON callers use.
    """

    AWAITING_CODE = "awaiting_code"
    LOGGING_IN = "logging_in"
    DOWNLOADING = "downloading"
    DONE = "done"
    LOGIN_FAILED = "login_failed"
    DOWNLOAD_FAILED = "download_failed"


@dataclass
class SessionState:
    """Per-visit state held in the Flask in-memory session store."""

    token: str
    output_dir: Path
    session: requests.Session

    # QR challenge — populated by start_login + refreshed by /status.
    qrtoken_url: Optional[str] = None
    qrtoken_fetched_at: float = 0.0
    deep_link: Optional[str] = None
    qr_image_data_uri: Optional[str] = None

    # Login completion — populated by /code submission.
    web_login_uuid: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    access_token_expires_at: Optional[int] = None

    # Lifecycle.
    status: Status = Status.AWAITING_CODE
    total_count: int = 0
    downloaded_count: int = 0
    failed_count: int = 0
    files: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class StatusResult:
    """One poll of /api/qrtoken/status/.

    ``qrtoken`` is the server's current value; if it differs from what we
    sent, C24 has rotated us to a fresh token and the caller should adopt
    it (that's how the SPA refreshes the displayed QR, no /generate/ call).
    """

    web_login_uuid: Optional[str]
    qrtoken: Optional[str]


# ---------------------------------------------------------------- HTTP

def _new_http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    retry = Retry(total=3, backoff_factor=1,
                  status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# ---------------------------------------------------------------- QR phase

def start_login(token: str, output_dir: Path) -> SessionState:
    """Open a fresh login session: anonymous GET /generate/, render the QR."""
    state = SessionState(
        token=token,
        output_dir=output_dir,
        session=_new_http_session(),
    )
    refresh_qrtoken(state)
    return state


def refresh_qrtoken(state: SessionState) -> None:
    """Fetch a fresh qrtoken via /generate/ and adopt it on state.

    Called at session start and as the safety-net fallback from /status if
    C24 somehow never rotates our token.
    """
    response = state.session.get(C24_QRTOKEN_URL, timeout=15)
    response.raise_for_status()
    try:
        qrtoken_url = response.json()["qrtoken"]
    except (ValueError, KeyError) as e:
        raise RuntimeError(
            f"C24 QR-token API returned unexpected body: {response.text[:200]!r}"
        ) from e
    adopt_qrtoken(state, qrtoken_url)


def adopt_qrtoken(state: SessionState, qrtoken_url: str) -> None:
    """Re-render QR + deep-link for ``qrtoken_url`` and stash on state.

    Used after /generate/ (refresh_qrtoken) and after /status/ tells us
    C24 rotated our token (the /status route in app.py).
    """
    state.qrtoken_url = qrtoken_url
    state.deep_link = qrtoken_url
    state.qr_image_data_uri = _render_qr_data_uri(qrtoken_url)
    state.qrtoken_fetched_at = time.time()


def poll_qrtoken_status(state: SessionState) -> StatusResult:
    """One-shot poll of /api/qrtoken/status/.

    Anonymous endpoint — no auth, no x-c24-* headers. Returns both auth
    status and the (possibly rotated) qrtoken from the server.
    """
    if not state.qrtoken_url:
        return StatusResult(None, None)
    response = state.session.post(
        C24_STATUS_URL,
        json={"qrtoken": state.qrtoken_url},
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    return StatusResult(data.get("web_login_uuid"), data.get("qrtoken"))


def _render_qr_data_uri(payload: str) -> str:
    """Render a QR PNG for ``payload`` as a data URI. qrcode defaults are
    kept on purpose — border=4 is the QR-spec quiet-zone requirement that
    the C24 app's scanner enforces."""
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ------------------------------------------------- code submission + tokens

def submit_code_and_download(state: SessionState, code: str) -> None:
    """Complete login with the 6-digit code, then run the document phase.

    Runs in a background thread (spawned by ``/code``); per-doc failures
    don't abort the whole run, they're tracked separately on the state.
    """
    state.status = Status.LOGGING_IN
    try:
        # The page's heartbeat usually caches web_login_uuid before the
        # user can hit submit; only loop if it didn't.
        if state.web_login_uuid is None:
            state.web_login_uuid = _poll_for_authorization(state)
        _submit_code(state, code)
    except Exception as e:
        logger.exception("C24 login failed")
        state.status = Status.LOGIN_FAILED
        state.error = str(e)
        return

    logger.info("C24 login OK (sub=%s)", _jwt_subject(state.access_token))

    state.status = Status.DOWNLOADING
    try:
        documents = _list_documents(state)
    except Exception as e:
        logger.exception("C24 document listing failed")
        state.status = Status.DOWNLOAD_FAILED
        state.error = str(e)
        return

    state.total_count = len(documents)
    for doc in documents:
        _run_one_download(state, doc)
    state.status = Status.DONE


def _run_one_download(state: SessionState, doc: dict) -> None:
    """One iteration of the document loop. Per-doc failures are caught and
    counted; the loop continues to the next doc."""
    try:
        fetched = _download_document(state, doc)
        state.downloaded_count += 1
        logger.info(
            "C24 doc %d/%d %s: %s",
            state.downloaded_count + state.failed_count,
            state.total_count,
            "fetched " if fetched else "(existed)",
            doc.get("download_name") or doc.get("document_id"),
        )
    except Exception as e:
        state.failed_count += 1
        logger.warning(
            "C24 doc %d/%d FAILED: %s — %s",
            state.downloaded_count + state.failed_count,
            state.total_count,
            doc.get("download_name") or doc.get("document_id"),
            e,
        )
        state.files.append(
            f"✗ {_safe_folder_name(doc.get('subtitle') or 'Sonstige')}/"
            f"{doc.get('download_name') or doc.get('document_id') or '?'}: {e}"
        )


def _poll_for_authorization(state: SessionState) -> str:
    """Loop /api/qrtoken/status/ until ``web_login_uuid`` is populated."""
    for _ in range(POLL_MAX_ATTEMPTS):
        result = poll_qrtoken_status(state)
        if result.web_login_uuid:
            return result.web_login_uuid
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        "QR-Token wurde nicht autorisiert — bitte zuerst den Login in der "
        "C24-App bestätigen und den angezeigten Code dann hier eingeben."
    )


def _submit_code(state: SessionState, code: str) -> None:
    """POST /api/web-login/complete/ with the PIN; capture bearer tokens.

    ``Authorization: Bearer undefined`` is sent **literally** as the string
    "undefined" — that's what the SPA does, because no token has been
    issued yet at this point.
    """
    response = state.session.post(
        C24_COMPLETE_URL,
        json={
            "web_login_pin": code,
            "qrtoken": state.qrtoken_url,
            "web_login_uuid": state.web_login_uuid,
        },
        headers={
            **_c24_session_headers(state),
            "Authorization": "Bearer undefined",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    state.access_token = data["access_token"]
    state.refresh_token = data.get("refresh_token")
    state.access_token_expires_at = data.get("access_token_expires_at")


# --------------------------------------------------------- document phase

def _list_documents(state: SessionState) -> list[dict]:
    """Enumerate every C24 document across the years that actually exist.

    Two-step:
    1. ``GET /api/document-center/filters/`` returns
       ``{"years": ["2025","2026"], "accounts":[…], "document_types":[…]}``
       — the years field tells us exactly which calendar years have any
       documents, so we don't have to guess a lookback window.
    2. For each year, ``GET /api/document-center/documents/year/<Y>/``
       returns a flat array of document objects.

    Years are iterated newest-first so the most recent statements land
    first if a run is interrupted.
    """
    response = state.session.get(
        C24_FILTERS_URL, headers=_auth_headers(state), timeout=15
    )
    response.raise_for_status()
    years = response.json().get("years", [])

    all_docs: list[dict] = []
    for year in sorted(years, reverse=True):
        response = state.session.get(
            C24_DOCUMENTS_BY_YEAR_URL.format(year=year),
            headers=_auth_headers(state),
            timeout=30,
        )
        if response.status_code == 404:
            continue
        response.raise_for_status()
        docs = response.json()
        if docs:
            logger.info("C24 %s: %d documents", year, len(docs))
            all_docs.extend(docs)
    return all_docs


def _download_document(state: SessionState, doc: dict) -> bool:
    """Fetch one document and write it under ``state.output_dir/<account>/``.

    Returns ``True`` if the file was actually fetched and written, ``False``
    if it already existed on disk (the caller uses this for a "fetched" vs
    "(existed)" log line).

    The relative ``url`` varies by document_type (main-account statements,
    pocket statements, ATC documents all live under different prefixes).
    The response body is the PDF, observed delivered as base64 (``JVBE``
    decodes to ``%PDF-1.7``); raw ``%PDF`` bytes are accepted as forward
    compatibility.
    """
    rel_url = doc.get("url") or ""
    if not rel_url.startswith("/"):
        raise RuntimeError(f"Unexpected url field on document: {rel_url!r}")
    folder = _safe_folder_name(doc.get("subtitle") or "Sonstige")
    output_path = state.output_dir / folder / _build_filename(doc)
    display = f"{folder}/{output_path.name}"

    if output_path.exists():
        state.files.append(f"{display} (existed)")
        return False

    response = state.session.get(
        f"{C24_API_BASE}{rel_url}",
        headers=_auth_headers(state),
        timeout=120,
    )
    response.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(_decode_document_body(response.content, doc))
    state.files.append(display)
    return True


def _decode_document_body(body: bytes, doc: dict) -> bytes:
    """Return PDF bytes from a download response.

    The C24 API delivers PDFs as base64 text (body starts with ``JVBE``,
    the base64 encoding of ``%PD``); raw ``%PDF`` bytes are accepted too.
    """
    if body.startswith(b"%PDF"):
        return body
    if body.startswith(b"JVBE"):
        return base64.b64decode(body)
    raise RuntimeError(
        f"Unexpected response body for {doc.get('document_id')}: {body[:30]!r}…"
    )


# ---------------------------------------------------- filename / folder

def _build_filename(doc: dict) -> str:
    """``{YYYY-MM-DD}_{download_name}.pdf`` (date from ``created_at``)."""
    created = (doc.get("created_at") or "")[:10]  # "YYYY-MM-DD HH:MM:SS" -> date
    name = doc.get("download_name") or doc.get("document_id") or "Dokument"
    ext = ".pdf" if "pdf" in (doc.get("mimetype") or "").lower() else ""
    raw = f"{created}_{name}{ext}" if created else f"{name}{ext}"
    return _sanitize_filename(raw)


def _safe_folder_name(name: str) -> str:
    """Folder-safe name that keeps spaces and German punctuation — matches
    how the C24 portal presents account names (``Berliner Ring 161b,
    Bensheim``). Only path separators and control chars are stripped."""
    name = name.replace("/", "_").replace("\\", "_")
    name = "".join(c for c in name if ord(c) >= 32)
    return name.strip() or "Sonstige"


def _sanitize_filename(name: str) -> str:
    """Filesystem-safe filename: replace illegal chars, collapse whitespace,
    cap at ``MAX_FILENAME_LENGTH`` (Windows-friendly)."""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = "".join(c for c in name if ord(c) >= 32)
    name = re.sub(r"[_\s]+", "_", name)
    base, ext = os.path.splitext(name)
    name = base.rstrip(".") + ext
    if len(name) > MAX_FILENAME_LENGTH:
        base, ext = os.path.splitext(name)
        name = base[: MAX_FILENAME_LENGTH - len(ext)] + ext
    return name.strip("_")


# ------------------------------------------------------ headers + JWT

def _c24_session_headers(state: SessionState) -> dict[str, str]:
    """The x-c24-* headers every C24 API call carries.

    The SPA generates a **fresh** ``x-c24-guid`` per request (verified
    across the captured traces), so we do the same — it's request-tracing,
    not a stable fingerprint. ``x-c24-unicorn-guid`` / ``x-c24-tracingSession``
    are the per-login UUID the C24 server hands us via ``/status/``.
    """
    return {
        "x-c24-guid": str(uuid.uuid4()),
        "x-c24-timestamp": str(int(time.time())),
        "x-c24-unicorn-guid": state.web_login_uuid or "",
        "x-c24-tracingSession": state.web_login_uuid or "",
    }


def _auth_headers(state: SessionState) -> dict[str, str]:
    """Headers for authenticated C24 API calls (post-login)."""
    return {
        **_c24_session_headers(state),
        "Authorization": f"Bearer {state.access_token}",
    }


def _jwt_subject(token: Optional[str]) -> Optional[str]:
    """Decode a JWT's ``sub`` claim — no signature check, display only."""
    if not token:
        return None
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # base64 padding
        return json.loads(base64.urlsafe_b64decode(payload_b64)).get("sub")
    except Exception:
        return None
