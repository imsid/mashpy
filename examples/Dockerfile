FROM mashpy/mash-host-base:latest

WORKDIR /app

COPY examples ./examples

ENV MASH_HOST_APP=examples.example_app:build_host
ENV MASH_DATA_DIR=/var/lib/mash

EXPOSE 8000
