"""sensor.<raum>_vent_control: on (ein Lüfter läuft) / idle / off (kein aktiver Lüfter); Attribut units = alle Lüfter mit Zustand.
sensor.haus_vent_control: Hauptschalter on/off; Attribute = Einstellungen (Hauptschalter, Nachtruhe)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .manager import VentManager


async def async_setup_platform(hass: HomeAssistant, _config, async_add_entities: AddEntitiesCallback, _discovery=None) -> None:
    mgr: VentManager = hass.data[DOMAIN]
    async_add_entities([RoomSensor(mgr, r) for r in mgr.rooms] + [HouseSensor(mgr)])


class _Base(SensorEntity):
    _attr_should_poll = False

    def __init__(self, mgr: VentManager) -> None:
        self.mgr = mgr

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()


class RoomSensor(_Base):
    _attr_icon = "mdi:fan"
    _unrecorded_attributes = frozenset({"units", "version"})

    def __init__(self, mgr: VentManager, room: str) -> None:
        super().__init__(mgr)
        self.room = room
        self._attr_name = f"{room.upper()} Lüftungssteuerung"
        self._attr_unique_id = f"{DOMAIN}_{room}"
        self.entity_id = f"sensor.{room}_vent_control"

    @property
    def native_value(self) -> str:
        return self.mgr.room_state(self.room)

    @property
    def extra_state_attributes(self) -> dict:
        return self.mgr.room_view(self.room)


class HouseSensor(_Base):
    _attr_icon = "mdi:fan-auto"
    _unrecorded_attributes = frozenset({"settings", "running", "version"})

    def __init__(self, mgr: VentManager) -> None:
        super().__init__(mgr)
        self._attr_name = "Haus Lüftungssteuerung"
        self._attr_unique_id = f"{DOMAIN}_haus"
        self.entity_id = "sensor.haus_vent_control"

    @property
    def native_value(self) -> str:
        return "on" if self.mgr.house["enabled"] else "off"

    @property
    def extra_state_attributes(self) -> dict:
        return self.mgr.house_view()
