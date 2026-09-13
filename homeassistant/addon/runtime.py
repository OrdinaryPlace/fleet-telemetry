"""Run the receiver and bridge without exposing credentials or telemetry in logs."""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

TLS_RECHECK_SECONDS = 24 * 60 * 60


def private_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            os.unlink(temporary)


def ensure_tls(directory: Path, hostname: str) -> None:
    """Preserve the CA; issue/renew only this receiver's server certificate."""
    if (not isinstance(hostname, str) or len(hostname) > 253 or "." not in hostname
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                       for label in hostname.split("."))):
        raise ValueError("Invalid telemetry hostname")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("A DNS hostname is required for telemetry")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    now = dt.datetime.now(dt.UTC)
    ca_path, ca_key_path = directory / "ca.pem", directory / "ca.key"
    server_path, server_key_path = directory / "server.pem", directory / "server.key"
    if ca_path.exists() != ca_key_path.exists():
        raise ValueError("Incomplete existing CA; restore matching files")
    if not ca_path.exists():
        ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Tesla Fleet Stream Local CA")])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
              .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - dt.timedelta(minutes=5))
              .not_valid_after(now + dt.timedelta(days=3650))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, None, None), critical=True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
              .sign(ca_key, hashes.SHA256()))
        private_write(ca_key_path, ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        private_write(ca_path, ca.public_bytes(serialization.Encoding.PEM))
    ca = x509.load_pem_x509_certificate(ca_path.read_bytes())
    ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    if ca.public_key().public_numbers() != ca_key.public_key().public_numbers():
        raise ValueError("Existing CA certificate/key mismatch")
    try:
        ca.verify_directly_issued_by(ca)
    except (ValueError, InvalidSignature):
        raise ValueError("Existing CA certificate signature is invalid") from None
    if not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("Existing certificate is not a CA")
    if not ca.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
        raise ValueError("Existing CA cannot sign certificates")
    if ca.not_valid_before_utc > now or ca.not_valid_after_utc <= now + dt.timedelta(days=30):
        raise ValueError("Existing CA validity requires explicit migration")
    ca_path.chmod(0o600)
    ca_key_path.chmod(0o600)
    if server_path.exists() != server_key_path.exists():
        raise ValueError("Incomplete existing server certificate; restore matching files")
    if server_path.exists():
        cert = x509.load_pem_x509_certificate(server_path.read_bytes())
        key = serialization.load_pem_private_key(server_key_path.read_bytes(), password=None)
        if cert.public_key().public_numbers() != key.public_key().public_numbers():
            raise ValueError("Existing server certificate/key mismatch")
        try:
            cert.verify_directly_issued_by(ca)
        except (ValueError, InvalidSignature):
            raise ValueError("Existing server certificate does not match the preserved CA") from None
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if hostname not in names.get_values_for_type(x509.DNSName):
            raise ValueError("Existing certificate hostname differs; explicit migration required")
        if (cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
                or ExtendedKeyUsageOID.SERVER_AUTH not in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
                or not cert.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature):
            raise ValueError("Existing certificate is not valid for this TLS server")
        server_path.chmod(0o600)
        server_key_path.chmod(0o600)
        try:
            identifiers_match = (
                cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
                == x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key())
                and cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
                == x509.SubjectKeyIdentifier.from_public_key(key.public_key())
            )
        except x509.ExtensionNotFound:
            # Migrate older leaves without changing the established CA or keys.
            identifiers_match = False
        if (identifiers_match and cert.not_valid_before_utc <= now
                and cert.not_valid_after_utc > now + dt.timedelta(days=30)):
            return
    else:
        key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(min(now + dt.timedelta(days=365), ca.not_valid_after_utc))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, None, None), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    if not server_key_path.exists():
        private_write(server_key_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    private_write(server_path, cert.public_bytes(serialization.Encoding.PEM))


def mqtt_service() -> dict:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise ValueError("Supervisor service credentials unavailable")
    request = urllib.request.Request("http://supervisor/services/mqtt", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.load(response)
    if body.get("result") != "ok":
        raise ValueError("MQTT service unavailable")
    data = body["data"]
    if not data.get("username") or not data.get("password"):
        raise ValueError("Authenticated MQTT service required")
    if data.get("ssl"):
        raise ValueError("TLS-only MQTT service is not supported by this receiver configuration")
    return data


def configurations(options: dict, mqtt: dict) -> tuple[dict, dict]:
    if mqtt.get("ssl"):
        raise ValueError("TLS-only MQTT service is not supported by this receiver configuration")
    vehicles = options.get("vehicles", [])
    if not vehicles:
        raise ValueError("Configure the intended vehicles before starting")
    receiver = {
        "host": "0.0.0.0", "port": 8443, "log_level": "error", "json_log_enable": True,
        "namespace": "tesla_live", "transmit_decoded_records": True,
        "reliable_ack_sources": {"V": "mqtt"},
        "monitoring": {"prometheus_metrics_port": 9090},
        "records": {"V": ["mqtt"], "connectivity": ["mqtt"]},
        "mqtt": {"broker": f'{mqtt["host"]}:{mqtt["port"]}', "username": mqtt["username"],
                 "password": mqtt["password"], "client_id": "tesla-fleet-receiver",
                 "topic_base": "tesla_raw", "qos": 1, "retained": False,
                 "publish_vehicle_records": True,
                 "location_archive": {"directory": "/share/tesla-fleet-location-history",
                                      "vehicles": {v["vin"]: v["slug"] for v in vehicles}} if options.get("location_history", False) else None},
        "tls": {"server_cert": "/ssl/tesla-fleet-stream/server.pem", "server_key": "/ssl/tesla-fleet-stream/server.key"},
    }
    bridge = {
        "mqtt": {"host": mqtt["host"], "port": mqtt["port"], "username": mqtt["username"],
                 "password": mqtt["password"], "client_id": "tesla-fleet-ha-bridge",
                 "topic_base": "tesla_raw", "state_prefix": "tesla_live", "discovery_prefix": "homeassistant"},
        "vehicles": vehicles,
        "freshness_seconds": options.get("freshness_seconds", 90),
        "max_transport_delay_seconds": options.get("max_transport_delay_seconds", 30),
        "future_tolerance_seconds": 5, "tick_seconds": 5,
        "state_file": "/data/bridge-state.json",
        "location_history_directory": "/share/tesla-fleet-location-history" if options.get("location_history", False) else None,
    }
    return receiver, bridge


def receiver_output(process: subprocess.Popen, label: str = "Receiver") -> None:
    """Child diagnostics may contain private data; emit only fixed categories."""
    last = 0.0
    for line in process.stdout:
        if time.monotonic() - last >= 30:
            lower = line.lower()
            category = next((value for value in ("mqtt", "tls", "certificate", "config", "error") if value in lower), "diagnostic")
            print(f"{label} {category} event; payload details suppressed", flush=True)
            last = time.monotonic()


def maintain_tls(directory: Path, hostname: str, next_check: float, monotonic_now: float) -> tuple[float, bool]:
    """Check daily, renewing with the preserved keys before leaf expiry."""
    if monotonic_now < next_check:
        return next_check, False
    before = (directory / "server.pem").read_bytes()
    ensure_tls(directory, hostname)
    changed = before != (directory / "server.pem").read_bytes()
    return monotonic_now + TLS_RECHECK_SECONDS, changed


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def start_receiver(child_env: dict) -> subprocess.Popen:
    process = subprocess.Popen(
        ["/usr/local/bin/fleet-telemetry", "-config", "/run/receiver.json"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", env=child_env,
    )
    threading.Thread(target=receiver_output, args=(process,), daemon=True).start()
    return process


def source_revision(path: Path = Path("/opt/SOURCE_REVISION")) -> str:
    """Expose only a validated build commit, never arbitrary file contents."""
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return "unavailable"
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else "unavailable"


def main() -> int:
    os.umask(0o077)
    print(f"Tesla Fleet Stream source revision: {source_revision()}", flush=True)
    with open("/data/options.json", encoding="utf-8") as stream:
        options = json.load(stream)
    tls_directory = Path("/ssl/tesla-fleet-stream")
    ensure_tls(tls_directory, options["hostname"])
    receiver, bridge = configurations(options, mqtt_service())
    private_write(Path("/run/receiver.json"), json.dumps(receiver).encode())
    private_write(Path("/run/bridge.json"), json.dumps(bridge).encode())
    child_env = {key: value for key, value in os.environ.items() if key not in {"SUPERVISOR_TOKEN", "HASSIO_TOKEN"}}
    processes = [
        subprocess.Popen([sys.executable, "/opt/bridge/bridge.py", "--config", "/run/bridge.json"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", env=child_env),
    ]
    threading.Thread(target=receiver_output, args=(processes[0], "Bridge"), daemon=True).start()
    stopped = threading.Event()
    try:
        processes.append(start_receiver(child_env))
        processes.append(subprocess.Popen([sys.executable, "/opt/archive_web.py"], env=child_env))
        next_tls_check = time.monotonic() + TLS_RECHECK_SECONDS
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stopped.set())
        print("Tesla Fleet Stream started; vehicle telemetry stays on the private MQTT network", flush=True)
        while not stopped.wait(1):
            if any(process.poll() is not None for process in processes):
                print("A stream process exited; stopping for Supervisor recovery", flush=True)
                break
            try:
                next_tls_check, renewed = maintain_tls(tls_directory, options["hostname"], next_tls_check, time.monotonic())
            except Exception:
                print("TLS maintenance failed; stopping for configuration recovery", flush=True)
                break
            if renewed:
                stop_process(processes[1])
                processes[1] = start_receiver(child_env)
                print("Receiver TLS certificate renewed; receiver restarted with preserved keys", flush=True)
    finally:
        for process in processes:
            stop_process(process)
    return 0 if stopped.is_set() else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Stream startup failed ({type(error).__name__}); no private details logged", file=sys.stderr)
        raise SystemExit(1) from None
