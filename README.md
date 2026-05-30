# c24-document-downloader

A small Flask web service that bridges C24 Bank's interactive QR-code login
to an automated mailbox download → Paperless consume folder.

C24 Bank requires a QR-code-and-app-code 2FA dance on **every** login (no
FinTS, no "trust this device" — verified against the canonical
`hbci4j/blz.properties` catalog and the `fints-url` DB for BLZ `50024024`).
This service does the only part that *can* be automated: it shows you the
QR on a phone-friendly page and, once you enter the code from the C24 app,
scrapes the document mailbox and writes the PDFs into the configured output
directory.

## Flow

1. **`GET /`** — fetches `banking.c24.de/login`, extracts the QR challenge,
   renders a page with the QR (and a tappable deep-link if the QR payload is
   a URL) plus a 6-digit code-entry form.
2. You scan the QR with the C24 app on your phone; the app shows a code.
3. You type the code into the page and submit.
4. **`POST /code`** — forwards the code to C24, scrapes the document
   mailbox, writes each PDF into `C24_OUTPUT_DIR`.
5. **`GET /done`** — results summary.

No CronJob. No stored credentials. Each visit is a complete, ephemeral
session held in memory under a short random token; nothing survives a pod
restart.

## Configuration

| Env var          | Default        | Description                              |
|------------------|----------------|------------------------------------------|
| `C24_OUTPUT_DIR` | `./downloads`  | Where downloaded PDFs land (mount this). |
| `PORT`           | `5000`         | HTTP listen port.                        |

## Local development

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
# open http://localhost:5000
```

## Docker

```bash
docker build -t c24-document-downloader .
docker run --rm -p 5000:5000 \
  -v "$(pwd)/downloads:/data/downloads" \
  c24-document-downloader
```

## Deploy

Helm chart lives in the apps repo at `apps/c24-document-downloader/`
(mirrors `apps/pytr`). Wired up via the `c24-document-downloader:` block in
`homelab_environments/zimmermann.lat/values.yaml`.

## Status

Scaffolding is complete; the wire-level C24 calls in `c24_client.py` are
intentionally stubbed (`NotImplementedError`) pending capture of the real
C24 login + mailbox network traces. See `c24_client.py` for the TODOs.
