"""Offline tests for receiver TLS lifecycle, service wiring and log boundaries."""

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

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

    def reissue(self, filename, expires, omit=()):
        """Change validity/extensions using the existing matching issuer and key."""
        existing = x509.load_pem_x509_certificate((self.directory / filename).read_bytes())
        ca_key = serialization.load_pem_private_key((self.directory / "ca.key").read_bytes(), None)
        builder = (x509.CertificateBuilder().subject_name(existing.subject).issuer_name(existing.issuer)
                   .public_key(existing.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5))
                   .not_valid_after(expires))
        for extension in existing.extensions:
            if not isinstance(extension.value, omit):
                builder = builder.add_extension(extension.value, extension.critical)
        certificate = builder.sign(ca_key, hashes.SHA256())
        (self.directory / filename).write_bytes(certificate.public_bytes(serialization.Encoding.PEM))

    def test_generation_verifies_hostname_and_preserves_keys_on_restart(self):
        runtime.ensure_tls(self.directory, HOSTNAME)
        initial = self.snapshot()
        result = subprocess.run(["openssl", "verify", "-x509_strict", "-CAfile", str(self.directory / "ca.pem"),
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

    def test_python_strict_tls_client_verifies_real_server_handshake(self):
        runtime.ensure_tls(self.directory, HOSTNAME)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(self.directory / "server.pem", self.directory / "server.key")
        client_context = ssl.create_default_context(cafile=self.directory / "ca.pem")
        client_context.verify_flags |= ssl.VERIFY_X509_STRICT
        errors = []
        with socket.socket() as listener:
            listener.settimeout(5)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)

            def serve():
                try:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(5)
                        with server_context.wrap_socket(connection, server_side=True) as secured:
                            self.assertEqual(secured.recv(1), b"?")
                            secured.sendall(b"!")
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=serve)
            thread.start()
            try:
                with socket.create_connection(listener.getsockname(), timeout=5) as connection:
                    with client_context.wrap_socket(connection, server_hostname=HOSTNAME) as secured:
                        secured.sendall(b"?")
                        self.assertEqual(secured.recv(1), b"!")
            finally:
                thread.join(timeout=6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_legacy_leaf_identifiers_are_repaired_preserving_all_keys_and_ca(self):
        for missing in ((x509.AuthorityKeyIdentifier,), (x509.SubjectKeyIdentifier,),
                        (x509.AuthorityKeyIdentifier, x509.SubjectKeyIdentifier)):
            with self.subTest(missing=missing):
                runtime.ensure_tls(self.directory, HOSTNAME)
                self.reissue("server.pem", dt.datetime.now(dt.UTC) + dt.timedelta(days=300), omit=missing)
                initial = self.snapshot()
                runtime.ensure_tls(self.directory, HOSTNAME)
                renewed = self.snapshot()
                for filename in ("ca.pem", "ca.key", "server.key"):
                    self.assertEqual(initial[filename], renewed[filename])
                self.assertNotEqual(initial["server.pem"], renewed["server.pem"])
                result = subprocess.run(["openssl", "verify", "-x509_strict", "-CAfile",
                                         str(self.directory / "ca.pem"), "-verify_hostname", HOSTNAME,
                                         str(self.directory / "server.pem")], capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                runtime.ensure_tls(self.directory, HOSTNAME)
                self.assertEqual(renewed, self.snapshot())

    def test_source_revision_emits_only_a_full_commit_or_fixed_diagnostic(self):
        path = Path(self.temporary.name) / "SOURCE_REVISION"
        self.assertEqual(runtime.source_revision(path), "unavailable")
        for value in ("synthetic-private-value", "a" * 39, "a" * 41, "a" * 40 + "\nextra", "\N{SNOWMAN}"):
            path.write_text(value)
            self.assertEqual(runtime.source_revision(path), "unavailable")
        path.write_text("a" * 40 + "\n")
        self.assertEqual(runtime.source_revision(path), "a" * 40)

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

    def test_daily_maintenance_checks_only_when_due_and_reports_actual_renewal(self):
        runtime.ensure_tls(self.directory, HOSTNAME)
        self.reissue("server.pem", dt.datetime.now(dt.UTC) + dt.timedelta(days=10))
        initial = self.snapshot()
        with patch.object(runtime, "ensure_tls", wraps=runtime.ensure_tls) as ensure:
            deadline, changed = runtime.maintain_tls(self.directory, HOSTNAME, 100, 99)
            self.assertEqual((deadline, changed), (100, False))
            ensure.assert_not_called()
            deadline, changed = runtime.maintain_tls(self.directory, HOSTNAME, deadline, 100)
            self.assertTrue(changed)
            self.assertEqual(deadline, 100 + runtime.TLS_RECHECK_SECONDS)
            ensure.assert_called_once()
            renewed = self.snapshot()
            for filename in ("ca.pem", "ca.key", "server.key"):
                self.assertEqual(initial[filename], renewed[filename])
            self.assertNotEqual(initial["server.pem"], renewed["server.pem"])
            next_deadline, changed = runtime.maintain_tls(self.directory, HOSTNAME, deadline, deadline)
            self.assertFalse(changed)
            self.assertEqual(next_deadline, deadline + runtime.TLS_RECHECK_SECONDS)
            self.assertEqual(renewed, self.snapshot())

    def test_receiver_shutdown_reaps_stubborn_child_before_restart(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("synthetic-child", 10), 0]
        runtime.stop_process(process)
        self.assertEqual(process.mock_calls, [call.poll(), call.terminate(), call.wait(timeout=10), call.kill(), call.wait()])

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

class ArchiveConfigurationTests(unittest.TestCase):
    def test_archive_is_opt_in_and_uses_allowlisted_aliases(self):
        options={'hostname':'telemetry.example.test','vehicles':[{'vin':'SYNTHETIC_ID','slug':'test_car','name':'Test car'}]}
        service={'host':'mqtt','port':1883,'username':'synthetic_user','password':'synthetic_password'}
        receiver,_=runtime.configurations(options,service)
        self.assertIsNone(receiver['mqtt']['location_archive'])
        options['location_history']=True
        receiver,_=runtime.configurations(options,service)
        self.assertEqual(receiver['mqtt']['location_archive'],{'directory':'/share/tesla-fleet-location-history','vehicles':{'SYNTHETIC_ID':'test_car'}})
