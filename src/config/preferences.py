from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_PREF_PATH = Path(__file__).resolve().parent / "user_prefs.json"


def load_preferences(path: Optional[Path] = None) -> Dict[str, Any]:
    target = Path(path or DEFAULT_PREF_PATH)
    if not target.exists():
        return {}
    try:
        raw = target.read_text(encoding="utf-8")
        return json.loads(raw) if raw else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_preferences(prefs: Dict[str, Any], path: Optional[Path] = None) -> None:
    target = Path(path or DEFAULT_PREF_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(prefs, indent=2, sort_keys=True)
    target.write_text(data, encoding="utf-8")
