FROM python:3.14.3-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && mkdir -p /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends 'postgresql-client-18=18.6-*' \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt \
    && groupadd -g 10001 watcher && useradd -u 10001 -g watcher -m watcher \
    && mkdir -p /data && chown watcher:watcher /data
COPY --chown=watcher:watcher . /app
ENV WATCHER_DATA_DIR=/data WATCHER_HOST=0.0.0.0 WATCHER_ENV=production WATCHER_TRUST_PROXY=1 WATCHER_SEED_ON_START=0
USER watcher
CMD ["python", "app.py"]

FROM app AS browser
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN python -m playwright install --with-deps chromium \
    && apt-get update && apt-get install -y --no-install-recommends xvfb tigervnc-scraping-server fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* && chmod -R a+rX /opt/playwright
USER watcher
CMD ["python", "scripts/run_browser.py"]

FROM debian:bookworm-slim AS gateway
RUN apt-get update && apt-get install -y --no-install-recommends nginx novnc \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/nginx.conf /etc/nginx/nginx.conf
CMD ["nginx", "-g", "daemon off;"]

FROM debian:bookworm-slim AS egress
RUN apt-get update && apt-get install -y --no-install-recommends squid ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/squid.conf /etc/squid/squid.conf
USER proxy
CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
