import json
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / 'data'
DB_DIR = DATA_DIR / 'db'

LEGACY_USERS_FILE = DATA_DIR / 'users.json'
LEGACY_CONFIG_FILE = DATA_DIR / 'config.json'
LEGACY_PANEL_CONFIG_FILE = ROOT_DIR / 'panel_config.json'
LEGACY_BANS_FILE = DATA_DIR / 'bans.json'
LEGACY_PLAYER_PROFILES_FILE = DATA_DIR / 'player_profiles.json'

USERS_FILE = DB_DIR / 'users.json'
CONFIG_FILE = DB_DIR / 'server.json'
PANEL_CONFIG_FILE = DB_DIR / 'panel.json'
BANS_FILE = DB_DIR / 'bans.json'
PLAYER_PROFILES_FILE = DB_DIR / 'player_profiles.json'
STATS_HISTORY_FILE = DB_DIR / 'stats_history.json'

DATA_DIR.mkdir(exist_ok=True)
DB_DIR.mkdir(exist_ok=True)


def _default_status_embed_template():
    return {
        "title": "{{serverName}}",
        "description": "",
        "fields": [
            {
                "name": "> STATUS",
                "value": "```\n{{statusString}}\n```",
                "inline": True
            },
            {
                "name": "> PLAYERS",
                "value": "```\n{{serverClients}}/{{serverMaxClients}}\n```",
                "inline": True
            },
            {
                "name": "> F8 CONNECT COMMAND",
                "value": "```\nconnect play.xanite.cz\n```"
            },
            {
                "name": "> NEXT RESTART",
                "value": "```\n{{nextScheduledRestart}}\n```",
                "inline": True
            },
            {
                "name": "> UPTIME",
                "value": "```\n{{uptime}}\n```",
                "inline": True
            }
        ],
        "image": {},
        "thumbnail": {}
    }


def _default_status_config():
    return {
        "onlineString": "Online",
        "onlineColor": "#0BA70B",
        "partialString": "Partial",
        "partialColor": "#FFF100",
        "offlineString": "Offline",
        "offlineColor": "#A70B28",
        "buttons": []
    }


def _default_discord_settings():
    return {
        'enabled': False,
        'token': '',
        'guild_id': '',
        'warnings_channel_id': '',
        'status_embed_json': json.dumps(_default_status_embed_template(), indent=4, ensure_ascii=False),
        'status_config_json': json.dumps(_default_status_config(), indent=4, ensure_ascii=False),
        'status_messages': []
    }


def _normalize_discord_settings(data):
    base = _default_discord_settings()
    incoming = data if isinstance(data, dict) else {}

    base['enabled'] = bool(incoming.get('enabled', base['enabled']))
    base['token'] = str(incoming.get('token', base['token']) or '').strip()
    base['guild_id'] = str(incoming.get('guild_id', base['guild_id']) or '').strip()
    base['warnings_channel_id'] = str(incoming.get('warnings_channel_id', base['warnings_channel_id']) or '').strip()

    status_embed_json = incoming.get('status_embed_json')
    if isinstance(status_embed_json, str) and status_embed_json.strip():
        base['status_embed_json'] = status_embed_json

    status_config_json = incoming.get('status_config_json')
    if isinstance(status_config_json, str) and status_config_json.strip():
        base['status_config_json'] = status_config_json

    status_messages = incoming.get('status_messages')
    if isinstance(status_messages, list):
        cleaned = []
        for item in status_messages:
            if not isinstance(item, dict):
                continue
            channel_id = str(item.get('channel_id') or '').strip()
            message_id = str(item.get('message_id') or '').strip()
            if not channel_id or not message_id:
                continue
            cleaned.append({
                'channel_id': channel_id,
                'message_id': message_id
            })
        base['status_messages'] = cleaned

    return base


def _default_panel_config():
    return {
        'locale': 'en',
        'panel_name': 'RageAdmin',
        'auto_start': False,
        'scheduled_restarts': [],
        'panel_secret': 'changeme',
        'panel_version': '0.0.0',
        'panel_port': 20000,
        'discord': _default_discord_settings()
    }


def _default_server_config():
    return {
        'server_path': './RageMP-Server/ragemp-srv/ragemp-server',
        'server_name': 'ragemp-server',
        'log_file': './RageMP-Server/ragemp-srv/server.log',
        'auto_restart': False,
        'restart_delay': 5,
        'max_restarts': 10,
        'ragemp_version': '',
        'ragemp_archive_url': '',
        'ragemp_etag': '',
        'ragemp_last_modified': ''
    }


def _default_stats_history():
    return {
        'samples': [],
        'sample_interval_sec': 10,
        'max_samples': 720,
        'updated_at': ''
    }


def _json_load(path: Path, default):
    try:
        if path.exists():
            with open(path, 'r', encoding='utf-8-sig') as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _json_save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)


def _ensure_dict(value, default):
    if isinstance(value, dict):
        return value
    return dict(default)


def _migrate_one(legacy_path: Path, target_path: Path):
    if target_path.exists() or not legacy_path.exists():
        return False
    data = _json_load(legacy_path, None)
    if data is None:
        return False
    _json_save(target_path, data)
    return True


def migrate_legacy_files():
    _migrate_one(LEGACY_USERS_FILE, USERS_FILE)
    _migrate_one(LEGACY_CONFIG_FILE, CONFIG_FILE)
    _migrate_one(LEGACY_PANEL_CONFIG_FILE, PANEL_CONFIG_FILE)
    _migrate_one(LEGACY_BANS_FILE, BANS_FILE)
    _migrate_one(LEGACY_PLAYER_PROFILES_FILE, PLAYER_PROFILES_FILE)


def ensure_storage_files():
    load_users()
    load_config()
    load_panel_config()
    load_bans()
    load_player_profiles()
    load_stats_history()


def load_users():
    migrate_legacy_files()
    data = _json_load(USERS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_users(users: dict):
    _json_save(USERS_FILE, users if isinstance(users, dict) else {})


def load_config():
    migrate_legacy_files()
    default_config = _default_server_config()
    data = _json_load(CONFIG_FILE, None)
    if data is None:
        _json_save(CONFIG_FILE, default_config)
        return dict(default_config)

    data = _ensure_dict(data, default_config)
    for key, value in default_config.items():
        data.setdefault(key, value)
    return data


def save_config(cfg: dict):
    _json_save(CONFIG_FILE, cfg if isinstance(cfg, dict) else _default_server_config())


def load_panel_config():
    migrate_legacy_files()
    defaults = _default_panel_config()
    data = _json_load(PANEL_CONFIG_FILE, None)
    if data is None:
        _json_save(PANEL_CONFIG_FILE, defaults)
        return dict(defaults)

    data = _ensure_dict(data, defaults)
    for key, value in defaults.items():
        if key == 'discord':
            data['discord'] = _normalize_discord_settings(data.get('discord'))
        else:
            data.setdefault(key, value)
    return data


def save_panel_config(cfg: dict):
    payload = cfg if isinstance(cfg, dict) else {}
    payload.setdefault('discord', _default_discord_settings())
    payload['discord'] = _normalize_discord_settings(payload.get('discord'))
    _json_save(PANEL_CONFIG_FILE, payload)


def load_bans():
    migrate_legacy_files()
    data = _json_load(BANS_FILE, [])
    return data if isinstance(data, list) else []


def save_bans(bans: list):
    _json_save(BANS_FILE, bans if isinstance(bans, list) else [])


def load_player_profiles():
    migrate_legacy_files()
    data = _json_load(PLAYER_PROFILES_FILE, {})
    return data if isinstance(data, dict) else {}


def save_player_profiles(profiles: dict):
    _json_save(PLAYER_PROFILES_FILE, profiles if isinstance(profiles, dict) else {})


def load_stats_history():
    migrate_legacy_files()
    defaults = _default_stats_history()
    data = _json_load(STATS_HISTORY_FILE, None)
    if data is None:
        _json_save(STATS_HISTORY_FILE, defaults)
        return dict(defaults)

    data = _ensure_dict(data, defaults)
    samples = data.get('samples')
    if not isinstance(samples, list):
        data['samples'] = []
    data['sample_interval_sec'] = int(data.get('sample_interval_sec') or defaults['sample_interval_sec'])
    data['max_samples'] = int(data.get('max_samples') or defaults['max_samples'])
    data['updated_at'] = str(data.get('updated_at') or '')
    return data


def save_stats_history(history: dict):
    payload = history if isinstance(history, dict) else {}
    payload.setdefault('samples', [])
    payload.setdefault('sample_interval_sec', 10)
    payload.setdefault('max_samples', 720)
    payload.setdefault('updated_at', '')
    _json_save(STATS_HISTORY_FILE, payload)


migrate_legacy_files()
