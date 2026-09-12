# Home Assistant Fleet Telemetry commissioner

This is a manual, one-shot helper for the existing native Home Assistant Tesla
Fleet account. Run it in a separate commissioning app with no published ports.
Mount `/homeassistant_config` and `/ssl` read-only and keep `/data` private. Set
the explicit `path` on both app mappings: Supervisor's default for
`homeassistant_config` is `/homeassistant`. Startup checks mount flags and
reports only prerequisite booleans before reading the native configuration. The
runtime needs Python 3.11+, OpenSSL, and the official `tesla-http-proxy` binary.

The helper reads the saved access token in place. It does not refresh tokens,
write Home Assistant storage, rotate keys, wake vehicles, operate vehicle
controls, or change billing. Start Home Assistant's normal token renewal flow
if the helper reports that its access token needs renewal.

Configure the app's `/data/options.json` with your own values:

```json
{
  "mode": "inspect",
  "vehicle_names": ["my-car"],
  "hostname": "telemetry.example.com",
  "port": 8443,
  "ca_file": "/ssl/tesla-fleet-stream/ca.pem",
  "replace_existing": false
}
```

Run `python3 commissioner.py`. There are three modes:

- `inspect`: check the named vehicles' key pairing, firmware, telemetry
  capability, current application configuration, and synchronization status.
- `configure`: run that preflight for every named vehicle, back up the existing
  configurations, then send one signed request for vehicles requiring changes.
- `get_errors`: report only counts of known TLS, DNS, connection, rate-limit,
  and other error categories. Raw errors and identifiers are not printed.

Vehicle names must resolve uniquely in Tesla's vehicle list. A full 100-vehicle
page is deliberately rejected because this personal-use helper does not guess
pagination parameters. North American and European Fleet API audiences are
supported; arbitrary endpoints and redirects are rejected.

The configuration sends only `Location` and `VehicleSpeed` at 5-second minimum
intervals, `Gear` at 10 seconds, and `Soc` at 60 seconds. These are minimum
intervals for changed values, not a promise of continuous transmission or a
fixed bill. Verify the receiver, direct mTLS route, and desired data access
before selecting `configure`.

The configure preflight requires the vehicle-data, location, and vehicle-command
OAuth scopes, matching application key pairing, a reported telemetry client,
and firmware at least 2024.26. Tesla's response remains authoritative about
hardware eligibility. A different existing configuration stops the entire batch
unless `replace_existing` is explicitly enabled. Matching configurations produce
no write. Comparisons ignore only the signing proxy's public `iss` and `aud`
claims; unknown extra fields remain a difference requiring review.

Before a write, the helper creates a new mode-0600 backup in
`/data/commissioner/backups/`, with directory mode 0700. Backups contain vehicle
identifiers and the previous application configuration for recovery, but no
OAuth tokens or private keys. Preserve these files on the HA host and out of
Git. Restoring or removing a configuration is an explicit separate operation;
the helper does not attempt automatic rollback after a partial or uncertain
response. Run `inspect` to establish the current state before retrying.

For the single signed request, the official proxy runs on `localhost:4443` and
receives `/homeassistant_config/tesla_fleet.key` through `-key-file`. A separate
short-lived localhost TLS key and certificate are generated in a private
temporary directory, and the Python client verifies that certificate. The
vehicle signing key is never copied. Proxy output is discarded because upstream
errors can include identifiers. The proxy is terminated and temporary TLS files
removed when the request finishes or fails.

A successful write and matching readback do not establish vehicle adoption:
`configuration_synced` must become true. They also do not prove the receiver has
received live data. Verify those boundaries separately without waking a vehicle.

Synthetic verification:

```sh
cd homeassistant/commissioner
python3 -m unittest -v
```

References: [Tesla vehicle endpoints](https://developer.tesla.com/docs/fleet-api/endpoints/vehicle-endpoints),
[official proxy configuration](https://github.com/teslamotors/vehicle-command#running-the-proxy-server),
[proxy signing implementation](https://github.com/teslamotors/vehicle-command/blob/main/pkg/proxy/proxy.go).
