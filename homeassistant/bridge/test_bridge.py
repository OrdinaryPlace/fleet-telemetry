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
        self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
        self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")
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

    def test_invalid_coordinates_preserve_last_valid_fix_without_home_away(self):
        self.location()
        for invalid in ({"invalid": True}, {"location_value": {"latitude": 91, "longitude": 0}}, {"location_value": {"latitude": float("nan"), "longitude": 0}}):
            self.clock.advance(1)
            self.record({"Location": invalid})
            self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
            self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")
        states = [payload for topic, payload, _ in self.messages if topic.endswith("location/state")]
        self.assertTrue(all(state == states[0] for state in states))
        self.assertNotIn("home", " ".join(states))
        self.assertNotIn("not_home", " ".join(states))

    def test_monotonic_expiry_survives_wall_clock_reversal(self):
        self.location()
        self.clock.monotonic += 91
        self.bridge.tick()
        self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
        self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")

    def test_discovery_uses_slug_ids_no_vin_or_tracker_state_override(self):
        self.bridge.resync()
        discovery = [json.loads(payload) for topic, payload, _ in self.messages if topic.endswith("/config")]
        tracker = next(item for item in discovery if item["unique_id"].endswith("_location"))
        self.assertEqual(tracker["source_type"], "gps")
        self.assertNotIn("state_topic", tracker)
        self.assertEqual(tracker["default_entity_id"], "device_tracker.test_car_live_location")
        self.assertEqual(tracker["availability_mode"], "all")
        self.assertEqual(len(discovery), 12)
        presence = next(item for item in discovery if item["unique_id"].endswith("_driver_present"))
        self.assertEqual(presence['device_class'], 'occupancy')
        self.assertEqual(presence['default_entity_id'], 'binary_sensor.test_car_live_driver_present')
        self.assertEqual(tracker["availability"], [{"topic": "test_live/test_car/location/availability"}])
        self.assertNotIn(TEST_VIN, json.dumps(discovery))
        self.assertEqual({tuple(item["device"]["identifiers"]) for item in discovery}, {("tesla_live_test_car",)})
        self.assertTrue(all(options["retain"] for topic, _, options in self.messages if topic.endswith("/config")))

    def test_ha_birth_and_broker_reconnect_restore_location_without_freshness(self):
        self.location()
        before = self.last("test_live/test_car/location/state")
        self.bridge.receive("homeassistant/status", b"online")
        self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
        self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")
        self.location()  # Same source-time cannot be restored as a new location.
        self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
        self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")
        self.clock.advance(1)
        self.location()
        self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
        self.bridge.resync()
        self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
        self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")
        self.assertIsNotNone(before)

    def test_quiet_connection_does_not_expire_with_location(self):
        self.connection("CONNECTED", "synthetic_session_a")
        self.location()
        self.clock.advance(120)
        self.bridge.tick()
        self.assertEqual(self.last("test_live/test_car/connected/state"), "ON")
        self.assertEqual(self.last("test_live/test_car/connected/availability"), "online")
        self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
        self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")
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
        self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
        self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")
        self.location(age=0.5)  # Delivered after disconnect, observed before it.
        self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
        self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")

    def test_zero_values_and_protobuf_numeric_strings(self):
        self.record({"VehicleSpeed": {"long_value": "0"}, "BatteryLevel": {"double_value": 0}, "Soc": {"string_value": "0"}, "Gear": {"shift_state_value": "ShiftStateP"}})
        self.assertEqual(self.last("test_live/test_car/speed/state"), "0.0")
        self.assertEqual(self.last("test_live/test_car/battery/state"), "0.0")
        self.assertEqual(self.last("test_live/test_car/usable_battery/state"), "0.0")
        self.assertEqual(self.last("test_live/test_car/gear/state"), "P")

    def test_driver_presence_boolean_edges_expiry_and_resync(self):
        topic = 'test_live/test_car/driver_present/state'
        for value, expected in ((False, 'OFF'), (True, 'ON'), (False, 'OFF')):
            self.clock.advance(5)
            self.record({'DriverSeatOccupied': {'boolean_value': value}})
            self.assertEqual(self.last(topic), expected)
        updates = lambda: [m for m in self.messages if m[0] == topic]
        self.assertEqual(len(updates()), 3)
        self.assertTrue(all(not m[2]['retain'] for m in updates()))
        self.assertIn('observed_at', json.loads(self.last('test_live/test_car/driver_present/attributes')))
        self.clock.advance(91)
        self.bridge.tick()
        self.assertEqual(self.last('test_live/test_car/driver_present/availability'), 'offline')
        self.bridge.resync()
        self.assertEqual(len(updates()), 3)  # Neither silence nor restoration is an exit.

    def test_driver_presence_invalid_stale_replayed_and_unordered(self):
        topic = 'test_live/test_car/driver_present/state'
        self.record({'DriverSeatOccupied': {'boolean_value': True}})
        for kwargs in ({'age': 31}, {'age': -6}, {'is_resend': True}, {'age': 1}, {}):
            self.record({'DriverSeatOccupied': {'boolean_value': False}}, **kwargs)
        raw = self.envelope({'DriverSeatOccupied': {'boolean_value': False}})
        self.bridge.receive(f'test_receiver/{TEST_VIN}/records', raw, retained=True)
        for value in ({'boolean_value': 'false'}, {'boolean_value': 0}, {'string_value': 'false'}, {'invalid': True}):
            self.clock.advance(1)
            self.record({'DriverSeatOccupied': value})
        self.assertEqual([m[1] for m in self.messages if m[0] == topic], ['ON'])
        self.assertEqual(self.last('test_live/test_car/driver_present/availability'), 'offline')

    def test_driver_presence_restart_retains_replay_fence_without_replaying_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config = deepcopy(CONFIG) | {'state_file': str(Path(directory) / 'state.json')}
            self.bridge = self.make_bridge(config)
            self.record({'DriverSeatOccupied': {'boolean_value': True}})
            self.messages.clear()
            self.bridge = self.make_bridge(config)
            self.bridge.resync()
            self.record({'DriverSeatOccupied': {'boolean_value': False}})
            self.assertIsNone(self.last('test_live/test_car/driver_present/state'))
            self.clock.advance(1)
            self.record({'DriverSeatOccupied': {'boolean_value': False}})
            self.assertEqual(self.last('test_live/test_car/driver_present/state'), 'OFF')

    def test_seat_belts_preserve_documented_polarity_and_enum_faults(self):
        for raw, expected in ((True, 'unbuckled'), (False, 'buckled')):
            self.clock.advance(1)
            self.record({'DriverSeatBelt': {'boolean_value': raw}})
            self.assertEqual(self.last('test_live/test_car/driver_seat_belt/state'), expected)
        for raw, expected in (('BuckleStatusUnlatched', 'unbuckled'), ('BuckleStatusLatched', 'buckled'), ('BuckleStatusFaulted', 'fault')):
            self.clock.advance(1)
            self.record({'PassengerSeatBelt': {'buckle_status_value': raw}})
            self.assertEqual(self.last('test_live/test_car/rear_center_seat_belt/state'), expected)
        before = [m for m in self.messages if m[0].endswith('seat_belt/state')]
        for key in ('DriverSeatBelt', 'PassengerSeatBelt'):
            for value in ({'invalid': True}, {'boolean_value': 'false'}, {'buckle_status_value': 'BuckleStatusUnknown'}):
                self.clock.advance(1)
                self.record({key: value})
        self.clock.advance(91)
        self.bridge.tick()
        self.bridge.resync()
        after = [m for m in self.messages if m[0].endswith('seat_belt/state')]
        self.assertEqual(before, after)
        self.assertTrue(all(not m[2]['retain'] for m in after))

    def test_distinct_battery_values_and_clock_correction_do_not_relabel_or_revive(self):
        self.record({"BatteryLevel": {"double_value": 55}, "Soc": {"double_value": 54}})
        self.location()
        self.assertEqual(self.last("test_live/test_car/battery/state"), "55.0")
        self.assertEqual(self.last("test_live/test_car/usable_battery/state"), "54.0")
        self.clock.wall += 120
        self.bridge.tick()
        self.clock.wall -= 120
        self.bridge.tick()
        self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
        self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")

    def test_duplicate_fields_and_malformed_input_do_not_partially_publish(self):
        payload = json.loads(self.envelope({"VehicleSpeed": {"double_value": 20}}))
        payload["data"] += payload["data"]
        self.bridge.receive(f"test_receiver/{TEST_VIN}/records", json.dumps(payload).encode())
        for raw in (b"null", b"[]", b"{", b"\xff", b"x" * (256 * 1024 + 1)):
            self.bridge.receive(f"test_receiver/{TEST_VIN}/records", raw)
        self.assertEqual(self.messages, [])

    def test_restart_persists_private_location_and_fences_without_vin(self):
        with tempfile.TemporaryDirectory() as directory:
            config = deepcopy(CONFIG)
            path = Path(directory) / "state.json"
            config["state_file"] = str(path)
            self.bridge = self.make_bridge(config)
            self.location(ns=123456789)
            stored = path.read_text()
            self.assertNotIn(TEST_VIN, stored)
            self.assertIn("last_location", stored)
            self.assertEqual(json.loads(stored)["version"], 2)
            self.assertIn(".123456789Z", stored)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.bridge = self.make_bridge(config)
            self.bridge.resync()
            self.location(ns=123456789)
            self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
            self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")
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

    def test_upgrade_restores_newest_valid_archive_fix_as_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            config = deepcopy(CONFIG)
            config.update(state_file=str(Path(directory) / "state.json"), location_history_directory=directory)
            Path(config["state_file"]).write_text(json.dumps({"version": 1, "vehicles": {"test_car": {"fields": {"location": iso_time(int(self.clock.wall * NANOSECOND))}}}}))
            def row(age, value=None, **extra):
                return {"schema_version": 1, "vehicle": "test_car", "source_time": iso_time(int((self.clock.wall - age) * NANOSECOND)), "location": value or {"location_value": {"latitude": 10, "longitude": 20}}, "is_resend": True, **extra}
            history = Path(directory) / "2025-12-31_test_car_recorder.ndjson"
            rows = [row(200), row(100), row(10, {"invalid": True}), row(-3600), row(0, vehicle="unknown_car"), row(300)]
            history.write_text("\n".join(json.dumps(r) for r in rows) + '\n{"partial":')
            ignored = Path(directory) / "2026-01-01_test_car.ndjson"
            outside = Path(directory) / "unrelated.txt"
            outside.write_text(json.dumps(row(0)) + "\n")
            ignored.symlink_to(outside)
            self.bridge = self.make_bridge(config)
            self.bridge.resync()
            restored = json.loads(self.last("test_live/test_car/location/state"))
            self.assertEqual(restored["observed_at"], row(100)["source_time"])
            self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
            self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")
            self.location(age=1)  # Existing newer disconnect fence still applies.
            self.assertEqual(json.loads(self.last("test_live/test_car/location/state")), restored)
            self.clock.advance(1)
            self.location()
            self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "ON")
            history.write_text(json.dumps(row(0)) + "\n")
            self.clock.advance(200)
            self.bridge.tick()
            self.bridge = self.make_bridge(config)
            self.bridge.resync()
            self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")
            self.assertNotEqual(json.loads(self.last("test_live/test_car/location/state"))["observed_at"], restored["observed_at"])

    def test_missing_location_stays_unavailable_and_never_becomes_zero(self):
        self.bridge.resync()
        self.record({"Location": {"invalid": True}})
        self.bridge.tick()
        self.assertIsNone(self.last("test_live/test_car/location/state"))
        self.assertEqual(self.last("test_live/test_car/location/availability"), "offline")
        self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")

    def test_last_fix_survives_expiry_resync_and_disconnect_unchanged(self):
        self.connection("CONNECTED", "synthetic_session")
        self.location()
        original = self.last("test_live/test_car/location/state")
        self.clock.advance(91)
        self.bridge.tick()
        self.clock.advance(1)
        self.connection("DISCONNECTED", "synthetic_session")
        self.bridge.resync()
        self.assertEqual(self.last("test_live/test_car/location/state"), original)
        self.assertEqual(self.last("test_live/test_car/location/availability"), "online")
        self.assertEqual(self.last("test_live/test_car/location_fresh/state"), "OFF")

    def test_persistence_failure_prevents_location_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            config = deepcopy(CONFIG)
            config["state_file"] = str(Path(directory) / "missing" / "state.json")
            self.bridge = self.make_bridge(config)
            with self.assertRaises(PersistenceError):
                self.location()
            self.assertEqual(self.messages, [])

    def test_last_known_coordinates_retained_without_refreshing_source_time(self):
        self.location()
        observed = self.last("test_live/test_car/last_update/state")
        self.clock.advance(10)
        self.bridge.tick()
        self.assertEqual(self.last("test_live/test_car/last_update/state"), observed)
        self.assertTrue(next(options["retain"] for topic, _, options in self.messages if topic.endswith("location/state")))
        self.assertEqual(next(options["qos"] for topic, _, options in self.messages if topic.endswith("location/state")), 1)

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
