"""Reviewed Fleet Telemetry -> native Home Assistant Fleet data mapping.

No credentials, account identifiers, HTTP calls or commands. Units are the
vehicle_data units expected by HA, not the user's display units. See Tesla's
available-data table and protos/vehicle_data.proto for the typed wire values.
"""
from __future__ import annotations

import math


SPECS = {}


def scalar(field, key, kind, low=None, high=None, interval=30, scale=1):
    SPECS[field] = {"keys": (key,), "kind": kind, "low": low,
                    "high": high, "interval": interval, "scale": scale}


def enum(field, key, wire, values, interval=5):
    SPECS[field] = {"keys": (key,), "kind": "enum", "wire": wire,
                    "values": values, "interval": interval}


for field, key, low, high, interval in (
    ("BatteryLevel", "charge_state_battery_level", 0, 100, 60),
    ("Soc", "charge_state_usable_battery_level", 0, 100, 60),
    ("RatedRange", "charge_state_battery_range", 0, 1500, 60),
    ("EstBatteryRange", "charge_state_est_battery_range", 0, 1500, 60),
    ("IdealBatteryRange", "charge_state_ideal_battery_range", 0, 1500, 60),
    ("DCChargingEnergyIn", "charge_state_charge_energy_added", 0, 1000, 30),
    ("ChargeAmps", "charge_state_charger_actual_current", 0, 1000, 10),
    ("ChargerVoltage", "charge_state_charger_voltage", 0, 1500, 30),
    ("ChargeRateMilePerHour", "charge_state_charge_rate", 0, 3000, 30),
    ("ChargeCurrentRequest", "charge_state_charge_current_request", 0, 1000, 5),
    ("ChargeCurrentRequestMax", "charge_state_charge_current_request_max", 0, 1000, 30),
    ("ChargeLimitSoc", "charge_state_charge_limit_soc", 0, 100, 5),
    ("ChargerPhases", "charge_state_charger_phases", 0, 3, 30),
    ("InsideTemp", "climate_state_inside_temp", -80, 100, 30),
    ("OutsideTemp", "climate_state_outside_temp", -80, 100, 30),
    ("HvacLeftTemperatureRequest", "climate_state_driver_temp_setting", 10, 35, 5),
    ("HvacRightTemperatureRequest", "climate_state_passenger_temp_setting", 10, 35, 5),
    ("VehicleSpeed", "drive_state_speed", 0, 400, 5),
    ("Odometer", "vehicle_state_odometer", 0, 10000000, 60),
    ("GpsHeading", "drive_state_heading", 0, 360, 10),
    ("MilesToArrival", "drive_state_active_route_miles_to_arrival", 0, 30000, 10),
    ("EstimatedHoursToChargeTermination", "charge_state_hours_to_charge_limit", 0, 1000, 30),
    ("MinutesToArrival", "drive_state_active_route_minutes_to_arrival", 0, 100000, 10),
    ("RouteTrafficMinutesDelay", "drive_state_active_route_traffic_minutes_delay", 0, 10000, 30),
    ("ExpectedEnergyPercentAtTripArrival", "drive_state_active_route_energy_at_arrival", -100, 100, 30),
    ("MediaAudioVolume", "vehicle_state_media_info_audio_volume", 0, 100, 5),
    ("MediaAudioVolumeMax", "vehicle_state_media_info_audio_volume_max", 1, 100, 60),
    ("MediaAudioVolumeIncrement", "vehicle_state_media_info_audio_volume_increment", 0, 100, 60),
    ("MediaNowPlayingDuration", "vehicle_state_media_info_now_playing_duration", 0, 1000000000, 15),
    ("MediaNowPlayingElapsed", "vehicle_state_media_info_now_playing_elapsed", 0, 1000000000, 15),
    ("SoftwareUpdateDownloadPercentComplete", "vehicle_state_software_update_download_perc", 0, 100, 30),
    ("SoftwareUpdateInstallationPercentComplete", "vehicle_state_software_update_install_perc", 0, 100, 30),
):
    scalar(field, key, "number", low, high, interval)

for field, key, high in (
    ("SeatHeaterLeft", "seat_heater_left", 3),
    ("SeatHeaterRight", "seat_heater_right", 3),
    ("SeatHeaterRearLeft", "seat_heater_rear_left", 3),
    ("SeatHeaterRearCenter", "seat_heater_rear_center", 3),
    ("SeatHeaterRearRight", "seat_heater_rear_right", 3),
    ("HvacSteeringWheelHeatLevel", "steering_wheel_heat_level", 2),
):
    scalar(field, "climate_state_" + key, "integer", 0, high, 5)

for corner, suffix in (("Fl", "fl"), ("Fr", "fr"), ("Rl", "rl"), ("Rr", "rr")):
    scalar("TpmsPressure" + corner, "vehicle_state_tpms_pressure_" + suffix,
           "number", 0, 10, 60)

for field, key in (
    ("AutoSeatClimateLeft", "climate_state_auto_seat_climate_left"),
    ("AutoSeatClimateRight", "climate_state_auto_seat_climate_right"),
    ("HvacSteeringWheelHeatAuto", "climate_state_auto_steering_wheel_heat"),
    ("BatteryHeaterOn", "charge_state_battery_heater_on"),
    ("PreconditioningEnabled", "charge_state_preconditioning_enabled"),
    ("ScheduledChargingPending", "charge_state_scheduled_charging_pending"),
    ("ChargePortDoorOpen", "charge_state_charge_port_door_open"),
    ("ChargeEnableRequest", "charge_state_charge_enable_request"),
    ("FastChargerPresent", "charge_state_fast_charger_present"),
    ("HomelinkNearby", "vehicle_state_homelink_nearby"),
    ("LocatedAtHome", "vehicle_state_located_at_home"),
    ("Locked", "vehicle_state_locked"),
    ("DriverSeatOccupied", "vehicle_state_is_user_present"),
    ("RightHandDrive", "vehicle_config_rhd"),
):
    scalar(field, key, "boolean", interval=5)

for field, key, interval in (
    ("Version", "vehicle_state_car_version", 300),
    ("SoftwareUpdateVersion", "vehicle_state_software_update_version", 60),
    ("DestinationName", "drive_state_active_route_destination", 10),
    ("MediaNowPlayingAlbum", "vehicle_state_media_info_now_playing_album", 5),
    ("MediaNowPlayingArtist", "vehicle_state_media_info_now_playing_artist", 5),
    ("MediaNowPlayingStation", "vehicle_state_media_info_now_playing_station", 5),
    ("MediaNowPlayingTitle", "vehicle_state_media_info_now_playing_title", 5),
    ("MediaPlaybackSource", "vehicle_state_media_info_now_playing_source", 5),
):
    scalar(field, key, "string", interval=interval)

scalar("TimeToFullCharge", "charge_state_minutes_to_full_charge", "number", 0, 1000, 30, 60)
scalar("SoftwareUpdateExpectedDurationMinutes", "vehicle_state_software_update_expected_duration_sec",
       "number", 0, 1440, 60, 60)

enum("DetailedChargeState", "charge_state_charging_state", "detailed_charge_state_value",
     {"DetailedChargeState" + s: s for s in ("Disconnected", "NoPower", "Starting", "Charging", "Complete", "Stopped")})
enum("ChargePortLatch", "charge_state_charge_port_latch", "charge_port_latch_value",
     {"ChargePortLatch" + s: s for s in ("Disengaged", "Engaged", "Blocking")})
enum("ChargingCableType", "charge_state_conn_charge_cable", "cable_type_value",
     {"CableType" + s: s for s in ("IEC", "SAE", "GB_AC", "GB_DC")})
enum("FastChargerType", "charge_state_fast_charger_type", "fast_charger_value",
     {"FastCharger" + s: s for s in ("Supercharger", "CHAdeMO", "GB", "ACSingleWireCAN", "Combo", "MCSingleWireCAN", "Other")})
enum("Gear", "drive_state_shift_state", "shift_state_value",
     {"ShiftState" + s: s for s in ("P", "R", "N", "D")}, 10)
for field, suffix in (("FdWindow", "fd"), ("FpWindow", "fp"), ("RdWindow", "rd"), ("RpWindow", "rp")):
    enum(field, "vehicle_state_" + suffix + "_window", "window_state_value",
         {"WindowStateClosed": 0, "WindowStatePartiallyOpen": 1, "WindowStateOpened": 1})
enum("SentryMode", "vehicle_state_sentry_mode", "sentry_mode_state_value",
     {"SentryModeState" + s: s != "Off" for s in ("Off", "Idle", "Armed", "Aware", "Panic", "Quiet")})
enum("DefrostMode", "climate_state_defrost_mode", "defrost_mode_value",
     {"DefrostModeStateOff": 0, "DefrostModeStateNormal": 1, "DefrostModeStateMax": 2, "DefrostModeStateAutoDefog": 3})
enum("ClimateKeeperMode", "climate_state_climate_keeper_mode", "climate_keeper_mode_value",
     {"ClimateKeeperModeStateOff": "off", "ClimateKeeperModeStateOn": "keep",
      "ClimateKeeperModeStateDog": "dog", "ClimateKeeperModeStateParty": "camp"})
enum("CabinOverheatProtectionMode", "climate_state_cabin_overheat_protection", "cabin_overheat_protection_mode_value",
     {"CabinOverheatProtectionModeState" + s: s for s in ("Off", "On", "FanOnly")})
enum("CabinOverheatProtectionTemperatureLimit", "climate_state_cop_activation_temperature", "cabin_overheat_protection_temperature_limit_value",
     {"ClimateOverheatProtectionTempLimit" + s: s for s in ("Low", "Medium", "High")})
enum("MediaPlaybackStatus", "vehicle_state_media_info_media_playback_status", "media_status_value",
     {"MediaStatusStopped": "Stopped", "MediaStatusPlaying": "Playing", "MediaStatusPaused": "Paused"})

DOORS = {"DriverFront": "df", "DriverRear": "dr", "PassengerFront": "pf",
         "PassengerRear": "pr", "TrunkFront": "ft", "TrunkRear": "rt"}
SPECS["DoorState"] = {"keys": tuple("vehicle_state_" + v for v in DOORS.values()), "kind": "doors", "interval": 5}
for field, prefix in (("Location", "drive_state_"), ("DestinationLocation", "drive_state_active_route_"),
                      ("OriginLocation", "drive_state_active_route_origin_")):
    SPECS[field] = {"keys": (prefix + "latitude", prefix + "longitude"), "kind": "location", "interval": 5}
SPECS["HvacPower"] = {"keys": ("climate_state_is_climate_on", "climate_state_is_preconditioning",
                                "climate_state_cabin_overheat_protection_actively_cooling"), "kind": "hvac", "interval": 5}
for field, warning in (("TpmsSoftWarnings", "soft"), ("TpmsHardWarnings", "hard")):
    SPECS[field] = {"keys": tuple("vehicle_state_tpms_" + warning + "_warning_" + s for s in ("fl", "fr", "rl", "rr")), "kind": "tires", "interval": 30}
for field in ("ACChargingPower", "DCChargingPower"):
    scalar(field, "charge_state_charger_power", "number", 0, 1000, 10)

STATUS_CONFIG = {name: {"interval_seconds": spec["interval"]} for name, spec in SPECS.items()}
NATIVE_KEYS = frozenset(k for spec in SPECS.values() for k in spec["keys"])
NULLABLE_FIELDS = frozenset({"OriginLocation", "EstimatedHoursToChargeTermination", "HomelinkNearby", "LocatedAtHome", "DestinationLocation", "DestinationName", "MilesToArrival", "MinutesToArrival",
    "ExpectedEnergyPercentAtTripArrival", "RouteTrafficMinutesDelay", "SoftwareUpdateVersion",
    "ChargingCableType", "TimeToFullCharge", "FastChargerType"})


def decode_status(field, raw):
    """Return an exact, bounded native data patch; explicit invalid => None.

    Unknown enums never turn into False/zero. Protobuf omits false members in
    structured messages, so an empty *typed* door/tire object means all clear.
    """
    spec = SPECS[field]
    keys = spec["keys"]
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ValueError("invalid telemetry value")
    if raw.get("invalid") is True:
        # Tesla explicitly documents invalid cable as no cable present.
        return dict.fromkeys(keys, "<invalid>" if field == "ChargingCableType" else None)
    kind = spec["kind"]
    value = next(iter(raw.values()))
    wire = next(iter(raw))
    if kind in ("number", "integer"):
        # protojson serializes int64 as decimal text, not a JSON number.
        if wire == "long_value" and isinstance(value, str) and value.lstrip("-").isdigit() and len(value) <= 20:
            value = int(value)
        if wire not in {"double_value", "float_value", "int_value", "long_value"} or type(value) not in (float, int):
            raise ValueError("invalid numeric telemetry")
        if not math.isfinite(value) or not spec["low"] <= value <= spec["high"] or (kind == "integer" and int(value) != value):
            raise ValueError("out of range telemetry")
        value *= spec["scale"]
        if kind == "integer": value = int(value)
    elif kind == "boolean":
        if wire != "boolean_value" or type(value) is not bool:
            raise ValueError("invalid boolean telemetry")
    elif kind == "string":
        if wire != "string_value" or not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
            raise ValueError("invalid text telemetry")
    elif kind == "enum":
        if wire != spec["wire"] or not isinstance(value, str):
            raise ValueError("invalid enum telemetry")
        value = spec["values"].get(value)
    elif kind == "location":
        if wire != "location_value" or not isinstance(value, dict) or set(value) - {"latitude", "longitude"}:
            raise ValueError("invalid location telemetry")
        lat, lon = value.get("latitude", 0), value.get("longitude", 0)
        if any(type(v) not in (float, int) or not math.isfinite(v) for v in (lat, lon)) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("invalid location telemetry")
        return dict(zip(keys, (lat, lon)))
    elif kind in ("doors", "tires"):
        members = tuple(DOORS) if kind == "doors" else ("front_left", "front_right", "rear_left", "rear_right")
        allowed = set(members)
        if kind == "tires":
            allowed.update({"semi_middle_axle_left_2", "semi_middle_axle_right_2", "semi_rear_axle_left", "semi_rear_axle_right", "semi_rear_axle_left_2", "semi_rear_axle_right_2"})
        if wire != ("door_value" if kind == "doors" else "tire_location_value") or not isinstance(value, dict) or set(value) - allowed or any(type(v) is not bool for v in value.values()):
            raise ValueError("invalid structured telemetry")
        return dict(zip(keys, (int(value.get(m, False)) if kind == "doors" else value.get(m, False) for m in members)))
    elif kind == "hvac":
        if wire != "hvac_power_value" or not isinstance(value, str):
            raise ValueError("invalid HVAC telemetry")
        modes = {"HvacPowerStateOff": (False, False, False), "HvacPowerStateOn": (True, False, False),
                 "HvacPowerStatePrecondition": (True, True, False), "HvacPowerStateOverheatProtect": (True, False, True)}
        return dict(zip(keys, modes.get(value, (None, None, None))))
    else:
        raise ValueError("unsupported field")
    return dict.fromkeys(keys, value)
