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

RUN mkdir -p /var/lib/mash

EXPOSE 8000

CMD ["sh", "-c", "mash host serve --host-app \"${MASH_HOST_APP}\" --host \"${MASH_API_HOST}\" --port \"${MASH_API_PORT}\" ${MASH_API_KEY:+--api-key \"$MASH_API_KEY\"} ${MASH_RUNTIME_BIND_HOST:+--runtime-bind-host \"$MASH_RUNTIME_BIND_HOST\"} ${MASH_MEMORY_DB:+--memory-db \"$MASH_MEMORY_DB\"}"]
