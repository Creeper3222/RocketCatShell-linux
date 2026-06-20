FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt

COPY LICENSE README.md ./
COPY rocketcat_shell ./rocketcat_shell
COPY data/plugins ./data/plugins
COPY tools ./tools
COPY data/plugins /opt/rocketcat/builtin_plugins
COPY docker/examples /opt/rocketcat/examples
COPY docker/entrypoint.sh /usr/local/bin/rocketcat-entrypoint.sh

RUN chmod +x /usr/local/bin/rocketcat-entrypoint.sh \
    && mkdir -p /app/config/plugins_config /app/data/temp /app/data/bots /app/data/user_identity /app/data/plugin_data /app/logs

EXPOSE 5751

VOLUME ["/app/config", "/app/data/temp", "/app/data/bots", "/app/data/user_identity", "/app/data/plugins", "/app/data/plugin_data", "/app/logs"]

ENTRYPOINT ["/usr/local/bin/rocketcat-entrypoint.sh"]
CMD []
