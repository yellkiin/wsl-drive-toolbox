"""Small, per-user preferences; never store proxy secrets or disk inventories."""
import json
import os
from pathlib import Path


def settings_path():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / '.config')) / 'WSLDriveToolbox' / 'settings.json'


def load_settings(path=None):
    try:
        data = Path(path or settings_path()).read_text(encoding='utf-8')
        value = json.loads(data)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings, path=None):
    path = Path(path or settings_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
