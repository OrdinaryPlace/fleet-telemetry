"""Offline tests for receiver TLS lifecycle, service wiring and log boundaries."""

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization


SPEC = importlib.util.spec_from_file_location("addon_runtime", Path(__file__).resolve().parents[1] / "addon" / "runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)
HOSTNAME = "telemetry.example.test"


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "tls"

    def snapshot(self):
        return {path.name: path.read_bytes() for path in self.directory.iterdir() if path.is_file()}

    def reissue(self, filename, expires):
        """Change only validity using the existing matching issuer and key."""
        existing = x509.load_pem_x509_certificate((self.directory / filename).read_bytes())
        ca_key = serialization.load_pem_private_key((self.directory / "ca.key").read_bytes(), None)
        builder = (x509.CertificateBuilder().subject_name(existing.subject).issuer_name(existing.issuer)
                   .public_key(existing.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5))
                   .not_valid_after(expires))
        for extension in existing.extensions:
            builder = builder.add_extension(extension.value, extension.critical)
        certificate = builder.sign(ca_key, hashes.SHA256())
        (self.directory / filename).write_bytes(certificate.public_bytes(serialization.Encoding.PEM))

    def test_generation_verifies_hostname_and_preserves_keys_on_restart(self):
        runtime.ensure_tls(self.directory, HOSTNAME)
        initial = self.snapshot()
        result = subprocess.run(["openssl", "verify", "-CAfile", str(self.directory / "ca.pem"),
                                 "-verify_hostname", HOSTNAME, str(self.directory / "server.pem")],
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        for path in self.directory.iterdir():
            path.chmod(0o644)
        self.directory.chmod(0o755)
        runtime.ensure_tls(self.directory, HOSTNAME)
        self.assertEqual(self.snapshot(), initial)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        for path in self.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_renewal_preserves_ca_and_server_private_key(self):
        runtime.ensure_tls(self.directory, HOSTNAME)
        self.reissue("server.pem", dt.datetime.now(dt.UTC) + dt.timedelta(days=10))
        initial = self.snapshot()
        runtime.ensure_tls(self.directory, HOSTNAME)
        renewed = self.snapshot()
        for filename in ("ca.pem", "ca.key", "server.key"):
            self.assertEqual(initial[filename], renewed[filename])
        self.assertNotEqual(initial["server.pem"], renewed["server.pem"])
        certificate = x509.load_pem_x509_certificate(renewed["server.pem"])
        ca = x509.load_pem_x509_certificate(renewed["ca.pem"])
        certificate.verify_directly_issued_by(ca)
        self.assertGreater(certificate.not_valid_after_utc, dt.datetime.now(dt.UTC) + dt.timedelta(days=300))

    def test_wrong_ca_chain_and_hostname_are_not_silently_reused(self):
        runtime.ensure_tls(self.directory, HOSTNAME)
        initial = self.snapshot()
        with self.assertRaises(ValueError):
            runtime.ensure_tls(self.directory, "other.example.test")
        self.assertEqual(initial, self.snapshot())
        other = Path(self.temporary.name) / "other-ca"
        runtime.ensure_tls(other, HOSTNAME)
        for filename in ("ca.pem", "ca.key"):
            (self.directory / filename).write_bytes((other / filename).read_bytes())
        mismatched = self.snapshot()
        with self.assertRaises(ValueError):
            runtime.ensure_tls(self.directory, HOSTNAME)
        self.assertEqual(mismatched, self.snapshot())

    def test_expiring_ca_requires_migration_instead_of_rotation(self):
        runtime.ensure_tls(self.directory, HOSTNAME)
        self.reissue("ca.pem", dt.datetime.now(dt.UTC) + dt.timedelta(days=10))
        initial = self.snapshot()
        with self.assertRaisesRegex(ValueError, "explicit migration"):
            runtime.ensure_tls(self.directory, HOSTNAME)
        self.assertEqual(initial, self.snapshot())

    def test_incomplete_key_pairs_are_preserved(self):
        for missing in ("ca.pem", "ca.key", "server.pem", "server.key"):
            with self.subTest(missing=missing):
                self.directory = Path(self.temporary.name) / missing
                runtime.ensure_tls(self.directory, HOSTNAME)
                (self.directory / missing).unlink()
                initial = self.snapshot()
                with self.assertRaisesRegex(ValueError, "Incomplete existing"):
                    runtime.ensure_tls(self.directory, HOSTNAME)
                self.assertEqual(initial, self.snapshot())

    def test_invalid_dns_names_do_not_create_tls_material(self):
        for hostname in ("a..example", "-a.example", "a-.example", "a" * 64 + ".example",
                         "*.example.com", "127.0.0.1", "localhost", "a.example."):
            with self.subTest(hostname=hostname):
                with self.assertRaises(ValueError):
                    runtime.ensure_tls(self.directory, hostname)
                self.assertFalse(self.directory.exists())

    def test_private_write_is_atomic_and_ignores_old_temporary_names(self):
        destination = Path(self.temporary.name) / "state.json"
        stale = destination.with_suffix(".json.new")
        stale.write_bytes(b"interrupted old attempt")
        runtime.private_write(destination, b"original")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        before = set(destination.parent.iterdir())
        with patch.object(runtime.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                runtime.private_write(destination, b"replacement")
        self.assertEqual(destination.read_bytes(), b"original")
        self.assertEqual(stale.read_bytes(), b"interrupted old attempt")
        self.assertEqual(set(destination.parent.iterdir()), before)

    def test_supervisor_mqtt_auth_and_production_client_trust(self):
        mqtt = {"host": "core-mosquitto", "port": 1883, "username": "test-user", "password": "test-password", "ssl": False}
        response = io.BytesIO(json.dumps({"result": "ok", "data": mqtt}).encode())
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "synthetic-supervisor-token"}):
            with patch.object(runtime.urllib.request, "urlopen", return_value=response) as request:
                self.assertEqual(runtime.mqtt_service(), mqtt)
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "http://supervisor/services/mqtt")
        self.assertEqual(sent.get_header("Authorization"), "Bearer synthetic-supervisor-token")
        options = {"vehicles": [{"vin": "5YJ3E1EA0KF000001", "slug": "test", "name": "Test"}]}
        receiver, bridge = runtime.configurations(options, mqtt)
        self.assertEqual(receiver["records"], {"V": ["mqtt"], "connectivity": ["mqtt"]})
        self.assertEqual(receiver["reliable_ack_sources"], {"V": "mqtt"})
        self.assertTrue(receiver["mqtt"]["publish_vehicle_records"])
        self.assertFalse(receiver.get("use_default_eng_ca", False))
        self.assertNotIn("ca_file", receiver["tls"])
        self.assertNotIn("synthetic-supervisor-token", json.dumps((receiver, bridge)))
        self.assertEqual(bridge["mqtt"]["host"], mqtt["host"])
        with self.assertRaisesRegex(ValueError, "TLS-only MQTT"):
            runtime.configurations(options, {**mqtt, "ssl": True})

    def test_child_logs_emit_only_fixed_categories(self):
        output = io.StringIO()
        process = SimpleNamespace(stdout=io.StringIO("mqtt synthetic-password 5YJ3E1EA0KF000001\ntls latitude=10 longitude=20\n"))
        with contextlib.redirect_stdout(output), patch.object(runtime.time, "monotonic", side_effect=[100, 101, 140, 141]):
            runtime.receiver_output(process, "Bridge")
        self.assertEqual(output.getvalue(), "Bridge mqtt event; payload details suppressed\nBridge tls event; payload details suppressed\n")


if __name__ == "__main__":
    unittest.main()
