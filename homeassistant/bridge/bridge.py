#!/usr/bin/env python3
"""Privacy-conscious Fleet Telemetry envelope to Home Assistant MQTT bridge.

Only allowlisted vehicles and explicitly supported fields are published. No raw
records, vehicle identifiers, coordinates, or credentials are logged or stored.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import tempfile
import time
from typing import Any, Callable

LOGGER = logging.getLogger("fleet_bridge")
VERSION = "1.0.0"
MAX_PAYLOAD_BYTES = 256 * 1024
SUPPORTED_FIELDS = {"Location": "location", "VehicleSpeed": "speed", "BatteryLevel": "battery", "Soc": "usable_battery", "Gear": "gear"}
NUMBER_KEYS = {"double_value", "float_value", "int_value", "long_value", "string_value"}
SLUG = re.compile(r"^[a-z0-9][a-z0-9_]{0,47}$")
VIN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
RFC3339 = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$")
NANOSECOND = 1_000_000_000


class PersistenceError(RuntimeError):
    """A durable replay fence could not be saved."""


def timestamp(value: Any) -> int:
    match = RFC3339.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("invalid timestamp")
    seconds = int(datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00")).timestamp())
    return seconds * NANOSECOND + int((match[2] or "0").ljust(9, "0"))


def iso_time(value: int) -> str:
    seconds, fraction = divmod(value, NANOSECOND)
    base = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return base + (f".{fraction:09d}".rstrip("0") if fraction else "") + "Z"


def number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("invalid number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("invalid number")
    return result


def decode_value(key: str, value: Any) -> Any:
    """Return a validated value, or None for an explicit invalid measurement."""
    if not isinstance(value, dict) or len(value) != 1:
        raise ValueError("invalid oneof")
    if value.get("invalid") is True:
        return None
    if key == "Location":
        location = value.get("location_value")
        if not isinstance(location, dict):
            raise ValueError("invalid location")
        latitude, longitude = number(location.get("latitude")), number(location.get("longitude"))
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("invalid coordinates")
        return {"latitude": latitude, "longitude": longitude}
    if key == "Gear":
        gear = value.get("shift_state_value", value.get("string_value"))
        gears = {"ShiftStateP": "P", "ShiftStateR": "R", "ShiftStateN": "N", "ShiftStateD": "D", "P": "P", "R": "R", "N": "N", "D": "D"}
        if gear in ("ShiftStateUnknown", "ShiftStateInvalid", "ShiftStateSNA"):
            return None
        if not isinstance(gear, str) or gear not in gears:
            raise ValueError("invalid gear")
        return gears[gear]
    value_key = next(iter(value))
    if value_key not in NUMBER_KEYS:
        raise ValueError("invalid numeric oneof")
    result = number(value[value_key])
    if key in ("BatteryLevel", "Soc") and not 0 <= result <= 100:
        raise ValueError("invalid battery")
    if key == "VehicleSpeed" and not 0 <= result <= 400:
        raise ValueError("invalid speed")
    return result


def validate_config(config: Any) -> dict:
    """Errors deliberately exclude runtime values, which may contain secrets."""
    if not isinstance(config, dict) or not isinstance(config.get("mqtt"), dict):
        raise ValueError("mqtt configuration required")
    mqtt = config["mqtt"]
    if not isinstance(mqtt.get("host"), str) or not mqtt["host"]:
        raise ValueError("mqtt host required")
    port = mqtt.get("port", 1883)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid mqtt port")
    for name, default in (("topic_base", "tesla"), ("state_prefix", "tesla_live"), ("discovery_prefix", "homeassistant")):
        prefix = mqtt.setdefault(name, default)
        if not isinstance(prefix, str) or not prefix or any(c in prefix for c in ("+", "#", "\x00")) or prefix.startswith("/") or prefix.endswith("/"):
            raise ValueError("invalid mqtt prefix")
    if mqtt["topic_base"] == mqtt["state_prefix"]:
        raise ValueError("input and output prefixes must differ")
    vehicles = config.get("vehicles")
    if not isinstance(vehicles, list) or not vehicles:
        raise ValueError("vehicle allowlist required")
    vins, slugs = set(), set()
    for vehicle in vehicles:
        if not isinstance(vehicle, dict) or not isinstance(vehicle.get("vin"), str) or not VIN.fullmatch(vehicle["vin"]):
            raise ValueError("invalid vehicle allowlist")
        if not isinstance(vehicle.get("slug"), str) or not SLUG.fullmatch(vehicle["slug"]):
            raise ValueError("invalid vehicle slug")
        if not isinstance(vehicle.get("name"), str) or not 1 <= len(vehicle["name"]) <= 80:
            raise ValueError("invalid vehicle name")
        if vehicle["vin"] in vins or vehicle["slug"] in slugs:
            raise ValueError("duplicate vehicle mapping")
        vins.add(vehicle["vin"])
        slugs.add(vehicle["slug"])
    for name, default, low, high in (("freshness_seconds", 90, 10, 3600), ("max_transport_delay_seconds", 30, 1, 300), ("future_tolerance_seconds", 5, 0, 30), ("tick_seconds", 5, 1, 30)):
        config.setdefault(name, default)
        if isinstance(config[name], bool) or not isinstance(config[name], (float, int)) or not low <= config[name] <= high:
            raise ValueError("invalid timing configuration")
    if config["max_transport_delay_seconds"] > config["freshness_seconds"]:
        raise ValueError("transport delay exceeds freshness")
    return config


@dataclass
class Measurement:
    observed: int
    received: float
    value: Any


@dataclass
class Vehicle:
    slug: str
    name: str
    measurements: dict[str, Measurement] = field(default_factory=dict)
    last_update: int | None = None
    last_received: float | None = None
    connection_id: str | None = None
    connection_time: int = 0
    connected: bool | None = None


class Bridge:
    def __init__(self, config: dict, publish: Callable, wall_clock=time.time, monotonic=time.monotonic):
        self.config = validate_config(config)
        self.publish = publish
        self.wall_clock = wall_clock
        self.monotonic = monotonic
        self.vehicles = {item["vin"]: Vehicle(item["slug"], item["name"]) for item in config["vehicles"]}
        self.input_prefix = config["mqtt"]["topic_base"]
        self.output_prefix = config["mqtt"]["state_prefix"]
        self.discovery_prefix = config["mqtt"]["discovery_prefix"]
        self.birth_topic = f"{self.discovery_prefix}/status"
        self.status_topic = f"{self.output_prefix}/bridge/availability"
        self.availability: dict[str, str] = {}
        self.counters = Counter()
        self.load_fences()

    def load_fences(self):
        state_path = self.config.get("state_file")
        if not state_path or not Path(state_path).exists():
            return
        state = json.loads(Path(state_path).read_text())
        if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("vehicles"), dict):
            raise ValueError("invalid timestamp state")
        for vehicle in self.vehicles.values():
            saved = state["vehicles"].get(vehicle.slug, {})
            if not isinstance(saved, dict) or not isinstance(saved.get("fields", {}), dict):
                raise ValueError("invalid timestamp state")
            for suffix, observed in saved.get("fields", {}).items():
                if suffix not in SUPPORTED_FIELDS.values():
                    continue
                vehicle.measurements[suffix] = Measurement(timestamp(observed), self.monotonic(), None)
            if saved.get("last_update") is not None:
                vehicle.last_update = timestamp(saved["last_update"])
            if saved.get("connection_time") is not None:
                vehicle.connection_time = timestamp(saved["connection_time"])

    def persist_fences(self):
        state_path = self.config.get("state_file")
        if not state_path:
            return
        state = {"version": 1, "vehicles": {vehicle.slug: {
            "fields": {suffix: iso_time(measurement.observed) for suffix, measurement in vehicle.measurements.items()},
            "last_update": iso_time(vehicle.last_update) if vehicle.last_update is not None else None,
            "connection_time": iso_time(vehicle.connection_time) if vehicle.connection_time else None,
        } for vehicle in self.vehicles.values()}}
        destination = Path(state_path)
        temporary = None
        try:
            # The runtime directory must already exist; never create an arbitrary path.
            descriptor, temporary = tempfile.mkstemp(prefix=".bridge-state-", dir=destination.parent)
            with os.fdopen(descriptor, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(state, handle, separators=(",", ":"), allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            raise PersistenceError("timestamp state unavailable") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def send(self, topic: str, payload: Any, retain=False, qos=0):
        if not isinstance(payload, str):
            payload = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        result = self.publish(topic, payload, qos=qos, retain=retain)
        if result is not None and getattr(result, "rc", 0) != 0:
            raise ConnectionError("MQTT publication unavailable")

    def base(self, vehicle: Vehicle) -> str:
        return f"{self.output_prefix}/{vehicle.slug}"

    def available(self, topic: str, value: bool, force=False):
        payload = "online" if value else "offline"
        if force or self.availability.get(topic) != payload:
            self.send(topic, payload, retain=True)
            self.availability[topic] = payload

    def fresh(self, observed: int | None, received: float | None) -> bool:
        if observed is None or received is None:
            return False
        age = self.wall_clock() - observed / NANOSECOND
        return (-self.config["future_tolerance_seconds"] <= age <= self.config["freshness_seconds"] and 0 <= self.monotonic() - received <= self.config["freshness_seconds"])

    def discoveries(self):
        for vehicle in self.vehicles.values():
            base = self.base(vehicle)
            device = {"identifiers": [f"tesla_live_{vehicle.slug}"], "name": f"{vehicle.name} Live", "manufacturer": "Tesla", "model": "Fleet Telemetry", "sw_version": VERSION}
            specs = [
                ("device_tracker", "location", {"json_attributes_topic": f"{base}/location/state", "source_type": "gps", "icon": "mdi:car-connected"}),
                ("sensor", "speed", {"state_topic": f"{base}/speed/state", "device_class": "speed", "unit_of_measurement": "mph", "state_class": "measurement"}),
                ("sensor", "battery", {"state_topic": f"{base}/battery/state", "device_class": "battery", "unit_of_measurement": "%", "state_class": "measurement", "enabled_by_default": False}),
                ("sensor", "usable_battery", {"state_topic": f"{base}/usable_battery/state", "device_class": "battery", "unit_of_measurement": "%", "state_class": "measurement", "enabled_by_default": False}),
                ("sensor", "gear", {"state_topic": f"{base}/gear/state", "icon": "mdi:car-shift-pattern", "enabled_by_default": False}),
                ("sensor", "last_update", {"state_topic": f"{base}/last_update/state", "device_class": "timestamp", "entity_category": "diagnostic"}),
                ("binary_sensor", "telemetry_fresh", {"state_topic": f"{base}/telemetry_fresh/state", "device_class": "connectivity", "entity_category": "diagnostic"}),
                ("binary_sensor", "connected", {"state_topic": f"{base}/connected/state", "device_class": "connectivity", "entity_category": "diagnostic"}),
            ]
            for component, suffix, specific in specs:
                availability = [{"topic": self.status_topic}]
                if suffix in SUPPORTED_FIELDS.values() or suffix == "connected":
                    availability.append({"topic": f"{base}/{suffix}/availability"})
                discovery = {
                    "name": suffix.replace("_", " ").capitalize(),
                    "unique_id": f"tesla_live_{vehicle.slug}_{suffix}",
                    "default_entity_id": f"{component}.{vehicle.slug}_live_{suffix}",
                    "device": device,
                    "availability": availability,
                    "availability_mode": "all",
                    "origin": {"name": "Fleet Telemetry bridge", "sw_version": VERSION},
                    **specific,
                }
                if suffix in ("speed", "battery", "usable_battery", "gear"):
                    discovery["json_attributes_topic"] = f"{base}/{suffix}/attributes"
                self.send(f"{self.discovery_prefix}/{component}/tesla_live_{vehicle.slug}/{suffix}/config", discovery, retain=True, qos=1)

    def resync(self):
        """Do not restore a GPS fix across broker/HA restarts as a new arrival."""
        self.available(self.status_topic, False, force=True)
        for vehicle in self.vehicles.values():
            for measurement in vehicle.measurements.values():
                measurement.value = None  # Preserve timestamp fences, never replay GPS.
            vehicle.last_received = None
            vehicle.connected = None
            for suffix in (*SUPPORTED_FIELDS.values(), "connected"):
                self.available(f"{self.base(vehicle)}/{suffix}/availability", False, force=True)
            self.send(f"{self.base(vehicle)}/telemetry_fresh/state", "OFF", retain=True)
        self.discoveries()
        for vehicle in self.vehicles.values():
            if vehicle.last_update is not None:
                self.send(f"{self.base(vehicle)}/last_update/state", iso_time(vehicle.last_update), retain=True)
        self.available(self.status_topic, True, force=True)

    def receive(self, topic: str, payload: bytes, retained=False):
        # Reject retained data even if a previous receiver used different settings.
        if topic == self.birth_topic:
            if payload == b"online":
                self.resync()
            return
        if retained:
            self.counters["retained_rejected"] += 1
            return
        prefix = self.input_prefix + "/"
        if not topic.startswith(prefix):
            return
        parts = topic[len(prefix):].split("/")
        if len(parts) != 2 or parts[0] not in self.vehicles or parts[1] not in ("records", "connectivity"):
            self.counters["unrecognized_topic"] += 1
            return
        if len(payload) > MAX_PAYLOAD_BYTES:
            self.counters["oversized_rejected"] += 1
            return
        try:
            record = json.loads(payload)
            if not isinstance(record, dict):
                raise ValueError("invalid record")
            if parts[1] == "connectivity":
                self.connectivity(self.vehicles[parts[0]], record)
            else:
                if record.get("vin") != parts[0]:
                    self.counters["identity_rejected"] += 1
                    return
                self.record(self.vehicles[parts[0]], record)
        except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
            self.counters["malformed_rejected"] += 1

    def acceptable_time(self, observed: int) -> bool:
        delay = self.wall_clock() - observed / NANOSECOND
        return -self.config["future_tolerance_seconds"] <= delay <= self.config["max_transport_delay_seconds"]

    def record(self, vehicle: Vehicle, record: dict):
        if record.get("is_resend") is not False:
            self.counters["replay_rejected"] += 1
            return
        observed = timestamp(record.get("created_at"))
        if not self.acceptable_time(observed):
            self.counters["delayed_or_future_rejected"] += 1
            return
        data = record.get("data")
        if not isinstance(data, list) or len(data) > 1000:
            raise ValueError("invalid data")
        seen = set()
        decoded = []
        for datum in data:
            if not isinstance(datum, dict) or not isinstance(datum.get("key"), str):
                raise ValueError("invalid datum")
            key = datum["key"]
            if key not in SUPPORTED_FIELDS:
                continue
            if key in seen:
                raise ValueError("duplicate field")
            seen.add(key)
            # A bad value invalidates this field at its authentic new timestamp.
            try:
                value = decode_value(key, datum.get("value"))
            except (ValueError, TypeError, OverflowError):
                value = None
                self.counters["invalid_field"] += 1
            decoded.append((SUPPORTED_FIELDS[key], value))
        changes = []
        for suffix, value in decoded:
            previous = vehicle.measurements.get(suffix)
            if previous and observed <= previous.observed:
                self.counters["out_of_order_rejected"] += 1
                continue
            measurement = Measurement(observed, self.monotonic(), value)
            vehicle.measurements[suffix] = measurement
            changes.append((suffix, value))
        if changes:
            if vehicle.last_update is None or observed >= vehicle.last_update:
                vehicle.last_update, vehicle.last_received = observed, self.monotonic()
            # Commit source-time fences before publishing any state externally.
            self.persist_fences()
        for suffix, value in changes:
            base = f"{self.base(vehicle)}/{suffix}"
            if value is not None:
                if suffix == "location":
                    self.send(f"{base}/state", {**value, "observed_at": iso_time(observed)})
                else:
                    self.send(f"{base}/state", str(value))
                    self.send(f"{base}/attributes", {"observed_at": iso_time(observed)})
            self.available(f"{base}/availability", value is not None)
        if changes:
            self.counters["accepted_records"] += 1
            self.send(f"{self.base(vehicle)}/last_update/state", iso_time(vehicle.last_update), retain=True)
            self.send(f"{self.base(vehicle)}/telemetry_fresh/state", "ON", retain=True)

    def connectivity(self, vehicle: Vehicle, record: dict):
        observed = timestamp(record.get("CreatedAt"))
        status, connection_id = record.get("Status"), record.get("ConnectionId")
        if status not in ("CONNECTED", "DISCONNECTED") or not isinstance(connection_id, str) or not connection_id:
            raise ValueError("invalid connection")
        if not self.acceptable_time(observed) or observed < vehicle.connection_time:
            self.counters["old_connection_rejected"] += 1
            return
        if status == "DISCONNECTED" and connection_id != vehicle.connection_id:
            self.counters["unmatched_disconnect_rejected"] += 1
            return
        if observed == vehicle.connection_time and (connection_id != vehicle.connection_id or vehicle.connected is False):
            self.counters["old_connection_rejected"] += 1
            return
        vehicle.connection_time, vehicle.connection_id = observed, connection_id
        vehicle.connected = status == "CONNECTED"
        if not vehicle.connected:
            # Disconnect proves the live path broke; do not infer home or away.
            for suffix in SUPPORTED_FIELDS.values():
                measurement = vehicle.measurements.get(suffix)
                fence = max(observed, measurement.observed) if measurement else observed
                vehicle.measurements[suffix] = Measurement(fence, self.monotonic(), None)
            vehicle.last_received = None
        self.persist_fences()
        self.send(f"{self.base(vehicle)}/connected/state", "ON" if vehicle.connected else "OFF")
        self.available(f"{self.base(vehicle)}/connected/availability", True)
        if not vehicle.connected:
            self.tick()

    def tick(self):
        for vehicle in self.vehicles.values():
            for suffix in SUPPORTED_FIELDS.values():
                measurement = vehicle.measurements.get(suffix)
                available = measurement is not None and measurement.value is not None and self.fresh(measurement.observed, measurement.received)
                if measurement is not None and not available:
                    measurement.value = None  # Clock corrections must not revive an expired fix.
                self.available(f"{self.base(vehicle)}/{suffix}/availability", available)
            self.send(f"{self.base(vehicle)}/telemetry_fresh/state", "ON" if self.fresh(vehicle.last_update, vehicle.last_received) else "OFF", retain=True)

    def shutdown(self):
        self.available(self.status_topic, False, force=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = validate_config(json.loads(args.config.read_text()))
        config.setdefault("state_file", "/data/bridge-state.json")
        import paho.mqtt.client as mqtt
    except Exception:
        LOGGER.error("Bridge startup failed: configuration or dependency unavailable")
        return 2
    settings = config["mqtt"]
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=settings.get("client_id", "tesla-live-bridge"), clean_session=True)
    client.max_queued_messages_set(500)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    if settings.get("username"):
        client.username_pw_set(settings["username"], settings.get("password"))
    if settings.get("tls"):
        client.tls_set()  # System trust roots and hostname verification remain enabled.
    try:
        bridge = Bridge(config, client.publish)
    except Exception:
        LOGGER.error("Bridge startup failed: timestamp state unavailable or invalid")
        return 2
    client.will_set(bridge.status_topic, "offline", qos=1, retain=True)
    stopping = False
    failed = False
    online = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    def on_connect(_client, _userdata, _flags, reason_code, _properties):
        nonlocal online
        if reason_code.is_failure:
            LOGGER.warning("MQTT authentication or connection rejected")
            return
        online = True
        bridge.resync()
        subscriptions = [(bridge.birth_topic, 1)]
        for vin in bridge.vehicles:
            subscriptions.extend([(f"{bridge.input_prefix}/{vin}/records", 1), (f"{bridge.input_prefix}/{vin}/connectivity", 1)])
        client.subscribe(subscriptions)
        LOGGER.info("MQTT connected; discovery published; awaiting fresh vehicle data")

    def on_disconnect(_client, _userdata, _flags, _reason_code, _properties):
        nonlocal online
        online = False
        LOGGER.warning("MQTT disconnected; live telemetry unavailable")

    def on_message(_client, _userdata, message):
        nonlocal stopping, failed, online
        try:
            bridge.receive(message.topic, message.payload, message.retain)
        except PersistenceError:
            try:
                bridge.shutdown()
            except ConnectionError:
                pass
            stopping = failed = True
            LOGGER.error("Timestamp state could not be saved; bridge stopped to preserve replay protection")
        except ConnectionError:
            online = False
            LOGGER.warning("MQTT publication unavailable; reconnecting")
        except Exception:
            # Never let malformed network input emit a traceback containing data.
            LOGGER.error("Telemetry handling failed; record discarded")

    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_tick = last_report = time.monotonic()
    retry_at = 0.0
    initialized = False
    while not stopping:
        try:
            if not online and time.monotonic() >= retry_at:
                if initialized:
                    client.reconnect()
                else:
                    client.connect(settings["host"], settings.get("port", 1883), keepalive=30)
                    initialized = True
                retry_at = time.monotonic() + 10
            result = client.loop(timeout=1)
            if result != mqtt.MQTT_ERR_SUCCESS:
                online = False
                time.sleep(1)
            now = time.monotonic()
            if online and now - last_tick >= config["tick_seconds"]:
                bridge.tick()
                last_tick = now
            if now - last_report >= 300:
                LOGGER.info("Bridge record counters: %s", json.dumps(dict(bridge.counters), sort_keys=True))
                last_report = now
        except (OSError, ValueError):
            online = False
            retry_at = time.monotonic() + 10
            LOGGER.warning("MQTT unavailable; retrying")
            time.sleep(1)
    if online:
        try:
            bridge.shutdown()
            client.loop(timeout=1)
        except (OSError, ValueError):
            pass
    client.disconnect()
    LOGGER.info("Bridge stopped")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
