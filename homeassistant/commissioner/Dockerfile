FROM golang:1.26-bookworm AS proxy
RUN GOBIN=/out go install github.com/teslamotors/vehicle-command/cmd/tesla-http-proxy@v0.4.1

FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=proxy /out/tesla-http-proxy /usr/local/bin/tesla-http-proxy
COPY commissioner.py /opt/commissioner.py
COPY entrypoint.py ha_support.py /opt/
ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH=amd64
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="addon" io.hass.arch="${BUILD_ARCH}"
ENV PYTHONUNBUFFERED=1
CMD ["python3", "/opt/entrypoint.py"]
