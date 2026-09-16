"""Synthetic-only checks for commissioning safety and response handling."""

from contextlib import contextmanager
from dataclasses import replace
import base64
import copy
import io
import json
import os
from pathlib import Path
import ssl
import stat
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import commissioner as c


# Construct conspicuously synthetic identifiers; never use a real vehicle fixture.
VIN_A = "A" * 17
VIN_B = "B" * 17
ALIASES = ("test-car-a", "test-car-b")


def jwt(claims):
    return ".".join(base64.urlsafe_b64encode(json.dumps(v).encode()).decode().rstrip("=")
                    for v in ({"alg": "none"}, claims, "synthetic-signature"))


class FakeAPI:
    def __init__(self, current=None, fleet=None):
        self.calls = []
        self.current = current or {v: {"config": None, "synced": True,
                                      "key_paired": True, "limit_reached": False}
                                   for v in (VIN_A, VIN_B)}
        self.fleet = fleet or {
            "key_paired_vins": [VIN_A, VIN_B], "unpaired_vins": [],
            "vehicle_info": {v: {"firmware_version": "2026.20.1", "fleet_telemetry_version": "1.0.0"}
                             for v in (VIN_A, VIN_B)},
        }

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/api/1/vehicles":
            return {"response": [{"vin": v, "display_name": a} for a, v in zip(ALIASES, (VIN_A, VIN_B))]}
        if path == "/api/1/vehicles/fleet_status":
            return {"response": self.fleet}
        if path.endswith("/fleet_telemetry_config") and method == "GET":
            return {"response": copy.deepcopy(self.current[path.split("/")[4]])}
        if path.endswith("/fleet_telemetry_errors"):
            return {"response": {"fleet_telemetry_errors": [
                {"error": "x509 unknown certificate " + VIN_A + " precise coordinates hidden",
                 "hostname": "private.example.invalid", "vin": VIN_A},
                {"error": "connection refused", "vin": VIN_B},
            ]}}
        raise AssertionError("unexpected endpoint")


class CommissionerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.ca_path = cls.root / "test-public.pem"
        generated = subprocess.run([
            "openssl", "req", "-x509", "-nodes", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:secp384r1", "-days", "1",
            "-subj", "/CN=synthetic-test-only", "-keyout", str(cls.root / "temporary-test.key"),
            "-out", str(cls.ca_path),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if generated.returncode:
            raise RuntimeError("synthetic certificate generation failed")
        cls.ca = cls.ca_path.read_text()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.data = Path(self.scratch.name)
        self.options = c.Options("configure", ALIASES, "telemetry.example.invalid")
        self.credential = c.Credential("synthetic-access-value", c.FLEET_HOSTS["na"],
                                      frozenset({"vehicle_device_data", "vehicle_cmds", "vehicle_location"}),
                                      time.time() + 3600)
        self.desired = c.desired_config(self.options, self.ca)

    def execute(self, api, options=None, factory=None):
        return c.run(options or self.options, self.credential, api,
                     config_dir=self.data / "never-read-live", data_dir=self.data,
                     ca_pem=self.ca, proxy_factory=factory or self.no_proxy)

    @staticmethod
    @contextmanager
    def no_proxy(*_):
        raise AssertionError("must not start signing proxy")
        yield

    def test_options_require_explicit_safe_aliases_and_ca_mount(self):
        values = {"vehicle_names": list(ALIASES), "hostname": "telemetry.example.invalid"}
        self.assertEqual(c.Options.parse(values).mode, "inspect")
        for update in ({"vehicle_names": [VIN_A]}, {"vehicle_names": ["same", "SAME"]},
                       {"hostname": "https://host.invalid/path"}, {"port": True},
                       {"ca_file": "/ssl/../homeassistant_config/tesla_fleet.key"}):
            with self.subTest(update=update), self.assertRaises(c.Stop):
                c.Options.parse(values | update)

    def test_access_token_read_does_not_change_storage_or_use_refresh(self):
        storage = self.data / ".storage"
        storage.mkdir()
        claims = {"aud": [c.FLEET_HOSTS["na"], c.FLEET_HOSTS["eu"]], "ou_code": "NA",
                  "scp": list(self.credential.scopes), "exp": 5000}
        raw = json.dumps({"data": {"entries": [{"domain": "tesla_fleet", "data": {"token": {
            "access_token": jwt(claims), "expires_at": 5000, "refresh_token": "never-read-or-used"}}}]}})
        file = storage / "core.config_entries"
        file.write_text(raw)
        credential = c.load_credential(self.data, now=1000)
        self.assertEqual(credential.base_url, c.FLEET_HOSTS["na"])
        self.assertEqual(file.read_text(), raw)
        with self.assertRaisesRegex(c.Stop, "refresh_needed"):
            c.load_credential(self.data, now=4900)
        self.assertEqual(file.read_text(), raw)

    def test_prerequisites_never_read_private_files_or_list_directory(self):
        storage = self.data / ".storage"
        storage.mkdir()
        (storage / "core.config_entries").write_text("synthetic-private-contents")
        (self.data / "tesla_fleet.key").write_text("synthetic-private-key")
        with patch.object(Path, "open", side_effect=AssertionError("no reads")), \
                patch.object(Path, "iterdir", side_effect=AssertionError("no listing")), \
                patch.object(c.os, "statvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            result = c.local_prerequisites(self.data)
        self.assertTrue(all(result.values()))
        self.assertTrue(all(type(value) is bool for value in result.values()))
        self.assertNotIn("synthetic-private", json.dumps(result))

    def test_prerequisites_distinguish_missing_and_writable_mounts(self):
        missing = c.local_prerequisites(self.data / "absent")
        self.assertFalse(any(missing.values()))
        with patch.object(c.os, "statvfs", return_value=SimpleNamespace(f_flag=0)):
            writable = c.local_prerequisites(self.data)
        self.assertTrue(writable["config_directory_exists"])
        self.assertFalse(writable["config_read_only"])

    def test_scope_failure_happens_before_vehicle_api(self):
        self.credential = c.Credential("unused", c.FLEET_HOSTS["na"], frozenset(), time.time() + 3600)
        api = FakeAPI()
        with self.assertRaisesRegex(c.Stop, "scope_missing"):
            self.execute(api)
        self.assertEqual(api.calls, [])

    def test_pairing_reports_keep_missing_false_and_conflicting_signals_distinct(self):
        api = FakeAPI()
        current = api.current[VIN_A]
        positive = c.status_summary(ALIASES[0], VIN_A, api.fleet, current, self.desired)
        self.assertTrue(positive["preflight_ready"])
        self.assertTrue(positive["pairing_evidence"]["fleet_paired_contains_vehicle"])
        self.assertFalse(positive["pairing_evidence"]["fleet_unpaired_contains_vehicle"])
        current["key_paired"] = False
        conflict = c.status_summary(ALIASES[0], VIN_A, api.fleet, current, self.desired)
        self.assertFalse(conflict["preflight_ready"])
        self.assertTrue(conflict["pairing_evidence"]["fleet_paired_contains_vehicle"])
        self.assertTrue(conflict["pairing_evidence"]["telemetry_key_paired_false"])
        del current["key_paired"]
        missing = c.pairing_evidence(VIN_A, api.fleet, current)
        for field in ("present", "is_bool", "true", "false"):
            self.assertFalse(missing["telemetry_key_paired_" + field])

    def test_unknown_pairing_shapes_fail_closed_without_emitting_identifiers(self):
        api = FakeAPI()
        for unknown in (None, VIN_A, {VIN_A: True}, [VIN_A, "invalid-private-value"]):
            for field in ("key_paired_vins", "unpaired_vins"):
                fleet = copy.deepcopy(api.fleet)
                fleet[field] = unknown
                summary = c.status_summary(ALIASES[0], VIN_A, fleet, api.current[VIN_A], self.desired)
                self.assertFalse(summary["preflight_ready"])
                self.assertNotIn(VIN_A, json.dumps(summary))
                self.assertNotIn("invalid-private-value", json.dumps(summary))
                self.assertTrue(all(type(v) is bool for v in summary["pairing_evidence"].values()))
        for info in (None, [], {}, {VIN_A: "private-unknown-shape"}):
            fleet = copy.deepcopy(api.fleet)
            fleet["vehicle_info"] = info
            summary = c.status_summary(ALIASES[0], VIN_A, fleet, api.current[VIN_A], self.desired)
            self.assertFalse(summary["preflight_ready"])
            self.assertFalse(summary["pairing_evidence"]["fleet_vehicle_info_for_vehicle_is_object"])
            self.assertNotIn("private-unknown-shape", json.dumps(summary))

    def test_closed_api_allowlist_rejects_vehicle_controls_and_unsigned_config(self):
        api = c.FleetAPI(self.credential)
        for path in (f"/api/1/vehicles/{VIN_A}/wake_up", f"/api/1/vehicles/{VIN_A}/command/door_unlock",
                     "/api/1/vehicles/fleet_telemetry_config"):
            with self.subTest(path=path), self.assertRaisesRegex(c.Stop, "endpoint_not_allowed"):
                api.request("POST", path, {})
        with self.assertRaisesRegex(c.Stop, "endpoint_not_allowed"):
            c.FleetAPI(self.credential, proxy=True).request("GET", "/api/1/vehicles")

    def test_redirect_cannot_forward_authorization(self):
        with self.assertRaisesRegex(c.Stop, "unexpected_api_redirect"):
            c.NoRedirect().redirect_request(None, None, 302, "", {}, "https://untrusted.example.invalid")

    def test_http_error_body_never_read_or_reported(self):
        body = io.BytesIO((VIN_A + " SECRET_VALUE GPS_DATA").encode())
        api = c.FleetAPI(self.credential)
        with patch.object(api.opener, "open", side_effect=HTTPError("private", 403, "private", {}, body)):
            with self.assertRaises(c.Stop) as caught:
                api.request("GET", "/api/1/vehicles")
        self.assertEqual(str(caught.exception), "api_forbidden")
        self.assertEqual(caught.exception.http_status, 403)

    def test_public_ca_validation_rejects_private_or_extra_content(self):
        self.assertEqual(len(c.certificate_fingerprint(self.ca)), 64)
        for invalid in (self.ca + "extra", "-----BEGIN PRIVATE KEY-----", "not a certificate"):
            with self.assertRaises(c.Stop):
                c.certificate_fingerprint(invalid)

    def test_existing_config_comparison_preserves_unknown_fields_and_intervals(self):
        current = copy.deepcopy(self.desired) | {"aud": "public-metadata", "iss": "public-metadata"}
        self.assertTrue(c.same_config(current, self.desired))
        current["fields"]["Location"]["interval_seconds"] = 1
        self.assertFalse(c.same_config(current, self.desired))
        self.assertFalse(c.same_config(self.desired | {"exp": 1234}, self.desired))
        self.assertFalse(c.same_config(self.desired | {"unrecognized": True}, self.desired))

    def test_one_unpaired_vehicle_prevents_all_configuration_writes(self):
        api = FakeAPI()
        api.fleet["unpaired_vins"] = [VIN_B]
        result = self.execute(api)
        self.assertEqual(result["stop_reason"], "vehicle_pairing_or_firmware_not_ready")
        self.assertFalse((self.data / "commissioner").exists())

    def test_unknown_firmware_prevents_all_configuration_writes(self):
        api = FakeAPI()
        api.fleet["vehicle_info"][VIN_A]["firmware_version"] = "unknown"
        self.assertEqual(self.execute(api)["stop_reason"], "vehicle_pairing_or_firmware_not_ready")

    def test_different_existing_config_prevents_all_writes_without_opt_in(self):
        api = FakeAPI()
        api.current[VIN_A]["config"] = self.desired | {"hostname": "another.example.invalid"}
        result = self.execute(api)
        self.assertEqual(result["stop_reason"], "existing_configuration_differs")
        self.assertFalse((self.data / "commissioner").exists())

    def test_matching_configuration_is_idempotent_without_proxy_or_backup(self):
        api = FakeAPI()
        for vin in (VIN_A, VIN_B):
            api.current[vin]["config"] = self.desired | {"iss": "public-metadata", "aud": "public-metadata"}
        result = self.execute(api)
        self.assertTrue(result["already_configured"])
        self.assertFalse(result["changed"])
        self.assertFalse((self.data / "commissioner").exists())

    def test_driver_presence_is_opt_in_and_only_exact_addition_is_allowed(self):
        options = replace(self.options, driver_presence=True)
        desired = c.desired_config(options, self.ca)
        self.assertNotIn('DriverSeatOccupied', self.desired['fields'])
        self.assertEqual(desired['fields']['DriverSeatOccupied'], {'interval_seconds': 5})
        api = FakeAPI()
        current = api.current[VIN_A] | {'config': self.desired}
        summary = c.status_summary(ALIASES[0], VIN_A, api.fleet, current, desired)
        self.assertTrue(summary['configuration_can_add_requested_fields'])
        self.assertEqual(c.plan_changes({ALIASES[0]: VIN_A}, [summary], False), [VIN_A])
        for patch_config in ({'hostname': 'other.example.invalid'}, {'port': 443}, {'extra': True},
                             {'fields': self.desired['fields'] | {'Gear': {'interval_seconds': 5}}},
                             {'fields': desired['fields'] | {'DriverSeatOccupied': {'interval_seconds': 10}}}):
            with self.subTest(change=patch_config):
                changed = current | {'config': self.desired | patch_config}
                summary = c.status_summary(ALIASES[0], VIN_A, api.fleet, changed, desired)
                self.assertFalse(summary['configuration_can_add_requested_fields'])
                with self.assertRaisesRegex(c.Stop, 'existing_configuration_differs'):
                    c.plan_changes({ALIASES[0]: VIN_A}, [summary], False)

    def test_driver_presence_flag_requires_boolean(self):
        values = {'vehicle_names': list(ALIASES), 'hostname': 'telemetry.example.invalid'}
        self.assertFalse(c.Options.parse(values).driver_presence)
        self.assertTrue(c.Options.parse(values | {'driver_presence': True}).driver_presence)
        for value in (1, 'true', None):
            with self.assertRaises(c.Stop):
                c.Options.parse(values | {'driver_presence': value})
            with self.assertRaises(c.Stop):
                c.Options.parse(values | {'seat_belts': value})

    def test_all_status_is_opt_in_and_preserves_every_existing_field(self):
        previous = c.desired_config(replace(self.options, driver_presence=True, seat_belts=True), self.ca)
        desired = c.desired_config(replace(self.options, driver_presence=True, seat_belts=True, all_status=True), self.ca)
        self.assertGreater(len(desired['fields']), 80)
        for field, value in previous['fields'].items():
            self.assertEqual(desired['fields'][field], value)
        api = FakeAPI()
        current = api.current[VIN_A] | {'config': previous}
        summary = c.status_summary(ALIASES[0], VIN_A, api.fleet, current, desired)
        self.assertTrue(summary['configuration_can_add_requested_fields'])
        changed = current | {'config': previous | {'port': 443}}
        self.assertFalse(c.status_summary(ALIASES[0], VIN_A, api.fleet, changed, desired)['configuration_can_add_requested_fields'])
        self.assertFalse(self.options.all_status)
        for value in (1, 'true', None):
            with self.assertRaises(c.Stop):
                c.Options.parse({'vehicle_names': list(ALIASES), 'hostname': 'telemetry.example.invalid', 'all_status': value})

    def test_seat_belts_are_opt_in_and_can_extend_presence_without_replacing_it(self):
        options = replace(self.options, driver_presence=True, seat_belts=True)
        desired = c.desired_config(options, self.ca)
        self.assertEqual(set(desired['fields']) - set(self.desired['fields']),
                         {'DriverSeatOccupied', 'DriverSeatBelt', 'PassengerSeatBelt'})
        api = FakeAPI()
        previous = c.desired_config(replace(self.options, driver_presence=True), self.ca)
        current = api.current[VIN_A] | {'config': previous}
        summary = c.status_summary(ALIASES[0], VIN_A, api.fleet, current, desired)
        self.assertTrue(summary['configuration_can_add_requested_fields'])
        current['config']['fields']['DriverSeatOccupied']['interval_seconds'] = 10
        summary = c.status_summary(ALIASES[0], VIN_A, api.fleet, current, desired)
        self.assertFalse(summary['configuration_can_add_requested_fields'])

    def test_driver_presence_extension_backs_up_once_and_is_idempotent(self):
        api = FakeAPI()
        for vin in (VIN_A, VIN_B):
            api.current[vin]['config'] = copy.deepcopy(self.desired)
        options = replace(self.options, driver_presence=True)
        writes = []
        outer = self
        @contextmanager
        def factory(*_):
            backups = list((outer.data / 'commissioner/backups').glob('*.json'))
            outer.assertEqual(len(backups), 1)
            saved = json.loads(backups[0].read_text())
            outer.assertTrue(all(v['previous']['config'] == outer.desired for v in saved['vehicles']))
            class Proxy:
                def request(self, method, path, body):
                    writes.append(body)
                    for vin in body['vins']:
                        api.current[vin].update(config=copy.deepcopy(body['config']), synced=True)
                    return {'response': {'updated_vehicles': 2, 'skipped_vehicles': {}}}
            yield Proxy()
        result = self.execute(api, options, factory)
        self.assertTrue(result['request_accepted_for_all'])
        self.assertTrue(result['all_readbacks_match'])
        self.assertEqual(len(writes), 1)
        self.assertTrue(self.execute(api, options)['already_configured'])

    def test_backup_precedes_one_signed_request_and_readback_distinguishes_synced(self):
        api = FakeAPI()
        writes = []
        outer = self

        @contextmanager
        def factory(*_):
            backups = list((outer.data / "commissioner/backups").glob("*.json"))
            outer.assertEqual(len(backups), 1)
            outer.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
            backup = json.loads(backups[0].read_text())
            outer.assertTrue(all(v["previous"]["config"] is None for v in backup["vehicles"]))
            outer.assertNotIn(outer.credential.access_token, backups[0].read_text())

            class Proxy:
                def request(self, method, path, body):
                    writes.append((method, path, body))
                    for vin in body["vins"]:
                        api.current[vin].update(config=copy.deepcopy(body["config"]), synced=False)
                    return {"response": {"updated_vehicles": 2, "skipped_vehicles": {}}}
            yield Proxy()

        result = self.execute(api, factory=factory)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][:2], ("POST", "/api/1/vehicles/fleet_telemetry_config"))
        self.assertTrue(result["all_readbacks_match"])
        self.assertTrue(result["request_accepted_for_all"])
        self.assertFalse(any(v["configuration_synced"] for v in result["vehicles"]))
        output = json.dumps(result)
        for sensitive in (VIN_A, VIN_B, self.credential.access_token, self.ca):
            self.assertNotIn(sensitive, output)

    def test_inspect_performs_no_write_even_if_ready(self):
        result = self.execute(FakeAPI(), c.Options("inspect", ALIASES, "telemetry.example.invalid"))
        self.assertFalse(result["changed"])
        self.assertTrue(all(v["preflight_ready"] for v in result["vehicles"]))
        self.assertFalse((self.data / "commissioner").exists())

    def test_error_projection_never_reports_remote_strings(self):
        api = FakeAPI()
        result = self.execute(api, c.Options("get_errors", ALIASES, "telemetry.example.invalid"))
        self.assertEqual(result["vehicles"][0]["error_categories"],
                         {"tls_or_certificate": 1, "connection_refused": 1})
        output = json.dumps(result)
        for sensitive in (VIN_A, VIN_B, "private.example.invalid", "precise coordinates"):
            self.assertNotIn(sensitive, output)
        self.assertEqual(len(api.calls), 3)

    def test_ambiguous_vehicle_name_stops(self):
        value = {"response": [{"display_name": ALIASES[0], "vin": v} for v in (VIN_A, VIN_B)]}
        with self.assertRaisesRegex(c.Stop, "ambiguous"):
            c.select_vehicles(value, (ALIASES[0],))


if __name__ == "__main__":
    unittest.main()
