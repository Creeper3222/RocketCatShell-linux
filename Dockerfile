ARG BUILD_VERSION=v0.2.3
ARG BUILD_REVISION=unknown
ARG CONTAINER_RUNTIME_GENERATION=1

FROM python:3.11-slim

ARG BUILD_VERSION
ARG BUILD_REVISION
ARG CONTAINER_RUNTIME_GENERATION

LABEL org.opencontainers.image.title="RocketCatShell Linux" \
      org.opencontainers.image.version="$BUILD_VERSION" \
      org.opencontainers.image.revision="$BUILD_REVISION" \
      org.opencontainers.image.source="https://github.com/Creeper3222/RocketCatShell-linux" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      io.rocketcat.container-runtime-generation="$CONTAINER_RUNTIME_GENERATION"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ROCKETCAT_IMAGE_VERSION="$BUILD_VERSION" \
    ROCKETCAT_CONTAINER_RUNTIME_GENERATION="$CONTAINER_RUNTIME_GENERATION"

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt

COPY LICENSE README.md CHANGELOG.md ./
COPY rocketcat_shell ./rocketcat_shell
COPY data/plugins ./data/plugins
COPY tools ./tools
COPY data/plugins /opt/rocketcat/builtin_plugins
COPY docker/examples /opt/rocketcat/examples
COPY docker/entrypoint.sh /usr/local/bin/rocketcat-entrypoint.sh
COPY tools/update_helper.py /opt/rocketcat/update_helper.py

RUN chmod +x /usr/local/bin/rocketcat-entrypoint.sh \
    && mkdir -p /app/config/plugins_config /app/data/temp /app/data/bots /app/data/user_identity /app/data/plugin_data /app/data/update /app/logs

EXPOSE 5751 3000 3001

VOLUME ["/app/config", "/app/data/temp", "/app/data/bots", "/app/data/user_identity", "/app/data/plugins", "/app/data/plugin_data", "/app/logs"]

ENTRYPOINT ["/usr/local/bin/rocketcat-entrypoint.sh"]
CMD []
