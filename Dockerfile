FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV MASH_DATA_DIR=/var/lib/mash
ENV MASH_API_HOST=0.0.0.0
ENV MASH_API_PORT=8000

WORKDIR /opt/mash

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN pip install .

# Two users: `mash` runs the runtime and holds its env; `agent` runs every bash
# session the agent opens and can read only /home/agent. `mash` may start bash
# as `agent` and nothing else.
RUN apt-get update && apt-get install -y --no-install-recommends sudo && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash mash \
    && useradd --create-home --shell /bin/bash agent \
    && chmod 0700 /home/mash \
    && mkdir -p /home/agent/memory /home/agent/workspace \
    && chown -R agent:agent /home/agent \
    && chmod 0755 /home/agent /home/agent/memory /home/agent/workspace \
    && echo "mash ALL=(agent) NOPASSWD: /bin/bash" > /etc/sudoers.d/mash-agent \
    && chmod 0440 /etc/sudoers.d/mash-agent \
    && mkdir -p /var/lib/mash \
    && chown mash:mash /var/lib/mash

USER mash

EXPOSE 8000

CMD ["sh", "-c", "mash host serve --host-app \"${MASH_HOST_APP}\" --host \"${MASH_API_HOST}\" --port \"${MASH_API_PORT}\" ${MASH_API_KEY:+--api-key \"$MASH_API_KEY\"} ${MASH_RUNTIME_BIND_HOST:+--runtime-bind-host \"$MASH_RUNTIME_BIND_HOST\"} ${MASH_MEMORY_DB:+--memory-db \"$MASH_MEMORY_DB\"}"]
