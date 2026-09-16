"""Exercise the real Paho client against an isolated loopback MQTT test peer."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from bridge import iso_time
from test_bridge import CONFIG, TEST_VIN


def packet(kind, body=b""):
    remaining = len(body)
    length = bytearray()
    while True:
        digit = remaining % 128
        remaining //= 128
        length.append(digit | (128 if remaining else 0))
        if not remaining:
            break
    return bytes([kind]) + bytes(length) + body


def string(value):
    raw = value.encode()
    return struct.pack("!H", len(raw)) + raw


class LoopbackPeer:
    """Only the MQTT3.1.1 packets required by this smoke test; never public."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.connection = None
        self.sessions = 0
        self.publications = []
        self.subscriptions = []
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def send(self, message):
        with self.lock:
            if self.connection:
                self.connection.sendall(message)

    def publish(self, topic, payload):
        self.send(packet(0x30, string(topic) + payload))

    def disconnect(self):
        with self.lock:
            if self.connection:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                self.connection = None

    @staticmethod
    def read_exact(connection, length):
        data = bytearray()
        while len(data) < length:
            part = connection.recv(length - len(data))
            if not part:
                raise EOFError()
            data.extend(part)
        return bytes(data)

    def serve(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.sessions += 1
            session = self.sessions
            self.connection = connection
            try:
                while not self.stopping.is_set():
                    header = self.read_exact(connection, 1)[0]
                    length, multiplier = 0, 1
                    while True:
                        digit = self.read_exact(connection, 1)[0]
                        length += (digit & 127) * multiplier
                        multiplier *= 128
                        if digit < 128:
                            break
                    body = self.read_exact(connection, length)
                    kind = header >> 4
                    if kind == 1:
                        self.send(packet(0x20, b"\x00\x00"))
                    elif kind == 3:
                        size = struct.unpack("!H", body[:2])[0]
                        topic = body[2:2 + size].decode()
                        qos = (header >> 1) & 3
                        position = 2 + size
                        if qos:
                            mid = body[position:position + 2]
                            position += 2
                            self.send(packet(0x40, mid))
                        self.publications.append((session, topic, body[position:].decode(), qos, bool(header & 1)))
                    elif kind == 8:
                        position = 2
                        topics = []
                        while position < len(body):
                            size = struct.unpack("!H", body[position:position + 2])[0]
                            position += 2
                            topics.append(body[position:position + size].decode())
                            position += size + 1
                        self.subscriptions.append((session, topics))
                        self.send(packet(0x90, body[:2] + b"\x01" * len(topics)))
                    elif kind == 12:
                        self.send(packet(0xD0))
                    elif kind == 14:
                        break
            except (OSError, EOFError):
                pass
            finally:
                connection.close()
                if self.connection is connection:
                    self.connection = None

    def close(self):
        self.stopping.set()
        self.disconnect()
        self.listener.close()
        self.thread.join(timeout=3)


@unittest.skipUnless(importlib.util.find_spec("paho") is not None, "install requirements.txt for the real-client test")
class RuntimeTests(unittest.TestCase):
    def wait_for(self, condition, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for isolated MQTT behavior")

    def test_reconnect_restores_last_position_with_freshness_off(self):
        peer = LoopbackPeer()
        process = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = deepcopy(CONFIG)
                config["mqtt"].update(host="127.0.0.1", port=peer.port, username="synthetic_test_user", password="synthetic_test_password")
                config["state_file"] = str(Path(directory) / "state.json")
                config_path = Path(directory) / "config.json"
                config_path.write_text(json.dumps(config))
                process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("bridge.py")), "--config", str(config_path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                self.wait_for(lambda: len(peer.subscriptions) == 1)
                self.assertEqual(set(peer.subscriptions[0][1]), {"homeassistant/status", f"test_receiver/{TEST_VIN}/records", f"test_receiver/{TEST_VIN}/connectivity"})
                source_time = time.time_ns()

                def sample(created_at):
                    return json.dumps({"vin": TEST_VIN, "created_at": iso_time(created_at), "is_resend": False, "data": [{"key": "Location", "value": {"location_value": {"latitude": 10.0, "longitude": 20.0}}}]}).encode()

                def locations(session):
                    return [item for item in peer.publications if item[0] == session and item[1] == "test_live/test_car/location/state"]

                peer.publish(f"test_receiver/{TEST_VIN}/records", sample(source_time))
                self.wait_for(lambda: len(locations(1)) == 1)
                self.assertEqual(locations(1)[0][3:], (1, True))
                peer.disconnect()
                self.wait_for(lambda: len(peer.subscriptions) == 2)
                self.assertEqual(peer.subscriptions[0][1], peer.subscriptions[1][1])
                time.sleep(0.1)
                self.assertEqual(len(locations(2)), 1)
                self.assertEqual(json.loads(locations(2)[0][2])["observed_at"], iso_time(source_time))
                freshness = [item[2] for item in peer.publications if item[0] == 2 and item[1].endswith("/location_fresh/state")]
                self.assertTrue(freshness)
                self.assertEqual(freshness[-1], "OFF")
                second_time = time.time_ns()
                peer.publish(f"test_receiver/{TEST_VIN}/records", sample(second_time))
                self.wait_for(lambda: len(locations(2)) == 2)
                discovery_before = sum(item[1].endswith("/config") for item in peer.publications)
                peer.publish("homeassistant/status", b"online")
                self.wait_for(lambda: sum(item[1].endswith("/config") for item in peer.publications) == discovery_before + 12)
                self.wait_for(lambda: len(locations(2)) == 3)
                peer.publish(f"test_receiver/{TEST_VIN}/records", sample(second_time))
                time.sleep(0.1)
                self.assertEqual(len(locations(2)), 3)
                peer.publish(f"test_receiver/{TEST_VIN}/records", sample(time.time_ns()))
                self.wait_for(lambda: len(locations(2)) == 4)
                process.terminate()
                logs, _ = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertNotIn(TEST_VIN, logs)
                self.assertNotIn("latitude", logs)
                self.assertNotIn("synthetic_test_password", logs)
                self.assertNotIn("Traceback", logs)
        finally:
            if process and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            peer.close()


if __name__ == "__main__":
    unittest.main()
