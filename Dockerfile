FROM python:3.13-slim

WORKDIR /app

# Pillow runtime deps (qrcode[pil])
RUN apt-get update \
 && apt-get install -y --no-install-recommends libjpeg62-turbo \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py c24_client.py ./
COPY templates/ ./templates/
COPY static/ ./static/

ENV C24_OUTPUT_DIR=/data/downloads
RUN mkdir -p /data/downloads
VOLUME ["/data/downloads"]

EXPOSE 5000

# Single worker keeps the in-memory session dict consistent;
# threads handle the rare concurrent visitor.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", \
     "--access-logfile", "-", "--error-logfile", "-", "app:app"]
