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
timestamps plus the latest valid GPS coordinates and their original source time.
It contains no VIN, tokens, connection IDs, or complete history. Treat it as
private location data and include app data in backups. A
corrupt file prevents startup; a write failure stops the process with exit 2.
Do not delete this file merely to clear an error: investigate and preserve the
recorded fences. The standalone `Bridge` class permits no state file for unit
tests, but production CLI use always supplies one.

## Entity and freshness behavior

For slug `example_car`, initial discovery requests these IDs:

| Entity | Meaning |
| --- | --- |
| `device_tracker.example_car_live_location` | Last known valid GPS fix; Home Assistant calculates zones from coordinates |
| `sensor.example_car_live_speed` | VehicleSpeed in mph, Home Assistant can convert display units |
| `sensor.example_car_live_last_update` | Original timestamp of the newest accepted supported field |
| `binary_sensor.example_car_live_location_fresh` | A new, valid GPS fix within the freshness window |
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
GPS fix fresh. Explicit invalid measurements make live fields unavailable and GPS freshness
false; the last valid GPS coordinates and their timestamp remain intact. The bridge rejects unknown vehicles, envelope/topic VIN
mismatches, retained input, resent records, delayed records, excessive future
timestamps, and duplicate/out-of-order field observations. Exact nanosecond
ordering survives bridge restarts. An expired fix cannot become fresh again
from clock correction or another field's update.

Location stays available after its freshness limit or a vehicle disconnect. Its
`observed_at` attribute always identifies the original fix, not the restore or
receipt time. Speed still expires after the freshness limit. Fleet Telemetry
sends changed values: a parked car can have quiet GPS while connected.
`location_fresh` distinguishes recent GPS from a saved position; battery or speed
updates cannot turn it on. `connected` describes the receiver connection, and
`telemetry_fresh` describes any recent supported observation.

The bridge never publishes `home` or `not_home`, or maps an offline connection
to away. For arrival automations require `location_fresh=on`, a recent GPS
`observed_at`, and a real previous numeric distance/known zone outside the arrival
boundary. A startup or restored position is not proof of arrival. Proximity
calculations using the tracker describe the last known position when GPS is old.

State schema 2 restores the last valid position after restart, with GPS freshness
off until a newer accepted observation arrives. Schema 1 timestamp fences migrate
without being discarded. When the app has location history enabled, missing
last-known fixes are seeded from the newest valid source timestamp in that car's
private archive, including imported Recorder snapshots. Invalid/future records,
wrong aliases, symlinks and incomplete tails are ignored. This recovery does not
replay history into the live measurement stream or mark archived samples fresh.
Existing saved fixes skip archive scanning on subsequent starts.

## Reconnect and privacy

- Discovery and last-known GPS are retained at QoS 1. Scalar live values remain
  non-retained at QoS 0. GPS carries its original source timestamp; MQTT delivery
  or restoration must never be used as its observation time.
- A retained MQTT last will marks live diagnostics unavailable on bridge loss.
  Last-known GPS availability is independent of that live-path status. Broker
  reconnect and Home Assistant birth restore the last position with freshness
  off and invalidate other live measurements until newer source timestamps arrive.
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
  reliable ACK confirms broker acceptance; this bridge persists its latest accepted fix separately from
  the optional full receiver archive.

## Verification

```sh
python3 -m unittest -v test_bridge.py test_runtime.py
```

Run from this directory. Unit tests use synthetic identity/coordinates and
require only Python. The runtime smoke test requires `requirements.txt`, starts
the real Paho client against a test peer bound to loopback, disconnects it, and
verifies subscription/discovery recovery and stale last-position restoration. No Tesla, Home
Assistant, or production broker is contacted. This does not replace checking
actual Home Assistant discovery and live vehicle updates after deployment.

Primary references:

- [Tesla available telemetry data](https://developer.tesla.com/docs/fleet-api/fleet-telemetry/available-data)
- [Tesla telemetry behavior](https://developer.tesla.com/docs/fleet-api/fleet-telemetry)
- [Home Assistant MQTT GPS tracker](https://www.home-assistant.io/integrations/device_tracker.mqtt/)
- [Home Assistant MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
