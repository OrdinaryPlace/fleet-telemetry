"""Navigation route origin, distinct from the vehicle's current location."""
from homeassistant.components.device_tracker import SourceType, TrackerEntity

from . import DOMAIN
from .entity import StreamField
from .status_fields import SPECS


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    adapter = hass.data[DOMAIN]
    async_add_entities([StreamOrigin(adapter, spec) for spec in adapter.vehicles])


class StreamOrigin(StreamField, TrackerEntity):
    _attr_icon = "mdi:map-marker-path"

    def __init__(self, adapter, spec):
        super().__init__(adapter, spec, "OriginLocation", "device_tracker", "route_origin", "route origin")

    @property
    def source_type(self):
        return SourceType.GPS

    @property
    def available(self):
        return self.latitude is not None and self.longitude is not None

    @property
    def latitude(self):
        return self.patch.get(SPECS[self.field]["keys"][0])

    @property
    def longitude(self):
        return self.patch.get(SPECS[self.field]["keys"][1])
