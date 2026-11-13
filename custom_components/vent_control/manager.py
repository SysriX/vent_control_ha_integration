"""Lüftungssteuerung: Lüfter je Raum, gesteuert nach Bewegung, Feuchte, Luftqualität und Intervall.

Ein Lüfter ist ein oder mehrere Geräte (fan, switch, light, input_boolean), die gemeinsam schalten.
Je Durchlauf (alle 20 s und sofort bei Änderung einer Quelle) wird gesammelt, welche Regeln gerade
lüften wollen:

  Nach Bewegung   Zeit:  x min nach Beginn (oder Ende) der Bewegung für y min
                  Feuchte / Luft: über der Schwelle, solange Bewegung ist oder bis z min danach;
                  einmal an, läuft er, bis der Wert unter die Aus-Schwelle fällt
  Ohne Bewegung   Feuchte / Luft mit eigenen Schwellen, Intervall alle x h für y min
  Danach          Nachlauf (nur nach Feuchte/Luft), Mindestlaufzeit, Höchstlaufzeit mit Pause
  Sperren         Fenster offen → aus, Nachtruhe (Feuchte darf wahlweise), Hand-Schaltung → Pause der Automatik

Mehrere Regeln dürfen gleichzeitig gelten; es zählt die höchste Stufe (Prozent bei fan-Geräten).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_HOUSE, DEFAULT_UNIT, DEVICE_DOMAINS, EVENT_CHANGED, LOG_LEN, MAX_UNITS_PER_ROOM, MOTION_HELPER, PRESENCE_HELPER,
    PRESENCE_THEN, REASONS, SEND_MIN_GAP_S, SIGNAL_UPDATE, STORE_KEY, STORE_VERSION, TICK_SECONDS, TIMER_FROM, VERSION,
)

_LOGGER = logging.getLogger(__name__)

RE_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
RE_ENT = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
BAD = ("unknown", "unavailable", "", None)
ON_STATES = ("on", "open", "offen", "true", "detected", "home")
THRESH_RULES = ("m_hum", "m_air", "a_hum", "a_air")
RULE_ORDER = ("m_timer", "m_hum", "m_air", "a_hum", "a_air", "a_interval")
FAN_SET_SPEED = 1                 # FanEntityFeature.SET_SPEED


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _iso(d: datetime | None) -> str | None:
    return dt_util.as_local(d).isoformat() if d else None


def _parse(s: str | None) -> datetime | None:
    d = dt_util.parse_datetime(s) if s else None
    if d and d.tzinfo is None:
        d = d.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return d


def in_window(frm: str, to: str, now: datetime) -> bool:
    """hh:mm-Fenster, auch über Mitternacht."""
    a, b = int(frm[:2]) * 60 + int(frm[3:]), int(to[:2]) * 60 + int(to[3:])
    m = now.hour * 60 + now.minute
    if a == b:
        return False
    return a <= m < b if a < b else (m >= a or m < b)


class VentManager:
    def __init__(self, hass: HomeAssistant, rooms: list[str]) -> None:
        self.hass = hass
        self.rooms = rooms
        self.store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self.house: dict = dict(DEFAULT_HOUSE)
        self.units: dict[str, dict] = {}
        self.rt: dict[str, dict] = {}          # Lüfter → Laufzeitdaten (Bewegung, Zeitfenster, Schwellen, Hand …)
        self.dev: dict[str, dict] = {}         # Gerät → letzter Befehl {on, pct, at}
        self.view: dict[str, dict] = {}        # Lüfter → zuletzt berechneter Zustand (Sensoren/Panel)
        self.root = Context()
        self._unsubs: list = []
        self._watch: set[str] = set()
        self._busy = False
        self._again = False
        self._snap = ""

    # ================================================================ Lebenszyklus
    async def async_start(self) -> None:
        data = await self.store.async_load() or {}
        self.house = self._clean_house({**DEFAULT_HOUSE, **(data.get("house") or {})})
        for k, v in (data.get("units") or {}).items():
            try:
                self.units[k] = self._clean({**DEFAULT_UNIT, **v}, k)
            except ValueError as err:
                _LOGGER.warning("Lüfter %s verworfen: %s", k, err)
        self.rt = {k: v for k, v in (data.get("rt") or {}).items() if k in self.units}
        self._unsubs.append(async_track_time_change(self.hass, self._tick, second=TICK_SECONDS))
        self._unsubs.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state, event_filter=self._is_watched))
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_stop)
        self._rewatch()
        self.hass.async_create_task(self._process())

    async def _async_stop(self, _ev: Event | None = None) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        await self.store.async_save(self._data())

    def _data(self) -> dict:
        return {"house": self.house, "units": self.units, "rt": self.rt}

    def _save(self) -> None:
        self.store.async_delay_save(self._data, 5)

    # ================================================================ Bereinigen
    @staticmethod
    def _num(d: dict, k: str, lo: float, hi: float, dflt: float) -> float:
        v = _f(d.get(k, dflt))
        return dflt if v is None else max(lo, min(hi, v))

    def _clean_thresh(self, raw: Any, dflt: dict, what: str, lo: float, hi: float) -> dict:
        x = raw if isinstance(raw, dict) else {}
        on, off = self._num(x, "on", lo, hi, dflt["on"]), self._num(x, "off", lo, hi, dflt["off"])
        out = {"enabled": bool(x.get("enabled", dflt["enabled"])), "on": on, "off": off,
               "speed": int(self._num(x, "speed", 0, 100, 0))}
        if out["enabled"] and off >= on:
            raise ValueError(f"{what}: „aus unter“ ({off:g}) muss kleiner sein als „an über“ ({on:g})")
        return out

    def _clean(self, u: dict, uid: str | None = None) -> dict:
        room = str(u.get("room") or "")
        if room not in self.rooms:
            raise ValueError(f"Unbekannter Raum: {room!r}")
        ents = lambda key, doms, n=16: [e for e in dict.fromkeys(u.get(key) or [])  # noqa: E731
                                        if isinstance(e, str) and RE_ENT.match(e) and e.split(".")[0] in doms][:n]
        dl = DEFAULT_UNIT
        t = u.get("m_timer") if isinstance(u.get("m_timer"), dict) else {}
        iv = u.get("a_interval") if isinstance(u.get("a_interval"), dict) else {}
        out = {
            "id": uid or u.get("id") or uuid.uuid4().hex[:10],
            "room": room,
            "name": (str(u.get("name") or "").strip() or "Lüfter")[:40],
            "enabled": bool(u.get("enabled")),
            "devices": ents("devices", DEVICE_DOMAINS, 8),
            "motion_sensors": ents("motion_sensors", ("binary_sensor", "input_boolean")),
            "presence_sensors": ents("presence_sensors", ("binary_sensor", "input_boolean")),
            "presence_light": bool(u.get("presence_light")),
            "min_presence_min": self._num(u, "min_presence_min", 0, 120, 0),
            "min_presence_then": u.get("min_presence_then") if u.get("min_presence_then") in PRESENCE_THEN else "now",
            "presence_gap_s": int(self._num(u, "presence_gap_s", 0, 900, dl["presence_gap_s"])),
            "humidity_sensors": ents("humidity_sensors", ("sensor",), 8),
            "air_sensors": ents("air_sensors", ("sensor",), 8),
            "windows": ents("windows", ("binary_sensor", "sensor", "input_boolean")),
            "window_delay_s": int(self._num(u, "window_delay_s", 0, 1800, 30)),
            "m_timer": {"enabled": bool(t.get("enabled", dl["m_timer"]["enabled"])),
                        "from": t.get("from") if t.get("from") in TIMER_FROM else "start",
                        "delay_min": self._num(t, "delay_min", 0, 120, dl["m_timer"]["delay_min"]),
                        "run_min": self._num(t, "run_min", 0.5, 240, dl["m_timer"]["run_min"]),
                        "restart": bool(t.get("restart", True)),
                        "speed": int(self._num(t, "speed", 0, 100, 0))},
            "m_hum": self._clean_thresh(u.get("m_hum"), dl["m_hum"], "Feuchte nach Bewegung", 0, 100),
            "m_air": self._clean_thresh(u.get("m_air"), dl["m_air"], "Luftqualität nach Bewegung", 0, 100000),
            "m_window_min": self._num(u, "m_window_min", 0, 240, dl["m_window_min"]),
            "a_hum": self._clean_thresh(u.get("a_hum"), dl["a_hum"], "Feuchte ohne Bewegung", 0, 100),
            "a_air": self._clean_thresh(u.get("a_air"), dl["a_air"], "Luftqualität ohne Bewegung", 0, 100000),
            "a_interval": {"enabled": bool(iv.get("enabled")),
                           "every_h": self._num(iv, "every_h", 0.5, 48, dl["a_interval"]["every_h"]),
                           "run_min": self._num(iv, "run_min", 1, 240, dl["a_interval"]["run_min"]),
                           "speed": int(self._num(iv, "speed", 0, 100, 0))},
            "min_run_min": self._num(u, "min_run_min", 0, 60, dl["min_run_min"]),
            "follow_min": self._num(u, "follow_min", 0, 120, dl["follow_min"]),
            "max_run_min": self._num(u, "max_run_min", 0, 1440, dl["max_run_min"]),
            "pause_min": self._num(u, "pause_min", 1, 240, dl["pause_min"]),
            "quiet": bool(u.get("quiet", True)),
            "quiet_hum": bool(u.get("quiet_hum", True)),
            "manual_hold_min": self._num(u, "manual_hold_min", 0, 720, dl["manual_hold_min"]),
        }
        if out["max_run_min"] and out["max_run_min"] < out["min_run_min"]:
            raise ValueError("Höchstlaufzeit muss größer sein als die Mindestlaufzeit")
        return out

    def _clean_house(self, h: dict) -> dict:
        hm = lambda k: str(h.get(k)) if RE_HM.match(str(h.get(k) or "")) else DEFAULT_HOUSE[k]  # noqa: E731
        out = {"enabled": bool(h.get("enabled", True)), "quiet_enabled": bool(h.get("quiet_enabled")),
               "quiet_from": hm("quiet_from"), "quiet_to": hm("quiet_to")}
        if out["quiet_enabled"] and out["quiet_from"] == out["quiet_to"]:
            raise ValueError("Nachtruhe: Beginn und Ende dürfen nicht gleich sein")
        return out

    # ================================================================ Bearbeiten (WebSocket/Dienste)
    async def async_save(self, patch: dict) -> str:
        uid = patch.get("id")
        if uid and uid not in self.units:
            uid = None
        if not uid and sum(1 for u in self.units.values() if u["room"] == patch.get("room")) >= MAX_UNITS_PER_ROOM:
            raise ValueError(f"Höchstens {MAX_UNITS_PER_ROOM} Lüfter je Raum")
        old = self.units.get(uid) or {}
        u = self._clean({**DEFAULT_UNIT, **old, **patch}, uid)
        self.units[u["id"]] = u
        rt = self.rt.setdefault(u["id"], {})
        rt.pop("manual", None)
        if old and (not u["enabled"] or set(old.get("devices") or []) - set(u["devices"])):
            # deaktiviert bzw. Gerät entfernt: was wir eingeschaltet haben, wieder aus
            gone = old.get("devices") if not u["enabled"] else set(old["devices"]) - set(u["devices"])
            if rt.get("running"):
                for e in gone:
                    await self._switch(e, False, 0, force=True)
                if not u["enabled"]:
                    self._stop_run(u, rt, dt_util.now(), "Lüftungssteuerung aus")
        self._save()
        self._rewatch()
        await self._process()
        return u["id"]

    async def async_delete(self, uid: str) -> None:
        u = self.units.get(uid)
        if u is None:
            raise ValueError("Lüfter nicht gefunden")
        if (self.rt.get(uid) or {}).get("running"):
            for e in u["devices"]:
                await self._switch(e, False, 0, force=True)
        self.units.pop(uid)
        self.rt.pop(uid, None)
        self.view.pop(uid, None)
        self._save()
        self._rewatch()
        self._push()

    async def async_house(self, patch: dict) -> dict:
        was = self.house["enabled"]
        self.house = self._clean_house({**self.house, **patch})
        if was and not self.house["enabled"]:
            now = dt_util.now()
            for uid, u in self.units.items():
                rt = self.rt.setdefault(uid, {})
                if rt.get("running"):
                    for e in u["devices"]:
                        await self._switch(e, False, 0, force=True)
                    self._stop_run(u, rt, now, "Hauptschalter aus")
        self._save()
        await self._process()
        return self.house

    async def async_run(self, uid: str, minutes: float) -> None:
        """Jetzt lüften (Taste im Panel / Dienst): wie eine Hand-Schaltung mit fester Dauer."""
        u = self.units.get(uid)
        if u is None:
            raise ValueError("Lüfter nicht gefunden")
        now = dt_util.now()
        rt = self.rt.setdefault(uid, {})
        for e in u["devices"]:
            self.dev.pop(e, None)               # Automatik darf sofort wieder schalten
        if minutes <= 0:
            rt.pop("manual", None)
        else:
            rt["manual"] = {"on": True, "until": (now + timedelta(minutes=minutes)).isoformat(), "by": "Taste"}
            for e in u["devices"]:
                await self._switch(e, True, self._speed(u, ["m_timer"]), force=True)
        self._save()
        await self._process()

    # ================================================================ Quellen
    def _state(self, eid: str | None):
        return self.hass.states.get(eid) if eid else None

    def motion_sensors(self, u: dict) -> list[str]:
        if u["motion_sensors"]:
            return u["motion_sensors"]
        s = self._state(MOTION_HELPER.format(room=u["room"]))
        txt = s.state if s and s.state not in BAD else ""
        return [x.strip() for x in txt.split(",") if RE_ENT.match(x.strip())]

    def presence_sensors(self, u: dict) -> list[str]:
        if u["presence_sensors"] or not u["presence_light"]:
            return u["presence_sensors"]
        s = self._state(PRESENCE_HELPER.format(room=u["room"]))
        txt = s.state if s and s.state not in BAD else ""
        return [x.strip() for x in txt.split(",") if RE_ENT.match(x.strip())]

    def _max(self, ents: list[str]) -> float | None:
        states = [self._state(e) for e in ents]
        vals = [v for v in (_f(s.state) for s in states if s) if v is not None]
        return max(vals) if vals else None

    def window_open(self, u: dict, now: datetime) -> tuple[bool, str | None]:
        first: datetime | None = None
        who = None
        for e in u["windows"]:
            s = self._state(e)
            if not s:
                continue
            st = s.state.lower()
            if (st in ("on", "open", "offen", "true", "tilted", "gekippt")) and (first is None or s.last_changed < first):
                first, who = s.last_changed, e
        if first is None:
            return False, None
        return (now - first).total_seconds() >= u["window_delay_s"], who

    def _dev_on(self, e: str) -> bool | None:
        s = self._state(e)
        if not s or s.state in BAD:
            return None
        return s.state == "on"

    # ================================================================ Beobachtung
    def _rewatch(self) -> None:
        w: set[str] = set()
        for u in self.units.values():
            w |= set(u["devices"]) | set(self.motion_sensors(u)) | set(self.presence_sensors(u))
            w |= set(u["humidity_sensors"]) | set(u["air_sensors"]) | set(u["windows"])
            w |= {MOTION_HELPER.format(room=u["room"]), PRESENCE_HELPER.format(room=u["room"])}
        self._watch = {x for x in w if x}

    @callback
    def _is_watched(self, data: Any) -> bool:
        return data.get("entity_id") in self._watch

    async def _on_state(self, ev: Event) -> None:
        eid = ev.data.get("entity_id", "")
        if eid.endswith(("_light_motion_cfg_motion_sensors", "_light_motion_cfg_presence_sensors")):
            self._rewatch()
        if eid.split(".")[0] in DEVICE_DOMAINS:
            self._check_manual(eid, ev.data.get("old_state"), ev.data.get("new_state"))
        await self._process()

    def _check_manual(self, eid: str, old, new) -> None:
        if not new or new.state in BAD or not old or old.state in BAD:
            return
        on_new, on_old = new.state == "on", old.state == "on"
        p_new, p_old = new.attributes.get("percentage"), old.attributes.get("percentage")
        if on_new == on_old and (not on_new or p_new == p_old):
            return
        if new.context and new.context.parent_id == self.root.id:
            return                                  # unser eigener Befehl
        d = self.dev.get(eid)
        now = dt_util.utcnow()
        if d and d.get("on") == on_new and (now - _parse(d["at"])).total_seconds() < 30:
            return                                  # Rückmeldung auf unseren Befehl (anderer Kontext, z. B. MQTT)
        for uid, u in self.units.items():
            if not u["enabled"] or not self.house["enabled"] or eid not in u["devices"] or u["manual_hold_min"] <= 0:
                continue
            rt = self.rt.setdefault(uid, {})
            until = dt_util.now() + timedelta(minutes=u["manual_hold_min"])
            rt["manual"] = {"on": on_new, "until": until.isoformat(), "by": eid}
            self.dev.pop(eid, None)
            self._log(rt, "Hand " + ("an" if on_new else "aus"))
            _LOGGER.info("Lüftung %s: Hand-Schaltung %s an %s", uid, "an" if on_new else "aus", eid)
            self.hass.bus.async_fire(EVENT_CHANGED, {"unit": uid, "room": u["room"], "action": "manual", "on": on_new})
            self._save()

    async def _tick(self, _now: datetime) -> None:
        await self._process()

    async def _process(self) -> None:
        if self._busy:
            self._again = True
            return
        self._busy = True
        try:
            while True:
                self._again = False
                try:
                    await self._run()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Lüftungssteuerung: Fehler im Durchlauf")
                if not self._again:
                    break
        finally:
            self._busy = False
        self._push()

    # ================================================================ Entscheiden
    @staticmethod
    def _log(rt: dict, txt: str) -> None:
        log = rt.setdefault("log", [])
        log.insert(0, {"at": dt_util.now().isoformat(), "t": txt})
        del log[LOG_LEN:]

    def _stop_run(self, u: dict, rt: dict, now: datetime, why: str) -> None:
        if rt.get("running"):
            self._log(rt, f"aus · {why}")
            self.hass.bus.async_fire(EVENT_CHANGED, {"unit": u["id"], "room": u["room"], "action": "off", "reason": why})
        rt["running"] = False
        rt.pop("on_since", None)
        rt["ran_at"] = now.isoformat()

    def _motion(self, u: dict, rt: dict, now: datetime) -> tuple[bool, list[str], dict]:
        """Bewegung/Anwesenheit auswerten: Aufenthalt (kurze Unterbrechungen erlaubt), Mindest-Anwesenheit,
        Zeitfenster der Regel „nach Bewegung · Zeit“."""
        ms = self.motion_sensors(u)
        sens = ms + [e for e in self.presence_sensors(u) if e not in ms]
        active = [e for e in sens if (s := self._state(e)) and s.state.lower() in ON_STATES]
        motion = bool(active)
        was = rt.get("motion")
        if was is None:
            was = motion                            # erster Lauf: keine Flanke erfinden
        t = u["m_timer"]
        need = u["min_presence_min"]
        gap = timedelta(seconds=u["presence_gap_s"])
        # ---- Aufenthalt: beginnt mit Bewegung, endet erst nach „gap“ ohne Meldung
        last = _parse(rt.get("motion_last"))
        # neuer Aufenthalt: nach einer Pause > gap, oder wenn lange nichts geprüft wurde (z. B. nach einem Neustart)
        stale = bool(last) and now - last > max(gap, timedelta(seconds=45))
        if motion and (not rt.get("sess_start") or not last or (not was and now - last > gap) or stale):
            rt["sess_start"] = now.isoformat()
            rt["sess_ok"] = False
        if motion or was:
            rt["motion_last"] = now.isoformat()     # bei Bewegung bzw. genau beim Ende der Bewegung
        ss, ml = _parse(rt.get("sess_start")), _parse(rt.get("motion_last"))
        in_sess = bool(ss) and (motion or bool(ml and now - ml <= gap))
        dur = ((now if motion else ml) - ss) if in_sess else timedelta(0)
        ok_before = bool(rt.get("sess_ok"))
        if in_sess:
            rt["sess_ok"] = ok_before or need <= 0 or dur >= timedelta(minutes=need)
        ok = bool(rt.get("sess_ok"))
        qualified = need > 0 and ok and not ok_before   # Mindest-Anwesenheit gerade erreicht
        # ---- Zeit nach Bewegung
        tm = rt.get("timer")
        start, end = (_parse(tm["start"]), _parse(tm["end"])) if tm else (None, None)
        running = bool(tm) and start <= now < end
        run = timedelta(minutes=t["run_min"])
        delay = timedelta(minutes=t["delay_min"])
        wait = delay if need <= 0 or u["min_presence_then"] == "delay" else timedelta(0)

        def plan(at: datetime) -> None:
            rt["timer"] = {"start": at.isoformat(), "end": (at + run).isoformat()}

        def extend(frm: datetime) -> None:
            rt["timer"]["end"] = max(end, frm + run).isoformat()

        if t["enabled"] and t["from"] == "start":
            if need <= 0:
                if motion and not was:
                    if not tm or now >= end:
                        plan(now + delay)
                    elif t["restart"]:
                        extend(max(now, start))
            elif qualified:
                if not tm or now >= end:
                    plan(now + wait)
                elif t["restart"]:
                    extend(max(now, start))
            elif motion and not was and ok and tm and now < end and t["restart"]:
                extend(max(now, start))             # zurück im selben (langen) Aufenthalt
        if t["enabled"] and t["from"] == "end":
            if motion and not was and tm and now < start:
                rt.pop("timer", None)               # wieder Bewegung, bevor es losging → neu warten
            if not motion and was and ok:
                if running:
                    if t["restart"]:
                        extend(now)
                else:
                    plan(now + wait)
        # „Läuft, solange Bewegung“: Ende immer mindestens Nachlauf-Zeit hinter der letzten Bewegung
        tm = rt.get("timer")
        if t["enabled"] and t["restart"] and tm and motion:
            tm["end"] = max(_parse(tm["end"]), now + run).isoformat()
        if motion and not was:
            rt["motion_on"] = now.isoformat()
        rt["motion"] = motion
        if not t["enabled"]:
            rt.pop("timer", None)
        tm = rt.get("timer")
        if tm and now >= _parse(tm["end"]):
            rt.pop("timer", None)
        info = {"presence_since": _iso(ss) if in_sess else None, "presence_min": round(dur.total_seconds() / 60, 1),
                "presence_ok": ok if need > 0 else None, "presence_need": need,
                "presence_left": round(max(0.0, need - dur.total_seconds() / 60), 1) if need > 0 and in_sess and not ok else None}
        return motion, active, info

    @staticmethod
    def _latch(rt: dict, key: str, rule: dict, val: float | None, armed: bool) -> bool:
        cur = bool(rt.get("latch", {}).get(key))
        if not rule["enabled"] or val is None:
            cur = False
        elif val > rule["on"] and armed:
            cur = True
        elif val < rule["off"]:
            cur = False
        rt.setdefault("latch", {})[key] = cur
        return cur

    def _speed(self, u: dict, reasons: list[str]) -> int:
        return max([u[r]["speed"] for r in reasons if r in RULE_ORDER] or [0])

    def _decide(self, u: dict, now: datetime) -> dict:
        rt = self.rt.setdefault(u["id"], {})
        hum, air = self._max(u["humidity_sensors"]), self._max(u["air_sensors"])
        motion, active, pres = self._motion(u, rt, now)
        ml = _parse(rt.get("motion_last"))
        armed_until = ml + timedelta(minutes=u["m_window_min"]) if ml else None
        # Feuchte/Luft „nach Bewegung“ nur nach ausreichend langem Aufenthalt (Mindest-Anwesenheit)
        stayed = u["min_presence_min"] <= 0 or bool(rt.get("sess_ok"))
        armed = stayed and (motion or bool(armed_until and now < armed_until))
        v: dict = {"humidity": hum, "air": air, "motion": motion, "motion_active": active,
                   "motion_sensors": self.motion_sensors(u), "presence_sensors": self.presence_sensors(u), "armed": armed,
                   "armed_until": _iso(armed_until) if armed and not motion else None, "timer": rt.get("timer"), **pres}
        # ---- Regeln
        want: list[str] = []
        tm = rt.get("timer")
        if tm and _parse(tm["start"]) <= now < _parse(tm["end"]):
            want.append("m_timer")
        for key, val in (("m_hum", hum), ("m_air", air)):
            if self._latch(rt, key, u[key], val, armed):
                want.append(key)
        for key, val in (("a_hum", hum), ("a_air", air)):
            if self._latch(rt, key, u[key], val, True):
                want.append(key)
        iv = u["a_interval"]
        if iv["enabled"]:
            ran = _parse(rt.get("ran_at")) if not rt.get("running") else now
            if not rt.get("ran_at"):
                rt["ran_at"] = now.isoformat()
                ran = now
            iu = _parse(rt.get("interval_until"))
            if iu and now < iu:
                want.append("a_interval")
            elif ran and now - ran >= timedelta(hours=iv["every_h"]):
                rt["interval_until"] = (now + timedelta(minutes=iv["run_min"])).isoformat()
                want.append("a_interval")
            v["interval_next"] = _iso(ran + timedelta(hours=iv["every_h"])) if ran and not (iu and now < iu) else None
        else:
            rt.pop("interval_until", None)
        # ---- Nachtruhe
        quiet = (self.house["quiet_enabled"] and u["quiet"]
                 and in_window(self.house["quiet_from"], self.house["quiet_to"], now))
        v["quiet"] = quiet
        blocked = []
        if quiet:
            keep = ("m_hum", "a_hum") if u["quiet_hum"] else ()
            blocked = [r for r in want if r not in keep]
            want = [r for r in want if r in keep]
        v["blocked"] = blocked
        if any(r in ("m_hum", "m_air", "a_hum", "a_air") for r in want):
            rt["air_end"] = now.isoformat()        # Nachlauf zählt ab dem letzten Durchlauf mit Feuchte/Luft
        # ---- Sperren
        win, who = self.window_open(u, now)
        pause = _parse(rt.get("pause_until"))
        if pause and now >= pause:
            rt.pop("pause_until", None)
            pause = None
        running = bool(rt.get("running"))
        reasons = list(want)
        if win:
            return {**v, "on": False, "reasons": [], "wanted": want, "state": "window", "window": who, "label": "Fenster offen"}
        if pause:
            return {**v, "on": False, "reasons": [], "wanted": want, "state": "pause", "until": _iso(pause),
                    "label": "Pause nach Höchstlaufzeit"}
        if not reasons and running:
            ae = _parse(rt.get("air_end"))
            since = _parse(rt.get("on_since"))
            if ae and u["follow_min"] and now < ae + timedelta(minutes=u["follow_min"]) and not quiet:
                reasons, v["until"] = ["follow"], _iso(ae + timedelta(minutes=u["follow_min"]))
            elif since and u["min_run_min"] and now < since + timedelta(minutes=u["min_run_min"]):
                reasons, v["until"] = ["min_run"], _iso(since + timedelta(minutes=u["min_run_min"]))
        since = _parse(rt.get("on_since"))
        if reasons and running and since and u["max_run_min"] and now - since >= timedelta(minutes=u["max_run_min"]):
            rt["pause_until"] = (now + timedelta(minutes=u["pause_min"])).isoformat()
            return {**v, "on": False, "reasons": [], "wanted": want, "state": "pause", "until": rt["pause_until"],
                    "label": "Höchstlaufzeit erreicht", "stop": "Höchstlaufzeit"}
        if reasons:
            if "m_timer" in reasons and tm and not v.get("until"):
                v["until"] = _iso(_parse(tm["end"]))
            if "a_interval" in reasons and not v.get("until"):
                v["until"] = _iso(_parse(rt.get("interval_until")))
            spd = self._speed(u, reasons) if any(r in RULE_ORDER for r in reasons) else rt.get("speed", 0)
            return {**v, "on": True, "reasons": reasons, "wanted": want, "state": "run", "speed": spd,
                    "label": " + ".join(REASONS[r] for r in reasons)}
        if quiet and blocked:
            return {**v, "on": False, "reasons": [], "wanted": want, "state": "quiet", "label": "Nachtruhe"}
        nxt = _parse(tm["start"]) if tm and now < _parse(tm["start"]) else None
        return {**v, "on": False, "reasons": [], "wanted": want, "state": "idle", "until": _iso(nxt),
                "label": f"Startet {nxt.strftime('%H:%M')}" if nxt else "Bereit"}

    # ================================================================ Durchlauf
    async def _run(self) -> None:
        now = dt_util.now()
        for uid, u in list(self.units.items()):
            try:
                rt = self.rt.setdefault(uid, {})
                v = self._decide(u, now)
                if not self.house["enabled"] or not u["enabled"]:
                    v = {**v, "state": "off", "on": None, "label": "Hauptschalter aus" if not self.house["enabled"] else "Inaktiv"}
                    self.view[uid] = v
                    continue
                m = rt.get("manual")
                if m and now >= _parse(m["until"]):
                    rt.pop("manual", None)
                    for e in u["devices"]:
                        self.dev.pop(e, None)
                    self._log(rt, "Automatik wieder aktiv")
                    m = None
                if m:
                    # Hand-Schaltung: Automatik pausiert; „an“ zählt als Lauf (Höchstlaufzeit, Nachlauf)
                    if m["on"] and not rt.get("running"):
                        rt["running"], rt["on_since"] = True, now.isoformat()
                    elif not m["on"] and rt.get("running"):
                        rt["running"] = False
                        rt.pop("on_since", None)
                        rt["ran_at"] = now.isoformat()
                    self.view[uid] = {**v, "state": "manual", "on": m["on"], "manual": m, "until": m["until"],
                                      "label": f"Von Hand {'an' if m['on'] else 'aus'}"}
                    continue
                if v["on"]:
                    if not rt.get("running"):
                        rt["running"], rt["on_since"] = True, now.isoformat()
                        self._log(rt, "an · " + v["label"])
                        self.hass.bus.async_fire(EVENT_CHANGED, {"unit": uid, "room": u["room"], "action": "on",
                                                                 "reasons": v["reasons"]})
                    rt["speed"] = v.get("speed", 0)
                elif rt.get("running"):
                    self._stop_run(u, rt, now, v.get("stop") or v["label"])
                for e in u["devices"]:
                    await self._switch(e, bool(v["on"]), v.get("speed", 0) if v["on"] else 0)
                self.view[uid] = v
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Lüftungssteuerung: Lüfter %s", uid)
        snap = json.dumps(self.rt, sort_keys=True, default=str)
        if snap != self._snap:
            self._snap = snap
            self._save()

    async def _switch(self, e: str, on: bool, pct: int, force: bool = False) -> None:
        s = self._state(e)
        if not s or s.state in BAD:
            return
        dom = e.split(".")[0]
        is_on = s.state == "on"
        use_pct = dom == "fan" and on and pct > 0 and int(s.attributes.get("supported_features") or 0) & FAN_SET_SPEED
        need = is_on != on or (use_pct and s.attributes.get("percentage") != pct)
        if not need:
            return
        now = dt_util.utcnow()
        d = self.dev.get(e)
        if d and not force and (now - _parse(d["at"])).total_seconds() < SEND_MIN_GAP_S:
            return
        self.dev[e] = {"on": on, "pct": pct, "at": now.isoformat()}
        ctx = Context(parent_id=self.root.id)
        try:
            if dom == "fan":
                data: dict = {"entity_id": e}
                if use_pct:
                    data["percentage"] = pct
                await self.hass.services.async_call("fan", "turn_on" if on else "turn_off", data, blocking=False, context=ctx)
            else:
                await self.hass.services.async_call(dom, "turn_on" if on else "turn_off", {"entity_id": e},
                                                    blocking=False, context=ctx)
            _LOGGER.debug("Lüftung → %s: %s %s", e, "an" if on else "aus", pct or "")
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Lüftung → %s fehlgeschlagen: %s", e, err)

    # ================================================================ Anzeige
    def unit_view(self, uid: str) -> dict:
        u = self.units[uid]
        v = self.view.get(uid) or {}
        rt = self.rt.get(uid) or {}
        devs = []
        for e in u["devices"]:
            s = self._state(e)
            devs.append({"entity_id": e, "state": s.state if s else None,
                         "percentage": s.attributes.get("percentage") if s else None})
        return {**u,
                "state": v.get("state"), "on": v.get("on"), "label": v.get("label"), "reasons": v.get("reasons", []),
                "wanted": v.get("wanted", []), "blocked": v.get("blocked", []), "until": v.get("until"),
                "speed": v.get("speed"), "humidity": v.get("humidity"), "air": v.get("air"),
                "motion": v.get("motion"), "motion_active": v.get("motion_active", []),
                "motion_sensors_used": v.get("motion_sensors", self.motion_sensors(u)),
                "presence_sensors_used": v.get("presence_sensors", self.presence_sensors(u)),
                "presence_since": v.get("presence_since"), "presence_min": v.get("presence_min"),
                "presence_ok": v.get("presence_ok"), "presence_left": v.get("presence_left"),
                "armed": v.get("armed"), "armed_until": v.get("armed_until"), "timer": v.get("timer"),
                "interval_next": v.get("interval_next"), "quiet_now": v.get("quiet"), "window": v.get("window"),
                "manual": v.get("manual"), "running": bool(rt.get("running")), "on_since": rt.get("on_since"),
                "log": rt.get("log", []), "device_states": devs}

    def room_view(self, room: str) -> dict:
        us = [self.unit_view(k) for k, u in sorted(self.units.items(), key=lambda x: x[1]["name"].lower())
              if u["room"] == room]
        return {"units": us, "version": VERSION}

    def room_state(self, room: str) -> str:
        us = [k for k, u in self.units.items() if u["room"] == room and u["enabled"]]
        if not us or not self.house["enabled"]:
            return "off"
        return "on" if any((self.rt.get(k) or {}).get("running") for k in us) else "idle"

    def house_view(self) -> dict:
        now = dt_util.now()
        return {"settings": self.house, "version": VERSION,
                "quiet_now": self.house["quiet_enabled"] and in_window(self.house["quiet_from"], self.house["quiet_to"], now),
                "running": [k for k, r in self.rt.items() if r.get("running")]}

    @callback
    def _push(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
