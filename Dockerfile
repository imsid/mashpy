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

# `mash` runs the host and the agent processes. The agents' code runs in
# per-user sandboxes from Dockerfile.sandbox, not here.
RUN useradd --create-home --shell /bin/bash mash \
    && chmod 0700 /home/mash \
    && mkdir -p /var/lib/mash \
    && chown mash:mash /var/lib/mash

USER mash

EXPOSE 8000

# serve reads MASH_HOST_APP, MASH_API_HOST, MASH_API_PORT and MASH_API_KEY.
CMD ["mash", "host", "serve"]
