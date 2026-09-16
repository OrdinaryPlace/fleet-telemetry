"""Tesla profile home and HomeLink proximity; neither is HA zone presence."""
from homeassistant.components.binary_sensor import BinarySensorEntity

from . import DOMAIN
from .entity import StreamField


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    adapter = hass.data[DOMAIN]
    async_add_entities([
        StreamFlag(adapter, spec, field, suffix, name)
        for spec in adapter.vehicles
        for field, suffix, name in (
            ("HomelinkNearby", "homelink_nearby", "HomeLink nearby"),
            ("LocatedAtHome", "tesla_home", "at Tesla profile home"),
        )
    ])


class StreamFlag(StreamField, BinarySensorEntity):
    _attr_icon = "mdi:home-map-marker"

    def __init__(self, adapter, spec, field, suffix, name):
        super().__init__(adapter, spec, field, "binary_sensor", suffix, name)

    @property
    def is_on(self):
        return self.value
