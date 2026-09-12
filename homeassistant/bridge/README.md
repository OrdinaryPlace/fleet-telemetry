# Fleet Telemetry Home Assistant bridge

This bridge creates a separate **Vehicle name Live** MQTT device with a GPS
tracker, speed sensor, last vehicle update timestamp, and connection/freshness
diagnostics. Optional battery and gear sensors are discovered disabled by
default. Native Tesla Fleet entities and controls are unchanged.

The receiver must enable `mqtt.publish_vehicle_records: true`. The bridge
consumes the non-retained `<topic_base>/<VIN>/records` envelope, preserving the
vehicle's original `created_at`, `is_resend`, and field values. Scalar MQTT
topics cannot supply the timestamp information needed for safe arrival use.

## Runtime interface

Install `requirements.txt` in the runtime, and start:

```sh
python3 /opt/bridge/bridge.py --config /run/bridge.json
```

The add-on generates that private configuration from Supervisor MQTT service
credentials and its vehicle allowlist. The file must be readable only by the
runtime owner. Never commit an actual vehicle mapping or broker credentials.
The following is an illustrative template; replace placeholders privately:

```json
{
  "mqtt": {
    "host": "core-mosquitto",
    "port": 1883,
    "username": "<runtime-service-username>",
    "password": "<runtime-service-password>",
    "topic_base": "tesla",
    "state_prefix": "tesla_live",
    "discovery_prefix": "homeassistant",
    "client_id": "tesla-live-bridge"
  },
  "vehicles": [
    {"vin": "<configured-vehicle-VIN>", "slug": "example_car", "name": "Example car"}
  ],
  "state_file": "/data/bridge-state.json",
  "freshness_seconds": 90,
  "max_transport_delay_seconds": 30,
  "future_tolerance_seconds": 5,
  "tick_seconds": 5
}
```

Use `mqtt.tls: true` when connecting through verified TLS; system certificate
roots and hostname checking remain enabled. Plain MQTT is intended for the
private Supervisor network only. The receiver is the public mTLS boundary;
neither MQTT nor this bridge needs public ingress.

`state_file` defaults to `/data/bridge-state.json` in the CLI. The directory must
already exist. The bridge atomically persists nanosecond timestamp fences before
publishing a state, with mode 0600. The file contains stable slugs and source
timestamps only: no VIN, coordinates, tokens, connection IDs, or history. A
corrupt file prevents startup; a write failure stops the process with exit 2.
Do not delete this file merely to clear an error: investigate and preserve the
recorded fences. The standalone `Bridge` class permits no state file for unit
tests, but production CLI use always supplies one.

## Entity and freshness behavior

For slug `example_car`, initial discovery requests these IDs:

| Entity | Meaning |
| --- | --- |
| `device_tracker.example_car_live_location` | Fresh GPS fix; Home Assistant calculates zones from coordinates |
| `sensor.example_car_live_speed` | VehicleSpeed in mph, Home Assistant can convert display units |
| `sensor.example_car_live_last_update` | Original timestamp of the newest accepted supported field |
| `binary_sensor.example_car_live_telemetry_fresh` | At least one accepted measurement is recent |
| `binary_sensor.example_car_live_connected` | Latest matching receiver connection event |
| `sensor.example_car_live_battery` | BatteryLevel percentage; disabled initially |
| `sensor.example_car_live_usable_battery` | Soc usable charge percentage; disabled initially |
| `sensor.example_car_live_gear` | P, R, N, or D; disabled initially |

These IDs are defaults; Home Assistant may assign a suffix if an ID already
exists, and user renames remain authoritative. Devices use stable slug-based
identifiers independent of the native Tesla Fleet integration. Keep each slug
stable for the same physical vehicle.

Each field retains its own source timestamp. A speed update cannot make an old
GPS fix fresh. Explicit invalid measurements and invalid coordinate ranges make
the field unavailable. The bridge rejects unknown vehicles, envelope/topic VIN
mismatches, retained input, resent records, delayed records, excessive future
timestamps, and duplicate/out-of-order field observations. Exact nanosecond
ordering survives bridge restarts. An expired fix cannot become fresh again
from clock correction or another field's update.

Location and speed become unavailable after their freshness limit, checked every
`tick_seconds`. Fleet Telemetry sends changed values: a stationary vehicle can
have a quiet GPS field while its connection remains open. Consequently, a quiet
parked location intentionally becomes unavailable; **connected** does not
expire merely because GPS is unchanged. **Telemetry fresh** describes recent
observations, while **connected** describes the last receiver connection event.
Neither metric is a guarantee of the current physical vehicle state.

The bridge never publishes `home` or `not_home` to the GPS tracker, and never
maps an offline connection to away. Its availability becomes unavailable when
the fix is stale or the live path disconnects. For arrival automations, use this
tracker as the sole vehicle location source, require a fresh fix, and avoid
treating `unknown`/`unavailable` recovery as proof of an arrival. Proximity
distance/direction still depend on the freshness of the selected tracker.

## Reconnect and privacy

- Discovery is retained at QoS 1. GPS and scalar states are non-retained at QoS
  0, so Paho cannot replay old in-flight GPS messages after reconnect. Timestamp
  and availability diagnostics may be retained, always with their source time.
  Loss of a live state is recovered by a later new measurement, never by
  replaying a previous GPS fix as current.
- A retained MQTT last will marks the bridge unavailable on connection loss.
  Broker reconnect and Home Assistant's birth message republish discovery and
  invalidate cached live measurements until new source timestamps arrive.
  Connection status is unknown until a new connection event after resync.
- Connectivity events are fenced by event time and connection ID; a delayed
  disconnect for a previous connection cannot take down a newer session.
- Only the allowlisted receiver topics and Home Assistant birth topic are
  subscribed. Raw MQTT topics contain VINs on the private broker; discovery and
  normalized Home Assistant topics use slugs.
- Logs contain generic lifecycle messages and aggregate rejection counts only.
  Raw payloads, coordinates, VINs, MQTT credentials, and connection IDs are never
  logged. Do not enable Paho packet logging or raw receiver logging.
- MQTT receipt is not end-to-end vehicle-to-Home-Assistant durability. Receiver
  reliable ACK confirms broker acceptance; this bridge intentionally prioritizes
  current observations over replaying a location after an outage.

## Verification

```sh
python3 -m unittest -v test_bridge.py test_runtime.py
```

Run from this directory. Unit tests use synthetic identity/coordinates and
require only Python. The runtime smoke test requires `requirements.txt`, starts
the real Paho client against a test peer bound to loopback, disconnects it, and
verifies subscription/discovery recovery without GPS replay. No Tesla, Home
Assistant, or production broker is contacted. This does not replace checking
actual Home Assistant discovery and live vehicle updates after deployment.

Primary references:

- [Tesla available telemetry data](https://developer.tesla.com/docs/fleet-api/fleet-telemetry/available-data)
- [Tesla telemetry behavior](https://developer.tesla.com/docs/fleet-api/fleet-telemetry)
- [Home Assistant MQTT GPS tracker](https://www.home-assistant.io/integrations/device_tracker.mqtt/)
- [Home Assistant MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
