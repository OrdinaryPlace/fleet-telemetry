"""Feed authenticated local MQTT status into existing native Fleet entities.

This version-tested companion uses native Fleet's coordinator data interface.
It does not replace authentication, sign/send commands, call Tesla, mutate state
through the REST API, or monkey-patch native code. It operates only after the
user disables the native integration's automatic polling through system options.
"""
from __future__ import annotations

from datetime import timedelta
import json
import logging
import time

import voluptuous as vol

from homeassistant.components import mqtt
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .model import StatusModel

DOMAIN = "tesla_fleet_stream"
LOGGER = logging.getLogger(__name__)
SIGNAL = DOMAIN + "_changed"
CONFIG_SCHEMA = vol.Schema({vol.Optional(DOMAIN): vol.Schema({
    vol.Required("vehicles"): vol.All(cv.ensure_list, [vol.Schema({
        vol.Required("name"): cv.string,
        vol.Required("slug"): vol.All(cv.string, vol.Match(r"^[a-z0-9][a-z0-9_]{0,47}$")),
    })]),
    vol.Optional("topic_prefix", default="tesla_live"): vol.All(cv.string, vol.Match(r"^[a-z0-9_/-]+$")),
})}, extra=vol.ALLOW_EXTRA)


class StreamAdapter:
    def __init__(self, hass, vehicles, prefix):
        self.hass = hass
        self.vehicles = vehicles
        self.prefix = prefix
        self.models = {v["slug"]: StatusModel(v["slug"]) for v in vehicles}
        self.bound = {}
        self.last_applied = {}
        self.status = {v["slug"]: "waiting_for_native_fleet" for v in vehicles}
        self.unsub = []
        self.polling_disabled = False
        self.bridge_available = False

    async def start(self):
        @callback
        def bridge_status(message):
            if message.payload not in ("online", "offline"):
                return
            self.bridge_available = message.payload == "online"
            for slug in self.models:
                self.apply(slug)
            async_dispatcher_send(self.hass, SIGNAL)
        self.unsub.append(await mqtt.async_subscribe(self.hass, f"{self.prefix}/bridge/availability", bridge_status, 1))
        for slug in self.models:
            @callback
            def receive(message, slug=slug):
                if len(message.payload) > 262144:
                    return
                try:
                    snapshot = json.loads(message.payload)
                    if self.models[slug].accept(snapshot, time.time()):
                        self.apply(slug)
                except (ValueError, TypeError, KeyError, OverflowError):
                    LOGGER.warning("Rejected malformed status snapshot for configured alias")
                async_dispatcher_send(self.hass, SIGNAL)
            self.unsub.append(await mqtt.async_subscribe(self.hass, f"{self.prefix}/{slug}/fleet_status/state", receive, 1))
        # This timer only inspects in-memory HA objects, never the vehicle/API.
        # It reconnects to the coordinator after a supported native entry reload.
        self.unsub.append(async_track_time_interval(self.hass, self.bind, timedelta(seconds=15)))
        await self.bind()
        self.unsub.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.stop))

    async def bind(self, _now=None):
        # A reload or changed entry layout must immediately invalidate bindings.
        self.bound = {}
        self.polling_disabled = False
        entries = [entry for entry in self.hass.config_entries.async_entries("tesla_fleet")
                   if not entry.disabled_by and getattr(entry, "runtime_data", None) is not None]
        if len(entries) != 1:
            self.status = dict.fromkeys(self.models, "waiting_for_native_fleet")
            async_dispatcher_send(self.hass, SIGNAL)
            return
        entry = entries[0]
        runtime = entry.runtime_data
        if not hasattr(runtime, "vehicles") or getattr(runtime, "energysites", None):
            self.status = dict.fromkeys(self.models, "unsupported_native_layout")
            async_dispatcher_send(self.hass, SIGNAL)
            return
        matches = {}
        for spec in self.vehicles:
            found = [v for v in runtime.vehicles if str(v.device.get("name", "")).casefold() == spec["name"].casefold()]
            if len(found) != 1 or not callable(getattr(found[0].coordinator, "async_set_updated_data", None)):
                self.status = dict.fromkeys(self.models, "vehicle_mapping_mismatch")
                async_dispatcher_send(self.hass, SIGNAL)
                return
            matches[spec["slug"]] = found[0].coordinator
        # Fail closed if disabling this entry would leave another vehicle polled
        # status unserved, or if two aliases resolve to the same coordinator.
        if len(matches) != len(runtime.vehicles) or len({id(c) for c in matches.values()}) != len(matches):
            self.status = dict.fromkeys(self.models, "vehicle_mapping_mismatch")
            async_dispatcher_send(self.hass, SIGNAL)
            return
        self.bound = matches
        self.polling_disabled = entry.pref_disable_polling
        for slug in self.models:
            self.apply(slug)
        async_dispatcher_send(self.hass, SIGNAL)

    @callback
    def apply(self, slug):
        model = self.models[slug]
        coordinator = self.bound.get(slug)
        if coordinator is None:
            return
        if not model.ready:
            self.status[slug] = "waiting_for_stream_baseline"
            return
        if not self.polling_disabled:
            self.status[slug] = "ready_to_disable_polling"
            return
        data = model.native_data(coordinator.data, time.time())
        if not self.bridge_available:
            data["state"] = "offline"
        # Fresh samples prove online only for the freshness window when no
        # connection event is known. Let the local timer expire that inference.
        marker = (id(coordinator), model.revision, self.bridge_available, data["state"])
        if self.last_applied.get(slug) == marker and coordinator.data.get("_tesla_stream_source") == "fleet_telemetry":
            return
        coordinator.updated_once = True
        coordinator.async_set_updated_data(data)
        self.last_applied[slug] = marker
        self.status[slug] = "streaming"

    @callback
    def stop(self, _event=None):
        for unsubscribe in self.unsub:
            unsubscribe()
        self.unsub.clear()


async def async_setup(hass, config):
    if DOMAIN not in config:
        return True
    options = config[DOMAIN]
    vehicles = options["vehicles"]
    if not vehicles or len({v["slug"] for v in vehicles}) != len(vehicles) or len({v["name"].casefold() for v in vehicles}) != len(vehicles):
        LOGGER.error("Stream vehicle mapping must be nonempty and unique")
        return False
    adapter = StreamAdapter(hass, vehicles, options["topic_prefix"])
    hass.data[DOMAIN] = adapter
    await discovery.async_load_platform(hass, Platform.SENSOR, DOMAIN, {}, config)
    await adapter.start()
    return True
