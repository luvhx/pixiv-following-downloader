FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --prefix=/install -r requirements.txt


FROM python:3.11-slim

ARG VERSION=dev
LABEL org.opencontainers.image.title="Pixiv Following Downloader" \
      org.opencontainers.image.description="Pixiv following downloader with WebUI, gallery and local/WebDAV storage" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

COPY main.py config.py rules.py pixiv.py storage.py database.py scheduler.py gallery.py webui.py healthcheck.py ./
COPY templates ./templates
COPY static ./static

RUN mkdir -p /app/data/downloads

EXPOSE 8971
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "healthcheck.py"]

CMD ["python", "main.py"]
