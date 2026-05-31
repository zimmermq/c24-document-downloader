# c24-document-downloader

A small Flask web service that bridges C24 Bank's interactive QR-code login
to an automated mailbox download → Paperless consume folder.

C24 Bank requires a QR-code-and-app-code 2FA dance on **every** login (no
FinTS, no "trust this device" — verified against the canonical
`hbci4j/blz.properties` catalog and the `fints-url` DB for BLZ `50024024`).
This service does the only part that *can* be automated: it shows you the
QR on a phone-friendly page and, once you confirm the login in the C24
app and enter the displayed code, scrapes the document mailbox and writes
the PDFs into the configured output directory.

## Flow

1. **`GET /`** — fetches a fresh qrtoken from `api.c24.de/api/qrtoken/generate/`
   and renders a page with the QR plus a 6-digit code-entry form.
2. You scan the QR with the C24 app on your phone; the app prompts you to
   confirm the login and then shows a 6-digit code.
3. The page polls `/status` at 1 Hz — it auto-detects the moment the app
   has authorized the login and also swaps in a fresh QR if C24 rotates
   the token.
4. You type the code into the page and submit.
5. **`POST /code`** — forwards the code to C24, captures the bearer JWT,
   enumerates `/api/document-center/filters/` for the relevant years,
   downloads each PDF, and writes them under `C24_OUTPUT_DIR`. The
   download runs in a background thread; the page meta-refreshes against
   `/status` for live progress.
6. **`GET /done`** — results summary.

No CronJob. No stored credentials. Each visit is a complete, ephemeral
session held in memory under a short random token; nothing survives a pod
restart, and sessions older than an hour are evicted automatically.

## Configuration

| Env var              | Default       | Description                                                                                  |
|----------------------|---------------|----------------------------------------------------------------------------------------------|
| `C24_OUTPUT_DIR`     | `./downloads` | Where downloaded PDFs land (mount this).                                                     |
| `C24_FLAT_STRUCTURE` | unset         | When truthy (`1/true/yes/on`), drop the per-account subfolder so every PDF lands flat in `C24_OUTPUT_DIR` — useful for Paperless-style consume folders. |
| `PORT`               | `5000`        | HTTP listen port (dev only; Docker hardcodes 5000).                                          |

Files are named `{YYYY-MM-DD}_{download_name}.pdf` from the document's
`created_at`. Without `C24_FLAT_STRUCTURE`, they're grouped into
per-account subfolders using the C24-supplied account label.

## Local development

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python app.py
# open http://localhost:5000

# Tests
.venv/bin/python -m pytest tests/ -v
```

## Docker

```bash
docker build -t c24-document-downloader .
docker run --rm -p 5000:5000 \
  -v "$(pwd)/downloads:/data/downloads" \
  c24-document-downloader
```

The image runs `gunicorn` with a single worker and 4 threads — the
in-memory session store is per-process, so don't raise the worker count
without introducing shared state.

## CI

`.github/workflows/docker-build-deploy.yml` runs the test suite on every
push to `main`, validates the Docker build, and pushes a tagged image to
ECR (`eu-central-1`) when a GitHub release is published.
