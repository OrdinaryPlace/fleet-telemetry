"""Privacy-safe status-source diagnostics; no raw fields or positions."""
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import DOMAIN, SIGNAL


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    adapter = hass.data[DOMAIN]
    async_add_entities([StreamStatus(adapter, spec) for spec in adapter.vehicles])


class StreamStatus(SensorEntity):
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:access-point-network"

    def __init__(self, adapter, spec):
        self.adapter = adapter
        self.slug = spec["slug"]
        self._attr_name = spec["name"] + " status source"
        self._attr_unique_id = "tesla_fleet_stream_" + self.slug + "_status_source"
        self.entity_id = "sensor." + self.slug + "_status_source"

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.async_write_ha_state))

    @property
    def native_value(self):
        return self.adapter.status[self.slug]

    @property
    def extra_state_attributes(self):
        return self.adapter.models[self.slug].summary() | {"automatic_polling_disabled": self.adapter.polling_disabled,
            "bridge_available": self.adapter.bridge_available,
            "software_update_schedule": "not provided by reliable streaming fields"}
