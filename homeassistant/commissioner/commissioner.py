#!/usr/bin/env python3
"""One-shot, private Home Assistant Fleet Telemetry commissioning.

Only configure mode writes to Tesla, using the official signing proxy. All
printed output is constructed from aliases, booleans, and fixed categories.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid
from status_fields import STATUS_CONFIG


FLEET_HOSTS = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
}
FIELDS = {
    "Location": {"interval_seconds": 5},
    "VehicleSpeed": {"interval_seconds": 5},
    "Gear": {"interval_seconds": 10},
    "Soc": {"interval_seconds": 60},
}
VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}", re.ASCII)
ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}", re.ASCII)
HOST_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
SKIP_CATEGORIES = {"missing_key", "unsupported_hardware", "unsupported_firmware", "max_configs"}
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class Stop(Exception):
    """An already sanitized reason to stop without printing an exception body."""

    def __init__(self, category: str, *, http_status: int | None = None):
        super().__init__(category)
        self.category = category
        self.http_status = http_status


@dataclass(frozen=True)
class Options:
    mode: str
    vehicle_names: tuple[str, ...]
    hostname: str
    port: int = 8443
    ca_file: Path = Path("/ssl/tesla-fleet-stream/ca.pem")
    replace_existing: bool = False
    driver_presence: bool = False
    seat_belts: bool = False
    all_status: bool = False

    @classmethod
    def parse(cls, value: Any) -> "Options":
        if not isinstance(value, dict):
            raise Stop("invalid_options")
        mode = value.get("mode", "inspect")
        names = value.get("vehicle_names")
        hostname = value.get("hostname", "")
        port = value.get("port", 8443)
        replace = value.get("replace_existing", False)
        driver_presence = value.get("driver_presence", False)
        seat_belts = value.get("seat_belts", False)
        all_status = value.get("all_status", False)
        ca = value.get("ca_file", "/ssl/tesla-fleet-stream/ca.pem")
        if mode not in {"inspect", "configure", "get_errors"}:
            raise Stop("invalid_mode")
        if (not isinstance(names, list) or not 1 <= len(names) <= 10
                or any(not isinstance(n, str) or not ALIAS_RE.fullmatch(n)
                       or VIN_RE.fullmatch(n.upper()) for n in names)
                or len({n.casefold() for n in names}) != len(names)):
            raise Stop("invalid_vehicle_aliases")
        if not isinstance(hostname, str) or not HOST_RE.fullmatch(hostname):
            raise Stop("invalid_telemetry_hostname")
        if type(port) is not int or not 1 <= port <= 65535 or any(type(v) is not bool for v in (replace, driver_presence, seat_belts, all_status)):
            raise Stop("invalid_options")
        # Limit mount reads to the app's public-certificate area.
        if (not isinstance(ca, str) or not Path(ca).is_absolute()
                or ".." in Path(ca).parts or not ca.startswith("/ssl/")):
            raise Stop("invalid_ca_path")
        return cls(mode, tuple(names), hostname, port, Path(ca), replace, driver_presence, seat_belts, all_status)


@dataclass(frozen=True)
class Credential:
    access_token: str = field(repr=False)
    base_url: str
    scopes: frozenset[str]
    expires_at: float


def read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        raise Stop("local_json_unavailable") from None


def local_prerequisites(config_dir: Path) -> dict[str, bool]:
    """Inspect only fixed file metadata; never read or enumerate configuration."""
    try:
        read_only = bool(os.statvfs(config_dir).f_flag & os.ST_RDONLY)
    except OSError:
        read_only = False

    def readable(relative: str) -> bool:
        path = config_dir / relative
        return path.is_file() and os.access(path, os.R_OK)

    return {
        "config_directory_exists": config_dir.is_dir(),
        "config_read_only": read_only,
        "config_entries_readable": readable(".storage/core.config_entries"),
        "signing_key_readable": readable("tesla_fleet.key"),
    }


def load_credential(config_dir: Path, now: float | None = None) -> Credential:
    """Read only the active native entry; never use or rotate refresh tokens."""
    current_time = time.time() if now is None else now
    entries = read_json(config_dir / ".storage/core.config_entries")
    try:
        candidates = [e for e in entries["data"]["entries"]
                      if e.get("domain") == "tesla_fleet" and not e.get("disabled_by")]
        if len(candidates) != 1:
            raise Stop("native_fleet_entry_not_unique")
        saved = candidates[0]["data"]["token"]
        access = saved["access_token"]
        if not isinstance(access, str) or len(access.split(".")) != 3:
            raise Stop("access_token_invalid")
        encoded = access.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        expiries = [float(v) for v in (saved.get("expires_at"), claims.get("exp")) if v is not None]
        if not expiries or any(not math.isfinite(v) for v in expiries):
            raise Stop("access_token_expiry_unknown")
        expires_at = min(expiries)
        if expires_at <= current_time + 120:
            raise Stop("access_token_refresh_needed_in_home_assistant")
        # JWT metadata is used only for routing and preflight; Tesla verifies it.
        audiences = claims.get("aud", [])
        if isinstance(audiences, str):
            audiences = [audiences]
        known = [u.rstrip("/") for u in audiences if isinstance(u, str)
                 and u.rstrip("/") in FLEET_HOSTS.values()]
        if not known:
            raise Stop("unsupported_fleet_audience")
        region_url = FLEET_HOSTS.get(str(claims.get("ou_code", "")).lower())
        base_url = region_url if region_url in known else known[-1]
        scopes = claims.get("scp", claims.get("scope", saved.get("scope", [])))
        if isinstance(scopes, str):
            scopes = scopes.split()
        if not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
            raise Stop("access_token_scopes_unknown")
        return Credential(access, base_url, frozenset(scopes), expires_at)
    except Stop:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise Stop("native_fleet_token_unreadable") from None


def require_scopes(credential: Credential, mode: str) -> None:
    required = {"vehicle_device_data"}
    if mode == "configure":
        required.update({"vehicle_cmds", "vehicle_location"})
    if not required.issubset(credential.scopes):
        raise Stop("required_oauth_scope_missing")


def certificate_fingerprint(pem: str) -> str:
    if not isinstance(pem, str) or "PRIVATE KEY" in pem:
        raise Stop("invalid_public_ca")
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----\s*([A-Za-z0-9+/=\s]+?)\s*-----END CERTIFICATE-----", pem)
    remainder = re.sub(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", "", pem, flags=re.S)
    if not blocks or remainder.strip():
        raise Stop("invalid_public_ca")
    try:
        # Parse every certificate with OpenSSL, not just its base64 envelope.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=pem)
        certificates = [base64.b64decode(re.sub(r"\s", "", b), validate=True) for b in blocks]
    except (ssl.SSLError, ValueError):
        raise Stop("invalid_public_ca") from None
    return hashlib.sha256(b"".join(certificates)).hexdigest()


def desired_config(options: Options, ca_pem: str) -> dict[str, Any]:
    certificate_fingerprint(ca_pem)
    fields = dict(FIELDS)
    if options.driver_presence:
        fields["DriverSeatOccupied"] = {"interval_seconds": 5}
    if options.seat_belts:
        fields.update({"DriverSeatBelt": {"interval_seconds": 5}, "PassengerSeatBelt": {"interval_seconds": 5}})
    if options.all_status:
        # Existing GPS/gear/battery/activity intervals remain exactly unchanged.
        fields = STATUS_CONFIG | fields
    return {"hostname": options.hostname, "port": options.port,
            "ca": ca_pem, "fields": fields}


def same_config(existing: Any, desired: dict[str, Any]) -> bool:
    if not isinstance(existing, dict):
        return False
    # The signing proxy adds these two public JWT claims. Preserve all other
    # fields in the comparison, including intervals, expiration, and extras.
    left = {k: v for k, v in existing.items() if k not in {"iss", "aud"}}
    right = dict(desired)
    try:
        left["ca"] = certificate_fingerprint(left.get("ca"))
        right["ca"] = certificate_fingerprint(right["ca"])
    except Stop:
        return False
    return left == right


def http_category(status: int) -> str:
    return {400: "api_request_rejected", 401: "access_token_rejected", 402: "payment_required",
            403: "api_forbidden", 404: "api_not_found", 408: "api_timeout",
            412: "api_precondition_failed", 421: "api_region_mismatch",
            429: "api_rate_limited"}.get(status, "api_unavailable" if status >= 500 else "api_http_error")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Stop("unexpected_api_redirect")


class FleetAPI:
    """A closed allowlist of endpoints, with no refresh, wake, or motion controls."""

    def __init__(self, credential: Credential, *, proxy: bool = False,
                 context: ssl.SSLContext | None = None):
        self.credential = credential
        self.proxy = proxy
        self.base_url = "https://localhost:4443" if proxy else credential.base_url
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context))

    def request(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        direct_allowed = ((method, path) in {("GET", "/api/1/vehicles"),
                                          ("POST", "/api/1/vehicles/fleet_status")}
                          or (method == "GET" and re.fullmatch(
                              r"/api/1/vehicles/[A-HJ-NPR-Z0-9]{17}/fleet_telemetry_(?:config|errors)", path)))
        proxy_allowed = method == "POST" and path == "/api/1/vehicles/fleet_telemetry_config"
        if not (proxy_allowed if self.proxy else direct_allowed):
            raise Stop("endpoint_not_allowed")
        if self.credential.expires_at <= time.time() + 30:
            raise Stop("access_token_refresh_needed_in_home_assistant")
        data = None if body is None else json.dumps(body).encode()
        req = Request(self.base_url + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.credential.access_token,
            "Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": "HomeAssistantFleetTelemetryCommissioner/1.0",
        })
        try:
            with self.opener.open(req, timeout=30) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise Stop("api_response_too_large")
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("error"):
                raise Stop("api_response_invalid")
            return value
        except HTTPError as exc:
            # Deliberately do not read error bodies: they may contain identifiers.
            status = exc.code
            exc.close()
            raise Stop(http_category(status), http_status=status) from None
        except (URLError, TimeoutError, OSError, ssl.SSLError):
            raise Stop("api_connection_failed") from None
        except (ValueError, UnicodeError):
            raise Stop("api_response_invalid") from None


def response_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("response"), dict):
        raise Stop("api_response_shape_unknown")
    return value["response"]


def select_vehicles(value: Any, aliases: tuple[str, ...]) -> dict[str, str]:
    vehicles = value.get("response") if isinstance(value, dict) else None
    if not isinstance(vehicles, list) or len(vehicles) >= 100:
        raise Stop("vehicle_list_unavailable_or_paginated")
    selected = {}
    for alias in aliases:
        matches = [v for v in vehicles if isinstance(v, dict)
                   and isinstance(v.get("display_name"), str)
                   and v["display_name"].casefold() == alias.casefold()]
        if len(matches) != 1:
            raise Stop("vehicle_alias_missing_or_ambiguous")
        vin = matches[0].get("vin")
        if not isinstance(vin, str) or not VIN_RE.fullmatch(vin):
            raise Stop("vehicle_identifier_invalid")
        if vin in selected.values():
            raise Stop("vehicle_aliases_not_distinct")
        selected[alias] = vin
    return selected


def firmware_supported(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    match = re.match(r"^(\d{4})\.(\d{1,2})(?:\.|\s|$)", value)
    return bool(match and tuple(map(int, match.groups())) >= (2024, 26))


def pairing_evidence(vin: str, fleet: dict[str, Any], current: dict[str, Any]) -> dict[str, bool]:
    """Separate absent/invalid pairing fields from an explicit negative report."""
    result = {}
    for prefix, key in (("fleet_paired", "key_paired_vins"),
                        ("fleet_unpaired", "unpaired_vins")):
        values = fleet.get(key)
        is_list = isinstance(values, list)
        valid = is_list and all(isinstance(v, str) and VIN_RE.fullmatch(v) for v in values)
        result.update({prefix + "_present": key in fleet,
                       prefix + "_is_list": is_list,
                       prefix + "_valid": bool(valid),
                       prefix + "_contains_vehicle": bool(valid and vin in values)})
    infos = fleet.get("vehicle_info")
    info_is_object = isinstance(infos, dict)
    info_present = info_is_object and vin in infos
    result.update({
        "fleet_vehicle_info_present": "vehicle_info" in fleet,
        "fleet_vehicle_info_is_object": info_is_object,
        "fleet_vehicle_info_contains_vehicle": info_present,
        "fleet_vehicle_info_for_vehicle_is_object": info_present and isinstance(infos[vin], dict),
        "telemetry_key_paired_present": "key_paired" in current,
        "telemetry_key_paired_is_bool": type(current.get("key_paired")) is bool,
        "telemetry_key_paired_true": current.get("key_paired") is True,
        "telemetry_key_paired_false": current.get("key_paired") is False,
    })
    return result


def status_summary(alias: str, vin: str, fleet: dict[str, Any], current: dict[str, Any],
                   desired: dict[str, Any]) -> dict[str, Any]:
    evidence = pairing_evidence(vin, fleet, current)
    info = fleet["vehicle_info"][vin] if evidence["fleet_vehicle_info_for_vehicle_is_object"] else {}
    if "config" not in current:
        raise Stop("vehicle_preflight_shape_unknown")
    paired = evidence["fleet_paired_contains_vehicle"]
    explicitly_unpaired = evidence["fleet_unpaired_contains_vehicle"] or evidence["telemetry_key_paired_false"]
    ready = paired and evidence["fleet_unpaired_valid"] and not explicitly_unpaired
    telemetry_version = info.get("fleet_telemetry_version")
    compatible = firmware_supported(info.get("firmware_version"))
    if "MediaPlaybackStatus" in desired["fields"]:
        match = re.match(r"^(\d{4})\.(\d+)\.(\d+)(?:\.|\s|$)", str(info.get("firmware_version", "")))
        compatible = compatible and bool(match and tuple(map(int, match.groups())) >= (2025, 2, 6))
    telemetry_present = isinstance(telemetry_version, str) and bool(re.fullmatch(r"\d+(?:\.\d+)+", telemetry_version))
    # Opt-ins allow only missing presence/belt fields to be added. Every prior
    # endpoint, CA, interval, field and unknown option must still match exactly.
    existing = current["config"]
    old_fields = existing.get('fields', {}) if isinstance(existing, dict) else {}
    additions = set(desired['fields']) - set(old_fields) if isinstance(old_fields, dict) else set()
    allowed_config = STATUS_CONFIG | {'DriverSeatOccupied': {'interval_seconds': 5},
                                     'DriverSeatBelt': {'interval_seconds': 5},
                                     'PassengerSeatBelt': {'interval_seconds': 5}}
    previous_desired = desired | {'fields': {k: v for k, v in desired['fields'].items() if k not in additions}}
    can_extend = (bool(additions) and additions <= allowed_config.keys()
                  and all(desired['fields'][k] == allowed_config[k] for k in additions)
                  and same_config(existing, previous_desired))
    return {"vehicle": alias, "key_paired": ready, "pairing_evidence": evidence,
            "firmware_supported": compatible, "telemetry_capability_reported": telemetry_present,
            "configuration_present": current["config"] is not None,
            "configuration_matches": same_config(current["config"], desired),
            "configuration_can_add_requested_fields": can_extend,
            "configuration_synced": current.get("synced") is True,
            "configuration_limit_reached": current.get("limit_reached") is True,
            "preflight_ready": ready and compatible and telemetry_present}


def plan_changes(selected: dict[str, str], summaries: list[dict[str, Any]],
                 replace_existing: bool) -> list[str]:
    if any(not s["preflight_ready"] for s in summaries):
        raise Stop("vehicle_pairing_or_firmware_not_ready")
    if any(s["configuration_present"] and not s["configuration_matches"]
           and not s.get("configuration_can_add_requested_fields", False) for s in summaries) and not replace_existing:
        raise Stop("existing_configuration_differs")
    if any(s["configuration_limit_reached"] and not s["configuration_present"] for s in summaries):
        raise Stop("vehicle_configuration_limit_reached")
    return [selected[s["vehicle"]] for s in summaries if not s["configuration_matches"]]


def backup_before_write(data_dir: Path, selected: dict[str, str], current: dict[str, Any],
                        desired: dict[str, Any]) -> Path:
    directory = data_dir / "commissioner" / "backups"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory.parent, 0o700)
    os.chmod(directory, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{stamp}-{uuid.uuid4().hex}.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"schema_version": 1, "created_at": stamp,
                   "vehicles": [{"alias": a, "vin": v, "previous": current[a]}
                                for a, v in selected.items()], "requested_config": desired}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return target


@contextmanager
def signing_proxy(config_dir: Path, credential: Credential):
    binary = shutil.which("tesla-http-proxy")
    openssl = shutil.which("openssl")
    key = config_dir / "tesla_fleet.key"
    if not binary or not openssl or not key.is_file():
        raise Stop("signing_proxy_prerequisite_missing")
    # A TLS connection must validate this new, independent localhost certificate.
    # The existing vehicle signing key is passed by path and never copied/read here.
    with tempfile.TemporaryDirectory(prefix="fleet-commissioner-") as scratch:
        cert = Path(scratch) / "localhost-cert.pem"
        tls_key = Path(scratch) / "localhost-key.pem"
        generated = subprocess.run([
            openssl, "req", "-x509", "-nodes", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:secp384r1", "-sha256", "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-addext", "extendedKeyUsage=serverAuth", "-keyout", str(tls_key), "-out", str(cert),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=15)
        if generated.returncode:
            raise Stop("proxy_tls_generation_failed")
        os.chmod(tls_key, 0o600)
        context = ssl.create_default_context(cafile=str(cert))
        # Avoid inheriting Supervisor tokens or TESLA_* credential/debug settings.
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
        child = subprocess.Popen([
            binary, "-host", "localhost", "-port", "4443", "-tls-key", str(tls_key),
            "-cert", str(cert), "-key-file", str(key), "-disable-session-cache",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise Stop("signing_proxy_start_failed")
                try:
                    with socket.create_connection(("localhost", 4443), timeout=0.5) as connection:
                        with context.wrap_socket(connection, server_hostname="localhost"):
                            break
                except (OSError, ssl.SSLError):
                    time.sleep(0.1)
            else:
                raise Stop("signing_proxy_start_timeout")
            yield FleetAPI(credential, proxy=True, context=context)
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def error_categories(value: Any) -> dict[str, int]:
    entries = response_object(value).get("fleet_telemetry_errors")
    if not isinstance(entries, list):
        raise Stop("telemetry_error_shape_unknown")
    counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            category = "other"
        else:
            # Inspect locally; emit only our fixed vocabulary, never remote strings.
            message = " ".join(str(entry.get(k, "")) for k in ("error", "error_name")).lower()
            if any(p in message for p in ("certificate", "x509", "tls", "ssl")):
                category = "tls_or_certificate"
            elif any(p in message for p in ("no such host", "dns", "name resolution")):
                category = "dns_resolution"
            elif "refused" in message:
                category = "connection_refused"
            elif any(p in message for p in ("timeout", "timed out", "deadline")):
                category = "connection_timeout"
            elif any(p in message for p in ("rate limit", "429", "throttl")):
                category = "rate_limited"
            elif any(p in message for p in ("websocket", "connection", "network")):
                category = "connection_other"
            else:
                category = "other"
        counts[category] = counts.get(category, 0) + 1
    return counts


def run(options: Options, credential: Credential, api: FleetAPI, *, config_dir: Path,
        data_dir: Path, ca_pem: str, proxy_factory: Callable = signing_proxy) -> dict[str, Any]:
    require_scopes(credential, options.mode)
    selected = select_vehicles(api.request("GET", "/api/1/vehicles"), options.vehicle_names)
    if options.mode == "get_errors":
        return {"mode": "get_errors", "vehicles": [
            {"vehicle": alias, "error_categories": error_categories(api.request(
                "GET", f"/api/1/vehicles/{vin}/fleet_telemetry_errors"))}
            for alias, vin in selected.items()]}
    desired = desired_config(options, ca_pem)
    fleet = response_object(api.request("POST", "/api/1/vehicles/fleet_status", {"vins": list(selected.values())}))
    current = {alias: response_object(api.request("GET", f"/api/1/vehicles/{vin}/fleet_telemetry_config"))
               for alias, vin in selected.items()}
    summaries = [status_summary(a, v, fleet, current[a], desired) for a, v in selected.items()]
    result = {"mode": options.mode, "vehicles": summaries, "changed": False}
    if options.mode == "inspect":
        return result
    try:
        changes = plan_changes(selected, summaries, options.replace_existing)
    except Stop as exc:
        result["stop_reason"] = exc.category
        return result
    if not changes:
        result["already_configured"] = True
        return result
    backup_before_write(data_dir, selected, current, desired)
    result["backup_created"] = True
    with proxy_factory(config_dir, credential) as proxy:
        # The only Tesla mutation implemented by this program. No implicit retry.
        response = response_object(proxy.request("POST", "/api/1/vehicles/fleet_telemetry_config",
                                                 {"vins": changes, "config": desired}))
    skipped = response.get("skipped_vehicles", {})
    if not isinstance(skipped, dict) or any(not isinstance(v, list) for v in skipped.values()):
        raise Stop("configuration_write_outcome_unknown")
    accepted_count = response.get("updated_vehicles")
    if type(accepted_count) is not int or not 0 <= accepted_count <= len(changes):
        raise Stop("configuration_write_outcome_unknown")
    result["changed"] = accepted_count > 0
    result["request_accepted_for_all"] = accepted_count == len(changes) and not any(skipped.values())
    for summary in summaries:
        alias = summary["vehicle"]
        vin = selected[alias]
        categories = [category if category in SKIP_CATEGORIES else "unknown_rejection"
                      for category, vins in skipped.items() if vin in vins]
        summary["configuration_requested"] = vin in changes
        summary["rejection_categories"] = sorted(set(categories))
        after = response_object(api.request("GET", f"/api/1/vehicles/{vin}/fleet_telemetry_config"))
        summary["configuration_matches"] = same_config(after.get("config"), desired)
        summary["configuration_synced"] = after.get("synced") is True
    result["all_readbacks_match"] = all(s["configuration_matches"] for s in summaries)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--options", type=Path, default=Path("/data/options.json"))
    args = parser.parse_args()
    os.umask(0o077)

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        options = Options.parse(read_json(args.options))
        config_dir = Path("/homeassistant_config")
        prerequisites = local_prerequisites(config_dir)
        print(json.dumps({"status": "local_prerequisites", **prerequisites}), flush=True)
        if not prerequisites["config_directory_exists"]:
            raise Stop("homeassistant_config_mount_unavailable")
        if not prerequisites["config_read_only"]:
            raise Stop("homeassistant_config_mount_not_read_only")
        credential = load_credential(config_dir)
        if options.mode == "get_errors":
            ca_pem = ""
        else:
            try:
                ca_pem = options.ca_file.read_text(encoding="ascii")
            except (OSError, UnicodeError):
                raise Stop("public_ca_unavailable") from None
        result = run(options, credential, FleetAPI(credential), config_dir=config_dir,
                     data_dir=Path("/data"), ca_pem=ca_pem)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 1 if "stop_reason" in result or result.get("all_readbacks_match") is False else 0
    except Stop as exc:
        result = {"status": "stopped", "category": exc.category}
        if exc.http_status is not None:
            result["http_status"] = exc.http_status
        print(json.dumps(result), flush=True)
        return 1
    except KeyboardInterrupt:
        print('{"status":"stopped","category":"interrupted"}', flush=True)
        return 130
    except Exception:
        # Tracebacks can interpolate request URLs or stored values. Never emit one.
        print('{"status":"stopped","category":"internal_error"}', flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
