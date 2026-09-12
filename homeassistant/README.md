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
