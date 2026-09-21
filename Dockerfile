ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_BASE_IMAGE}

ARG DEBIAN_MIRROR=
ARG PIP_INDEX_URL=
ARG PLAYWRIGHT_DOWNLOAD_HOST=
ARG PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/bonski \
    TEMP_ROOT=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    XDG_CONFIG_HOME=/tmp/.config \
    XDG_RUNTIME_DIR=/tmp/runtime \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN if [ -n "$DEBIAN_MIRROR" ]; then \
        mirror="${DEBIAN_MIRROR%/}"; \
        for source_file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do \
            [ -f "$source_file" ] || continue; \
            sed -i \
                -e "s|http://deb.debian.org/debian|${mirror}|g" \
                -e "s|https://deb.debian.org/debian|${mirror}|g" \
                -e "s|http://deb.debian.org/debian-security|${mirror}-security|g" \
                -e "s|https://deb.debian.org/debian-security|${mirror}-security|g" \
                -e "s|http://security.debian.org/debian-security|${mirror}-security|g" \
                -e "s|https://security.debian.org/debian-security|${mirror}-security|g" \
                "$source_file"; \
        done; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin bonski
RUN if [ -n "$PLAYWRIGHT_DOWNLOAD_HOST" ]; then \
        export PLAYWRIGHT_DOWNLOAD_HOST; \
    fi; \
    if [ -n "$PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT" ]; then \
        export PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT; \
    fi; \
    python -m playwright install --with-deps chromium \
    && chown -R bonski:bonski "$PLAYWRIGHT_BROWSERS_PATH"

COPY --chown=bonski:bonski app ./app
COPY --chown=bonski:bonski pokemon-theme.css ./pokemon-theme.css

USER bonski

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]
