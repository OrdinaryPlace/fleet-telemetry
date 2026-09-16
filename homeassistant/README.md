# Home Assistant streaming

This optional deployment combines Tesla's Fleet Telemetry receiver with a local
MQTT bridge. It creates separate **Live** devices while the native Tesla Fleet
integration continues to provide its existing controls.

```
Vehicle → direct DNS / TCP 8443 → Tesla-authenticated mTLS receiver
                                         ↓
                           private Mosquitto → bridge → HA MQTT discovery
```

The additive receiver option `mqtt.publish_vehicle_records` defaults to false.
It preserves source timestamps and resend metadata needed to distinguish live
locations from buffered history. See the [MQTT guide](../datastore/mqtt/README.md).

## Requirements and installation

- Home Assistant OS/Supervised on amd64 with the official Mosquitto app.
- A connected native Tesla Fleet entry and its original signing key.
- Compatible vehicle firmware, paired virtual key, and the required Tesla scopes.
- A public DNS name reaching the receiver directly on TCP 8443, with dynamic DNS
  maintenance if the home's public IP changes. A conventional HTTP reverse proxy
  or Cloudflare HTTP tunnel cannot terminate vehicle mTLS.

Only the receiver port should be exposed. MQTT, metrics and the signing proxy
stay internal. The receiver trusts Tesla's embedded production vehicle roots;
do not add the server's own CA as an accepted vehicle client CA.

Create fresh build directories from a clean reviewed checkout:

```sh
python3 homeassistant/scripts/build_context.py /path/to/new-receiver-context
python3 homeassistant/scripts/build_context.py /path/to/new-setup-context --kind commissioner
```

Use destinations outside the source repository. The builder accepts only
committed app/bridge runtime files and tracked receiver source, and rejects
symlinks or selected runtime files that differ from the reviewed commit. It
reads committed blobs, so ignored/untracked local files cannot enter a context.
Unrelated edits to this Home Assistant documentation do not affect the selected
runtime. Both contexts include `SOURCE_REVISION` identifying their exact source.

Copy the contexts to separate directories under HA `/addons`, reload the app
store, and install **Tesla Fleet Stream** and **Tesla Fleet Stream Setup** as
local apps. Docker can also build each context for validation. The receiver uses
Go 1.26; the commissioning proxy is pinned to v0.4.1.

Start Mosquitto with its host port mappings disabled. Set the receiver hostname
and explicit vehicle VIN/name/slug list privately in HA app options. Never commit
a real mapping, token or TLS material. Keep slugs stable for entity identity.
Start the receiver, enable its watchdog, and verify its listener/certificate
before forwarding the one public port.

MQTT credentials come from Supervisor. A dedicated CA and server key stay under
`/ssl/tesla-fleet-stream`; the native signing key is neither copied nor replaced.
The leaf certificate is checked daily and renewed with the same CA/key before
expiry. Only the receiver restarts after renewal. Preserve the CA and backups.

## Commission and verify

The setup app is manual-only and exits after each action. Configure the intended
vehicle names, receiver hostname/port and public CA path, then choose its mode:

- `mqtt_setup`: completes the official Mosquitto integration flow if absent.
- `inspect`: checks vehicle pairing, firmware and existing stream configuration.
- `configure`: backs up the old configuration, signs the request with Tesla's
  official proxy, and reads the resulting configuration back.
- `get_errors`: reports fixed categories of Tesla connection errors.
- `ha_status`: reports integration/entity availability without GPS values.

The initial fields are Location and VehicleSpeed at five-second intervals, Gear
at ten seconds, and Soc at sixty seconds. These are change-driven maximum
frequencies, not a promise of a message every interval. Configuration acceptance,
vehicle synchronization and actual records reaching HA are separate checks.
Verify a real vehicle sample updates HA before claiming successful streaming.
Do not operate unrelated vehicle controls just to test the installation.

Receiver 0.4.0 adds an enabled live driver-presence binary sensor. In setup 0.2.0,
opt into `driver_presence: true` to add `DriverSeatOccupied` at a five-second
minimum interval, preserving every other stream field and connection setting.
The commissioner permits only this exact additive extension unless broader
replacement is explicitly selected. Driver presence is distinct from Tesla's
polled User present sensor and does not identify individual passengers.
Setup `seat_belts: true` also adds driver and Tesla-reported rear-center belt
states at five-second change intervals. The bridge uses Tesla's documented
boolean polarity and BuckleStatus enum, and keeps missing/invalid data distinct.

Different existing configuration is preserved unless replacement is explicitly
selected. The setup app reads a valid native access token in place; an expired
token must be refreshed by the native integration. It never refreshes tokens
independently or writes authentication storage. Preserve its rollback backup.

## Entities and freshness

See the [bridge guide](bridge/README.md) for exact entity and delivery semantics.
Location and speed expire after 90 seconds without a new valid source sample.
Connection and freshness are separate: a parked car may remain connected while
its unchanged location becomes unavailable. Buffered, retained, future and
out-of-order fixes are rejected. Restart fences persist timestamps only.

Home Assistant's recorder can store locations, as with other trackers; apply the
household retention policy there. Arrival automations should use the Live
tracker and a fresh-fix condition, accounting for startup and unavailable-to-home
transitions before attaching consequential actions. Do not mix native polling
and live fixes into a single location source.

## Tests and rollback

Run `go test ./datastore/mqtt` and `go test -race ./datastore/mqtt`. Python tests
are colocated in `bridge`, `commissioner`, and `tests`; dependencies are listed in
the app Dockerfiles and bridge requirements. All test inputs are synthetic.

Stop the receiver and disable only its exact network forward to stop streaming.
Restore this application's previous telemetry configuration from its backup when
necessary. Preserve native Fleet credentials, virtual key, controls, existing HA
routes and other MQTT consumers. Make verified HA backups before and after
deployment.

## Optional permanent location archive (receiver 0.2.0)

Set `location_history: true` to retain every received Location datum for the configured vehicles. The default is false. The receiver saves UTC source and receipt times, the configured vehicle alias, resend status, and the complete typed Location value before publishing to MQTT or acknowledging the vehicle. Invalid measurements and delayed/resend records are kept; retransmissions can appear more than once. No VIN or other telemetry fields are written to the archive. GPS freshness/replay protection remains independent of the last-known display introduced in receiver 0.3.0.

The private directory `/share/tesla-fleet-location-history` contains daily per-vehicle NDJSON files, mode 600 inside a mode 700 directory. There is no automatic purge. Include the **share** folder in HA backups; backing up only the app does not include a shared-folder archive. These files contain precise locations: keep them outside Git, static web directories, and ordinary logs. A full disk or failed write prevents the affected location record from being acknowledged; restore storage health rather than deleting history automatically.

Open the app's **Open Web UI** to download daily CSV or original NDJSON through authenticated Home Assistant Ingress. Port 8099 has no host port mapping and rejects requests not originating from Supervisor Ingress. No new WAN port is needed. CSV preserves zero coordinates, leaves invalid coordinates empty, and skips interrupted rows; the original download preserves all bytes for recovery. Raw files append after a restart and separate incomplete tails without overwriting them.

This archive starts when enabled; it cannot recreate GPS records that were never received. Home Assistant Recorder history can be imported separately as clearly identified snapshots, with its original state timestamp distinguished from a Tesla source timestamp.
