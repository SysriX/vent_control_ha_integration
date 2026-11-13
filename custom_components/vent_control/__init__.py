"""Lüftungssteuerung (vent_control) – Lüfter je Raum nach Bewegung, Feuchte, Luftqualität und Intervall,
bearbeitet im Raum-Panel (Einstellungen → Lüftungssteuerung). Einstellungen liegen in .storage/vent_control.

configuration.yaml:

    vent_control:
      rooms: [wz, bu, ku, sz, fl, ke, ba, bz]   # optional, das ist der Standard
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType

from . import websocket
from .const import CONF_ROOMS, DEFAULT_ROOMS, DOMAIN
from .manager import VentManager

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Any(None, vol.Schema({
    vol.Optional(CONF_ROOMS, default=DEFAULT_ROOMS): vol.All(cv.ensure_list, [cv.slug]),
}))}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    conf = config.get(DOMAIN) or {}
    mgr = VentManager(hass, conf.get(CONF_ROOMS) or DEFAULT_ROOMS)
    hass.data[DOMAIN] = mgr
    websocket.async_register(hass)
    await mgr.async_start()

    async def _guard(coro) -> None:
        try:
            await coro
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    def _uid(call: ServiceCall) -> str:
        uid = call.data["unit_id"]
        if uid not in mgr.units:
            raise ServiceValidationError(f"Lüfter {uid} nicht gefunden")
        return uid

    async def set_enabled(call: ServiceCall) -> None:
        uid = _uid(call)
        await _guard(mgr.async_save({"id": uid, "room": mgr.units[uid]["room"], "enabled": call.data["enabled"]}))

    async def run(call: ServiceCall) -> None:
        await _guard(mgr.async_run(_uid(call), call.data["minutes"]))

    async def set_master(call: ServiceCall) -> None:
        await _guard(mgr.async_house({"enabled": call.data["enabled"]}))

    hass.services.async_register(DOMAIN, "set_enabled", set_enabled, vol.Schema({
        vol.Required("unit_id"): cv.string, vol.Required("enabled"): cv.boolean}))
    hass.services.async_register(DOMAIN, "run", run, vol.Schema({
        vol.Required("unit_id"): cv.string, vol.Required("minutes"): vol.All(vol.Coerce(float), vol.Range(min=0, max=720))}))
    hass.services.async_register(DOMAIN, "set_master", set_master, vol.Schema({vol.Required("enabled"): cv.boolean}))
    hass.async_create_task(async_load_platform(hass, "sensor", DOMAIN, {}, config))
    return True
