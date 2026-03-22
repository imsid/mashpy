FROM mashpy/mash-host-base:latest

WORKDIR /app

COPY pilot ./pilot

ENV MASH_HOST_APP=pilot.spec:build_host
ENV MASH_DATA_DIR=/var/lib/mash

EXPOSE 8000
