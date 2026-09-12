"""Synthetic-only behavioral tests; no Tesla, HA, or broker access required."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest

from bridge import Bridge, NANOSECOND, PersistenceError, iso_time, timestamp, validate_config

TEST_VIN = "5YJ3E1EA0JF000001"
SECOND_VIN = "5YJ3E1EA0JF000002"
CONFIG = {"mqtt": {"host": "broker.test", "topic_base": "test_receiver", "state_prefix": "test_live"}, "vehicles": [{"vin": TEST_VIN, "slug": "test_car", "name": "Test car"}]}


class Clock:
    def __init__(self):
        self.wall = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        self.monotonic = 100.0

    def advance(self, seconds):
        self.wall += seconds
        self.monotonic += seconds


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.messages = []
        self.bridge = self.make_bridge()

    def make_bridge(self, config=None):
        return Bridge(deepcopy(config or CONFIG), self.publish, lambda: self.clock.wall, lambda: self.clock.monotonic)

    def publish(self, topic, payload, **kwargs):
        self.messages.append((topic, payload, kwargs))

    def last(self, topic):
        return next((payload for name, payload, _ in reversed(self.messages) if name == topic), None)

    def envelope(self, fields, *, age=0, is_resend=False, vin=TEST_VIN, ns=0):
        return json.dumps({"vin": vin, "created_at": iso_time(int((self.clock.wall - age) * NANOSECOND) + ns), "is_resend": is_resend, "data": [{"key": key, "value": value} for key, value in fields.items()]}).encode()

    def record(self, fields, **kwargs):
        self.bridge.receive(f"test_receiver/{TEST_VIN}/records", self.envelope(fields, **kwargs))

    def location(self, **kwargs):
        self.record({"Location": {"location_value": {"latitude": 10.0, "longitude": 20.0}}}, **kwargs)

    def connection(self, state, session, age=0):
        self.bridge.receive(f"test_receiver/{TEST_VIN}/connectivity", json.dumps({"Status": state, "ConnectionId": session, "CreatedAt": iso_time(int((self.clock.wall - age) * NANOSECOND))}).encode())

    def test_partial_messages_do_not_refresh_location(self):
        self.location()
        original = self.bridge.vehicles[TEST_VIN].measurements["location"].observed
        self.clock.advance(60)
        self.record({"VehicleSpeed": {"double_value": 25.0}})
        self.clock.advance(31)
        self.bridge.tick()
        self.assertEqual(self.bridge.vehicles[TEST_VIN].measurements["location"].observed, original)
        self.assertEqual(self.last("test_live/test_car/location/availability"), "offline")
        self.assertEqual(self.last("test_live/test_car/speed/availability"), "online")
        self.assertEqual(self.last("test_live/test_car/telemetry_fresh/state"), "ON")

    def test_old_future_replay_retained_and_unknown_identity_fail_closed(self):
        for kwargs in ({"age": 31}, {"age": -6}, {"is_resend": True}, {"vin": SECOND_VIN}):
            self.location(**kwargs)
        self.bridge.receive(f"test_receiver/{TEST_VIN}/records", self.envelope({"Location": {"location_value": {"latitude": 10, "longitude": 20}}}), retained=True)
        self.bridge.receive(f"test_receiver/{SECOND_VIN}/records", self.envelope({"VehicleSpeed": {"double_value": 20}}, vin=SECOND_VIN))
        self.assertEqual(self.messages, [])
        self.assertEqual(len(self.bridge.vehicles[TEST_VIN].measurements), 0)

    def test_per_field_order_handles_partial_out_of_order_records(self):
        self.location(ns=100)
        self.record({"Location": {"location_value": {"latitude": 30, "longitude": 40}}, "VehicleSpeed": {"double_value": 0}}, ns=99)
        location = json.loads(self.last("test_live/test_car/location/state"))
        self.assertEqual(location["latitude"], 10)
        self.assertEqual(self.last("test_live/test_car/speed/state"), "0.0")
        self.assertEqual(self.bridge.counters["out_of_order_rejected"], 1)

    def test_invalid_coordinates_unavailable_without_home_away(self):
        self.location()
        for invalid in ({"invalid": True}, {"location_value": {"latitude": 91, "longitude": 0}}, {"location_value": {"latitude": float("nan"), "longitude": 0}}):
            self.clock.advance(1)
            self.record({"Location": invalid})
            self.assertEqual(self.last("test_live/test_car/location/availability"), "offline")
        states = [payload for topic, payload, _ in self.messages if topic.endswith("location/state")]
        self.assertEqual(len(states), 1)
        self.assertNotIn("home", " ".join(states))
        self.assertNotIn("not_home", " ".join(states))

    def test_monotonic_expiry_survives_wall_clock_reversal(self):
        self.location()
        self.clock.monotonic += 91
        self.bridge.tick()
        self.assertEqual(self.last("test_live/test_car/location/availability"), "offline")

    def test_discovery_uses_slug_ids_no_vin_or_tracker_state_override(self):
        self.bridge.resync()
        discovery = [json.loads(payload) for topic, payload, _ in self.messages if topic.endswith("/config")]
        tracker = next(item for item in discovery if item["unique_id"].endswith("_location"))
        self.assertEqual(tracker["source_type"], "gps")
        self.assertNotIn("state_topic", tracker)
        self.assertEqual(tracker["default_entity_id"], "device_tracker.test_car_live_location")
        self.assertEqual(tracker["availability_mode"], "all")
        self.assertEqual(len(discovery), 8)
        self.assertNotIn(TEST_VIN, json.dumps(discovery))
        self.assertEqual({tuple(item["device"]["identifiers"]) for item in discovery}, {("tesla_live_test_car",)})
        self.assertTrue(all(options["retain"] for topic, _, options in self.messages if topic.endswith("/config")))

    def test_ha_birth_and_broker_reconnect_require_new_location(self):
        self.location()
        before = self.last("test_live/test_car/location/state")
        self.bridge.receive("homeassistant/status", b"online")
        self.assertEqual(self.last("test_live/test_car/location/availability"), "offline")
        self.location()  # Same source-time cannot be restored as a new location.
        self.assertEqual(self.last("test_live/test_car/location/availability"), "offline")
        self.clock.advance(1)
        self.location()
        self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
        self.bridge.resync()
        self.assertEqual(self.last("test_live/test_car/location/availability"), "offline")
        self.assertIsNotNone(before)

    def test_quiet_connection_does_not_expire_with_location(self):
        self.connection("CONNECTED", "synthetic_session_a")
        self.location()
        self.clock.advance(120)
        self.bridge.tick()
        self.assertEqual(self.last("test_live/test_car/connected/state"), "ON")
        self.assertEqual(self.last("test_live/test_car/connected/availability"), "online")
        self.assertEqual(self.last("test_live/test_car/location/availability"), "offline")
        self.assertEqual(self.last("test_live/test_car/telemetry_fresh/state"), "OFF")

    def test_session_fence_ignores_disconnect_from_previous_connection(self):
        self.connection("CONNECTED", "synthetic_session_a")
        self.clock.advance(1)
        self.connection("CONNECTED", "synthetic_session_b")
        self.clock.advance(1)
        self.connection("DISCONNECTED", "synthetic_session_a")
        self.assertEqual(self.last("test_live/test_car/connected/state"), "ON")
        self.location()
        self.clock.advance(1)
        self.connection("DISCONNECTED", "synthetic_session_b")
        self.assertEqual(self.last("test_live/test_car/connected/state"), "OFF")
        self.assertEqual(self.last("test_live/test_car/location/availability"), "offline")
        self.location(age=0.5)  # Delivered after disconnect, observed before it.
        self.assertEqual(self.last("test_live/test_car/location/availability"), "offline")

    def test_zero_values_and_protobuf_numeric_strings(self):
        self.record({"VehicleSpeed": {"long_value": "0"}, "BatteryLevel": {"double_value": 0}, "Soc": {"string_value": "0"}, "Gear": {"shift_state_value": "ShiftStateP"}})
        self.assertEqual(self.last("test_live/test_car/speed/state"), "0.0")
        self.assertEqual(self.last("test_live/test_car/battery/state"), "0.0")
        self.assertEqual(self.last("test_live/test_car/usable_battery/state"), "0.0")
        self.assertEqual(self.last("test_live/test_car/gear/state"), "P")

    def test_distinct_battery_values_and_clock_correction_do_not_relabel_or_revive(self):
        self.record({"BatteryLevel": {"double_value": 55}, "Soc": {"double_value": 54}})
        self.location()
        self.assertEqual(self.last("test_live/test_car/battery/state"), "55.0")
        self.assertEqual(self.last("test_live/test_car/usable_battery/state"), "54.0")
        self.clock.wall += 120
        self.bridge.tick()
        self.clock.wall -= 120
        self.bridge.tick()
        self.assertEqual(self.last("test_live/test_car/location/availability"), "offline")

    def test_duplicate_fields_and_malformed_input_do_not_partially_publish(self):
        payload = json.loads(self.envelope({"VehicleSpeed": {"double_value": 20}}))
        payload["data"] += payload["data"]
        self.bridge.receive(f"test_receiver/{TEST_VIN}/records", json.dumps(payload).encode())
        for raw in (b"null", b"[]", b"{", b"\xff", b"x" * (256 * 1024 + 1)):
            self.bridge.receive(f"test_receiver/{TEST_VIN}/records", raw)
        self.assertEqual(self.messages, [])

    def test_restart_fences_persist_without_coordinates_or_vin(self):
        with tempfile.TemporaryDirectory() as directory:
            config = deepcopy(CONFIG)
            path = Path(directory) / "state.json"
            config["state_file"] = str(path)
            self.bridge = self.make_bridge(config)
            self.location(ns=123456789)
            stored = path.read_text()
            self.assertNotIn(TEST_VIN, stored)
            self.assertNotIn("latitude", stored)
            self.assertNotIn("longitude", stored)
            self.assertIn(".123456789Z", stored)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.bridge = self.make_bridge(config)
            self.bridge.resync()
            self.location(ns=123456789)
            self.assertEqual(self.last("test_live/test_car/location/availability"), "offline")
            self.location(ns=123456790)
            self.assertEqual(self.last("test_live/test_car/location/availability"), "online")

    def test_corrupt_timestamp_state_fails_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            config = deepcopy(CONFIG)
            path = Path(directory) / "state.json"
            path.write_text('{"version": 999}')
            config["state_file"] = str(path)
            with self.assertRaises(ValueError):
                self.make_bridge(config)

    def test_persistence_failure_prevents_location_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            config = deepcopy(CONFIG)
            config["state_file"] = str(Path(directory) / "missing" / "state.json")
            self.bridge = self.make_bridge(config)
            with self.assertRaises(PersistenceError):
                self.location()
            self.assertEqual(self.messages, [])

    def test_coordinates_not_retained_and_timestamp_not_refreshed_by_tick(self):
        self.location()
        observed = self.last("test_live/test_car/last_update/state")
        self.clock.advance(10)
        self.bridge.tick()
        self.assertEqual(self.last("test_live/test_car/last_update/state"), observed)
        self.assertFalse(next(options["retain"] for topic, _, options in self.messages if topic.endswith("location/state")))
        self.assertEqual(next(options["qos"] for topic, _, options in self.messages if topic.endswith("location/state")), 0)

    def test_publication_failure_does_not_advance_availability_cache(self):
        class FailedPublish:
            rc = 4

        self.bridge.publish = lambda *args, **kwargs: FailedPublish()
        with self.assertRaises(ConnectionError):
            self.bridge.available("test_live/availability", True)
        self.assertNotIn("test_live/availability", self.bridge.availability)

    def test_invalid_configuration_rejects_ambiguous_identity(self):
        for mutate in (lambda cfg: cfg["vehicles"].append(deepcopy(cfg["vehicles"][0])), lambda cfg: cfg["vehicles"][0].update(slug="car/+"), lambda cfg: cfg["mqtt"].update(topic_base="+")):
            config = deepcopy(CONFIG)
            mutate(config)
            with self.assertRaises(ValueError):
                validate_config(config)

    def test_nanosecond_timestamp_round_trip(self):
        self.assertEqual(iso_time(timestamp("2026-01-01T01:00:00.123456789+01:00")), "2026-01-01T00:00:00.123456789Z")


if __name__ == "__main__":
    unittest.main()
