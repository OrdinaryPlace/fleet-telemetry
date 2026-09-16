"""Privacy-safe status-source diagnostics; no raw fields or positions."""
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import DOMAIN, SIGNAL
from .entity import StreamField


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    adapter = hass.data[DOMAIN]
    entities = [StreamStatus(adapter, spec) for spec in adapter.vehicles]
    for spec in adapter.vehicles:
        entities.extend([
            StreamDuration(adapter, spec, "EstimatedHoursToChargeTermination", "hours_to_charge_limit", "hours to charge limit", UnitOfTime.HOURS),
            StreamDuration(adapter, spec, "MinutesToArrival", "minutes_to_arrival", "minutes to arrival", UnitOfTime.MINUTES),
        ])
    async_add_entities(entities)


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


class StreamDuration(StreamField, SensorEntity):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_suggested_display_precision = 1

    def __init__(self, adapter, spec, field, suffix, name, unit):
        super().__init__(adapter, spec, field, "sensor", suffix, name)
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self):
        return self.value
