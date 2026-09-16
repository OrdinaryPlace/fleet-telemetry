FROM golang:1.26-bookworm AS receiver
RUN apt-get update && apt-get install -y --no-install-recommends libzmq3-dev pkg-config && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY receiver/go.mod receiver/go.sum ./
RUN go mod download
COPY receiver/ ./
RUN CGO_ENABLED=1 CGO_LDFLAGS=-lstdc++ go build -trimpath -ldflags='-s -w' -o /out/fleet-telemetry ./cmd

FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libzmq5 ca-certificates openssl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir paho-mqtt==2.1.0 cryptography==50.0.1
COPY --from=receiver /out/fleet-telemetry /usr/local/bin/fleet-telemetry
COPY bridge/ /opt/bridge/
COPY runtime.py archive_web.py /opt/
COPY SOURCE_REVISION /opt/SOURCE_REVISION
ARG BUILD_VERSION=0.4.0
ARG BUILD_ARCH=amd64
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="addon" io.hass.arch="${BUILD_ARCH}"
ENV PYTHONUNBUFFERED=1 SUPPRESS_TLS_HANDSHAKE_ERROR_LOGGING=true
CMD ["python3", "/opt/runtime.py"]
