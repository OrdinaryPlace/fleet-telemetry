"""Last-reported stream entities with original source time and explicit quality."""
from datetime import datetime, timezone

from homeassistant.helpers.entity import Entity
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import SIGNAL
from .status_fields import SPECS


class StreamField(Entity):
    _attr_should_poll = False

    def __init__(self, adapter, spec, field, domain, suffix, name):
        self.adapter = adapter
        self.slug = spec["slug"]
        self.field = field
        self._attr_name = spec["name"] + " " + name
        self._attr_unique_id = "tesla_fleet_stream_" + self.slug + "_" + suffix
        self.entity_id = domain + "." + self.slug + "_" + suffix

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.async_write_ha_state))

    @property
    def patch(self):
        model = self.adapter.models[self.slug]
        item = model.fields.get(self.field)
        return item[1] if item is not None and self.field not in model.invalid else {}

    @property
    def value(self):
        return self.patch.get(SPECS[self.field]["keys"][0])

    @property
    def extra_state_attributes(self):
        model = self.adapter.models[self.slug]
        item = model.fields.get(self.field)
        return {"source": "Tesla Fleet Telemetry", "telemetry_field": self.field,
                "status_semantics": "last reported", "bridge_available": self.adapter.bridge_available,
                "observed_at": datetime.fromtimestamp(item[0] / 1e9, timezone.utc).isoformat() if item else None,
                "reported_invalid": self.field in model.invalid}
