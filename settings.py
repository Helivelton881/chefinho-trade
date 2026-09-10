"""Configurações persistentes e locais do painel do Telegram."""

import json
from pathlib import Path

SETTINGS_FILE = Path(__file__).with_name("settings.json")
DEFAULTS = {
    "account_mode": "PRACTICE",
    "entry_amount": 2.0,
    "stop_loss": 10.0,
    "stop_win": 10.0,
    "expiration": 1,
    "autodemo_enabled": False,
    "signals": {},
    "daily_result": 0.0,
    "daily_date": "",
}

def load_settings():
    if not SETTINGS_FILE.exists():
        return DEFAULTS.copy()
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as file:
            saved = json.load(file)
    except (OSError, json.JSONDecodeError):
        return DEFAULTS.copy()
    return {**DEFAULTS, **saved}

def save_settings(settings):
    with SETTINGS_FILE.open("w", encoding="utf-8") as file:
        json.dump(settings, file, ensure_ascii=False, indent=2)
