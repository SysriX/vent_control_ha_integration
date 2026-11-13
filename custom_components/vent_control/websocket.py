"""WebSocket-API für das Raum-Panel (Einstellungen → Lüftungssteuerung)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


@callback
def async_register(hass: HomeAssistant) -> None:
    for cmd in (ws_save, ws_delete, ws_house, ws_run):
        websocket_api.async_register_command(hass, cmd)


async def _run(connection, msg, coro) -> None:
    try:
        res = await coro
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(msg["id"], {"result": res})


@websocket_api.websocket_command({vol.Required("type"): "vent_control/save", vol.Required("unit"): dict})
@websocket_api.async_response
async def ws_save(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, hass.data[DOMAIN].async_save(msg["unit"]))


@websocket_api.websocket_command({vol.Required("type"): "vent_control/delete", vol.Required("unit_id"): str})
@websocket_api.async_response
async def ws_delete(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, hass.data[DOMAIN].async_delete(msg["unit_id"]))


@websocket_api.websocket_command({vol.Required("type"): "vent_control/house", vol.Required("settings"): dict})
@websocket_api.async_response
async def ws_house(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, hass.data[DOMAIN].async_house(msg["settings"]))


@websocket_api.websocket_command({vol.Required("type"): "vent_control/run", vol.Required("unit_id"): str,
                                  vol.Required("minutes"): vol.Coerce(float)})
@websocket_api.async_response
async def ws_run(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, hass.data[DOMAIN].async_run(msg["unit_id"], msg["minutes"]))
