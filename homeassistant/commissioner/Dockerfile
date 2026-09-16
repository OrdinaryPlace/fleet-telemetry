FROM golang:1.26-bookworm AS proxy
# Build inside the pinned module: versioned go install rejects its replace directive.
RUN go mod download github.com/teslamotors/vehicle-command@v0.4.1 \
    && cd "$(go env GOMODCACHE)/github.com/teslamotors/vehicle-command@v0.4.1" \
    && mkdir -p /out \
    && go build -trimpath -o /out/tesla-http-proxy ./cmd/tesla-http-proxy

FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=proxy /out/tesla-http-proxy /usr/local/bin/tesla-http-proxy
COPY commissioner.py /opt/commissioner.py
COPY entrypoint.py ha_support.py status_fields.py /opt/
COPY SOURCE_REVISION /opt/SOURCE_REVISION
ARG BUILD_VERSION=0.3.0
ARG BUILD_ARCH=amd64
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="addon" io.hass.arch="${BUILD_ARCH}"
ENV PYTHONUNBUFFERED=1
CMD ["python3", "/opt/entrypoint.py"]
