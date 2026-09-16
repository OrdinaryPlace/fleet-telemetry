"""Pure validation and conversion of the bridge's durable status snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
import re

from .status_fields import SPECS, NATIVE_KEYS, decode_status

RFC3339 = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$")
REQUIRED = frozenset({"DoorState", "Locked", "ChargePortDoorOpen", "FdWindow", "FpWindow", "RdWindow", "RpWindow"})
# These are capabilities/limits, not periodically changing physical status.
CAPABILITIES = frozenset({"climate_state_min_avail_temp", "climate_state_max_avail_temp",
                         "charge_state_charge_limit_soc_min", "charge_state_charge_limit_soc_max"})


def timestamp(value):
    match = RFC3339.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("invalid source timestamp")
    seconds = int(datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00")).timestamp())
    return seconds * 1_000_000_000 + int((match[2] or "0").ljust(9, "0"))


class StatusModel:
    def __init__(self, slug):
        self.slug = slug
        self.fields = {}
        self.invalid = set()
        self.connection = None
        self.connection_time = 0
        self.revision = 0
        self.envelope_time = 0

    def accept(self, snapshot, now):
        """Validate the entire envelope before changing state; never log values."""
        if not isinstance(snapshot, dict) or snapshot.get("schema") != 1 or snapshot.get("vehicle") != self.slug:
            raise ValueError("invalid status envelope")
        fields = snapshot.get("fields")
        invalid = snapshot.get("invalid_fields", [])
        if not isinstance(fields, dict) or not 1 <= len(fields) <= len(SPECS) or not set(fields) <= SPECS.keys():
            raise ValueError("invalid status fields")
        if not isinstance(invalid, list) or any(not isinstance(k, str) or k not in SPECS for k in invalid):
            raise ValueError("invalid status quality")
        decoded = {}
        envelope_time = timestamp(snapshot.get("observed_at"))
        if envelope_time / 1e9 > now + 5:
            raise ValueError("future status envelope")
        for name, item in fields.items():
            if not isinstance(item, dict) or set(item) != {"value", "observed_at"}:
                raise ValueError("invalid status datum")
            observed = timestamp(item["observed_at"])
            if observed / 1e9 > now + 5:
                raise ValueError("future status datum")
            patch = decode_status(name, item["value"])
            decoded[name] = (observed, patch)
            if observed > envelope_time:
                raise ValueError("inconsistent source timestamp")
        connected = snapshot.get("connection")
        if connected is not None and type(connected) is not bool:
            raise ValueError("invalid connection state")
        connection_time = snapshot.get("connection_observed_at")
        connection_time = timestamp(connection_time) if connection_time is not None else 0
        if connection_time / 1e9 > now + 5 or connection_time > envelope_time:
            raise ValueError("future connection state")
        if envelope_time < self.envelope_time:
            return False
        # Validate all equal-time conflicts before applying any newer fields.
        for name, item in decoded.items():
            old = self.fields.get(name)
            if old is not None and item[0] == old[0] and item[1] != old[1]:
                raise ValueError("conflicting source timestamp")
        changed = False
        for name, item in decoded.items():
            old = self.fields.get(name)
            if old is None or item[0] > old[0]:
                self.fields[name] = item
                changed = True
        if connection_time >= self.connection_time and (connected, connection_time) != (self.connection, self.connection_time):
            self.connection, self.connection_time = connected, connection_time
            changed = True
        if set(invalid) != self.invalid:
            self.invalid = set(invalid)
            changed = True
        if changed:
            self.revision += 1
        self.envelope_time = envelope_time
        return changed

    @property
    def missing(self):
        return sorted(k for k in REQUIRED if k not in self.fields or
                      any(v is None for v in self.fields[k][1].values()))

    @property
    def ready(self):
        return not self.missing

    @property
    def observed(self):
        return max((item[0] for item in self.fields.values()), default=0)

    def native_data(self, base, now):
        """Keep static capabilities; all physical values come from the stream.

        Original source times remain in diagnostics. Quiet fields are last-known,
        never re-timestamped as live. Unsupported polled fields become unknown.
        """
        if not self.ready:
            raise ValueError("required status has not been observed")
        data = {k: v for k, v in base.items() if k.startswith(("vehicle_config_", "gui_settings_"))
                or k in CAPABILITIES or not k.startswith(("drive_state_", "vehicle_state_", "charge_state_", "climate_state_"))}
        data.update(dict.fromkeys(NATIVE_KEYS))
        for name, (observed, patch) in self.fields.items():
            if name in ("ACChargingPower", "DCChargingPower"):
                continue
            patch = dict(patch)
            if name in ("TimeToFullCharge", "MinutesToArrival"):
                for key, minutes in patch.items():
                    if minutes is not None:
                        patch[key] = max(0, minutes - max(0, now - observed / 1e9) / 60)
            data.update(patch)
        fast = data.get("charge_state_fast_charger_present")
        power = self.fields.get("DCChargingPower" if fast is True else "ACChargingPower") if fast is not None else None
        if power is not None:
            data.update(power[1])
        if data.get("vehicle_config_rhd") is True:
            left, right = data.get("climate_state_driver_temp_setting"), data.get("climate_state_passenger_temp_setting")
            data["climate_state_driver_temp_setting"], data["climate_state_passenger_temp_setting"] = right, left
        # No reliable telemetry field reports the software updater's state or
        # schedule. Do not infer availability/installing from download progress.
        data["vehicle_state_software_update_status"] = None
        data["vehicle_state_software_update_scheduled_time_ms"] = None
        # An authenticated connection or fresh sample proves online. Disconnect
        # proves only loss of stream, not sleep; commands can use native wake logic.
        data["state"] = "online" if self.connection is True or (self.connection is not False and -5 <= now - self.observed / 1e9 <= 90) else "offline"
        data["_tesla_stream_source"] = "fleet_telemetry"
        return data

    def activity_changes(self, previous, previous_invalid, now):
        """Selected fresh transitions only; no initial, replay or position events.

        Called only after accepting a non-retained snapshot. Previous values are
        a local baseline, never REST state. Explicit no-offer may become a new
        software offer; unknown physical values never become edges.
        """
        events = []
        for field in ("DetailedChargeState", "DoorState", "TpmsSoftWarnings", "SoftwareUpdateVersion"):
            old, new = previous.get(field), self.fields.get(field)
            if old is None or new is None or field in self.invalid or new[0] <= old[0]:
                continue
            if not -5 <= now - new[0] / 1e9 <= 30:
                continue
            before, after = old[1], new[1]
            if field != "SoftwareUpdateVersion" and (field in previous_invalid or any(v is None for v in before.values())):
                continue
            observed = datetime.fromtimestamp(new[0] / 1e9, timezone.utc).isoformat()
            common = {"observed_at": observed, "source": "Tesla Fleet Telemetry"}
            if field == "DetailedChargeState":
                key = "charge_state_charging_state"
                if after[key] == "Complete" and before[key] != "Complete":
                    events.append(common | {"kind": "charge_complete"})
            elif field == "DoorState":
                for suffix, position in (("df", "front_driver"), ("pf", "front_passenger"), ("dr", "rear_driver"), ("pr", "rear_passenger")):
                    key = "vehicle_state_" + suffix
                    if before[key] == 0 and after[key] == 1:
                        events.append(common | {"kind": "door_opened", "position": position})
            elif field == "TpmsSoftWarnings":
                for suffix, position in (("fl", "front_left"), ("fr", "front_right"), ("rl", "rear_left"), ("rr", "rear_right")):
                    key = "vehicle_state_tpms_soft_warning_" + suffix
                    if before[key] is False and after[key] is True:
                        pressure = self.fields.get("TpmsPressure" + suffix.title())
                        # Preserve pressure age; tires can report quietly while parked.
                        pressure_value = pressure[1].get("vehicle_state_tpms_pressure_" + suffix) if pressure and "TpmsPressure" + suffix.title() not in self.invalid else None
                        events.append(common | {"kind": "tire_pressure_warning", "position": position,
                                                "pressure_bar": pressure_value,
                                                "pressure_observed_at": datetime.fromtimestamp(pressure[0] / 1e9, timezone.utc).isoformat() if pressure_value is not None else None})
            else:
                key = "vehicle_state_software_update_version"
                version = after[key]
                installed = self.fields.get("Version")
                current = installed[1].get("vehicle_state_car_version") if installed else None
                if version and version.strip() and version != before[key] and version.split()[0] != (current or "").split(" ")[0]:
                    events.append(common | {"kind": "software_update_available", "version": version})
        return events

    def summary(self):
        return {"field_count": len(self.fields), "missing_required_fields": self.missing,
                "invalid_fields": sorted(self.invalid),
                "observed_at": datetime.fromtimestamp(self.observed / 1e9, timezone.utc).isoformat() if self.observed else None,
                "source": "Tesla Fleet Telemetry", "status_semantics": "last reported"}
