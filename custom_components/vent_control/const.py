"""Konstanten der Lüftungssteuerung (vent_control)."""

DOMAIN = "vent_control"
VERSION = "1.2.0"

CONF_ROOMS = "rooms"
DEFAULT_ROOMS = ["wz", "bu", "ku", "sz", "fl", "ke", "ba", "bz"]

STORE_KEY = "vent_control"
STORE_VERSION = 1

TICK_SECONDS = [0, 20, 40]       # Prüftakt alle 20 s; zusätzlich sofort bei Änderung einer Quelle
SEND_MIN_GAP_S = 10              # höchstens ein Befehl je Gerät alle 10 s
MAX_UNITS_PER_ROOM = 6
LOG_LEN = 12                     # so viele Schaltvorgänge je Lüfter merken

SIGNAL_UPDATE = "vent_control_update"
EVENT_CHANGED = "vent_control_changed"

# Bewegungs-/Anwesenheitsmelder des Raums aus der Licht-Bewegungssteuerung, wenn im Lüfter keine eigenen gewählt sind
MOTION_HELPER = "input_text.{room}_light_motion_cfg_motion_sensors"
PRESENCE_HELPER = "input_text.{room}_light_motion_cfg_presence_sensors"

DEVICE_DOMAINS = ("fan", "switch", "light", "input_boolean")
TIMER_FROM = ["start", "end"]    # Verzögerung ab Bewegungsbeginn / ab Ende der Bewegung
PRESENCE_THEN = ["now", "delay"]  # nach erreichter Mindest-Anwesenheit: sofort / erst nach der Verzögerung

# Gründe, warum ein Lüfter läuft (Schlüssel → Anzeige)
REASONS = {
    "m_timer": "Nach Bewegung",
    "m_hum": "Bewegung + Feuchte",
    "m_air": "Bewegung + Luftqualität",
    "a_hum": "Feuchte",
    "a_air": "Luftqualität",
    "a_interval": "Intervall",
    "follow": "Nachlauf",
    "min_run": "Mindestlaufzeit",
    "manual": "Von Hand",
}

DEFAULT_HOUSE = {
    "enabled": True,             # Hauptschalter für alle Räume
    "quiet_enabled": False,      # hausweite Nachtruhe (je Lüfter abschaltbar)
    "quiet_from": "22:00",
    "quiet_to": "06:00",
}

# Schwellen: an über "on", aus unter "off" (Hysterese)
DEFAULT_UNIT = {
    "name": "Lüfter",
    "enabled": False,
    "devices": [],
    "motion_sensors": [],          # leer = Bewegungsmelder der Lichtsteuerung des Raums
    "presence_sensors": [],        # Anwesenheitsmelder (zählen wie Bewegung)
    "presence_light": False,       # keine gewählt + an = Anwesenheitsmelder der Lichtsteuerung des Raums
    # ---- Mindest-Anwesenheit: „nach Bewegung“ erst, wenn so lange am Stück Bewegung/Anwesenheit war (0 = aus)
    "min_presence_min": 0.0,
    "min_presence_then": "now",    # dann sofort an oder erst nach der Verzögerung der Zeit-Regel
    "presence_gap_s": 60,          # so lange darf der Melder zwischendurch „nichts“ melden, ohne dass der Aufenthalt endet
    "humidity_sensors": [],
    "air_sensors": [],             # CO2, VOC, Feinstaub … (höchster Wert zählt)
    "windows": [],
    "window_delay_s": 30,
    # ---- nach Bewegung
    "m_timer": {"enabled": True, "from": "start", "delay_min": 2.0, "run_min": 10.0, "restart": True, "speed": 0},
    "m_hum": {"enabled": False, "on": 65.0, "off": 58.0, "speed": 0},
    "m_air": {"enabled": False, "on": 1000.0, "off": 800.0, "speed": 0},
    "m_window_min": 30.0,          # so lange nach der letzten Bewegung reagieren Feuchte/Luft „nach Bewegung“
    # ---- ohne Bewegung (immer)
    "a_hum": {"enabled": False, "on": 75.0, "off": 65.0, "speed": 0},
    "a_air": {"enabled": False, "on": 1400.0, "off": 1000.0, "speed": 0},
    "a_interval": {"enabled": False, "every_h": 4.0, "run_min": 10.0, "speed": 0},
    # ---- Laufzeit, Ruhe, Hand
    "min_run_min": 2.0,
    "follow_min": 2.0,             # Nachlauf, nachdem Feuchte/Luft wieder gut sind
    "max_run_min": 90.0,           # 0 = unbegrenzt
    "pause_min": 15.0,             # Pause nach Erreichen der Höchstlaufzeit
    "quiet": True,                 # hausweite Nachtruhe gilt für diesen Lüfter
    "quiet_hum": True,             # in der Nachtruhe darf Feuchte trotzdem lüften
    "manual_hold_min": 30.0,       # Hand-Schaltung: so lange nicht eingreifen (0 = sofort wieder Automatik)
}
