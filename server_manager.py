try:
    import flask_patch
except ImportError:
    pass                                        

import os
import argparse
import sys
import subprocess
import threading
import asyncio
import time
import signal
import json
import hashlib
import base64
import secrets
import string
import shutil
import tarfile
import zipfile
import urllib.request
import tempfile
import re
import stat
import logging
import xml.etree.ElementTree as ET
from io import BytesIO
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from functools import wraps

from flask import Flask, render_template, jsonify, request, session, redirect, url_for, send_from_directory, send_file, abort
from flask_socketio import SocketIO, emit, disconnect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import psutil

try:
    import discord
    from discord import app_commands
except Exception:
    discord = None
    app_commands = None

import storage

ROOT_DIR = Path(__file__).resolve().parent


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def _env_str(name: str, default: str = '') -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip()


FORCE_HTTPS = _env_bool('PANEL_FORCE_HTTPS', False)
SESSION_COOKIE_SECURE = _env_bool('PANEL_SESSION_COOKIE_SECURE', FORCE_HTTPS)
PANEL_PRODUCTION_MODE = _env_bool('PANEL_PRODUCTION', True)
PANEL_ACCESS_LOGS = _env_bool('PANEL_ACCESS_LOGS', not PANEL_PRODUCTION_MODE)
PANEL_SOCKETIO_ASYNC_MODE = _env_str('PANEL_SOCKETIO_ASYNC_MODE', '').lower()


def _resolve_socketio_async_mode():
    requested = PANEL_SOCKETIO_ASYNC_MODE
    is_windows = (os.name == 'nt')

    if requested in ('threading', 'eventlet'):
        if requested == 'eventlet':
            if is_windows:
                return 'threading', 'eventlet_disabled_windows'
            try:
                import eventlet  # noqa: F401
                return 'eventlet', 'forced_eventlet'
            except Exception:
                return 'threading', 'eventlet_missing'
        return 'threading', 'forced_threading'

    return 'threading', 'default_threading'


SOCKETIO_ASYNC_MODE, SOCKETIO_ASYNC_REASON = _resolve_socketio_async_mode()

app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(32)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024                      
app.config['SESSION_COOKIE_SECURE'] = SESSION_COOKIE_SECURE
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
socketio = SocketIO(
    app,
    async_mode=SOCKETIO_ASYNC_MODE,
    logger=False,
    engineio_logger=False,
    ping_interval=25,
    ping_timeout=30
)

if PANEL_PRODUCTION_MODE and not PANEL_ACCESS_LOGS:
    werkzeug_logger = logging.getLogger('werkzeug')
    werkzeug_logger.setLevel(logging.ERROR)
    werkzeug_logger.propagate = False

                                                         
_login_attempts = {}                                               
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_LOCK_SECONDS = 300             

       
DATA_DIR = ROOT_DIR / 'data'
CONFIG_FILE = DATA_DIR / 'config.json'
PANEL_CONFIG_FILE = ROOT_DIR / 'panel_config.json'
LOCALES_DIR = ROOT_DIR / 'locales'
LOGS_DIR = DATA_DIR / 'logs'
PID_FILE = DATA_DIR / 'server.pid'
TEMPLATES_DIR = ROOT_DIR / 'templates'
PFP_DIR = TEMPLATES_DIR / 'pfp'
PFP_ALLOWED_EXTS = {'.png', '.jpg', '.jpeg', '.svg', '.webp'}

                    
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
LOCALES_DIR.mkdir(exist_ok=True)

              
server_state = {
    'running': False,
    'pid': None,
    'start_time': None,
    'auto_restart': False,
    'restart_count': 0,
    'cpu_usage': 0,
    'memory_usage': 0,
    'attached': False
}

server_process = None
console_lines = []
MAX_CONSOLE_LINES = 1000
ragemp_startup_table_active = False
ragemp_startup_table_armed = False

                                                                    
resource_states = {}

                                                        
connected_players = {}                                                                  
pending_actions = []                                                                        
panel_connector_last_heartbeat = 0.0
player_profiles = {}
PROFILE_HISTORY_LIMIT = 120
PROFILE_WARN_LIMIT = 200
PROFILE_PLAYER_ID_PREFIX = 'PLR'
PROFILE_PLAYER_ID_LEN = 12
RUNTIME_SAMPLE_INTERVAL_SEC = 10
RUNTIME_SAMPLE_MAX_POINTS = 720
runtime_stats_history = storage.load_stats_history()
runtime_stats_dirty = False
runtime_stats_last_saved_at = 0.0
runtime_stats_last_sample_at = 0.0


DEFAULT_DISCORD_STATUS_EMBED_TEMPLATE = {
    'title': '{{serverName}}',
    'description': '',
    'fields': [
        {
            'name': '> STATUS',
            'value': '```\n{{statusString}}\n```',
            'inline': True
        },
        {
            'name': '> PLAYERS',
            'value': '```\n{{serverClients}}/{{serverMaxClients}}\n```',
            'inline': True
        },
        {
            'name': '> CONNECTED PLAYERS',
            'value': '```\n{{connectedPlayersList}}\n```'
        },
        {
            'name': '> F8 CONNECT COMMAND',
            'value': '```\nconnect play.xanite.cz\n```'
        },
        {
            'name': '> NEXT RESTART',
            'value': '```\n{{nextScheduledRestart}}\n```',
            'inline': True
        },
        {
            'name': '> UPTIME',
            'value': '```\n{{uptime}}\n```',
            'inline': True
        }
    ],
    'image': {},
    'thumbnail': {}
}

DEFAULT_DISCORD_STATUS_CONFIG = {
    'onlineString': 'Online',
    'onlineColor': '#0BA70B',
    'partialString': 'Partial',
    'partialColor': '#FFF100',
    'offlineString': 'Offline',
    'offlineColor': '#A70B28',
    'buttons': []
}

          
BANS_FILE = DATA_DIR / 'bans.json'

                      
SETUP_SERVER_ARCHIVE_URL = 'https://cdn.rage.mp/updater/prerelease/server-files/linux_x64.tar.gz'
RAGEMP_SERVER_ROOT_DIR = ROOT_DIR / 'RageMP-Server'
RAGEMP_SERVER_DIR_NAME = 'ragemp-srv'
RAGEMP_SERVER_EXECUTABLE = 'ragemp-server'
RAGEMP_CONTENT_DIRS = ('packages', 'client_packages', 'maps', 'plugins')
RAGEMP_BRIDGE_PACKAGE_NAME = 'rageadmin'
RAGEMP_BRIDGE_TEMPLATE_DIR = ROOT_DIR / 'package_templates' / RAGEMP_BRIDGE_PACKAGE_NAME
RAGEMP_BRIDGE_CLIENT_TEMPLATE_DIR = ROOT_DIR / 'package_templates' / f'{RAGEMP_BRIDGE_PACKAGE_NAME}_client'
RAGEMP_STARTUP_TABLE_END_MARKERS = (
    '[info] loading nodejs packages',
    '[info] starting packages',
    '[done] server packages have been started.',
    '[done] started resource transfer server',
    '[done] client-side packages weight',
    '[info] initializing networking',
    '[done] networking has been started',
    '[done] the server is ready to accept connections.'
)


def _resolve_ragemp_default_port():
    raw = os.getenv('SERVER_PORT')
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return 22005
    if 1 <= port <= 65535:
        return port
    return 22005


def _resolve_ragemp_default_bind():
    if os.getenv('SERVER_PORT'):
        return '0.0.0.0'
    return '127.0.0.1'


def _is_official_ragemp_archive(url: str = '') -> bool:
    normalized = str(url or '').strip().lower()
    return normalized.startswith('https://cdn.rage.mp/updater/prerelease/server-files/')


def _looks_like_header_date_version(version: str = '') -> bool:
    return bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(version or '').strip()))


RAGEMP_DEFAULT_SETTINGS = {
    'announce': False,
    'bind': _resolve_ragemp_default_bind(),
    'gamemode': 'freeroam',
    'name': 'RageMP Server',
    'maxplayers': 100,
    'port': _resolve_ragemp_default_port(),
    'stream-distance': 500.0,
    'language': 'us',
    'sync-rate': 40,
    'enable-nodejs': True,
    'csharp': 'disabled',
    'enable-http-security': False,
    'allow-cef-debugging': False,
    'voice-chat': True
}
SETUP_DOWNLOAD_DIR = DATA_DIR / 'downloads'
DEFAULT_PANEL_PORT = 20000
PANEL_PORT = DEFAULT_PANEL_PORT


def _panel_port_arg(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('Port must be an integer')
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError('Port must be between 1 and 65535')
    return port


def _panel_port_or_default(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PANEL_PORT
    if port < 1 or port > 65535:
        return DEFAULT_PANEL_PORT
    return port


def get_panel_host():
    return f'http://127.0.0.1:{PANEL_PORT}'

setup_state = {
    'running': False,
    'finished': False,
    'success': False,
    'progress': 0,
    'step': '',
    'message': '',
    'log': []
}
setup_lock = threading.Lock()
SETUP_PIN_FILE = DATA_DIR / 'setup_pin.json'


def _load_setup_pin():
    try:
        if SETUP_PIN_FILE.exists():
            return json.loads(SETUP_PIN_FILE.read_text(encoding='utf-8'))
    except Exception:
        return None
    return None


def _save_setup_pin(pin: str):
    payload = {
        'pin': str(pin),
        'created_at': datetime.now().isoformat()
    }
    SETUP_PIN_FILE.write_text(json.dumps(payload, indent=4), encoding='utf-8')
    try:
        os.chmod(SETUP_PIN_FILE, 0o600)
    except Exception:
        pass


def _clear_setup_pin():
    try:
        if SETUP_PIN_FILE.exists():
            SETUP_PIN_FILE.unlink()
    except Exception:
        pass


def get_setup_pin():
    data = _load_setup_pin()
    if not data:
        return None
    pin = str(data.get('pin', '')).strip()
    if re.fullmatch(r'\d{4}', pin):
        return pin
    return None


def ensure_setup_pin():
    
    if not setup_required():
        _clear_setup_pin()
        return None
    existing_pin = get_setup_pin()
    if existing_pin:
        return existing_pin
    pin = f"{secrets.randbelow(10000):04d}"
    _save_setup_pin(pin)
    return pin


def verify_setup_pin(pin: str) -> bool:
    if not pin or not re.fullmatch(r'\d{4}', str(pin).strip()):
        return False
    data = _load_setup_pin()
    if not data:
        return False
    stored = str(data.get('pin', '')).strip()
    return secrets.compare_digest(stored, str(pin).strip())


def _announce_setup_pin(pin: str):
    message = f"[SETUP] Setup not completed. PIN: {pin}"
    print(message)
    add_console_line(message)

                                                         
                                                              
PANEL_VERSION_FILE = ROOT_DIR / 'panel_version.json'
UPDATE_CONFIG_FILE = ROOT_DIR / 'update_config.json'
UPDATE_STATUS_FILE = DATA_DIR / 'update_status.json'
UPDATE_JOB_FILE = DATA_DIR / 'update_job.json'

DEFAULT_PANEL_REPO = 'zuraxscripts/RageAdmin'
DEFAULT_UPDATE_CONFIG_URL = ''
DEFAULT_RAGEMP_SERVER_URL = SETUP_SERVER_ARCHIVE_URL
DEFAULT_UPDATE_INTERVAL_MINUTES = 30

update_state = {
    'checking': False,
    'last_check': None,
    'error': '',
    'panel': {
        'current': '',
        'latest': '',
        'available': False,
        'zip_url': '',
        'release_url': ''
    },
    'ragemp': {
        'current': '',
        'latest': '',
        'available': False,
        'archive_url': '',
        'etag': '',
        'last_modified': ''
    }
}
update_lock = threading.Lock()

def _setup_log(message: str):
    timestamp = datetime.now().strftime('%H:%M:%S')
    line = f'[{timestamp}] {message}'
    with setup_lock:
        setup_state['log'].append(line)
        if len(setup_state['log']) > 200:
            setup_state['log'].pop(0)
        setup_state['message'] = message
    add_console_line(f'[SETUP] {message}')


def _setup_update(step=None, progress=None, message=None):
    with setup_lock:
        if step is not None:
            setup_state['step'] = step
        if progress is not None:
            setup_state['progress'] = int(progress)
        if message is not None:
            setup_state['message'] = message
    if message:
        _setup_log(message)


def _setup_fail(message: str):
    with setup_lock:
        setup_state['running'] = False
        setup_state['finished'] = True
        setup_state['success'] = False
        setup_state['message'] = message
    _setup_log(f'FAILED: {message}')


def _setup_finish():
    with setup_lock:
        setup_state['running'] = False
        setup_state['finished'] = True
        setup_state['success'] = True
        setup_state['progress'] = 100
        setup_state['message'] = 'Setup complete'
    _setup_log('Setup complete')
    add_console_line('[SETUP] Cleaning setup downloads')
    _cleanup_setup_downloads()
    _clear_setup_pin()


def load_bans():
    
    try:
        return storage.load_bans()
    except Exception:
        return []


def save_bans(bans):
    
    storage.save_bans(bans)


def normalize_player_ip(ip):
    value = str(ip or '').strip()
    if value.lower().startswith('::ffff:'):
        return value[7:]
    return value


def is_player_banned(ip=None, name=None):
    
    ip_norm = normalize_player_ip(ip)
    name_norm = str(name or '').strip().lower()
    bans = load_bans()
    for ban in bans:
        ban_ip = normalize_player_ip(ban.get('ip'))
        ban_name = str(ban.get('name', '')).strip().lower()
        if ip_norm and ban_ip == ip_norm and ban_ip:
            return True, ban
        if name_norm and ban_name == name_norm and ban_name:
            return True, ban
    return False, None


def load_player_profiles():
    try:
        data = storage.load_player_profiles()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_player_profiles(profiles):
    storage.save_player_profiles(profiles if isinstance(profiles, dict) else {})


def _queue_pending_action(action):
    global pending_actions
    if not isinstance(action, dict):
        return
    pending_actions.append(dict(action))


def _queue_restart_notice(message, duration=None, title='Server Restart'):
    try:
        base_duration = int(duration if duration is not None else config.get('restart_delay', 5))
    except Exception:
        base_duration = 5
    base_duration = max(4, min(30, base_duration))
    _queue_pending_action({
        'type': 'broadcast',
        'title': _safe_profile_text(title, 64) or 'Server Restart',
        'message': _safe_profile_text(message, 280) or 'Server restart incoming',
        'duration': base_duration,
        'variant': 'restart'
    })


def _now_iso():
    return datetime.now().isoformat(timespec='seconds')


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _normalize_runtime_stats_history():
    global runtime_stats_history

    if not isinstance(runtime_stats_history, dict):
        runtime_stats_history = {}

    samples = runtime_stats_history.get('samples')
    if not isinstance(samples, list):
        samples = []

    max_points = max(60, _safe_int(runtime_stats_history.get('max_samples', RUNTIME_SAMPLE_MAX_POINTS), RUNTIME_SAMPLE_MAX_POINTS))
    interval = max(5, _safe_int(runtime_stats_history.get('sample_interval_sec', RUNTIME_SAMPLE_INTERVAL_SEC), RUNTIME_SAMPLE_INTERVAL_SEC))

    cleaned = []
    for row in samples[-max_points:]:
        if not isinstance(row, dict):
            continue
        cleaned.append({
            'ts': _safe_profile_text(row.get('ts'), 64),
            'cpu': round(float(row.get('cpu') or 0), 2),
            'memory': round(float(row.get('memory') or 0), 2),
            'players': max(0, _safe_int(row.get('players'), 0)),
            'saved_players': max(0, _safe_int(row.get('saved_players'), 0)),
            'max_players': max(0, _safe_int(row.get('max_players'), 0)),
            'running': bool(row.get('running')),
            'bridge_online': bool(row.get('bridge_online')),
            'resources_running': max(0, _safe_int(row.get('resources_running'), 0))
        })

    runtime_stats_history = {
        'samples': cleaned[-max_points:],
        'sample_interval_sec': interval,
        'max_samples': max_points,
        'updated_at': _safe_profile_text(runtime_stats_history.get('updated_at'), 64)
    }
    return runtime_stats_history


def _count_saved_player_profiles():
    return sum(1 for profile in player_profiles.values() if isinstance(profile, dict))


def _bridge_online(now_ts=None):
    check_ts = now_ts if now_ts is not None else time.time()
    return bool(panel_connector_last_heartbeat and (check_ts - panel_connector_last_heartbeat) <= 45)


def _current_max_players():
    try:
        settings = parse_settings_xml()
        return max(0, _safe_int((settings or {}).get('maxplayers'), 0))
    except Exception:
        return 0


def _current_resources_running():
    return sum(1 for state in resource_states.values() if state == 'started')


def _build_runtime_sample(now_ts=None):
    ts = now_ts if now_ts is not None else time.time()
    return {
        'ts': _now_iso(),
        'cpu': round(float(server_state.get('cpu_usage') or 0), 2),
        'memory': round(float(server_state.get('memory_usage') or 0), 2),
        'players': len(connected_players),
        'saved_players': _count_saved_player_profiles(),
        'max_players': _current_max_players(),
        'running': bool(server_state.get('running')),
        'bridge_online': _bridge_online(ts),
        'resources_running': _current_resources_running()
    }


def _save_runtime_stats_history():
    global runtime_stats_last_saved_at
    _normalize_runtime_stats_history()
    runtime_stats_history['updated_at'] = _now_iso()
    storage.save_stats_history(runtime_stats_history)
    runtime_stats_last_saved_at = time.time()


def _append_runtime_sample(force=False, emit_socket=True):
    global runtime_stats_last_sample_at, runtime_stats_dirty

    _normalize_runtime_stats_history()
    now_ts = time.time()
    interval = max(5, _safe_int(runtime_stats_history.get('sample_interval_sec'), RUNTIME_SAMPLE_INTERVAL_SEC))
    if not force and runtime_stats_last_sample_at and (now_ts - runtime_stats_last_sample_at) < interval:
        return None

    sample = _build_runtime_sample(now_ts)
    runtime_stats_history['samples'].append(sample)
    max_points = max(60, _safe_int(runtime_stats_history.get('max_samples'), RUNTIME_SAMPLE_MAX_POINTS))
    if len(runtime_stats_history['samples']) > max_points:
        del runtime_stats_history['samples'][:-max_points]

    runtime_stats_last_sample_at = now_ts
    runtime_stats_dirty = True
    _save_runtime_stats_history()

    if emit_socket:
        socketio.emit('stats_point', sample)
    return sample


def build_runtime_status_payload(include_history=False):
    _normalize_runtime_stats_history()
    now_ts = time.time()
    payload = {
        'running': server_state['running'],
        'uptime': int(now_ts - server_state['start_time']) if server_state['start_time'] else 0,
        'auto_restart': server_state['auto_restart'],
        'restart_count': server_state['restart_count'],
        'cpu': server_state['cpu_usage'],
        'memory': server_state['memory_usage'],
        'attached': server_state['attached'],
        'players_online': len(connected_players),
        'players_saved': _count_saved_player_profiles(),
        'max_players': _current_max_players(),
        'resources_running': _current_resources_running(),
        'resources_total': len(resource_states),
        'bridge_online': _bridge_online(now_ts),
        'bridge_last_heartbeat': panel_connector_last_heartbeat,
        'bridge_age_sec': round(max(0.0, now_ts - panel_connector_last_heartbeat), 1) if panel_connector_last_heartbeat else None
    }
    if include_history:
        payload['history'] = list(runtime_stats_history.get('samples') or [])
        payload['history_meta'] = {
            'sample_interval_sec': _safe_int(runtime_stats_history.get('sample_interval_sec'), RUNTIME_SAMPLE_INTERVAL_SEC),
            'max_samples': _safe_int(runtime_stats_history.get('max_samples'), RUNTIME_SAMPLE_MAX_POINTS),
            'updated_at': runtime_stats_history.get('updated_at') or ''
        }
    return payload


def _safe_profile_name(value):
    name = str(value or '').strip()
    if not name:
        return 'Unknown'
    return name[:64]


def _safe_profile_text(value, max_len=2000):
    return str(value or '').strip()[:max_len]


def _normalize_player_id(value):
    pid = _safe_profile_text(value, 64).upper()
    if not pid:
        return ''
    return re.sub(r'[^A-Z0-9\-]', '', pid)[:64]


def _generate_player_id(existing_ids=None):
    existing = set(existing_ids or [])
    while True:
        candidate = f'{PROFILE_PLAYER_ID_PREFIX}-{secrets.token_hex(4).upper()}'
        if len(candidate) > PROFILE_PLAYER_ID_LEN:
            candidate = candidate[:PROFILE_PLAYER_ID_LEN]
        if candidate not in existing:
            return candidate


def _ensure_profile_player_id(profile, existing_ids=None):
    if not isinstance(profile, dict):
        return False
    pid = _normalize_player_id(profile.get('player_id'))
    if pid:
        profile['player_id'] = pid
        return False
    profile['player_id'] = _generate_player_id(existing_ids=existing_ids)
    return True


def _ensure_all_profile_player_ids():
    changed = False
    known_ids = set()
    for profile in player_profiles.values():
        if not isinstance(profile, dict):
            continue
        pid = _normalize_player_id(profile.get('player_id'))
        if not pid:
            continue
        profile['player_id'] = pid
        known_ids.add(pid)

    for profile in player_profiles.values():
        if not isinstance(profile, dict):
            continue
        if _ensure_profile_player_id(profile, existing_ids=known_ids):
            changed = True
            known_ids.add(profile.get('player_id'))
    return changed


def _find_profile_by_player_id(player_id):
    needle = _normalize_player_id(player_id)
    if not needle:
        return None, None
    for key, profile in player_profiles.items():
        if not isinstance(profile, dict):
            continue
        if _normalize_player_id(profile.get('player_id')) == needle:
            return key, profile
    return None, None


def _normalize_identifier_key(key):
    k = str(key or '').strip().lower()
    mapping = {
        'rockstarid': 'rockstar_id',
        'rockstar_id': 'rockstar_id',
        'socialclub': 'social_club',
        'social_club': 'social_club',
        'rgscid': 'rgsc_id',
        'rgsc_id': 'rgsc_id',
        'serial': 'serial',
        'gametype': 'game_type',
        'game_type': 'game_type',
        'license': 'license',
        'license2': 'license2',
        'steam': 'steam',
        'discord': 'discord',
        'xbl': 'xbl',
        'live': 'live'
    }
    return mapping.get(k, re.sub(r'[^a-z0-9_]+', '_', k)[:32] or 'id')


def _extract_player_identifiers(player_data):
    out = {}
    src = player_data if isinstance(player_data, dict) else {}
    nested = src.get('identifiers')
    if isinstance(nested, dict):
        for k, v in nested.items():
            val = _safe_profile_text(v, 128)
            if val:
                out[_normalize_identifier_key(k)] = val

    for key in (
        'rockstarId',
        'rockstar_id',
        'socialClub',
        'social_club',
        'rgscId',
        'rgsc_id',
        'serial',
        'gameType',
        'game_type',
        'license',
        'license2',
        'steam',
        'discord',
        'xbl',
        'live'
    ):
        val = _safe_profile_text(src.get(key), 128)
        if val:
            out[_normalize_identifier_key(key)] = val

    return out


def _profile_candidate_keys(player_data, identifiers=None):
    src = player_data if isinstance(player_data, dict) else {}
    ids = identifiers if isinstance(identifiers, dict) else _extract_player_identifiers(src)
    candidates = []

    for id_key in ('rgsc_id', 'serial', 'social_club', 'rockstar_id', 'license', 'license2', 'steam', 'discord'):
        val = _safe_profile_text(ids.get(id_key), 128)
        if val:
            candidates.append(f'{id_key}:{val.lower()}')

    ip = normalize_player_ip(src.get('ip', ''))
    if ip:
        candidates.append(f'ip:{ip}')

    name = _safe_profile_name(src.get('name')).lower()
    if name and name != 'unknown':
        candidates.append(f'name:{name}')

    sid = str(src.get('serverId') or '').strip()
    if sid:
        candidates.append(f'sid:{sid}')

    unique = []
    seen = set()
    for key in candidates:
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)
    return unique


def _new_player_profile(profile_key):
    now = _now_iso()
    return {
        'profile_key': profile_key,
        'player_id': _generate_player_id(),
        'first_seen_at': now,
        'last_connection_at': now,
        'connections': 0,
        'total_playtime_sec': 0,
        'notes': '',
        'identifiers': {},
        'warnings': [],
        'history': [],
        'last_name': '',
        'last_ip': '',
        'last_server_id': '',
        'last_social_club': '',
        'last_rgsc_id': '',
        'last_serial': '',
        'last_game_type': '',
        'last_packet_loss': 0
    }


def _append_profile_history(profile, event_type, details=''):
    if not isinstance(profile, dict):
        return
    hist = profile.setdefault('history', [])
    hist.append({
        'type': _safe_profile_text(event_type, 32) or 'event',
        'details': _safe_profile_text(details, 280),
        'at': _now_iso()
    })
    if len(hist) > PROFILE_HISTORY_LIMIT:
        del hist[:-PROFILE_HISTORY_LIMIT]


def _append_profile_warning(profile, reason, warned_by):
    if not isinstance(profile, dict):
        return None
    warns = profile.setdefault('warnings', [])
    entry = {
        'id': secrets.token_hex(6),
        'reason': _safe_profile_text(reason, 280) or 'Warned by admin',
        'warned_by': _safe_profile_text(warned_by, 64) or 'SYSTEM',
        'warned_at': _now_iso()
    }
    warns.append(entry)
    if len(warns) > PROFILE_WARN_LIMIT:
        del warns[:-PROFILE_WARN_LIMIT]
    return entry


def _resolve_player_profile(player_data, touch_join=False):
    global player_profiles
    src = player_data if isinstance(player_data, dict) else {}
    existing_key = _safe_profile_text(src.get('profile_key'), 140)
    identifiers = _extract_player_identifiers(src)
    candidates = _profile_candidate_keys(src, identifiers)

    profile_key = None
    if existing_key and existing_key in player_profiles:
        profile_key = existing_key
    else:
        for key in candidates:
            if key in player_profiles:
                profile_key = key
                break
    if not profile_key:
        profile_key = candidates[0] if candidates else f'anon:{secrets.token_hex(8)}'

    profile = player_profiles.get(profile_key)
    if not isinstance(profile, dict):
        profile = _new_player_profile(profile_key)
        player_profiles[profile_key] = profile

    profile.setdefault('profile_key', profile_key)
    if not _normalize_player_id(profile.get('player_id')):
        known_ids = {
            _normalize_player_id(p.get('player_id'))
            for p in player_profiles.values()
            if isinstance(p, dict) and _normalize_player_id(p.get('player_id'))
        }
        _ensure_profile_player_id(profile, existing_ids=known_ids)
    else:
        profile['player_id'] = _normalize_player_id(profile.get('player_id'))
    profile.setdefault('first_seen_at', _now_iso())
    profile.setdefault('last_connection_at', profile.get('first_seen_at') or _now_iso())
    profile['connections'] = max(0, _safe_int(profile.get('connections'), 0))
    profile['total_playtime_sec'] = max(0, _safe_int(profile.get('total_playtime_sec'), 0))
    profile['notes'] = _safe_profile_text(profile.get('notes', ''), 2000)
    profile.setdefault('identifiers', {})
    profile.setdefault('warnings', [])
    profile.setdefault('history', [])

    if isinstance(profile.get('identifiers'), dict):
        for k, v in identifiers.items():
            val = _safe_profile_text(v, 128)
            if val:
                profile['identifiers'][_normalize_identifier_key(k)] = val

    name = _safe_profile_name(src.get('name'))
    ip = normalize_player_ip(src.get('ip', ''))
    sid = str(src.get('serverId') or '').strip()
    social_club = _safe_profile_text(identifiers.get('social_club') or src.get('socialClub') or profile.get('last_social_club'), 128)
    rgsc_id = _safe_profile_text(identifiers.get('rgsc_id') or src.get('rgscId') or profile.get('last_rgsc_id'), 128)
    serial = _safe_profile_text(identifiers.get('serial') or src.get('serial') or profile.get('last_serial'), 128)
    game_type = _safe_profile_text(identifiers.get('game_type') or src.get('gameType') or profile.get('last_game_type'), 64)
    profile['last_name'] = name
    profile['last_ip'] = ip
    profile['last_server_id'] = sid
    profile['last_social_club'] = social_club
    profile['last_rgsc_id'] = rgsc_id
    profile['last_serial'] = serial
    profile['last_game_type'] = game_type
    profile['last_packet_loss'] = max(0, _safe_int(src.get('packetLoss', profile.get('last_packet_loss', 0)), 0))

    if touch_join:
        _append_profile_history(profile, 'join', f'{name} ({sid or "?"})')
        profile['last_connection_at'] = _now_iso()
        profile['connections'] = max(0, _safe_int(profile.get('connections'), 0)) + 1

    src['profile_key'] = profile_key
    src['playerId'] = profile.get('player_id') or ''
    if identifiers:
        src['identifiers'] = identifiers
    return profile_key, profile


def _find_player_profile_from_connected(server_id):
    sid = str(server_id or '').strip()
    if not sid:
        return None, None, None
    player = connected_players.get(sid)
    if not isinstance(player, dict):
        return None, None, None
    key, profile = _resolve_player_profile(player, touch_join=False)
    return player, key, profile


def _find_connected_player_by_profile_key(profile_key):
    for row in connected_players.values():
        if not isinstance(row, dict):
            continue
        if _safe_profile_text(row.get('profile_key'), 140) == profile_key:
            return row
    return None


def _profile_stub_player_row(profile):
    if not isinstance(profile, dict):
        return {}
    return {
        'serverId': profile.get('last_server_id') or '',
        'name': _safe_profile_name(profile.get('last_name')),
        'ip': normalize_player_ip(profile.get('last_ip', '')),
        'ping': 0,
        'session': 0,
        'sessionActive': False,
        'joinTime': 0
    }


def _resolve_player_profile_ref(player_ref):
    ref = _safe_profile_text(player_ref, 160)
    if not ref:
        return None, None, None, False

    player_row, profile_key, profile = _find_player_profile_from_connected(ref)
    if profile:
        return player_row, profile_key, profile, True

    profile_key, profile = _find_profile_by_player_id(ref)
    if profile:
        player_row = _find_connected_player_by_profile_key(profile_key)
        return player_row, profile_key, profile, bool(player_row)

    if ref in player_profiles and isinstance(player_profiles.get(ref), dict):
        profile_key = ref
        profile = player_profiles.get(ref)
        player_row = _find_connected_player_by_profile_key(profile_key)
        return player_row, profile_key, profile, bool(player_row)

    return None, None, None, False


def _build_players_listing():
    if _ensure_all_profile_player_ids():
        save_player_profiles(player_profiles)

    now_ts = int(time.time())
    rows = []
    seen_profile_keys = set()

    for sid, row in connected_players.items():
        if not isinstance(row, dict):
            continue
        key, profile = _resolve_player_profile(row, touch_join=False)
        seen_profile_keys.add(key)

        join_ts = _safe_int(row.get('joinTime'), now_ts)
        current_session = max(0, now_ts - join_ts)
        total_playtime = max(0, _safe_int(profile.get('total_playtime_sec'), 0) + current_session)

        rows.append({
            'playerId': profile.get('player_id') or '',
            'serverId': row.get('serverId', sid),
            'name': _safe_profile_name(row.get('name')),
            'ping': _safe_int(row.get('ping'), 0),
            'session': _safe_int(row.get('session'), 0),
            'sessionActive': bool(row.get('sessionActive')),
            'online': True,
            'first_seen': profile.get('first_seen_at') or _now_iso(),
            'last_connection': profile.get('last_connection_at') or profile.get('first_seen_at') or _now_iso(),
            'playtime_seconds': total_playtime
        })

    for key, profile in player_profiles.items():
        if not isinstance(profile, dict) or key in seen_profile_keys:
            continue
        rows.append({
            'playerId': profile.get('player_id') or '',
            'serverId': profile.get('last_server_id') or '',
            'name': _safe_profile_name(profile.get('last_name')),
            'ping': 0,
            'session': 0,
            'sessionActive': False,
            'online': False,
            'first_seen': profile.get('first_seen_at') or _now_iso(),
            'last_connection': profile.get('last_connection_at') or profile.get('first_seen_at') or _now_iso(),
            'playtime_seconds': max(0, _safe_int(profile.get('total_playtime_sec'), 0))
        })

    rows.sort(
        key=lambda item: (
            1 if item.get('online') else 0,
            str(item.get('last_connection') or ''),
            str(item.get('name') or '').lower()
        ),
        reverse=True
    )
    return rows


def _players_payload(rows=None):
    payload_rows = rows if isinstance(rows, list) else _build_players_listing()
    online_rows = [row for row in payload_rows if bool(row.get('online'))]
    saved_rows = [row for row in payload_rows if not bool(row.get('online'))]
    return {
        'players': payload_rows,
        'online_players': online_rows,
        'saved_players': saved_rows,
        'counts': {
            'total': len(payload_rows),
            'online': len(online_rows),
            'saved': len(saved_rows)
        }
    }


def _profile_matching_bans(profile, player_row=None):
    ip_candidates = set()
    name_candidates = set()

    if isinstance(profile, dict):
        p_ip = normalize_player_ip(profile.get('last_ip', ''))
        p_name = _safe_profile_name(profile.get('last_name')).lower()
        if p_ip:
            ip_candidates.add(p_ip)
        if p_name and p_name != 'unknown':
            name_candidates.add(p_name)

    if isinstance(player_row, dict):
        row_ip = normalize_player_ip(player_row.get('ip', ''))
        row_name = _safe_profile_name(player_row.get('name')).lower()
        if row_ip:
            ip_candidates.add(row_ip)
        if row_name and row_name != 'unknown':
            name_candidates.add(row_name)

    matches = []
    for idx, ban in enumerate(load_bans()):
        ban_ip = normalize_player_ip(ban.get('ip', ''))
        ban_name = _safe_profile_name(ban.get('name')).lower()
        if (ban_ip and ban_ip in ip_candidates) or (ban_name and ban_name in name_candidates):
            row = dict(ban)
            row['_index'] = idx
            matches.append(row)
    return matches


def _finalize_player_session(player_row, reason='disconnect'):
    if not isinstance(player_row, dict):
        return
    _, profile = _resolve_player_profile(player_row, touch_join=False)
    now_ts = int(time.time())
    join_ts = _safe_int(player_row.get('joinTime'), now_ts)
    delta = max(0, now_ts - join_ts)
    profile['total_playtime_sec'] = max(0, _safe_int(profile.get('total_playtime_sec'), 0) + delta)
    profile['last_connection_at'] = _now_iso()
    _append_profile_history(profile, 'disconnect', _safe_profile_text(reason, 120) or 'disconnect')
    save_player_profiles(player_profiles)


def _finalize_all_connected_sessions(reason='server-stop'):
    rows = list(connected_players.values())
    if not rows:
        return
    for row in rows:
        _finalize_player_session(row, reason=reason)


player_profiles = load_player_profiles()
if _ensure_all_profile_player_ids():
    save_player_profiles(player_profiles)


                                                           

def generate_csrf_token():
    
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']

def validate_csrf_token():
    
    token = request.headers.get('X-CSRF-Token') or request.form.get('_csrf_token')
    expected = session.get('_csrf_token')
    if not expected or not token or not secrets.compare_digest(token, expected):
        return False
    return True

@app.before_request
def enforce_https_if_enabled():
    
    if not FORCE_HTTPS:
        return
    if request.is_secure:
        return
    if str(request.headers.get('X-Forwarded-Proto', '')).lower() == 'https':
        return
    host = str(request.host or '').lower()
    if host.startswith('127.0.0.1') or host.startswith('localhost'):
        return
    secure_url = request.url.replace('http://', 'https://', 1)
    return redirect(secure_url, code=301)

@app.before_request
def csrf_protect():
    
    if request.method in ('POST', 'PUT', 'DELETE'):
                                                                                
        if request.path in ('/api/login', '/api/setup', '/api/setup-pin', '/api/db-test') or request.path.startswith('/socket.io') or request.path.startswith('/api/panel-hook/'):
            return
        if not validate_csrf_token():
            return jsonify({'error': 'Invalid or missing CSRF token'}), 403

@app.after_request
def add_security_headers(response):
    
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.socket.io https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "font-src 'self' https://cdnjs.cloudflare.com; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss: http: https: https://cdn.socket.io https://cdn.jsdelivr.net https://cdnjs.cloudflare.com;"
    )
    return response

@app.route('/api/csrf-token')
def api_csrf_token():
    
    return jsonify({'token': generate_csrf_token()})

                                                        

def load_panel_config():
    
    return storage.load_panel_config()


def save_panel_config(cfg):
    
    storage.save_panel_config(cfg)


panel_config = load_panel_config()


def _resolve_initial_panel_port():
    env_port = os.getenv('PANEL_PORT') or os.getenv('PORT')
    if env_port:
        return _panel_port_or_default(env_port)
    return _panel_port_or_default(panel_config.get('panel_port'))


def _persist_panel_port(port: int):
    global panel_config
    if panel_config.get('panel_port') == port:
        return
    panel_config['panel_port'] = port
    save_panel_config(panel_config)
    try:
        _install_ragemp_bridge_package()
    except Exception:
        pass


def _default_discord_settings():
    return {
        'enabled': False,
        'token': '',
        'guild_id': '',
        'warnings_channel_id': '',
        'status_embed_json': json.dumps(DEFAULT_DISCORD_STATUS_EMBED_TEMPLATE, indent=4, ensure_ascii=False),
        'status_config_json': json.dumps(DEFAULT_DISCORD_STATUS_CONFIG, indent=4, ensure_ascii=False),
        'status_messages': []
    }


def _is_valid_discord_id(value):
    val = str(value or '').strip()
    return bool(val) and bool(re.fullmatch(r'\d{10,32}', val))


def _safe_parse_json(text, fallback):
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str) or not text.strip():
        return fallback
    try:
        return json.loads(text)
    except Exception:
        return fallback


def _normalize_discord_settings(data):
    out = _default_discord_settings()
    src = data if isinstance(data, dict) else {}
    out['enabled'] = bool(src.get('enabled', out['enabled']))
    out['token'] = str(src.get('token', out['token']) or '').strip()
    out['guild_id'] = str(src.get('guild_id', out['guild_id']) or '').strip()
    out['warnings_channel_id'] = str(src.get('warnings_channel_id', out['warnings_channel_id']) or '').strip()

    embed_json = src.get('status_embed_json')
    if isinstance(embed_json, str) and embed_json.strip():
        out['status_embed_json'] = embed_json

    config_json = src.get('status_config_json')
    if isinstance(config_json, str) and config_json.strip():
        out['status_config_json'] = config_json

    messages = src.get('status_messages')
    if isinstance(messages, list):
        clean_messages = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            channel_id = str(item.get('channel_id') or '').strip()
            message_id = str(item.get('message_id') or '').strip()
            if not (_is_valid_discord_id(channel_id) and _is_valid_discord_id(message_id)):
                continue
            clean_messages.append({'channel_id': channel_id, 'message_id': message_id})
        out['status_messages'] = clean_messages

    return out


def _get_discord_config():
    cfg = panel_config.get('discord')
    if not isinstance(cfg, dict):
        cfg = _default_discord_settings()
        panel_config['discord'] = cfg
    return _normalize_discord_settings(cfg)


panel_config['discord'] = _get_discord_config()


def _parse_discord_status_embed_template(discord_cfg):
    parsed = _safe_parse_json(discord_cfg.get('status_embed_json'), DEFAULT_DISCORD_STATUS_EMBED_TEMPLATE)
    return parsed if isinstance(parsed, dict) else DEFAULT_DISCORD_STATUS_EMBED_TEMPLATE


def _parse_discord_status_config(discord_cfg):
    parsed = _safe_parse_json(discord_cfg.get('status_config_json'), DEFAULT_DISCORD_STATUS_CONFIG)
    if not isinstance(parsed, dict):
        parsed = dict(DEFAULT_DISCORD_STATUS_CONFIG)
    out = dict(DEFAULT_DISCORD_STATUS_CONFIG)
    out.update({k: v for k, v in parsed.items() if v is not None})
    if not isinstance(out.get('buttons'), list):
        out['buttons'] = []
    return out


def _parse_hex_color(value, fallback):
    s = str(value or '').strip()
    if not re.fullmatch(r'#?[0-9A-Fa-f]{6}', s):
        return fallback
    s = s.lstrip('#')
    try:
        return int(s, 16)
    except Exception:
        return fallback


def _format_uptime_short(seconds):
    try:
        seconds = max(0, int(seconds))
    except Exception:
        seconds = 0
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, sec = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f'{days}d')
    if hours or days:
        parts.append(f'{hours}h')
    if minutes or hours or days:
        parts.append(f'{minutes}m')
    parts.append(f'{sec}s')
    return ' '.join(parts)


def _safe_player_name(value):
    name = str(value or '').strip() or 'Unknown'
    name = name.replace('\r', ' ').replace('\n', ' ').replace('`', "'")
    return name[:64]


def _players_snapshot_signature(players):
    rows = []
    for p in players or []:
        if not isinstance(p, dict):
            continue
        sid = str(p.get('serverId') or '').strip()
        name = _safe_player_name(p.get('name'))
        try:
            ping = max(0, int(p.get('ping', 0)))
        except Exception:
            ping = 0
        rows.append(f'{sid}:{name}:{ping}')
    rows.sort()
    return '|'.join(rows)


def _format_connected_players_block(max_lines=20, include_ping=False, include_id=False):
    rows = []
    for p in connected_players.values():
        if not isinstance(p, dict):
            continue
        name = _safe_player_name(p.get('name'))
        sid = str(p.get('serverId') or '').strip()
        try:
            ping = max(0, int(p.get('ping', 0)))
        except Exception:
            ping = 0
        parts = []
        if include_id and sid:
            parts.append(f'id:{sid}')
        if include_ping:
            parts.append(f'ping:{ping}ms')
        if parts:
            rows.append(f'{name} ({", ".join(parts)})')
        else:
            rows.append(name)

    if not rows:
        return 'No players connected'

    visible = rows[:max_lines]
    remaining = max(0, len(rows) - len(visible))
    if remaining:
        visible.append(f'+{remaining} more...')

    text = '\n'.join(visible)
    if len(text) > 900:
        text = text[:900].rstrip()
    return text


def _compute_next_scheduled_restart(times):
    now = datetime.now()
    candidates = []
    for item in times or []:
        try:
            hh, mm = str(item).strip().split(':', 1)
            hour = int(hh)
            minute = int(mm)
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                continue
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            candidates.append(candidate)
        except Exception:
            continue
    if not candidates:
        return 'Not scheduled'
    nearest = min(candidates)
    return nearest.strftime('%Y-%m-%d %H:%M')


def _resolve_status_state(status_cfg):
    if not server_state.get('running'):
        return 'offline', status_cfg.get('offlineString', 'Offline'), _parse_hex_color(status_cfg.get('offlineColor'), 0xA70B28)

    return 'online', status_cfg.get('onlineString', 'Online'), _parse_hex_color(status_cfg.get('onlineColor'), 0x0BA70B)


def _status_template_context():
    settings = parse_settings_xml() or {}
    discord_cfg = _get_discord_config()
    status_cfg = _parse_discord_status_config(discord_cfg)
    _, status_string, status_color = _resolve_status_state(status_cfg)
    uptime_seconds = int(time.time() - server_state['start_time']) if server_state.get('start_time') else 0

    return {
        'serverName': settings.get('name') or 'RageMP Server',
        'statusString': status_string,
        'serverClients': len(connected_players),
        'serverMaxClients': settings.get('maxplayers') or 0,
        'connectedPlayersList': _format_connected_players_block(max_lines=20, include_ping=False, include_id=False),
        'connectedPlayersWithPing': _format_connected_players_block(max_lines=20, include_ping=True, include_id=False),
        'connectedPlayersWithId': _format_connected_players_block(max_lines=20, include_ping=False, include_id=True),
        'connectedPlayersDetailed': _format_connected_players_block(max_lines=20, include_ping=True, include_id=True),
        'nextScheduledRestart': _compute_next_scheduled_restart(panel_config.get('scheduled_restarts') or []),
        'uptime': _format_uptime_short(uptime_seconds),
        '_status_color': status_color
    }


def _build_txadmin_monitor_payload():
    settings = parse_settings_xml() or {}
    discord_cfg = _get_discord_config()
    uptime_seconds = int(time.time() - server_state['start_time']) if server_state.get('start_time') else 0
    max_players = 0
    try:
        max_players = max(0, int(settings.get('maxplayers') or 0))
    except Exception:
        max_players = 0

    return {
        'serverName': str(settings.get('name') or 'RageMP Server').strip() or 'RageMP Server',
        'status': 'online' if server_state.get('running') else 'offline',
        'playersOnline': len(connected_players),
        'playersMax': max_players,
        'uptimeSec': uptime_seconds,
        'uptimeText': _format_uptime_short(uptime_seconds),
        'discordBotEnabled': bool((discord_cfg or {}).get('enabled')),
        'nextRestart': _compute_next_scheduled_restart(panel_config.get('scheduled_restarts') or []),
        'autoRestart': bool(server_state.get('auto_restart')),
        'panelName': str((panel_config or {}).get('panel_name') or 'RageAdmin').strip() or 'RageAdmin'
    }


def _presence_text_for_discord():
    settings = parse_settings_xml() or {}
    server_name = str(settings.get('name') or 'RageMP Server').strip()
    server_name = server_name.replace('\r', ' ').replace('\n', ' ')
    if not server_name:
        server_name = 'RageMP Server'

    try:
        max_clients = int(settings.get('maxplayers') or 0)
    except Exception:
        max_clients = 0
    max_clients = max(0, max_clients)

    current_clients = len(connected_players)
    text = f'[{current_clients}/{max_clients}] {server_name}'
    if len(text) > 120:
        text = text[:120].rstrip()
    return text or '[0/0] RageMP Server'


def _build_presence_payload_for_discord():
    if discord is None:
        return None

    status_cfg = _parse_discord_status_config(_get_discord_config())
    status_key, _, _ = _resolve_status_state(status_cfg)
    text = _presence_text_for_discord()

    if status_key == 'online':
        status_obj = discord.Status.online
    elif status_key == 'partial':
        status_obj = discord.Status.idle
    else:
        status_obj = discord.Status.dnd

    return {
        'status': status_obj,
        'activity': discord.Game(name=text),
        'signature': f'{status_key}|{text}'
    }


def _status_refresh_signature():
    
    settings = parse_settings_xml() or {}
    discord_cfg = _get_discord_config()
    status_cfg = _parse_discord_status_config(discord_cfg)
    status_key, status_string, status_color = _resolve_status_state(status_cfg)
    payload = {
        'serverName': settings.get('name') or 'RageMP Server',
        'statusKey': status_key,
        'statusString': status_string,
        'statusColor': status_color,
        'serverClients': len(connected_players),
        'serverMaxClients': settings.get('maxplayers') or 0,
        'connectedPlayersSig': _players_snapshot_signature(connected_players.values()),
        'nextScheduledRestart': _compute_next_scheduled_restart(panel_config.get('scheduled_restarts') or []),
        'statusEmbedJson': str(discord_cfg.get('status_embed_json') or ''),
        'statusConfigJson': str(discord_cfg.get('status_config_json') or '')
    }
    try:
        return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    except Exception:
        return str(payload)


def _apply_template_values(value, mapping):
    if isinstance(value, dict):
        return {k: _apply_template_values(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_apply_template_values(v, mapping) for v in value]
    if isinstance(value, str):
        out = value
        for k, v in mapping.items():
            out = out.replace(f'{{{{{k}}}}}', str(v))
        return out
    return value


def _build_status_embed_for_discord():
    discord_cfg = _get_discord_config()
    tpl = _parse_discord_status_embed_template(discord_cfg)
    context = _status_template_context()
    rendered = _apply_template_values(tpl, context)
    status_color = context.get('_status_color', 0x0BA70B)

    if discord is None:
        return None

    title = str(rendered.get('title') or '')[:256]
    description = str(rendered.get('description') or '')[:4096]

    embed = discord.Embed(
        title=title or None,
        description=description or None,
        color=status_color
    )

    fields = rendered.get('fields')
    if isinstance(fields, list):
        for field in fields[:25]:
            if not isinstance(field, dict):
                continue
            name = str(field.get('name') or '\u200b')[:256]
            value = str(field.get('value') or '\u200b')[:1024]
            inline = bool(field.get('inline', False))
            embed.add_field(name=name, value=value, inline=inline)

    image = rendered.get('image')
    if isinstance(image, dict):
        img_url = str(image.get('url') or '').strip()
        if img_url:
            embed.set_image(url=img_url)

    thumb = rendered.get('thumbnail')
    if isinstance(thumb, dict):
        thumb_url = str(thumb.get('url') or '').strip()
        if thumb_url:
            embed.set_thumbnail(url=thumb_url)

    return embed


def _build_status_view_for_discord():
    if discord is None:
        return None
    status_cfg = _parse_discord_status_config(_get_discord_config())
    buttons = status_cfg.get('buttons', [])
    if not isinstance(buttons, list):
        return None
    view = discord.ui.View(timeout=None)
    added = 0
    for btn in buttons[:5]:
        if not isinstance(btn, dict):
            continue
        label = str(btn.get('label') or '').strip()[:80]
        url = str(btn.get('url') or '').strip()
        if not label or not re.match(r'^https?://', url, re.IGNORECASE):
            continue
        emoji = btn.get('emoji')
        try:
            view.add_item(discord.ui.Button(label=label, url=url, emoji=emoji))
            added += 1
        except Exception:
            continue
    return view if added else None


def _upsert_discord_status_message(channel_id, message_id):
    global panel_config
    discord_cfg = _get_discord_config()
    existing = list(discord_cfg.get('status_messages', []))
    entry = {'channel_id': str(channel_id), 'message_id': str(message_id)}
    dedup = []
    for row in existing:
        if not isinstance(row, dict):
            continue
        if row.get('channel_id') == entry['channel_id'] and row.get('message_id') == entry['message_id']:
            continue
        dedup.append({'channel_id': str(row.get('channel_id')), 'message_id': str(row.get('message_id'))})
    dedup.append(entry)
    discord_cfg['status_messages'] = dedup
    panel_config['discord'] = discord_cfg
    save_panel_config(panel_config)


def _remove_discord_status_message(channel_id, message_id):
    global panel_config
    discord_cfg = _get_discord_config()
    target_channel = str(channel_id)
    target_message = str(message_id)
    filtered = []
    for row in discord_cfg.get('status_messages', []):
        if not isinstance(row, dict):
            continue
        if str(row.get('channel_id')) == target_channel and str(row.get('message_id')) == target_message:
            continue
        filtered.append({
            'channel_id': str(row.get('channel_id')),
            'message_id': str(row.get('message_id'))
        })
    discord_cfg['status_messages'] = filtered
    panel_config['discord'] = discord_cfg
    save_panel_config(panel_config)


class PanelDiscordBotClient(discord.Client if discord is not None else object):
    def __init__(self, runtime, settings):
        if discord is None:
            return
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents)
        self.runtime = runtime
        self.settings = settings
        self.guild_id = int(settings.get('guild_id'))
        self.tree = app_commands.CommandTree(self)
        self.status_group = app_commands.Group(name='status', description='Server status commands')
        self.status_group.add_command(
            app_commands.Command(
                name='add',
                description='Add live server status embed to this channel',
                callback=self._status_add_command
            )
        )
        self.tree.add_command(self.status_group, guild=discord.Object(id=self.guild_id))
        self._status_task = None
        self._admin_permission_blocked = False
        self._status_update_event = asyncio.Event()
        self._last_status_signature = None
        self._last_presence_signature = None

    async def on_ready(self):
        self.runtime.set_running(True)
        self.runtime.set_error('')
        add_console_line(f'[Discord] Logged in as {self.user} (guild: {self.guild_id})')

        try:
            guild_obj = discord.Object(id=self.guild_id)
            await self.tree.sync(guild=guild_obj)
            add_console_line('[Discord] Slash commands synced: /status add')
        except Exception as e:
            self.runtime.set_error(f'Failed to sync slash commands: {e}')
            add_console_line(f'[Discord] Failed to sync slash commands: {e}')

        try:
            guild = self.get_guild(self.guild_id)
            if guild is None:
                guild = await self.fetch_guild(self.guild_id)
            member = None
            if guild is not None and self.user is not None:
                member = getattr(guild, 'me', None)
                if member is None:
                    try:
                        member = await guild.fetch_member(self.user.id)
                    except Exception:
                        member = None
            if member and member.guild_permissions.administrator:
                self._admin_permission_blocked = True
                self.runtime.set_admin_permission_blocked(True)
                self.runtime.set_error('Bot cannot run with Administrator permission. Remove Admin permission.')
                add_console_line('[Discord] Bot has Administrator permission. Remove it and restart Discord integration.')
            else:
                self._admin_permission_blocked = False
                self.runtime.set_admin_permission_blocked(False)
        except Exception as e:
            self.runtime.set_error(f'Failed to validate guild permissions: {e}')
            add_console_line(f'[Discord] Failed to validate guild permissions: {e}')

        if self._status_task is None or self._status_task.done():
            self._status_task = asyncio.create_task(self._status_update_loop())
        await self._refresh_presence(force=True)
        self.request_status_refresh(force=True)

    async def close(self):
        try:
            if self._status_task and not self._status_task.done():
                self._status_task.cancel()
                try:
                    await self._status_task
                except asyncio.CancelledError:
                    pass
        except Exception:
            pass
        await super().close()

    def request_status_refresh(self, force=False):
        if force:
            self._last_status_signature = None
            self._last_presence_signature = None
        if self._status_update_event is not None:
            self._status_update_event.set()

    async def _status_add_command(self, interaction):
        try:
            if self._admin_permission_blocked:
                await interaction.response.send_message('The bot cannot run with Administrator permission.', ephemeral=True)
                return

            if interaction.guild_id != self.guild_id:
                await interaction.response.send_message('This command is allowed only in the configured guild/server.', ephemeral=True)
                return

            if interaction.channel is None:
                await interaction.response.send_message('Invalid channel.', ephemeral=True)
                return

            guild = interaction.guild
            member = None
            if guild and self.user:
                member = guild.me
                if member is None:
                    try:
                        member = await guild.fetch_member(self.user.id)
                    except Exception:
                        member = None

            if member is None:
                await interaction.response.send_message('Failed to load bot permissions.', ephemeral=True)
                return

            perms = interaction.channel.permissions_for(member)
            is_thread = isinstance(interaction.channel, discord.Thread)
            can_send = perms.send_messages_in_threads if is_thread else perms.send_messages
            if not can_send:
                await interaction.response.send_message('The bot is missing "Send Messages" permission in this channel.', ephemeral=True)
                return
            if not perms.embed_links:
                await interaction.response.send_message('The bot is missing "Embed Links" permission in this channel.', ephemeral=True)
                return

            embed = _build_status_embed_for_discord()
            view = _build_status_view_for_discord()
            if embed is None:
                await interaction.response.send_message('Discord runtime is not available.', ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)
            msg = await interaction.channel.send(embed=embed, view=view)
            threading.Thread(
                target=_upsert_discord_status_message,
                args=(interaction.channel_id, msg.id),
                daemon=True
            ).start()
            self._last_status_signature = _status_refresh_signature()
            await interaction.followup.send('Status embed has been added. Auto-refresh is active.', ephemeral=True)
        except Exception as e:
            self.runtime.set_error(f'/status add failed: {e}')
            add_console_line(f'[Discord] /status add failed: {e}')
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        'Failed to add status embed. Check bot permissions (Send Messages + Embed Links).',
                        ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        'Failed to add status embed. Check bot permissions (Send Messages + Embed Links).',
                        ephemeral=True
                    )
            except Exception:
                pass

    async def _status_update_loop(self):
        while not self.is_closed():
            try:
                try:
                    await asyncio.wait_for(self._status_update_event.wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass
                self._status_update_event.clear()
                await self._refresh_presence()
                await self._refresh_status_embeds()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.runtime.set_error(f'Status embed refresh failed: {e}')

    async def _refresh_presence(self, force=False):
        payload = _build_presence_payload_for_discord()
        if not isinstance(payload, dict):
            return
        signature = str(payload.get('signature') or '')
        if not force and signature == self._last_presence_signature:
            return
        try:
            await self.change_presence(
                status=payload.get('status') or discord.Status.online,
                activity=payload.get('activity')
            )
            self._last_presence_signature = signature
        except Exception:
            return

    async def _refresh_status_embeds(self):
        cfg = _get_discord_config()
        entries = list(cfg.get('status_messages', []))
        if not entries:
            return
        signature = _status_refresh_signature()
        if signature == self._last_status_signature:
            return
        embed = _build_status_embed_for_discord()
        view = _build_status_view_for_discord()
        if embed is None:
            return

        for row in entries:
            channel_id = str(row.get('channel_id') or '').strip()
            message_id = str(row.get('message_id') or '').strip()
            if not (_is_valid_discord_id(channel_id) and _is_valid_discord_id(message_id)):
                continue
            try:
                chan = self.get_channel(int(channel_id))
                if chan is None:
                    chan = await self.fetch_channel(int(channel_id))
                msg = await chan.fetch_message(int(message_id))
                await msg.edit(embed=embed, view=view)
            except discord.NotFound:
                _remove_discord_status_message(channel_id, message_id)
            except discord.Forbidden:
                continue
            except Exception:
                continue
        self._last_status_signature = signature


class DiscordRuntimeManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._thread = None
        self._loop = None
        self._client = None
        self._running = False
        self._admin_permission_blocked = False
        self._last_error = ''
        self._fingerprint = None

    def set_running(self, value):
        with self._lock:
            self._running = bool(value)

    def set_admin_permission_blocked(self, value):
        with self._lock:
            self._admin_permission_blocked = bool(value)

    def set_error(self, message):
        with self._lock:
            self._last_error = str(message or '')

    def status_payload(self):
        with self._lock:
            return {
                'running': bool(self._running),
                'admin_permission_blocked': bool(self._admin_permission_blocked),
                'last_error': self._last_error
            }

    def _target_fingerprint(self, cfg):
        if not isinstance(cfg, dict):
            return (False, '', '')
        return (
            bool(cfg.get('enabled')),
            str(cfg.get('token') or ''),
            str(cfg.get('guild_id') or '')
        )

    def sync_from_config(self, force=False):
        cfg = _get_discord_config()
        fp = self._target_fingerprint(cfg)

        if discord is None:
            self.stop()
            self.set_running(False)
            if cfg.get('enabled'):
                self.set_error('discord.py dependency is missing. Install requirements and restart.')
            else:
                self.set_error('')
            with self._lock:
                self._fingerprint = fp
            return

        if not cfg.get('enabled'):
            self.stop()
            self.set_running(False)
            self.set_admin_permission_blocked(False)
            self.set_error('')
            with self._lock:
                self._fingerprint = fp
            return

        if not cfg.get('token') or not _is_valid_discord_id(cfg.get('guild_id')):
            self.stop()
            self.set_running(False)
            self.set_error('Discord enabled, but token or guild/server ID is missing/invalid.')
            with self._lock:
                self._fingerprint = fp
            return

        with self._lock:
            same = (fp == self._fingerprint)
            thread_alive = self._thread is not None and self._thread.is_alive()
        if same and thread_alive and not force:
            return

        self.stop()
        self.set_admin_permission_blocked(False)
        self.set_error('')
        self._start(cfg, fp)

    def _start(self, cfg, fp):
        def runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            client = PanelDiscordBotClient(self, cfg)
            with self._lock:
                self._loop = loop
                self._client = client
                self._running = False
                self._fingerprint = fp
            try:
                loop.run_until_complete(client.start(cfg.get('token')))
            except Exception as e:
                self.set_error(f'Discord bot stopped: {e}')
                self.set_running(False)
                add_console_line(f'[Discord] Bot stopped: {e}')
            finally:
                try:
                    if not client.is_closed():
                        loop.run_until_complete(client.close())
                except Exception:
                    pass
                loop.stop()
                loop.close()
                with self._lock:
                    self._client = None
                    self._loop = None
                    self._running = False

        thread = threading.Thread(target=runner, daemon=True, name='discord-bot')
        with self._lock:
            self._thread = thread
        thread.start()
        add_console_line('[Discord] Starting bot runtime...')

    def stop(self):
        with self._lock:
            loop = self._loop
            client = self._client
            thread = self._thread

        if loop and client:
            try:
                fut = asyncio.run_coroutine_threadsafe(client.close(), loop)
                fut.result(timeout=8)
            except Exception:
                pass

        if thread and thread.is_alive():
            try:
                thread.join(timeout=8)
            except Exception:
                pass

        with self._lock:
            self._client = None
            self._loop = None
            self._thread = None
            self._running = False
            self._admin_permission_blocked = False

    def request_status_refresh(self, force=False):
        with self._lock:
            loop = self._loop
            client = self._client
        if loop is None or client is None:
            return

        def _trigger():
            try:
                client.request_status_refresh(force=force)
            except Exception:
                return

        try:
            loop.call_soon_threadsafe(_trigger)
        except Exception:
            return

    def send_warning(self, message):
        text = str(message or '').strip()
        if not text:
            return
        cfg = _get_discord_config()
        channel_id = str(cfg.get('warnings_channel_id') or '').strip()
        if not _is_valid_discord_id(channel_id):
            return
        with self._lock:
            loop = self._loop
            client = self._client
        if loop is None or client is None:
            return

        async def _send():
            try:
                chan = client.get_channel(int(channel_id))
                if chan is None:
                    chan = await client.fetch_channel(int(channel_id))
                await chan.send(text)
            except Exception:
                return

        try:
            asyncio.run_coroutine_threadsafe(_send(), loop)
        except Exception:
            return


discord_runtime = DiscordRuntimeManager()


                                                       
def get_current_user():
    
    username = session.get('username')
    if not username:
        return None, None
    return username, get_user(username)


def require_permission(permission_key: str):
    
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'username' not in session:
                return jsonify({'error': 'Authentication required'}), 401
            _, user = get_current_user()
            if not user:
                return jsonify({'error': 'User not found'}), 404
                                                 
            if user.get('role') == 'admin':
                return f(*args, **kwargs)
            perms = user.get('permissions', {})
                                                          
            if not perms.get(permission_key, False):
                return jsonify({'error': 'Permission denied'}), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator

                                                           

def init_users():
    
    return storage.load_users()

def save_users(users):
    
    storage.save_users(users)

def generate_password(length=16):
    
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(secrets.choice(chars) for _ in range(length))

def get_user(username):
    
    users = init_users()
    return users.get(username)

def list_pfp_files():
    if not PFP_DIR.exists():
        return []
    files = []
    for p in PFP_DIR.iterdir():
        if p.is_file() and p.suffix.lower() in PFP_ALLOWED_EXTS:
            files.append(p.name)
    return sorted(files)

def resolve_avatar_url(avatar_value: str):
    if not avatar_value:
        return ''
    if avatar_value.startswith('builtin:'):
        name = avatar_value.split(':', 1)[1]
        if name in list_pfp_files():
            return f'/pfp/{name}'
        return ''
    if avatar_value.startswith('data:image/'):
        return avatar_value
    return ''

def resolve_avatar_url_for_user(username: str, avatar_value: str):
    if not avatar_value:
        return ''
    if avatar_value.startswith('builtin:'):
        name = avatar_value.split(':', 1)[1]
        if name in list_pfp_files():
            return f'/pfp/{name}'
        return ''
    if avatar_value.startswith('data:image/'):
        try:
            token = hashlib.sha1(avatar_value.encode('utf-8')).hexdigest()[:12]
        except Exception:
            token = ''
        safe_user = secure_filename(username or '')
        if not safe_user:
            return ''
        suffix = f'?v={token}' if token else ''
        return f'/api/avatar/{safe_user}{suffix}'
    return ''

def verify_user_password(username, password):
    
    if not username or not password:
        return False
    users = init_users()
    user = users.get(username)
    if not user:
        return False
    if not user.get('enabled', True):
        return False
    try:
        return check_password_hash(user.get('password', ''), password)
    except Exception:
        return False

def create_user(username, password, role='user', force_password_change=True, permissions=None):
    
    users = init_users()

    if username in users:
        return False, "User already exists"

                         
    if permissions is None:
        if role == 'admin':
                                        
            permissions = {
                'can_view_dashboard': True,
                'can_control_server': True,
                'can_view_console': True,
                'can_send_commands': True,
                'can_view_files': True,
                'can_edit_files': True,
                'can_view_logs': True,
                'can_view_players': True,
                'can_manage_resources': True,
                'can_view_settings': True
            }
        else:
            permissions = {
                'can_view_dashboard': True,
                'can_control_server': True,
                'can_view_console': True,
                'can_send_commands': True,
                'can_view_files': True,
                'can_edit_files': False,
                'can_view_logs': True,
                'can_view_players': True,
                'can_manage_resources': False,
                'can_view_settings': False
            }

    users[username] = {
        'password': generate_password_hash(password),
        'role': role,
        'force_password_change': force_password_change,
        'created_at': datetime.now().isoformat(),
        'last_login': None,
        'enabled': True,
        'permissions': permissions,
        'display_name': username,
        'avatar': ''
    }

    save_users(users)

                                 
    user_log_dir = LOGS_DIR / username
    user_log_dir.mkdir(exist_ok=True)

    return True, "User created successfully"

def update_user_password(username, new_password):
    
    users = init_users()

    if username not in users:
        return False, "User not found"

    users[username]['password'] = generate_password_hash(new_password)
    users[username]['force_password_change'] = False
    save_users(users)

    return True, "Password updated successfully"

def authenticate_user(username, password):
    
    users = init_users()
    user = users.get(username)

    if not user:
        return False, None

    if not user.get('enabled', True):
        return False, None

    if check_password_hash(user['password'], password):
                           
        users[username]['last_login'] = datetime.now().isoformat()
        save_users(users)
        return True, user

    return False, None

def setup_required():
    
    try:
        users = init_users()
        return len(users) == 0
    except Exception as e:
        add_console_line(f'ERROR: Failed to load users: {e}')
        return False

                                                      

def login_required(f):
    
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return jsonify({'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return jsonify({'error': 'Authentication required'}), 401

        user = get_user(session['username'])
        if not user or user.get('role') != 'admin':
            return jsonify({'error': 'Admin access required'}), 403

        return f(*args, **kwargs)
    return decorated_function

                                                   

def log_user_action(username, action, details=""):
    
    user_log_dir = LOGS_DIR / username
    user_log_dir.mkdir(exist_ok=True)

    log_file = user_log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.log"

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] {action}: {details}\n"

    with open(log_file, 'a') as f:
        f.write(log_entry)


def _reset_ragemp_console_filters():
    global ragemp_startup_table_active, ragemp_startup_table_armed
    ragemp_startup_table_active = False
    ragemp_startup_table_armed = False

def add_console_line(line, username=None):
    
    global console_lines, ragemp_startup_table_active, ragemp_startup_table_armed
    timestamp = datetime.now().strftime('%H:%M:%S')

                                                          
    ansi_escape = re.compile(r'''
        \x1B          # ESC character
        (?:
            [@-Z\\-_]                # 2-char sequences (Fe)
          | \[  [0-?]* [ -/]* [@-~]  # CSI sequences
          | \]  .*? (?:\x1B\\|\x07)  # OSC sequences
          | [()#][A-Z0-9]            # Charset select
        )
      | \x1B\[[\d;]*m               # SGR params (explicit)
      | \x1B\[\d*[A-HJKSTfn]        # Cursor/erase sequences
      | [\x00-\x08\x0e-\x1f]        # Remaining control chars except \t \n \r
    ''', re.VERBOSE)
    clean_line = ansi_escape.sub('', str(line or ''))
    clean_line = clean_line.replace('\r', '\n')
    clean_line = clean_line.strip('\r\n')
    clean_line = re.sub(r'^(\[\d{2}:\d{2}:\d{2}\]\s*){1,3}', '', clean_line).strip()
    clean_line = re.sub(r'\s+', ' ', clean_line)
    lower_line = clean_line.lower()
    is_ansi_reset = bool(re.fullmatch(r'\[\d+m\]', clean_line, re.IGNORECASE))
    is_separator = bool(
        re.fullmatch(r'\[=+\]', clean_line) or
        re.fullmatch(r'=+', clean_line) or
        re.fullmatch(r'\[[-=| ]+\]', clean_line)
    )

    if not clean_line:
        return
    if clean_line in {'||', '|', '[]'}:
        return
    if is_ansi_reset:
        return
    if lower_line.startswith('[info] starting rage multiplayer server'):
        ragemp_startup_table_armed = True
    if ragemp_startup_table_active:
        if any(lower_line.startswith(marker) for marker in RAGEMP_STARTUP_TABLE_END_MARKERS):
            ragemp_startup_table_active = False
        else:
            return
    if ragemp_startup_table_armed and is_separator:
        ragemp_startup_table_armed = False
        ragemp_startup_table_active = True
        return
    if ragemp_startup_table_armed and lower_line and not lower_line.startswith('[info] starting rage multiplayer server'):
        ragemp_startup_table_armed = False
    if is_separator:
        return

    formatted_line = f"[{timestamp}] {clean_line}"
    console_lines.append(formatted_line)

    if len(console_lines) > MAX_CONSOLE_LINES:
        console_lines.pop(0)

    socketio.emit('console_update', {'line': formatted_line})

                                     
    if username:
        log_user_action(username, 'CONSOLE', clean_line)

                                                             

def load_config():
    
    return storage.load_config()

def save_config(cfg):
    
    storage.save_config(cfg)

config = load_config()

                                                        

def _resolve_runtime_path(path_value, default=''):
    raw = str(path_value or default or '').strip()
    if not raw:
        return ROOT_DIR.resolve()
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path.resolve()


def _resolve_server_executable_path(path_value=None):
    if path_value is None:
        path_value = (config or {}).get('server_path', './RageMP-Server/ragemp-srv/ragemp-server')
    path = _resolve_runtime_path(path_value, './RageMP-Server/ragemp-srv/ragemp-server')
    if path.is_dir():
        return (path / RAGEMP_SERVER_EXECUTABLE).resolve()
    return path


def get_server_dir():
    return str(_resolve_server_executable_path().parent)


def _get_ragemp_bridge_target_dir(server_dir=None):
    base_dir = Path(server_dir) if server_dir is not None else Path(get_server_dir())
    return base_dir.resolve() / 'packages' / RAGEMP_BRIDGE_PACKAGE_NAME


def _get_ragemp_bridge_client_target_dir(server_dir=None):
    base_dir = Path(server_dir) if server_dir is not None else Path(get_server_dir())
    return base_dir.resolve() / 'client_packages' / RAGEMP_BRIDGE_PACKAGE_NAME


def _ensure_ragemp_client_bootstrap(server_dir=None):
    base_dir = Path(server_dir) if server_dir is not None else Path(get_server_dir())
    client_root = base_dir.resolve() / 'client_packages'
    client_root.mkdir(parents=True, exist_ok=True)
    index_path = client_root / 'index.js'
    require_line = f"require('./{RAGEMP_BRIDGE_PACKAGE_NAME}/index');"

    if index_path.exists():
        content = index_path.read_text(encoding='utf-8-sig')
    else:
        content = ''

    if require_line in content or f"require('./{RAGEMP_BRIDGE_PACKAGE_NAME}');" in content:
        return index_path

    new_content = content.rstrip()
    if new_content:
        new_content += '\n'
    new_content += f'{require_line}\n'
    index_path.write_text(new_content, encoding='utf-8')
    return index_path


def _build_ragemp_bridge_config():
    host = get_panel_host()
    secret = str((panel_config or {}).get('panel_secret') or 'changeme').strip() or 'changeme'
    return {
        'panelHost': host,
        'panelSecret': secret,
        'syncIntervalMs': 5000,
        'heartbeatIntervalMs': 15000,
        'requestTimeoutMs': 5000,
        'logVerbose': True
    }


def _install_ragemp_bridge_package(server_dir=None):
    target_dir = _get_ragemp_bridge_target_dir(server_dir)
    client_target_dir = _get_ragemp_bridge_client_target_dir(server_dir)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    client_target_dir.parent.mkdir(parents=True, exist_ok=True)
    if not RAGEMP_BRIDGE_TEMPLATE_DIR.exists():
        raise RuntimeError(f'RageAdmin package template not found: {RAGEMP_BRIDGE_TEMPLATE_DIR}')
    if not RAGEMP_BRIDGE_CLIENT_TEMPLATE_DIR.exists():
        raise RuntimeError(f'RageAdmin client package template not found: {RAGEMP_BRIDGE_CLIENT_TEMPLATE_DIR}')

    shutil.copytree(RAGEMP_BRIDGE_TEMPLATE_DIR, target_dir, dirs_exist_ok=True)
    shutil.copytree(RAGEMP_BRIDGE_CLIENT_TEMPLATE_DIR, client_target_dir, dirs_exist_ok=True)
    config_path = target_dir / 'config.json'
    config_path.write_text(
        json.dumps(_build_ragemp_bridge_config(), indent=4),
        encoding='utf-8'
    )
    _ensure_ragemp_client_bootstrap(server_dir)
    return target_dir


def jail_path(requested_path):

    server_dir = get_server_dir()
                          
    requested_path = requested_path.replace('\\', '/')
                            
    while requested_path.startswith('/'):
        requested_path = requested_path[1:]
    abs_path = os.path.normpath(os.path.join(server_dir, requested_path))
                                                                       
    if not abs_path.startswith(server_dir):
        return None, 'Access denied: path outside server directory'
    return abs_path, None

def save_pid(pid):
    
    with open(PID_FILE, 'w') as f:
        f.write(str(pid))

def load_pid():
    
    if PID_FILE.exists():
        try:
            with open(PID_FILE, 'r') as f:
                return int(f.read().strip())
        except Exception:
            return None
    return None

def remove_pid():
    
    if PID_FILE.exists():
        PID_FILE.unlink()

def find_server_process():
    
    server_name = config.get('server_name', 'ragemp-server')

    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
        try:
            if server_name.lower() in proc.info['name'].lower():
                return proc.info['pid'], proc.info['create_time']

            if proc.info['cmdline']:
                cmdline = ' '.join(proc.info['cmdline'])
                if server_name in cmdline:
                    return proc.info['pid'], proc.info['create_time']
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return None, None


def _get_live_process(pid):
    if not pid:
        return None
    try:
        proc = psutil.Process(int(pid))
        if not proc.is_running():
            return None
        try:
            if proc.status() == psutil.STATUS_ZOMBIE:
                return None
        except Exception:
            pass
        return proc
    except Exception:
        return None


def sync_server_state_with_system(emit_change=False):
    
    global server_process, server_state, resource_states, connected_players, pending_actions, panel_connector_last_heartbeat

    prev_running = bool(server_state.get('running'))
    pid = server_state.get('pid')
    proc = _get_live_process(pid)

    if not proc:
        saved_pid = load_pid()
        proc = _get_live_process(saved_pid)
        if proc:
            pid = proc.pid
        else:
            found_pid, _ = find_server_process()
            proc = _get_live_process(found_pid)
            if proc:
                pid = proc.pid

    if proc:
        server_state['running'] = True
        server_state['pid'] = proc.pid
        if not server_state.get('start_time'):
            try:
                server_state['start_time'] = proc.create_time()
            except Exception:
                server_state['start_time'] = time.time()
        managed_pid = server_process.pid if server_process else None
        server_state['attached'] = managed_pid != proc.pid
        if server_state['attached']:
            server_process = None
        save_pid(proc.pid)
    else:
        server_state['running'] = False
        server_state['pid'] = None
        server_state['start_time'] = None
        server_state['attached'] = False
        server_state['cpu_usage'] = 0
        server_state['memory_usage'] = 0
        resource_states = {}
        _finalize_all_connected_sessions(reason='process-missing')
        connected_players = {}
        pending_actions = []
        panel_connector_last_heartbeat = 0.0
        server_process = None
        remove_pid()

    if emit_change and prev_running != bool(server_state.get('running')):
        socketio.emit('server_status', {'running': bool(server_state.get('running'))})
        discord_runtime.request_status_refresh(force=True)

    return bool(server_state.get('running'))


def start_server(username):
    
    global server_process, server_state, resource_states, panel_connector_last_heartbeat

    if server_state['running']:
        return {'success': False, 'message': 'Server is already running'}

    try:
        _reset_ragemp_console_filters()
        server_path = _resolve_server_executable_path()

        if not server_path.exists():
            return {'success': False, 'message': f'Server executable not found: {server_path}'}

        _install_ragemp_bridge_package(server_path.parent)

        try:
            os.chmod(server_path, 0o755)
        except Exception:
            pass                                    

        server_process = subprocess.Popen(
            [str(server_path)],
            cwd=str(server_path.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            bufsize=-1
        )

        server_state['running'] = True
        server_state['pid'] = server_process.pid
        server_state['start_time'] = time.time()
        server_state['attached'] = False
        panel_connector_last_heartbeat = 0.0

                                                      
        settings = parse_settings_xml()
        configured_resources = settings.get('resources', []) if settings else []
        configured_addons = settings.get('addons', []) if settings else []
        resource_states = {}
        for res_name in list(configured_resources) + list(configured_addons):
            resource_states[res_name] = 'started'

        save_pid(server_process.pid)

        add_console_line(f'=== SERVER STARTED BY {username.upper()} ===', username)
        add_console_line(f'PID: {server_process.pid}', username)
        discord_runtime.send_warning(f'[ONLINE] Server started by {username}')
        discord_runtime.request_status_refresh(force=True)

        log_user_action(username, 'START_SERVER', f'PID: {server_process.pid}')

        monitor_thread = threading.Thread(target=monitor_process, daemon=True)
        monitor_thread.start()

        socketio.emit('server_status', {'running': True})
        socketio.emit('stats_update', build_runtime_status_payload(include_history=False))
        _append_runtime_sample(force=True, emit_socket=True)

        return {'success': True, 'message': 'Server started successfully'}

    except Exception as e:
        add_console_line(f'ERROR: Failed to start server - {str(e)}', username)
        log_user_action(username, 'START_SERVER_FAILED', str(e))
        return {'success': False, 'message': str(e)}

def stop_server(username):
    
    global server_process, server_state, resource_states, connected_players, pending_actions, panel_connector_last_heartbeat

    if not server_state['running']:
        return {'success': False, 'message': 'Server is not running'}

    try:
        add_console_line(f'=== STOPPING SERVER BY {username.upper()} ===', username)

        if server_state['attached']:
            try:
                process = psutil.Process(server_state['pid'])
                process.terminate()

                try:
                    process.wait(timeout=10)
                except psutil.TimeoutExpired:
                    process.kill()
                    add_console_line('Server force killed', username)
            except psutil.NoSuchProcess:
                pass
        else:
            if server_process:
                server_process.terminate()

                try:
                    server_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server_process.kill()
                    add_console_line('Server force killed', username)

        server_state['running'] = False
        server_state['pid'] = None
        server_state['start_time'] = None
        server_state['attached'] = False
        resource_states = {}
        _finalize_all_connected_sessions(reason='server-stop')
        connected_players = {}
        pending_actions = []
        panel_connector_last_heartbeat = 0.0

        remove_pid()

        add_console_line('=== SERVER STOPPED ===', username)
        log_user_action(username, 'STOP_SERVER', 'Success')
        discord_runtime.send_warning(f'[OFFLINE] Server stopped by {username}')
        discord_runtime.request_status_refresh(force=True)

        socketio.emit('server_status', {'running': False})
        socketio.emit('stats_update', build_runtime_status_payload(include_history=False))
        _append_runtime_sample(force=True, emit_socket=True)

        return {'success': True, 'message': 'Server stopped successfully'}
    except Exception as e:
        add_console_line(f'ERROR: Failed to stop server - {str(e)}', username)
        log_user_action(username, 'STOP_SERVER_FAILED', str(e))
        return {'success': False, 'message': str(e)}

def restart_server(username):
    
    add_console_line(f'=== RESTARTING SERVER BY {username.upper()} ===', username)
    server_state['restart_count'] += 1
    log_user_action(username, 'RESTART_SERVER', f'Count: {server_state["restart_count"]}')
    discord_runtime.send_warning(f'[RESTART] Server restart requested by {username}')
    _queue_restart_notice(
        f'Restart requested by {username}. Please prepare to reconnect.',
        duration=config.get('restart_delay', 5),
        title='Restart Incoming'
    )

    def _do_restart():
        try:
            delay = int(config.get('restart_delay', 5))
        except Exception:
            delay = 5
        delay = max(4, min(30, delay))
        time.sleep(delay)
        stop_server(username)
        time.sleep(1)
        start_server(username)

    threading.Thread(target=_do_restart, daemon=True).start()
    return {'success': True, 'message': 'Restarting...'}

def monitor_process():
    
    global server_process

    if server_process:
        for line in iter(server_process.stdout.readline, b''):
            if not line:
                continue
            try:
                decoded_line = line.decode('utf-8', errors='replace')
                decoded_line = ''.join(ch for ch in decoded_line if ch == '\t' or ch == '\n' or ch == '\r' or (ord(ch) >= 32 and ord(ch) != 127))
                for part in decoded_line.replace('\r', '\n').split('\n'):
                    add_console_line(part)
            except Exception:
                continue

def update_stats():
    
    global server_state, resource_states, connected_players, pending_actions, panel_connector_last_heartbeat

    while True:
        sync_server_state_with_system(emit_change=True)
        if server_state['running'] and server_state['pid']:
            try:
                process = psutil.Process(server_state['pid'])

                if not process.is_running():
                    add_console_line('!!! PROCESS TERMINATED !!!')
                    server_state['running'] = False
                    server_state['pid'] = None
                    server_state['attached'] = False
                    resource_states = {}
                    _finalize_all_connected_sessions(reason='process-terminated')
                    connected_players = {}
                    pending_actions = []
                    panel_connector_last_heartbeat = 0.0
                    remove_pid()
                    socketio.emit('server_status', {'running': False})
                    socketio.emit('stats_update', build_runtime_status_payload(include_history=False))
                    _append_runtime_sample(force=True, emit_socket=True)
                    continue

                server_state['cpu_usage'] = process.cpu_percent(interval=1)
                server_state['memory_usage'] = process.memory_info().rss / (1024 * 1024)
            except psutil.NoSuchProcess:
                add_console_line('!!! PROCESS NO LONGER EXISTS !!!')
                server_state['running'] = False
                server_state['pid'] = None
                server_state['attached'] = False
                resource_states = {}
                _finalize_all_connected_sessions(reason='process-gone')
                connected_players = {}
                pending_actions = []
                panel_connector_last_heartbeat = 0.0
                remove_pid()
                socketio.emit('server_status', {'running': False})
            except Exception:
                pass

        socketio.emit('stats_update', build_runtime_status_payload(include_history=False))
        _append_runtime_sample(force=False, emit_socket=True)

        time.sleep(2)

                                                              

_last_scheduled_restart_minute = None

def scheduled_restart_thread():
    
    global _last_scheduled_restart_minute
    while True:
        try:
            now = datetime.now()
            current_hhmm = now.strftime('%H:%M')
            current_minute = now.strftime('%Y-%m-%d %H:%M')
            times = panel_config.get('scheduled_restarts', [])
            if current_hhmm in times and current_minute != _last_scheduled_restart_minute:
                if server_state['running']:
                    _last_scheduled_restart_minute = current_minute
                    add_console_line(f'=== SCHEDULED RESTART ({current_hhmm}) ===')
                    log_user_action('SYSTEM', 'SCHEDULED_RESTART', current_hhmm)
                    discord_runtime.send_warning(f'[SCHEDULED] Scheduled restart triggered ({current_hhmm})')
                    restart_server('SYSTEM')
        except Exception:
            pass
        time.sleep(30)

                                                        

def get_settings_xml_path():
    
    return os.path.join(get_server_dir(), 'conf.json')


def _coerce_ragemp_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _coerce_ragemp_int(value, default=0, minimum=None, maximum=None):
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        coerced = default
    if minimum is not None:
        coerced = max(minimum, coerced)
    if maximum is not None:
        coerced = min(maximum, coerced)
    return coerced


def _coerce_ragemp_float(value, default=0.0, minimum=None):
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        coerced = default
    if minimum is not None:
        coerced = max(minimum, coerced)
    return coerced


def _normalize_ragemp_settings(data, existing=None):
    base = dict(RAGEMP_DEFAULT_SETTINGS)
    if isinstance(existing, dict):
        base.update(existing)
    if not isinstance(data, dict):
        return base
    base.update(data)

    text_fields = (
        'bind', 'gamemode', 'name', 'url', 'language', 'fastdl-host',
        'fqdn', 'node-commandline-flags'
    )
    for field in text_fields:
        if field in data and data.get(field) is not None:
            base[field] = str(data.get(field)).strip()

    if 'announce' in data:
        base['announce'] = _coerce_ragemp_bool(data.get('announce'), bool(base.get('announce', False)))
    if 'disallow-multiple-connections-per-ip' in data:
        base['disallow-multiple-connections-per-ip'] = _coerce_ragemp_bool(
            data.get('disallow-multiple-connections-per-ip'),
            bool(base.get('disallow-multiple-connections-per-ip', False))
        )
    if 'enable-nodejs' in data:
        base['enable-nodejs'] = _coerce_ragemp_bool(data.get('enable-nodejs'), bool(base.get('enable-nodejs', True)))
    if 'enable-http-security' in data:
        base['enable-http-security'] = _coerce_ragemp_bool(
            data.get('enable-http-security'),
            bool(base.get('enable-http-security', False))
        )
    if 'allow-cef-debugging' in data:
        base['allow-cef-debugging'] = _coerce_ragemp_bool(
            data.get('allow-cef-debugging'),
            bool(base.get('allow-cef-debugging', False))
        )
    if 'voice-chat' in data:
        base['voice-chat'] = _coerce_ragemp_bool(data.get('voice-chat'), bool(base.get('voice-chat', True)))

    numeric_fields = {
        'port': (22005, 1, 65535),
        'maxplayers': (100, 1, 10000),
        'sync-rate': (40, 1, 1000),
        'limit-time-of-connections-per-ip': (0, 0, 3600),
        'resources-compression-level': (1, -9, 9),
        'http-threads': (50, 1, 1000)
    }
    for field, (default, minimum, maximum) in numeric_fields.items():
        if field in data:
            base[field] = _coerce_ragemp_int(data.get(field), _coerce_ragemp_int(base.get(field), default), minimum, maximum)

    if 'stream-distance' in data:
        base['stream-distance'] = _coerce_ragemp_float(
            data.get('stream-distance'),
            _coerce_ragemp_float(base.get('stream-distance'), 500.0),
            0.0
        )

    if 'csharp' in data and data.get('csharp') is not None:
        base['csharp'] = str(data.get('csharp')).strip() or 'disabled'

    return base


def parse_settings_xml():
    
    path = get_settings_xml_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            raw = json.load(f)
        return _normalize_ragemp_settings(raw)
    except Exception:
        return None


def write_settings_xml(data):
    
    path = Path(get_settings_xml_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = parse_settings_xml() or {}
    payload = _normalize_ragemp_settings(data, existing)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=4)

                                                         

def _download_with_progress(url: str, dest_path: Path, start_pct: int, end_pct: int, label: str):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={'User-Agent': 'RageAdmin/1.0'})
    final_url = str(url or '')
    with urllib.request.urlopen(req) as resp, open(dest_path, 'wb') as f:
        try:
            final_url = str(resp.geturl() or final_url)
        except Exception:
            pass
        total = resp.getheader('Content-Length')
        try:
            total = int(total) if total else None
        except Exception:
            total = None

        downloaded = 0
        last_tick = time.time()
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = start_pct + (downloaded / total) * (end_pct - start_pct)
            else:
                pct = min(end_pct, start_pct + 1)
            if time.time() - last_tick > 0.5:
                _setup_update(progress=pct, message=f'{label}: {downloaded / (1024 * 1024):.1f} MB')
                last_tick = time.time()
        _setup_update(progress=end_pct, message=f'{label}: download complete')
    return final_url


def _find_file(root: Path, filename: str):
    for p in root.rglob(filename):
        return p
    return None


def _merge_tree(src: Path, dst: Path):
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        dest = dst / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)

def _cleanup_setup_downloads():
    
    try:
        if SETUP_DOWNLOAD_DIR.exists():
            shutil.rmtree(SETUP_DOWNLOAD_DIR, ignore_errors=True)
    except Exception:
        pass


def _extract_server_archive(archive_path: Path, extract_dir: Path):
    with tarfile.open(archive_path, 'r:gz') as tf:
        tf.extractall(extract_dir)


def _locate_ragemp_server_root(extract_dir: Path):
    direct = extract_dir / RAGEMP_SERVER_DIR_NAME
    if direct.is_dir():
        return direct
    server_bin = _find_file(extract_dir, RAGEMP_SERVER_EXECUTABLE)
    return server_bin.parent if server_bin else None


def _ensure_ragemp_content_dirs(server_dir: Path):
    for name in RAGEMP_CONTENT_DIRS:
        (server_dir / name).mkdir(parents=True, exist_ok=True)


def _write_default_ragemp_conf(conf_path: Path):
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    with open(conf_path, 'w', encoding='utf-8') as f:
        json.dump(dict(RAGEMP_DEFAULT_SETTINGS), f, indent=4)


def _terminate_setup_process(proc):
    if not proc:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _ensure_ragemp_runtime_files(server_dir: Path):
    server_dir = server_dir.resolve()
    conf_path = server_dir / 'conf.json'
    _ensure_ragemp_content_dirs(server_dir)
    _install_ragemp_bridge_package(server_dir)
    if conf_path.exists():
        return

    server_bin = (server_dir / RAGEMP_SERVER_EXECUTABLE).resolve()
    if not server_bin.exists():
        raise RuntimeError(f'RageMP server executable missing: {server_bin}')

    proc = None
    try:
        _setup_update(step='Preparing server', progress=80, message='Running RageMP once to generate conf.json...')
        proc = subprocess.Popen(
            [str(server_bin)],
            cwd=str(server_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if conf_path.exists():
                break
            if proc.poll() is not None:
                break
            time.sleep(1)
    except Exception as e:
        _setup_update(message=f'RageMP first-run initialization failed, creating default conf.json ({e})')
    finally:
        _terminate_setup_process(proc)

    _ensure_ragemp_content_dirs(server_dir)
    if not conf_path.exists():
        _write_default_ragemp_conf(conf_path)
        _setup_update(message='Default RageMP conf.json created')


def ensure_server_files():
    global config

    server_path = _resolve_server_executable_path(config.get('server_path', './RageMP-Server/ragemp-srv/ragemp-server'))
    if server_path.exists():
        _setup_update(message='Server executable already present, skipping download')
        info = _load_ragemp_local_info()
        if _ragemp_local_info_is_empty(info):
            try:
                remote_info = _fetch_remote_ragemp_info(str(info.get('archive_url') or SETUP_SERVER_ARCHIVE_URL).strip())
                _persist_local_ragemp_info(
                    version=remote_info.get('version') or '',
                    archive_url=remote_info.get('archive_url') or str(info.get('archive_url') or SETUP_SERVER_ARCHIVE_URL).strip(),
                    etag=remote_info.get('etag') or '',
                    last_modified=remote_info.get('last_modified') or ''
                )
                info = _load_ragemp_local_info()
            except Exception:
                pass
        detected = str(info.get('version') or '').strip()
        if detected:
            _setup_update(message=f'RageMP build detected: {detected}')
        _ensure_ragemp_runtime_files(server_path.parent)
        _setup_update(message='RageAdmin package synchronized')
        return

    _setup_update(step='Downloading server files', message='Downloading RageMP server files...')
    archive_path = SETUP_DOWNLOAD_DIR / 'linux_x64.tar.gz'
    downloaded_url = _download_with_progress(SETUP_SERVER_ARCHIVE_URL, archive_path, 25, 55, 'Server files')

    extract_dir = SETUP_DOWNLOAD_DIR / 'server_extract'
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    _setup_update(step='Extracting server files', progress=60, message='Extracting RageMP archive...')
    _extract_server_archive(archive_path, extract_dir)

    server_root = _locate_ragemp_server_root(extract_dir)
    if not server_root:
        raise RuntimeError('ragemp-server not found in extracted RageMP archive')

    target_root = RAGEMP_SERVER_ROOT_DIR
    target_root.mkdir(parents=True, exist_ok=True)
    target_dir = target_root / server_root.name
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    shutil.copytree(server_root, target_dir, dirs_exist_ok=True)

    dst = target_dir / RAGEMP_SERVER_EXECUTABLE
    if not dst.exists():
        raise RuntimeError('RageMP server executable missing after extraction')

    try:
        os.chmod(dst, 0o755)
    except Exception:
        pass

    _ensure_ragemp_runtime_files(target_dir)
    _setup_update(message='RageAdmin package installed')

    resolved_archive_url = str(downloaded_url or SETUP_SERVER_ARCHIVE_URL or '').strip()
    remote_info = _fetch_remote_ragemp_info(resolved_archive_url)
    detected_version = remote_info.get('version') or ''

    _persist_local_ragemp_info(
        version=detected_version,
        archive_url=resolved_archive_url or SETUP_SERVER_ARCHIVE_URL,
        etag=remote_info.get('etag') or '',
        last_modified=remote_info.get('last_modified') or ''
    )

    if detected_version:
        _setup_update(message=f'Server files ready (RageMP build {detected_version})')
    else:
        _setup_update(message='Server files ready')
def _safe_json_load(path: Path, default):
    try:
        if path.exists():
                                                                          
            with open(path, 'r', encoding='utf-8-sig') as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _safe_json_save(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, path)


def _load_update_config():
    defaults = {
        'panel_repo': DEFAULT_PANEL_REPO,
        'ragemp_server_url': DEFAULT_RAGEMP_SERVER_URL,
        'check_interval_minutes': DEFAULT_UPDATE_INTERVAL_MINUTES
    }
                                                                       
    update_cfg_url = os.getenv('HPM_UPDATE_CONFIG_URL')
    if update_cfg_url is None:
        update_cfg_url = DEFAULT_UPDATE_CONFIG_URL
    if update_cfg_url:
        try:
            data = _fetch_json(update_cfg_url)
            if isinstance(data, dict):
                defaults.update({k: v for k, v in data.items() if v is not None})
        except Exception:
            pass

                                      
    data = _safe_json_load(UPDATE_CONFIG_FILE, {})
    if isinstance(data, dict):
        defaults.update({k: v for k, v in data.items() if v is not None})

                                    
    try:
        cfg = storage.load_panel_config()
    except Exception:
        cfg = {}
    if isinstance(cfg, dict):
        for key in ('panel_repo', 'ragemp_server_url', 'check_interval_minutes'):
            if cfg.get(key) is not None:
                defaults[key] = cfg.get(key)

                            
    repo_env = os.getenv('HPM_PANEL_REPO')
    if repo_env:
        defaults['panel_repo'] = repo_env.strip()
    ragemp_env = os.getenv('HPM_RAGEMP_SERVER_URL')
    if ragemp_env:
        defaults['ragemp_server_url'] = ragemp_env.strip()
    interval_env = os.getenv('HPM_UPDATE_INTERVAL_MINUTES')
    if interval_env:
        try:
            defaults['check_interval_minutes'] = int(interval_env)
        except Exception:
            pass

    return defaults


def _load_panel_version():
    global panel_config
    version = str((panel_config or {}).get('panel_version') or '').strip()
    file_version = ''
    data = _safe_json_load(PANEL_VERSION_FILE, {})
    if isinstance(data, dict):
        file_version = str(data.get('version') or '').strip()

    use_file_version = bool(
        file_version and (
            not version or
            version in ('0.0.0', 'v0.0.0') or
            _is_newer_version(file_version, version)
        )
    )
    if use_file_version:
        version = file_version
        try:
            if not panel_config:
                panel_config = storage.load_panel_config()
            panel_config['panel_version'] = version
            storage.save_panel_config(panel_config)
        except Exception:
            pass
    if not version:
        version = '0.0.0'
    return version


def _is_unknown_version_value(version: str) -> bool:
    val = str(version or '').strip().lower()
    return val in ('', '0', '0.0', '0.0.0', 'v0', 'v0.0', 'v0.0.0')


def _format_ragemp_build_label(last_modified: str = '', etag: str = '', archive_url: str = '') -> str:
    etag = str(etag or '').strip().strip('"')
    if etag:
        return f'build {etag[:8]}'
    if _is_official_ragemp_archive(archive_url):
        return 'official-prerelease'
    last_modified = str(last_modified or '').strip()
    if last_modified:
        try:
            parsed = parsedate_to_datetime(last_modified)
            return parsed.strftime('%Y-%m-%d')
        except Exception:
            pass
    return ''


def _fetch_remote_ragemp_info(url: str = ''):
    archive_url = str(url or SETUP_SERVER_ARCHIVE_URL or '').strip()
    req = urllib.request.Request(archive_url, headers={'User-Agent': 'RageAdmin/1.0'}, method='HEAD')
    with urllib.request.urlopen(req, timeout=20) as resp:
        final_url = str(resp.geturl() or archive_url).strip()
        etag = str(resp.headers.get('ETag') or '').strip()
        last_modified = str(resp.headers.get('Last-Modified') or '').strip()
    return {
        'version': _format_ragemp_build_label(last_modified, etag, final_url or archive_url),
        'archive_url': final_url or archive_url,
        'etag': etag,
        'last_modified': last_modified
    }


def _persist_local_ragemp_info(version: str = '', archive_url: str = '', etag: str = '', last_modified: str = ''):
    global config
    version = str(version or '').strip()
    archive_url = str(archive_url or '').strip()
    etag = str(etag or '').strip()
    last_modified = str(last_modified or '').strip()
    formatted = _format_ragemp_build_label(last_modified, etag, archive_url)
    if not version or (_looks_like_header_date_version(version) and formatted):
        version = formatted

    if not config:
        config = storage.load_config()
    if not isinstance(config, dict):
        return False

    current_version = str(config.get('ragemp_version') or '').strip()
    current_archive = str(config.get('ragemp_archive_url') or '').strip()
    current_etag = str(config.get('ragemp_etag') or '').strip()
    current_last_modified = str(config.get('ragemp_last_modified') or '').strip()
    changed = False

    if version and current_version != version:
        config['ragemp_version'] = version
        changed = True
    if archive_url and current_archive != archive_url:
        config['ragemp_archive_url'] = archive_url
        changed = True
    if etag and current_etag != etag:
        config['ragemp_etag'] = etag
        changed = True
    if last_modified and current_last_modified != last_modified:
        config['ragemp_last_modified'] = last_modified
        changed = True

    if changed:
        storage.save_config(config)
    return changed


def _load_ragemp_local_info():
    global config
    defaults = {
        'version': '',
        'archive_url': SETUP_SERVER_ARCHIVE_URL,
        'etag': '',
        'last_modified': ''
    }
    version = str((config or {}).get('ragemp_version') or '').strip()
    archive_url = str((config or {}).get('ragemp_archive_url') or '').strip()
    etag = str((config or {}).get('ragemp_etag') or '').strip()
    last_modified = str((config or {}).get('ragemp_last_modified') or '').strip()
    formatted = _format_ragemp_build_label(last_modified, etag, archive_url)

    if not archive_url:
        archive_url = defaults['archive_url']
    if not version or (_looks_like_header_date_version(version) and formatted):
        version = formatted

    if version or archive_url or etag or last_modified:
        _persist_local_ragemp_info(version, archive_url, etag, last_modified)

    return {
        'version': version or defaults['version'],
        'archive_url': archive_url or defaults['archive_url'],
        'etag': etag or defaults['etag'],
        'last_modified': last_modified or defaults['last_modified']
    }


def _ragemp_local_info_is_empty(info) -> bool:
    info = info or {}
    return not any(str(info.get(key) or '').strip() for key in ('version', 'etag', 'last_modified'))


def _guess_version_from_url(url: str) -> str:
    if not url:
        return ''
    match = re.search(r'(\d+\.\d+(?:\.\d+)?)', str(url))
    return match.group(1) if match else ''


def _normalize_version(val: str):
    if not val:
        return ()
    s = str(val).strip()
    if s.startswith(('v', 'V')):
        s = s[1:]
    parts = re.split(r'[^0-9]+', s)
    nums = [int(p) for p in parts if p.isdigit()]
    return tuple(nums)


def _is_newer_version(new: str, current: str) -> bool:
    if not new:
        return False
    if not current:
        return True
    new_t = _normalize_version(new)
    cur_t = _normalize_version(current)
    if new_t and cur_t:
        return new_t > cur_t
    return str(new) != str(current)


def _fetch_json(url: str):
    req = urllib.request.Request(url, headers={'User-Agent': 'HPM-Panel/1.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        text = raw.decode('utf-8-sig')
        return json.loads(text)


def check_for_updates(force: bool = False):
    with update_lock:
        if update_state['checking'] and not force:
            return
        update_state['checking'] = True
        update_state['error'] = ''

    errors = []
    panel_current = _load_panel_version()
    ragemp_local = _load_ragemp_local_info()
    update_cfg = _load_update_config()

    panel_latest = ''
    panel_zip = ''
    panel_release_url = ''
    panel_available = False

    panel_repo = (update_cfg.get('panel_repo') or '').strip()
    if panel_repo:
        try:
            api_url = f'https://api.github.com/repos/{panel_repo}/releases/latest'
            data = _fetch_json(api_url)
            panel_latest = data.get('tag_name') or ''
            panel_zip = data.get('zipball_url') or ''
            panel_release_url = data.get('html_url') or ''
            panel_available = _is_newer_version(panel_latest, panel_current)
        except Exception as e:
            errors.append(f'Panel update check failed: {e}')

    ragemp_latest = ''
    ragemp_archive = ''
    ragemp_etag = ''
    ragemp_last_modified = ''
    ragemp_available = False
    ragemp_url = (update_cfg.get('ragemp_server_url') or '').strip()
    if ragemp_url:
        try:
            data = _fetch_remote_ragemp_info(ragemp_url)
            ragemp_latest = str(data.get('version') or '').strip()
            ragemp_archive = str(data.get('archive_url') or '').strip()
            ragemp_etag = str(data.get('etag') or '').strip()
            ragemp_last_modified = str(data.get('last_modified') or '').strip()
            if _ragemp_local_info_is_empty(ragemp_local) and _resolve_server_executable_path().exists():
                _persist_local_ragemp_info(
                    version=ragemp_latest,
                    archive_url=ragemp_archive or ragemp_url,
                    etag=ragemp_etag,
                    last_modified=ragemp_last_modified
                )
                ragemp_local = _load_ragemp_local_info()
                ragemp_available = False
            else:
                ragemp_available = (
                    (ragemp_etag and ragemp_etag != str(ragemp_local.get('etag') or '').strip()) or
                    (ragemp_last_modified and ragemp_last_modified != str(ragemp_local.get('last_modified') or '').strip()) or
                    (not ragemp_local.get('version') and bool(ragemp_archive))
                )
        except Exception as e:
            errors.append(f'RageMP update check failed: {e}')

    with update_lock:
        update_state['panel'] = {
            'current': panel_current,
            'latest': panel_latest,
            'available': bool(panel_available and panel_zip),
            'zip_url': panel_zip,
            'release_url': panel_release_url
        }
        update_state['ragemp'] = {
            'current': ragemp_local.get('version') or '',
            'latest': ragemp_latest,
            'available': bool(ragemp_available and ragemp_archive),
            'archive_url': ragemp_archive,
            'etag': ragemp_etag,
            'last_modified': ragemp_last_modified
        }
        update_state['last_check'] = datetime.now().isoformat()
        update_state['error'] = '; '.join(errors)
        update_state['checking'] = False

    socketio.emit('update_status', get_update_payload())


def _load_update_run_state():
    defaults = {
        'running': False,
        'finished': False,
        'success': False,
        'progress': 0,
        'step': '',
        'message': '',
        'targets': [],
        'error': '',
        'log': [],
        'updated_at': None
    }
    data = _safe_json_load(UPDATE_STATUS_FILE, defaults)
    if not isinstance(data, dict):
        data = dict(defaults)
    for k, v in defaults.items():
        data.setdefault(k, v)
    return data


def get_update_payload():
    with update_lock:
        state = json.loads(json.dumps(update_state))
    if not (state.get('panel') or {}).get('current'):
        state.setdefault('panel', {})['current'] = _load_panel_version()
    run_state = _load_update_run_state()
    state['available'] = bool(state['panel']['available'] or state['ragemp']['available'])
    state['run'] = run_state
    return state


def _detect_restart_mode():
    try:
        proc = psutil.Process(os.getpid())
        parent = proc.parent()
        if parent and parent.cmdline():
            cmd = ' '.join(parent.cmdline()).lower()
            if 'main.py' in cmd:
                return 'main'
    except Exception:
        pass
    return 'standalone'


def _update_check_loop():
    while True:
        try:
            check_for_updates()
        except Exception:
            pass
        update_cfg = _load_update_config()
        interval_min = update_cfg.get('check_interval_minutes', DEFAULT_UPDATE_INTERVAL_MINUTES)
        try:
            interval_min = int(interval_min)
        except Exception:
            interval_min = DEFAULT_UPDATE_INTERVAL_MINUTES
        if interval_min <= 0:
            interval_min = DEFAULT_UPDATE_INTERVAL_MINUTES
        time.sleep(interval_min * 60)

                                                         

@app.route('/')
def index():
    
    if setup_required():
        return redirect(url_for('setup'))

    if 'username' not in session:
        return redirect(url_for('login'))

                                       
    user = get_user(session['username'])
    if user and user.get('force_password_change'):
        return redirect(url_for('change_password'))

    return send_from_directory(str(TEMPLATES_DIR), 'dashboard.html')

@app.route('/logo.png')
def logo():
    
    return send_from_directory(str(TEMPLATES_DIR), 'logo.png')

@app.route('/pfp/<filename>')
def pfp_file(filename):
    
    safe_name = secure_filename(filename)
    if safe_name not in list_pfp_files():
        abort(404)
    return send_from_directory(PFP_DIR, safe_name)

@app.route('/setup')
def setup():
    
    if not setup_required():
        return redirect(url_for('index'))
    ensure_setup_pin()
    return send_from_directory(str(TEMPLATES_DIR), 'setup.html')

@app.route('/login')
def login():
    
    if setup_required():
        return redirect(url_for('setup'))

    if 'username' in session:
        return redirect(url_for('index'))

    return send_from_directory(str(TEMPLATES_DIR), 'login.html')

@app.route('/change-password')
def change_password():
    
    if 'username' not in session:
        return redirect(url_for('login'))

    return send_from_directory(str(TEMPLATES_DIR), 'change_password.html')

                                                      

@app.route('/api/setup-pin', methods=['POST'])
def api_setup_pin():
    
    if not setup_required():
        return jsonify({'success': False, 'message': 'Setup already completed'}), 400

    if not _load_setup_pin():
        pin = ensure_setup_pin()
        if pin:
            _announce_setup_pin(pin)
        return jsonify({'success': False, 'message': 'Setup PIN not initialized. Check server console.'}), 400

    data = request.json or {}
    pin = str(data.get('pin', '')).strip()
    if not verify_setup_pin(pin):
        return jsonify({'success': False, 'message': 'Invalid setup PIN'}), 403

    return jsonify({'success': True})

@app.route('/api/setup', methods=['POST'])
def api_setup():
    
    if not setup_required():
        return jsonify({'success': False, 'message': 'Setup already completed'})

    if not _load_setup_pin():
        pin = ensure_setup_pin()
        if pin:
            _announce_setup_pin(pin)
        return jsonify({'success': False, 'message': 'Setup PIN not initialized. Check server console.'}), 400

    data = request.json or {}
    pin = str(data.get('pin', '')).strip()
    if not verify_setup_pin(pin):
        return jsonify({'success': False, 'message': 'Invalid setup PIN'}), 403
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()

    if not username or not password:
        return jsonify({'success': False, 'message': 'Username and password required'})

    if len(password) < 8:
        return jsonify({'success': False, 'message': 'Password must be at least 8 characters'})

    with setup_lock:
        if setup_state['running']:
            return jsonify({'success': False, 'message': 'Setup already running'})
        setup_state['running'] = True
        setup_state['finished'] = False
        setup_state['success'] = False
        setup_state['progress'] = 0
        setup_state['step'] = 'Starting'
        setup_state['message'] = 'Starting setup...'
        setup_state['log'] = []

    def _runner():
        global config, panel_config
        try:
            _setup_update(step='Storage', progress=5, message='Preparing standalone file database')
            storage.ensure_storage_files()

                                                              
            config = storage.load_config()
            panel_config = storage.load_panel_config()

            if not panel_config.get('panel_secret') or panel_config.get('panel_secret') == 'changeme':
                panel_config['panel_secret'] = secrets.token_hex(24)
                storage.save_panel_config(panel_config)

                                 
            ensure_server_files()

                                                    
            config['server_path'] = './RageMP-Server/ragemp-srv/ragemp-server'
            config['server_name'] = 'ragemp-server'
            config['log_file'] = './RageMP-Server/ragemp-srv/server.log'
            storage.save_config(config)

                                                      
            _setup_update(step='User', progress=95, message='Creating admin account')
            success, message = create_user(username, password, role='admin', force_password_change=False)
            if not success:
                raise RuntimeError(message)

            log_user_action(username, 'SETUP', 'Admin account created')
            _setup_finish()
        except Exception as e:
            _setup_fail(str(e))

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({'success': True, 'message': 'Setup started'})


@app.route('/api/db-test', methods=['POST'])
def api_db_test():
    storage.ensure_storage_files()
    return jsonify({'success': True, 'message': 'Standalone file storage is active'})


@app.route('/api/setup-status', methods=['GET'])
def api_setup_status():
    with setup_lock:
        return jsonify(dict(setup_state))

@app.route('/api/login', methods=['POST'])
def api_login():
    
    client_ip = request.remote_addr or 'unknown'

                      
    attempt = _login_attempts.get(client_ip, {})
    if attempt.get('locked_until', 0) > time.time():
        remaining = int(attempt['locked_until'] - time.time())
        return jsonify({'success': False, 'message': f'Too many failed attempts. Try again in {remaining}s.'}), 429

    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    success, user = authenticate_user(username, password)

    if success:
                                   
        _login_attempts.pop(client_ip, None)
        session['username'] = username
        session.permanent = True
        log_user_action(session['username'], 'LOGIN', 'Success')

        return jsonify({
            'success': True,
            'force_password_change': user.get('force_password_change', False)
        })

                          
    if client_ip not in _login_attempts:
        _login_attempts[client_ip] = {'count': 0, 'locked_until': 0}
    _login_attempts[client_ip]['count'] += 1
    if _login_attempts[client_ip]['count'] >= RATE_LIMIT_MAX_ATTEMPTS:
        _login_attempts[client_ip]['locked_until'] = time.time() + RATE_LIMIT_LOCK_SECONDS
        _login_attempts[client_ip]['count'] = 0

    return jsonify({'success': False, 'message': 'Invalid credentials'})

@app.route('/api/logout', methods=['POST'])
def api_logout():
    
    if 'username' in session:
        log_user_action(session['username'], 'LOGOUT', 'Success')
        session.pop('username', None)

    return jsonify({'success': True})

@app.route('/api/change-password', methods=['POST'])
@login_required
def api_change_password():
    
    data = request.json
    new_password = data.get('password', '').strip()

    if len(new_password) < 8:
        return jsonify({'success': False, 'message': 'Password must be at least 8 characters'})

    success, message = update_user_password(session['username'], new_password)

    if success:
        log_user_action(session['username'], 'PASSWORD_CHANGE', 'Success')

    return jsonify({'success': success, 'message': message})

@app.route('/api/verify-password', methods=['POST'])
@login_required
@admin_required
def api_verify_password():
    
    data = request.json or {}
    password = (data.get('password') or '').strip()
    if not password:
        return jsonify({'success': False, 'message': 'Password required'}), 400
    username = session.get('username')
    if not username:
        return jsonify({'success': False, 'message': 'Authentication required'}), 401
    if not verify_user_password(username, password):
        return jsonify({'success': False, 'message': 'Invalid password'}), 403
    return jsonify({'success': True})

@app.route('/api/secrets/settings', methods=['POST'])
@login_required
@admin_required
def api_get_settings_secret():
    
    return jsonify({'success': False, 'message': 'RageMP conf.json does not expose a panel-managed secret'}), 404

@app.route('/api/secrets/panel', methods=['POST'])
@login_required
@admin_required
def api_get_panel_secret():
    
    data = request.json or {}
    password = (data.get('password') or '').strip()
    if not password:
        return jsonify({'success': False, 'message': 'Admin password required'}), 400
    if not verify_user_password(session.get('username'), password):
        return jsonify({'success': False, 'message': 'Invalid password'}), 403
    return jsonify({'success': True, 'secret': panel_config.get('panel_secret', '')})

@app.route('/api/current-user')
@login_required
def api_current_user():
    
    user = get_user(session['username'])

    if not user:
        return jsonify({'error': 'User not found'}), 404

    return jsonify({
        'username': session['username'],
        'display_name': user.get('display_name') or session['username'],
        'role': user.get('role'),
        'created_at': user.get('created_at'),
        'last_login': user.get('last_login'),
        'permissions': user.get('permissions', {}),
        'avatar_url': resolve_avatar_url_for_user(session['username'], user.get('avatar') or '')
    })

@app.route('/api/pfp')
@login_required
def api_list_pfp():
    return jsonify({'files': list_pfp_files()})

@app.route('/api/avatar/<username>')
@login_required
def api_user_avatar(username):
    safe_user = secure_filename(username or '')
    if not safe_user:
        return ('', 404)
    user = get_user(safe_user)
    if not user:
        return ('', 404)
    avatar_value = user.get('avatar') or ''
    if avatar_value.startswith('builtin:'):
        name = avatar_value.split(':', 1)[1]
        if name in list_pfp_files():
            return send_from_directory(PFP_DIR, name)
        return ('', 404)
    if avatar_value.startswith('data:image/'):
        try:
            header, b64 = avatar_value.split(',', 1)
            mime = 'image/png'
            if header.startswith('data:') and ';' in header:
                mime = header[5:].split(';', 1)[0] or mime
            raw = base64.b64decode(b64, validate=True)
            resp = send_file(BytesIO(raw), mimetype=mime)
            resp.headers['Cache-Control'] = 'private, max-age=86400'
            return resp
        except Exception:
            return ('', 404)
    return ('', 404)

@app.route('/api/profile', methods=['GET'])
@login_required
def api_get_profile():
    user = get_user(session['username'])
    if not user:
        return jsonify({'error': 'User not found'}), 404
    avatar_value = user.get('avatar') or ''
    return jsonify({
        'username': session['username'],
        'display_name': user.get('display_name') or session['username'],
        'avatar': avatar_value,
        'avatar_url': resolve_avatar_url_for_user(session['username'], avatar_value)
    })

@app.route('/api/profile', methods=['POST'])
@login_required
def api_update_profile():
    data = request.json or {}
    users = init_users()
    username = session.get('username')
    if username not in users:
        return jsonify({'success': False, 'message': 'User not found'}), 404

    user = users[username]

                  
    if 'display_name' in data:
        display_name = (data.get('display_name') or '').strip()
        if len(display_name) > 32:
            return jsonify({'success': False, 'message': 'Display name too long'}), 400
        user['display_name'] = display_name or username

            
    if 'avatar' in data:
        avatar = (data.get('avatar') or '').strip()
        if not avatar:
            user['avatar'] = ''
        elif avatar.startswith('builtin:'):
            name = avatar.split(':', 1)[1]
            if name not in list_pfp_files():
                return jsonify({'success': False, 'message': 'Invalid avatar selection'}), 400
            user['avatar'] = f'builtin:{name}'
        elif avatar.startswith('data:image/'):
                                          
            try:
                header, b64 = avatar.split(',', 1)
            except ValueError:
                return jsonify({'success': False, 'message': 'Invalid avatar data'}), 400
            if len(b64) > 1500000:
                return jsonify({'success': False, 'message': 'Avatar too large'}), 400
            try:
                raw = base64.b64decode(b64, validate=True)
            except Exception:
                return jsonify({'success': False, 'message': 'Invalid avatar data'}), 400
            if len(raw) > 1024 * 1024:
                return jsonify({'success': False, 'message': 'Avatar too large'}), 400
            user['avatar'] = avatar
        else:
            return jsonify({'success': False, 'message': 'Invalid avatar value'}), 400

    users[username] = user
    save_users(users)
    log_user_action(username, 'UPDATE_PROFILE', json.dumps({'display_name': user.get('display_name') or '', 'avatar': bool(user.get('avatar'))}))
    return jsonify({
        'success': True,
        'display_name': user.get('display_name') or username,
        'avatar_url': resolve_avatar_url_for_user(username, user.get('avatar') or '')
    })

@app.route('/api/profile/change-password', methods=['POST'])
@login_required
def api_profile_change_password():
    data = request.json or {}
    current_password = (data.get('current_password') or '').strip()
    new_password = (data.get('new_password') or '').strip()
    if not current_password:
        return jsonify({'success': False, 'message': 'Current password required'}), 400
    if not verify_user_password(session.get('username'), current_password):
        return jsonify({'success': False, 'message': 'Invalid password'}), 403
    if len(new_password) < 8:
        return jsonify({'success': False, 'message': 'Password must be at least 8 characters'}), 400
    success, message = update_user_password(session['username'], new_password)
    if success:
        log_user_action(session['username'], 'PASSWORD_CHANGE', 'Success')
    return jsonify({'success': success, 'message': message})

                                                              

@app.route('/api/panel-config', methods=['GET'])
@login_required
@admin_required
def api_get_panel_config():
    
    safe_cfg = dict(panel_config)
    panel_secret = safe_cfg.pop('panel_secret', None)
    safe_cfg['panel_secret_set'] = bool(panel_secret)
    discord_cfg = _normalize_discord_settings(safe_cfg.get('discord'))
    token_set = bool(discord_cfg.get('token'))
    discord_cfg['token'] = ''
    discord_cfg['token_set'] = token_set
    safe_cfg['discord'] = discord_cfg
    safe_cfg['discord_runtime'] = discord_runtime.status_payload()
    return jsonify(safe_cfg)


@app.route('/api/panel-config', methods=['POST'])
@login_required
@admin_required
def api_set_panel_config():
    
    global panel_config
    data = request.json or {}
    incoming_secret = data.get('panel_secret')
    current_secret = panel_config.get('panel_secret')
    discord_changed = False

                                              
    if not incoming_secret:
        data['panel_secret'] = current_secret
    elif incoming_secret != current_secret:
        admin_password = (data.get('admin_password') or '').strip()
        if not admin_password:
            return jsonify({'success': False, 'message': 'Admin password required'}), 400
        if not verify_user_password(session.get('username'), admin_password):
            return jsonify({'success': False, 'message': 'Invalid password'}), 403

    for key in ('locale', 'panel_name', 'auto_start', 'scheduled_restarts', 'panel_secret'):
        if key in data:
            panel_config[key] = data[key]

    if 'discord' in data:
        raw_discord = data.get('discord')
        if not isinstance(raw_discord, dict):
            return jsonify({'success': False, 'message': 'Invalid discord configuration'}), 400

        current_discord = _get_discord_config()
        new_discord = dict(current_discord)

        if 'enabled' in raw_discord:
            new_discord['enabled'] = bool(raw_discord.get('enabled'))
        if 'guild_id' in raw_discord:
            new_discord['guild_id'] = str(raw_discord.get('guild_id') or '').strip()
        if 'warnings_channel_id' in raw_discord:
            new_discord['warnings_channel_id'] = str(raw_discord.get('warnings_channel_id') or '').strip()
        if 'status_embed_json' in raw_discord:
            new_discord['status_embed_json'] = str(raw_discord.get('status_embed_json') or '').strip()
        if 'status_config_json' in raw_discord:
            new_discord['status_config_json'] = str(raw_discord.get('status_config_json') or '').strip()
        if 'status_messages' in raw_discord:
            new_discord['status_messages'] = raw_discord.get('status_messages')

        if 'token' in raw_discord:
            candidate_token = str(raw_discord.get('token') or '').strip()
            if candidate_token:
                new_discord['token'] = candidate_token

        new_discord = _normalize_discord_settings(new_discord)

        if new_discord.get('guild_id') and not _is_valid_discord_id(new_discord.get('guild_id')):
            return jsonify({'success': False, 'message': 'Invalid Guild/Server ID'}), 400
        if new_discord.get('warnings_channel_id') and not _is_valid_discord_id(new_discord.get('warnings_channel_id')):
            return jsonify({'success': False, 'message': 'Invalid Warnings Channel ID'}), 400

        try:
            parsed_embed = json.loads(new_discord.get('status_embed_json') or '{}')
            if not isinstance(parsed_embed, dict):
                raise ValueError('Status Embed JSON must be an object.')
        except Exception as e:
            return jsonify({'success': False, 'message': f'Invalid Status Embed JSON: {e}'}), 400

        try:
            parsed_status_cfg = json.loads(new_discord.get('status_config_json') or '{}')
            if not isinstance(parsed_status_cfg, dict):
                raise ValueError('Status Config JSON must be an object.')
        except Exception as e:
            return jsonify({'success': False, 'message': f'Invalid Status Config JSON: {e}'}), 400

        panel_config['discord'] = new_discord
        discord_changed = True

    save_panel_config(panel_config)
    try:
        _install_ragemp_bridge_package()
    except Exception:
        pass
    if discord_changed:
        discord_runtime.sync_from_config(force=True)
    discord_runtime.request_status_refresh(force=True)

    log_payload = dict(data)
    log_payload.pop('panel_secret', None)
    log_payload.pop('admin_password', None)
    if isinstance(log_payload.get('discord'), dict):
        log_payload['discord'] = dict(log_payload['discord'])
        if 'token' in log_payload['discord']:
            log_payload['discord']['token'] = '[hidden]'
    log_user_action(session['username'], 'UPDATE_PANEL_CONFIG', json.dumps(log_payload))
    return jsonify({'success': True, 'message': 'Panel configuration saved.'})

                                                                          

@app.route('/api/panel-locale', methods=['GET'])
@login_required
def api_panel_locale():
    
    return jsonify({
        'locale': panel_config.get('locale', 'en'),
        'panel_name': panel_config.get('panel_name', 'RageAdmin')
    })

                                                         

@app.route('/api/locales/<locale>')
@login_required
def api_get_locale(locale):
    
    safe_locale = secure_filename(locale)
    locale_file = LOCALES_DIR / f'{safe_locale}.json'
    if not locale_file.exists():
                             
        locale_file = LOCALES_DIR / 'en.json'
    if not locale_file.exists():
        return jsonify({}), 404
    with open(locale_file, 'r', encoding='utf-8') as f:
        return jsonify(json.load(f))

                                                              

@app.route('/api/settings', methods=['GET'])
@login_required
@admin_required
def api_get_settings():
    
    data = parse_settings_xml()
    if data is None:
        return jsonify({'error': 'conf.json not found or unreadable'}), 404
    return jsonify(data)


@app.route('/api/settings', methods=['POST'])
@login_required
@admin_required
def api_set_settings():
    
    data = request.json or {}
    try:
        write_settings_xml(data)
        discord_runtime.request_status_refresh(force=True)

        log_user_action(session['username'], 'UPDATE_SETTINGS', json.dumps(data))
        return jsonify({'success': True, 'message': 'Settings saved.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

                                                       

@app.route('/api/files')
@login_required
@require_permission('can_view_files')
def api_files():
    
    req_path = request.args.get('path', '')
    abs_path, err = jail_path(req_path)
    if err:
        return jsonify({'error': err, 'files': []}), 403

    if not os.path.isdir(abs_path):
        return jsonify({'error': 'Directory not found', 'files': []}), 404

    entries = []
    try:
        for name in sorted(os.listdir(abs_path)):
            if name in {'.', '..'}:
                continue
            full = os.path.join(abs_path, name)
            try:
                st = os.stat(full)
                is_dir = os.path.isdir(full)
                entries.append({
                    'name': name,
                    'is_dir': is_dir,
                    'size': 0 if is_dir else int(st.st_size),
                    'modified': int(st.st_mtime),
                })
            except FileNotFoundError:
                continue
    except PermissionError:
        return jsonify({'error': 'Permission denied reading directory', 'files': []})

    return jsonify({'path': req_path, 'files': entries})


@app.route('/api/files/read')
@login_required
@require_permission('can_view_files')
def api_files_read():
    
    req_path = request.args.get('path', '')
    abs_path, err = jail_path(req_path)
    if err:
        return jsonify({'error': err}), 403

    if not os.path.isfile(abs_path):
        return jsonify({'error': 'File not found'}), 404

    if os.path.getsize(abs_path) > 2 * 1024 * 1024:
        return jsonify({'error': 'File too large to edit (max 2MB)'}), 413

    try:
        with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return jsonify({'content': content, 'path': req_path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/files/write', methods=['POST'])
@login_required
@require_permission('can_edit_files')
def api_files_write():
    
    data = request.json or {}
    req_path = data.get('path', '')
    content = data.get('content', '')
    abs_path, err = jail_path(req_path)
    if err:
        return jsonify({'error': err}), 403

    try:
        with open(abs_path, 'w', encoding='utf-8') as f:
            f.write(content)
        log_user_action(session['username'], 'EDIT_FILE', req_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/files/rename', methods=['POST'])
@login_required
@require_permission('can_edit_files')
def api_files_rename():
    
    data = request.json or {}
    req_path = data.get('path', '')
    new_name = data.get('new_name', '').strip()
    if not new_name or '/' in new_name or '\\' in new_name:
        return jsonify({'error': 'Invalid new name'}), 400

    abs_path, err = jail_path(req_path)
    if err:
        return jsonify({'error': err}), 403

    if not os.path.exists(abs_path):
        return jsonify({'error': 'Path not found'}), 404

    new_abs = os.path.join(os.path.dirname(abs_path), new_name)
    new_abs = os.path.normpath(new_abs)
    if not new_abs.startswith(get_server_dir()):
        return jsonify({'error': 'Access denied'}), 403

    try:
        os.rename(abs_path, new_abs)
        log_user_action(session['username'], 'RENAME_FILE', f'{req_path} -> {new_name}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/files/delete', methods=['DELETE'])
@login_required
@require_permission('can_edit_files')
def api_files_delete():
    
    req_path = request.args.get('path', '')
    abs_path, err = jail_path(req_path)
    if err:
        return jsonify({'error': err}), 403

    if not os.path.exists(abs_path):
        return jsonify({'error': 'Path not found'}), 404

                                      
    if os.path.normpath(abs_path) == os.path.normpath(get_server_dir()):
        return jsonify({'error': 'Cannot delete server root'}), 403

    try:
        if os.path.isdir(abs_path):
            shutil.rmtree(abs_path)
        else:
            os.remove(abs_path)
        log_user_action(session['username'], 'DELETE_FILE', req_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/files/create-folder', methods=['POST'])
@login_required
@require_permission('can_edit_files')
def api_files_create_folder():
    
    data = request.json or {}
    req_path = data.get('path', '')
    abs_path, err = jail_path(req_path)
    if err:
        return jsonify({'error': err}), 403

    try:
        os.makedirs(abs_path, exist_ok=True)
        log_user_action(session['username'], 'CREATE_FOLDER', req_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/files/compress', methods=['POST'])
@login_required
@require_permission('can_edit_files')
def api_files_compress():
    
    data = request.json or {}
    req_path = data.get('path', '')
    abs_path, err = jail_path(req_path)
    if err:
        return jsonify({'error': err}), 403

    if not os.path.exists(abs_path):
        return jsonify({'error': 'Path not found'}), 404

    zip_path = abs_path + '.zip'
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            if os.path.isdir(abs_path):
                for root, dirs, files in os.walk(abs_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, os.path.dirname(abs_path))
                        zf.write(file_path, arcname)
            else:
                zf.write(abs_path, os.path.basename(abs_path))
        log_user_action(session['username'], 'COMPRESS', req_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/files/compress-multiple', methods=['POST'])
@login_required
@require_permission('can_view_files')
def api_files_compress_multiple():
    
    data = request.json or {}
    paths = data.get('paths', [])
    if not paths:
        return jsonify({'error': 'No files selected'}), 400

    import tempfile
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
        tmp_path = tmp.name
        tmp.close()

        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for req_path in paths:
                abs_path, err = jail_path(req_path)
                if err:
                    continue
                if not os.path.exists(abs_path):
                    continue
                if os.path.isdir(abs_path):
                    for root, dirs, files in os.walk(abs_path):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.join(os.path.basename(abs_path),
                                                   os.path.relpath(file_path, abs_path))
                            zf.write(file_path, arcname)
                else:
                    zf.write(abs_path, os.path.basename(abs_path))

        log_user_action(session['username'], 'COMPRESS_MULTIPLE', f'{len(paths)} items')
        return send_file(tmp_path, as_attachment=True, download_name='selected_files.zip')
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/files/decompress', methods=['POST'])
@login_required
@require_permission('can_edit_files')
def api_files_decompress():
    
    data = request.json or {}
    req_path = data.get('path', '')
    abs_path, err = jail_path(req_path)
    if err:
        return jsonify({'error': err}), 403

    if not os.path.isfile(abs_path):
        return jsonify({'error': 'File not found'}), 404

    if not abs_path.lower().endswith('.zip'):
        return jsonify({'error': 'Not a zip file'}), 400

    dest_dir = os.path.dirname(abs_path)
    try:
        with zipfile.ZipFile(abs_path, 'r') as zf:
                                                           
            for name in zf.namelist():
                resolved = os.path.normpath(os.path.join(dest_dir, name))
                if not resolved.startswith(get_server_dir()):
                    return jsonify({'error': 'Archive contains unsafe paths'}), 400
            zf.extractall(dest_dir)
        log_user_action(session['username'], 'DECOMPRESS', req_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/files/download')
@login_required
@require_permission('can_view_files')
def api_files_download():
    
    req_path = request.args.get('path', '')
    abs_path, err = jail_path(req_path)
    if err:
        return jsonify({'error': err}), 403

    if not os.path.isfile(abs_path):
        return jsonify({'error': 'File not found'}), 404

    return send_file(abs_path, as_attachment=True, download_name=os.path.basename(abs_path))


@app.route('/api/files/upload', methods=['POST'])
@login_required
@require_permission('can_edit_files')
def api_files_upload():
    
    req_path = request.form.get('path', '')
    abs_path, err = jail_path(req_path)
    if err:
        return jsonify({'error': err}), 403

    if not os.path.isdir(abs_path):
        return jsonify({'error': 'Destination directory not found'}), 404

    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files provided'}), 400

    saved = []
    for f in files:
        filename = secure_filename(f.filename)
        if not filename:
            continue
        dest = os.path.join(abs_path, filename)
                        
        if not os.path.normpath(dest).startswith(get_server_dir()):
            continue
        f.save(dest)
        saved.append(filename)

    log_user_action(session['username'], 'UPLOAD', f'{req_path}: {", ".join(saved)}')
    return jsonify({'success': True, 'files': saved})

                                                      

def _log_user_meta(users_map, username: str):
    user = users_map.get(username) if users_map else None
    display_name = (user or {}).get('display_name') or username
    avatar_url = resolve_avatar_url_for_user(username, (user or {}).get('avatar', ''))
    return display_name, avatar_url

def _parse_log_file(log_file: Path, username: str, display_name: str, avatar_url: str, entries: list):
    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                                                               
                match = re.match(r'^\[(.+?)\]\s+(\S+?):\s*(.*)', line)
                if match:
                    ts = match.group(1)
                    action = match.group(2)
                    details = match.group(3)
                else:
                    ts = ''
                    action = ''
                    details = line
                ts_epoch = 0
                if ts:
                    try:
                        ts_epoch = int(datetime.strptime(ts, '%Y-%m-%d %H:%M:%S').timestamp())
                    except Exception:
                        ts_epoch = 0
                entries.append({
                    'timestamp': ts,
                    'action': action,
                    'details': details,
                    'username': username,
                    'display_name': display_name,
                    'avatar_url': avatar_url,
                    '_ts': ts_epoch
                })
    except Exception:
        pass

@app.route('/api/logs')
@login_required
@require_permission('can_view_logs')
def api_logs_list():
    
    result = {}
    all_dates = set()
    try:
        users_map = init_users()
    except Exception:
        users_map = {}
    if not LOGS_DIR.exists():
        return jsonify({'users': result, 'all_dates': []})

    for user_dir in sorted(LOGS_DIR.iterdir()):
        if not user_dir.is_dir():
            continue
        username = user_dir.name
        dates = []
        for log_file in sorted(user_dir.glob('*.log'), reverse=True):
            dates.append(log_file.stem)                     
        if dates:
            display_name, avatar_url = _log_user_meta(users_map, username)
            result[username] = {
                'dates': dates,
                'display_name': display_name,
                'avatar_url': avatar_url
            }
            for d in dates:
                all_dates.add(d)

    return jsonify({'users': result, 'all_dates': sorted(all_dates, reverse=True)})


@app.route('/api/logs/<username>/<date>')
@login_required
@require_permission('can_view_logs')
def api_logs_get(username, date):
    
    safe_user = secure_filename(username)
    safe_date = secure_filename(date)
    try:
        users_map = init_users()
    except Exception:
        users_map = {}

    if not safe_date:
        return jsonify({'entries': []})

    if safe_user.upper() == 'ALL':
        entries = []
        if LOGS_DIR.exists():
            for user_dir in sorted(LOGS_DIR.iterdir()):
                if not user_dir.is_dir():
                    continue
                uname = user_dir.name
                log_file = user_dir / f'{safe_date}.log'
                if not log_file.exists():
                    continue
                display_name, avatar_url = _log_user_meta(users_map, uname)
                _parse_log_file(log_file, uname, display_name, avatar_url, entries)
        entries.sort(key=lambda x: x.get('_ts', 0), reverse=True)
        for e in entries:
            e.pop('_ts', None)
        return jsonify({'entries': entries})

    log_file = LOGS_DIR / safe_user / f'{safe_date}.log'
    if not log_file.exists():
        return jsonify({'entries': []})

    entries = []
    display_name, avatar_url = _log_user_meta(users_map, safe_user)
    _parse_log_file(log_file, safe_user, display_name, avatar_url, entries)
    entries.sort(key=lambda x: x.get('_ts', 0), reverse=True)
    for e in entries:
        e.pop('_ts', None)
    return jsonify({'entries': entries})

                                                                              

@app.route('/api/users', methods=['GET'])
@admin_required
def api_list_users():
    
    users = init_users()

    user_list = []
    for username, user_data in users.items():
        user_list.append({
            'username': username,
            'display_name': user_data.get('display_name') or username,
            'avatar_url': resolve_avatar_url_for_user(username, user_data.get('avatar') or ''),
            'role': user_data.get('role'),
            'created_at': user_data.get('created_at'),
            'last_login': user_data.get('last_login'),
            'enabled': user_data.get('enabled', True),
            'permissions': user_data.get('permissions', {})
        })

    return jsonify({'users': user_list})

@app.route('/api/users', methods=['POST'])
@admin_required
def api_create_user():
    
    data = request.json or {}
    username = data.get('username', '').strip()
    role = data.get('role', 'user')
    permissions = data.get('permissions', None)
    password = (data.get('password') or '').strip()

    if not username:
        return jsonify({'success': False, 'message': 'Username required'})

                              
    if not re.match(r'^[a-zA-Z0-9_]{3,32}$', username):
        return jsonify({'success': False, 'message': 'Username must be 3-32 alphanumeric characters (a-z, 0-9, _)'})

    generated_password = False
    if not password:
        password = generate_password()
        generated_password = True
    elif len(password) < 8:
        return jsonify({'success': False, 'message': 'Password must be at least 8 characters'})

    success, message = create_user(username, password, role=role, force_password_change=True, permissions=permissions)

    if success:
        log_user_action(session['username'], 'CREATE_USER', f'Created user: {username}')

        return jsonify({
            'success': True,
            'message': message,
            'password': password,
            'username': username,
            'generated_password': generated_password
        })

    return jsonify({'success': False, 'message': message})

@app.route('/api/users/<username>', methods=['DELETE'])
@admin_required
def api_delete_user(username):
    
    users = init_users()

    if username not in users:
        return jsonify({'success': False, 'message': 'User not found'})

    if username == session['username']:
        return jsonify({'success': False, 'message': 'Cannot delete yourself'})

    del users[username]
    save_users(users)

    log_user_action(session['username'], 'DELETE_USER', f'Deleted user: {username}')

    return jsonify({'success': True, 'message': 'User deleted'})

@app.route('/api/users/<username>', methods=['PUT'])
@admin_required
def api_update_user(username):
    
    users = init_users()
    if username not in users:
        return jsonify({'success': False, 'message': 'User not found'})

    data = request.json or {}

    if 'role' in data:
        users[username]['role'] = data['role']

    if 'permissions' in data:
        users[username]['permissions'] = data['permissions']

    save_users(users)
    log_user_action(session['username'], 'UPDATE_USER', f'Updated user: {username}')
    return jsonify({'success': True, 'message': 'User updated'})


@app.route('/api/users/<username>/force-password-change', methods=['POST'])
@admin_required
def api_force_password_change(username):
    
    users = init_users()
    if username not in users:
        return jsonify({'success': False, 'message': 'User not found'})

    users[username]['force_password_change'] = True
    save_users(users)
    log_user_action(session['username'], 'FORCE_PW_CHANGE', f'User: {username}')
    return jsonify({'success': True, 'message': f'{username} must change password on next login'})


@app.route('/api/users/<username>/toggle', methods=['POST'])
@admin_required
def api_toggle_user(username):
    
    users = init_users()

    if username not in users:
        return jsonify({'success': False, 'message': 'User not found'})

    if username == session['username']:
        return jsonify({'success': False, 'message': 'Cannot disable yourself'})

    users[username]['enabled'] = not users[username].get('enabled', True)
    save_users(users)

    status = 'enabled' if users[username]['enabled'] else 'disabled'
    log_user_action(session['username'], 'TOGGLE_USER', f'{username} {status}')

    return jsonify({'success': True, 'enabled': users[username]['enabled']})

                                                           

@app.route('/api/resources')
@login_required
@require_permission('can_manage_resources')
def api_list_resources():
    
    resources_dir = os.path.join(get_server_dir(), 'resources')
    if not os.path.isdir(resources_dir):
        return jsonify({'resources': []})

                                              
    settings = parse_settings_xml()
    configured_resources = settings.get('resources', []) if settings else []

    resources = []
    for name in sorted(os.listdir(resources_dir)):
        full_path = os.path.join(resources_dir, name)
        if os.path.isdir(full_path):
                             
            if name in resource_states:
                state = resource_states[name]
            elif server_state['running'] and name in configured_resources:
                state = 'started'
            elif server_state['running']:
                state = 'stopped'
            else:
                state = 'stopped'

                                   
            has_meta = os.path.isfile(os.path.join(full_path, 'meta.xml'))

            resources.append({
                'name': name,
                'status': state,
                'configured': name in configured_resources,
                'has_meta': has_meta
            })
    return jsonify({'resources': resources})


@app.route('/api/resources/<name>/configure', methods=['POST'])
@login_required
@admin_required
def api_resource_configure(name):
    
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '', name or '')
    if not safe_name:
        return jsonify({'success': False, 'message': 'Invalid resource name'}), 400

    resources_dir = os.path.join(get_server_dir(), 'resources')
    full_path = os.path.join(resources_dir, safe_name)
    if not os.path.isdir(full_path):
        return jsonify({'success': False, 'message': 'Resource not found'}), 404

    settings = parse_settings_xml()
    if settings is None:
        return jsonify({'success': False, 'message': 'Server config not found'}), 404

    resources = settings.get('resources', [])
    if safe_name not in resources:
        resources.append(safe_name)
        settings['resources'] = resources
        write_settings_xml(settings)
        log_user_action(session['username'], 'RESOURCE_CONFIGURE', safe_name)

    return jsonify({'success': True, 'configured': True})


@app.route('/api/resources/<name>/start', methods=['POST'])
@login_required
@require_permission('can_manage_resources')
def api_resource_start(name):
    
    global server_process, resource_states
    if not server_state['running']:
        return jsonify({'success': False, 'message': 'Server is not running'})
    if server_state['attached']:
        return jsonify({'success': False, 'message': 'Cannot send commands to attached process'})
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '', name)
    try:
        if server_process and server_process.stdin:
            server_process.stdin.write(f"resstart {safe_name}\n".encode())
            server_process.stdin.flush()
            resource_states[safe_name] = 'started'
            add_console_line(f'> resstart {safe_name}', session['username'])
            log_user_action(session['username'], 'RESOURCE_START', safe_name)
            return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    return jsonify({'success': False, 'message': 'Failed to send command'})


@app.route('/api/addons')
@login_required
@require_permission('can_manage_resources')
def api_list_addons():
    
    addons_dir = os.path.join(get_server_dir(), 'addons')
    if not os.path.isdir(addons_dir):
        return jsonify({'addons': []})

    settings = parse_settings_xml()
    configured_addons = settings.get('addons', []) if settings else []

    addons = []
    for name in sorted(os.listdir(addons_dir)):
        full_path = os.path.join(addons_dir, name)
        if os.path.isdir(full_path):
            if name in resource_states:
                state = resource_states[name]
            elif server_state['running'] and name in configured_addons:
                state = 'started'
            elif server_state['running']:
                state = 'stopped'
            else:
                state = 'stopped'

            has_meta = os.path.isfile(os.path.join(full_path, 'meta.xml'))
            addons.append({
                'name': name,
                'status': state,
                'configured': name in configured_addons,
                'has_meta': has_meta
            })
    return jsonify({'addons': addons})


@app.route('/api/addons/<name>/configure', methods=['POST'])
@login_required
@admin_required
def api_addon_configure(name):
    
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '', name or '')
    if not safe_name:
        return jsonify({'success': False, 'message': 'Invalid addon name'}), 400

    addons_dir = os.path.join(get_server_dir(), 'addons')
    full_path = os.path.join(addons_dir, safe_name)
    if not os.path.isdir(full_path):
        return jsonify({'success': False, 'message': 'Addon not found'}), 404

    settings = parse_settings_xml()
    if settings is None:
        return jsonify({'success': False, 'message': 'Server config not found'}), 404

    addons = settings.get('addons', [])
    if safe_name not in addons:
        addons.append(safe_name)
        settings['addons'] = addons
        write_settings_xml(settings)
        log_user_action(session['username'], 'ADDON_CONFIGURE', safe_name)

    return jsonify({'success': True, 'configured': True})


@app.route('/api/addons/<name>/start', methods=['POST'])
@login_required
@require_permission('can_manage_resources')
def api_addon_start(name):
    
    global server_process, resource_states
    if not server_state['running']:
        return jsonify({'success': False, 'message': 'Server is not running'})
    if server_state['attached']:
        return jsonify({'success': False, 'message': 'Cannot send commands to attached process'})
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '', name)
    try:
        if server_process and server_process.stdin:
            server_process.stdin.write(f"addonstart {safe_name}\n".encode())
            server_process.stdin.flush()
            resource_states[safe_name] = 'started'
            add_console_line(f'> addonstart {safe_name}', session['username'])
            log_user_action(session['username'], 'ADDON_START', safe_name)
            return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    return jsonify({'success': False, 'message': 'Failed to send command'})


@app.route('/api/addons/<name>/stop', methods=['POST'])
@login_required
@require_permission('can_manage_resources')
def api_addon_stop(name):
    
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '', name or '')
    if safe_name:
        log_user_action(session['username'], 'ADDON_STOP_BLOCKED', safe_name)
    return jsonify({
        'success': False,
        'message': 'Addons cannot be stopped individually. Restart the server.'
    }), 400


@app.route('/api/addons/<name>/restart', methods=['POST'])
@login_required
@require_permission('can_manage_resources')
def api_addon_restart(name):
    
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '', name or '')
    if safe_name:
        log_user_action(session['username'], 'ADDON_RESTART_BLOCKED', safe_name)
    return jsonify({
        'success': False,
        'message': 'Addons cannot be restarted individually. Restart the server.'
    }), 400


@app.route('/api/resources/<name>/stop', methods=['POST'])
@login_required
@require_permission('can_manage_resources')
def api_resource_stop(name):
    
    global server_process, resource_states
    if not server_state['running']:
        return jsonify({'success': False, 'message': 'Server is not running'})
    if server_state['attached']:
        return jsonify({'success': False, 'message': 'Cannot send commands to attached process'})
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '', name)
    try:
        if server_process and server_process.stdin:
            server_process.stdin.write(f"resstop {safe_name}\n".encode())
            server_process.stdin.flush()
            resource_states[safe_name] = 'stopped'
            add_console_line(f'> resstop {safe_name}', session['username'])
            log_user_action(session['username'], 'RESOURCE_STOP', safe_name)
            return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    return jsonify({'success': False, 'message': 'Failed to send command'})


@app.route('/api/resources/<name>/restart', methods=['POST'])
@login_required
@require_permission('can_manage_resources')
def api_resource_restart(name):
    
    global server_process, resource_states
    if not server_state['running']:
        return jsonify({'success': False, 'message': 'Server is not running'})
    if server_state['attached']:
        return jsonify({'success': False, 'message': 'Cannot send commands to attached process'})
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '', name)
    try:
        if server_process and server_process.stdin:
            server_process.stdin.write(f"resrestart {safe_name}\n".encode())
            server_process.stdin.flush()
            resource_states[safe_name] = 'started'
            add_console_line(f'> resrestart {safe_name}', session['username'])
            log_user_action(session['username'], 'RESOURCE_RESTART', safe_name)
            return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    return jsonify({'success': False, 'message': 'Failed to send command'})

                                                                                    

def _check_panel_secret():
    
    secret = request.headers.get('X-Panel-Secret', '')
    expected = panel_config.get('panel_secret', 'changeme')
    return secrets.compare_digest(secret, expected)


@app.route('/api/panel-hook/incoming-connection', methods=['POST'])
def api_panel_hook_incoming_connection():
    global panel_connector_last_heartbeat

    if not _check_panel_secret():
        return jsonify({'error': 'Invalid secret'}), 403

    data = request.json or {}
    data['ip'] = normalize_player_ip(data.get('ip', ''))
    row = {
        'name': data.get('name') or data.get('socialClub') or 'Unknown',
        'ip': data.get('ip', ''),
        'socialClub': _safe_profile_text(data.get('socialClub', ''), 128),
        'rgscId': _safe_profile_text(data.get('rgscId', ''), 128),
        'serial': _safe_profile_text(data.get('serial', ''), 128),
        'gameType': _safe_profile_text(data.get('gameType', ''), 64),
        'identifiers': _extract_player_identifiers(data)
    }
    _, profile = _resolve_player_profile(row, touch_join=False)
    profile['last_connection_at'] = _now_iso()
    _append_profile_history(
        profile,
        'incoming',
        f'{row.get("name", "Unknown")} / {row.get("ip", "-") or "-"} / {row.get("rgscId", "-") or "-"}'
    )
    save_player_profiles(player_profiles)

    panel_connector_last_heartbeat = time.time()
    socketio.emit('players_update', _players_payload())
    socketio.emit('stats_update', build_runtime_status_payload(include_history=False))
    return jsonify({'ok': True})


@app.route('/api/panel-hook/player-join', methods=['POST'])
def api_panel_hook_player_join():
    
    global panel_connector_last_heartbeat
    if not _check_panel_secret():
        return jsonify({'error': 'Invalid secret'}), 403
    data = request.json or {}
    server_id = data.get('serverId')
    name = data.get('name', 'Unknown')
    ip = normalize_player_ip(data.get('ip', ''))
    data['ip'] = ip

    now_ts = int(time.time())
    if server_id is not None:
        sid = str(server_id)
        old = connected_players.get(sid, {})
        join_ts = _safe_int(data.get('joinTime'), 0) or _safe_int(old.get('joinTime'), 0) or now_ts
        connected_players[str(server_id)] = {
            'serverId': server_id,
            'name': name,
            'ping': data.get('ping', 0),
            'ip': ip,
            'socialClub': _safe_profile_text(data.get('socialClub', ''), 128),
            'rgscId': _safe_profile_text(data.get('rgscId', ''), 128),
            'serial': _safe_profile_text(data.get('serial', ''), 128),
            'gameType': _safe_profile_text(data.get('gameType', ''), 64),
            'packetLoss': _safe_int(data.get('packetLoss'), 0),
            'session': data.get('session', 0),
            'sessionActive': data.get('sessionActive', False),
            'joinTime': join_ts,
            'identifiers': _extract_player_identifiers(data)
        }
        _resolve_player_profile(connected_players[sid], touch_join=(not old))
        save_player_profiles(player_profiles)

                     
    banned, ban_info = is_player_banned(ip=ip, name=name)

    panel_connector_last_heartbeat = time.time()
    socketio.emit('player_join', data)
    socketio.emit('players_update', _players_payload())
    socketio.emit('stats_update', build_runtime_status_payload(include_history=False))
    discord_runtime.request_status_refresh(force=False)

    if banned:
        return jsonify({'ok': True, 'banned': True, 'reason': ban_info.get('reason', 'Banned')})

    return jsonify({'ok': True, 'banned': False})


@app.route('/api/panel-hook/player-disconnect', methods=['POST'])
def api_panel_hook_player_disconnect():
    
    global panel_connector_last_heartbeat
    if not _check_panel_secret():
        return jsonify({'error': 'Invalid secret'}), 403
    data = request.json or {}
    server_id = str(data.get('serverId', ''))
    reason = data.get('reason', 'disconnect')

    row = connected_players.get(server_id)
    if row:
        _finalize_player_session(row, reason=reason)
    connected_players.pop(server_id, None)

    panel_connector_last_heartbeat = time.time()
    socketio.emit('player_disconnect', data)
    socketio.emit('players_update', _players_payload())
    socketio.emit('stats_update', build_runtime_status_payload(include_history=False))
    discord_runtime.request_status_refresh(force=False)
    return jsonify({'ok': True})


@app.route('/api/panel-hook/players-sync', methods=['POST'])
def api_panel_hook_players_sync():
    
    global connected_players, panel_connector_last_heartbeat
    if not _check_panel_secret():
        return jsonify({'error': 'Invalid secret'}), 403
    data = request.json or {}
    players = data.get('players', [])

                        
    now_ts = int(time.time())
    old_players = connected_players
    new_players = {}
    for p in players:
        sid = str(p.get('serverId', ''))
        if sid:
            row = dict(p)
            row['ip'] = normalize_player_ip(row.get('ip', ''))
            old = old_players.get(sid, {})
            row['joinTime'] = _safe_int(row.get('joinTime'), 0) or _safe_int(old.get('joinTime'), 0) or now_ts
            row['profile_key'] = row.get('profile_key') or old.get('profile_key', '')
            row['identifiers'] = _extract_player_identifiers(row)
            _resolve_player_profile(row, touch_join=(not old))
            new_players[sid] = row
    prev_sig = _players_snapshot_signature(old_players.values())

    for old_sid, old_row in old_players.items():
        if old_sid not in new_players:
            _finalize_player_session(old_row, reason='sync-missing')

    connected_players = new_players
    panel_connector_last_heartbeat = time.time()
    save_player_profiles(player_profiles)

    socketio.emit('players_update', _players_payload())
    socketio.emit('stats_update', build_runtime_status_payload(include_history=False))
    if _players_snapshot_signature(connected_players.values()) != prev_sig:
        discord_runtime.request_status_refresh(force=False)
    return jsonify({'ok': True})


@app.route('/api/panel-hook/resource-state', methods=['POST'])
def api_panel_hook_resource_state():
    
    global resource_states, panel_connector_last_heartbeat
    if not _check_panel_secret():
        return jsonify({'error': 'Invalid secret'}), 403
    data = request.json or {}
    res_name = data.get('resource', '')
    state = data.get('state', 'unknown')
    if res_name and state in ('started', 'stopped', 'unknown'):
        resource_states[res_name] = state
        panel_connector_last_heartbeat = time.time()
    return jsonify({'ok': True})


@app.route('/api/panel-hook/heartbeat', methods=['POST'])
def api_panel_hook_heartbeat():
    
    global panel_connector_last_heartbeat
    if not _check_panel_secret():
        return jsonify({'error': 'Invalid secret'}), 403
    data = request.json or {}
    now_ts = time.time()
    was_stale = (not panel_connector_last_heartbeat) or ((now_ts - panel_connector_last_heartbeat) > 90)
    panel_connector_last_heartbeat = now_ts
    socketio.emit('panel_heartbeat', data)
    socketio.emit('stats_update', build_runtime_status_payload(include_history=False))
    if was_stale:
        discord_runtime.request_status_refresh(force=True)
    return jsonify({'ok': True, 'monitor': _build_txadmin_monitor_payload()})


@app.route('/api/panel-hook/pending-actions', methods=['GET'])
def api_panel_hook_pending_actions():
    
    global pending_actions
    secret = request.args.get('secret', '')
    expected = panel_config.get('panel_secret', 'changeme')
    if not secrets.compare_digest(secret, expected):
        return jsonify([]), 403
    actions = list(pending_actions)
    pending_actions = []
    return jsonify(actions)


                                                         

@app.route('/api/players')
@login_required
@require_permission('can_view_players')
def api_players_list():
    
    return jsonify(_players_payload())


@app.route('/api/players/profile/<server_id>')
@login_required
@require_permission('can_view_players')
def api_players_profile(server_id):
    
    player_row, profile_key, profile, online = _resolve_player_profile_ref(server_id)
    if not profile:
        return jsonify({'success': False, 'message': 'Player not found'}), 404

    now_ts = int(time.time())
    join_ts = _safe_int((player_row or {}).get('joinTime'), now_ts)
    current_session = max(0, now_ts - join_ts) if online else 0
    playtime_seconds = max(0, _safe_int(profile.get('total_playtime_sec'), 0) + current_session)

    match_row = player_row if online else _profile_stub_player_row(profile)
    bans = _profile_matching_bans(profile, match_row)
    warnings = list(profile.get('warnings') or [])
    history = list(profile.get('history') or [])

    warnings.sort(key=lambda w: str(w.get('warned_at') or ''), reverse=True)
    history.sort(key=lambda h: str(h.get('at') or ''), reverse=True)

    display_name = _safe_profile_name((player_row or {}).get('name') or profile.get('last_name'))
    payload = {
        'success': True,
        'player': {
            'playerId': _normalize_player_id(profile.get('player_id')),
            'serverId': (player_row or {}).get('serverId') if online else profile.get('last_server_id'),
            'name': display_name,
            'ping': _safe_int((player_row or {}).get('ping'), 0) if online else 0,
            'session': _safe_int((player_row or {}).get('session'), 0) if online else 0,
            'sessionActive': bool((player_row or {}).get('sessionActive')) if online else False,
            'online': bool(online)
        },
        'profile': {
            'player_id': _normalize_player_id(profile.get('player_id')),
            'join_date': profile.get('first_seen_at') or _now_iso(),
            'last_connection': profile.get('last_connection_at') or profile.get('first_seen_at') or _now_iso(),
            'connections': max(0, _safe_int(profile.get('connections'), 0)),
            'playtime_seconds': playtime_seconds,
            'notes': _safe_profile_text(profile.get('notes', ''), 2000),
            'last_ip': normalize_player_ip(profile.get('last_ip', '')),
            'last_social_club': _safe_profile_text(profile.get('last_social_club', ''), 128),
            'last_rgsc_id': _safe_profile_text(profile.get('last_rgsc_id', ''), 128),
            'last_serial': _safe_profile_text(profile.get('last_serial', ''), 128),
            'last_game_type': _safe_profile_text(profile.get('last_game_type', ''), 64),
            'last_packet_loss': max(0, _safe_int(profile.get('last_packet_loss'), 0)),
            'id_whitelisted': bool(profile.get('id_whitelisted', False)),
            'identifiers': profile.get('identifiers') or {},
            'sanctions': {
                'warns': len(warnings),
                'bans': len(bans)
            },
            'warnings': warnings[:40],
            'bans': bans[:40],
            'history': history[:60]
        }
    }
    return jsonify(payload)


@app.route('/api/players/profile/<server_id>/notes', methods=['POST'])
@login_required
@require_permission('can_control_server')
def api_players_profile_notes(server_id):
    
    player_row, _, profile, _ = _resolve_player_profile_ref(server_id)
    if not profile:
        return jsonify({'success': False, 'message': 'Player not found'}), 404

    data = request.json or {}
    notes = _safe_profile_text(data.get('notes', ''), 2000)
    profile['notes'] = notes
    _append_profile_history(profile, 'notes', f'Updated by {session["username"]}')
    save_player_profiles(player_profiles)
    display_name = _safe_profile_name((player_row or {}).get('name') or profile.get('last_name'))
    player_id = _normalize_player_id(profile.get('player_id'))
    log_user_action(session['username'], 'PLAYER_NOTES_UPDATE', f'{display_name} ({player_id or server_id})')
    return jsonify({'success': True, 'message': 'Notes saved'})


@app.route('/api/players/warn', methods=['POST'])
@login_required
@require_permission('can_control_server')
def api_players_warn():
    
    global pending_actions
    data = request.json or {}
    server_id = data.get('serverId')
    reason = _safe_profile_text(data.get('reason', ''), 280) or 'Warning from admin'
    duration = _safe_int(data.get('duration', 8), 8)
    duration = max(3, min(20, duration))

    if server_id is None:
        return jsonify({'success': False, 'message': 'serverId required'})

    player_row = connected_players.get(str(server_id))
    if not player_row:
        return jsonify({'success': False, 'message': 'Player not found'}), 404

    _, profile = _resolve_player_profile(player_row, touch_join=False)
    entry = _append_profile_warning(profile, reason, session['username'])
    _append_profile_history(profile, 'warn', reason)
    save_player_profiles(player_profiles)

    pending_actions.append({
        'type': 'warn',
        'serverId': server_id,
        'reason': reason,
        'duration': duration
    })

    pname = player_row.get('name', 'Unknown')
    log_user_action(session['username'], 'WARN_PLAYER', f'{pname} (ID: {server_id}) - {reason}')
    add_console_line(f'[Panel] Warn queued: {pname} (ID: {server_id}) - {reason}', session.get('username'))

    warns_count = len(profile.get('warnings') or [])
    return jsonify({
        'success': True,
        'message': f'Warn queued for {pname}',
        'warn': entry,
        'warns_count': warns_count
    })


@app.route('/api/players/kick', methods=['POST'])
@login_required
@require_permission('can_control_server')
def api_players_kick():
    
    global pending_actions
    data = request.json or {}
    server_id = data.get('serverId')
    reason = data.get('reason', 'Kicked by admin')

    if server_id is None:
        return jsonify({'success': False, 'message': 'serverId required'})

    pending_actions.append({
        'type': 'kick',
        'serverId': server_id,
        'reason': reason
    })

    player_row = connected_players.get(str(server_id), {})
    player_name = player_row.get('name', 'Unknown')
    if player_row:
        _, profile = _resolve_player_profile(player_row, touch_join=False)
        _append_profile_history(profile, 'kick', reason)
        save_player_profiles(player_profiles)
    add_console_line(f'[Panel] Kick queued: {player_name} (ID: {server_id}) - {reason}', session.get('username'))
    log_user_action(session['username'], 'KICK_PLAYER', f'{player_name} (ID: {server_id}) - {reason}')

    return jsonify({'success': True, 'message': f'Kick queued for {player_name}'})


@app.route('/api/players/ban', methods=['POST'])
@login_required
@admin_required
def api_players_ban():
    
    global pending_actions
    data = request.json or {}
    server_id = data.get('serverId')
    player_ref = _safe_profile_text(data.get('playerId'), 64)
    ip = normalize_player_ip(_safe_profile_text(data.get('ip', ''), 128))
    name = _safe_profile_text(data.get('name', ''), 64)
    reason = _safe_profile_text(data.get('reason', ''), 280) or 'Banned by admin'

    matched_row = None
    matched_profile = None
    if player_ref:
        matched_row, _, matched_profile, _ = _resolve_player_profile_ref(player_ref)
    if server_id is not None and not matched_row:
        matched_row = connected_players.get(str(server_id))
        if matched_row:
            _, matched_profile = _resolve_player_profile(matched_row, touch_join=False)

    if matched_profile:
        if not name:
            name = _safe_profile_text(matched_profile.get('last_name', ''), 64)
        if not ip:
            ip = normalize_player_ip(matched_profile.get('last_ip', ''))
    if matched_row:
        if not name:
            name = _safe_profile_text(matched_row.get('name', ''), 64)
        if not ip:
            ip = normalize_player_ip(matched_row.get('ip', ''))
        if server_id is None:
            server_id = matched_row.get('serverId')

    clean_name = _safe_profile_name(name) if name else ''
    valid_name = bool(clean_name and clean_name.lower() != 'unknown')

    if not ip and not valid_name:
        return jsonify({'success': False, 'message': 'IP or name required for ban'})

    bans = load_bans()

    ban_entry = {
        'ip': ip,
        'name': clean_name if valid_name else '',
        'reason': reason,
        'banned_by': session['username'],
        'banned_at': datetime.now().isoformat()
    }
    bans.append(ban_entry)
    save_bans(bans)

    profile_source = matched_row or (connected_players.get(str(server_id), {}) if server_id is not None else {})
    if not profile_source:
        profile_source = {'serverId': server_id, 'name': clean_name if valid_name else '', 'ip': ip, 'playerId': player_ref}
    _, profile = _resolve_player_profile(profile_source, touch_join=False)
    _append_profile_history(profile, 'ban', reason)
    save_player_profiles(player_profiles)

                                            
    connected_sid = str(server_id) if server_id is not None else ''
    if connected_sid and connected_sid in connected_players:
        pending_actions.append({
            'type': 'ban',
            'serverId': connected_players[connected_sid].get('serverId', server_id),
            'reason': f'Banned: {reason}'
        })

    safe_player_id = _normalize_player_id((profile or {}).get('player_id'))
    log_user_action(session['username'], 'BAN_PLAYER', f'PlayerID: {safe_player_id}, Name: {clean_name if valid_name else ""}, Reason: {reason}')

    return jsonify({'success': True, 'message': f'Player banned ({(clean_name if valid_name else "") or ip})'})


@app.route('/api/players/bans', methods=['GET'])
@login_required
@admin_required
def api_players_bans_list():
    
    return jsonify({'bans': load_bans()})


@app.route('/api/players/bans', methods=['DELETE'])
@login_required
@admin_required
def api_players_bans_remove():
    
    data = request.json or {}
    index = data.get('index')

    if index is None:
        return jsonify({'success': False, 'message': 'Ban index required'})

    bans = load_bans()
    if 0 <= index < len(bans):
        removed = bans.pop(index)
        save_bans(bans)
        log_user_action(session['username'], 'UNBAN_PLAYER',
                        f'IP: {removed.get("ip", "")}, Name: {removed.get("name", "")}')
        return jsonify({'success': True, 'message': 'Ban removed'})

    return jsonify({'success': False, 'message': 'Invalid ban index'})


@app.route('/api/players/message', methods=['POST'])
@login_required
@require_permission('can_send_commands')
def api_players_message():
    
    data = request.json or {}
    server_id = data.get('serverId')
    message = data.get('message', '').strip()
    broadcast = data.get('broadcast', False)
    duration = data.get('duration', 5)
    title = _safe_profile_text(data.get('title', ''), 64)
    variant = _safe_profile_text(data.get('variant', ''), 24).lower() or ('announce' if broadcast else 'message')

    try:
        duration = int(duration)
    except Exception:
        duration = 5
    duration = max(2, min(20, duration))

    if not message:
        return jsonify({'success': False, 'message': 'Message required'})

    if broadcast:
        _queue_pending_action({
            'type': 'broadcast',
            'title': title or 'Server Announcement',
            'message': message,
            'duration': duration,
            'variant': variant
        })
        log_user_action(session['username'], 'BROADCAST_MESSAGE', message)
        return jsonify({'success': True, 'message': 'Broadcast queued'})
    elif server_id is not None:
        _queue_pending_action({
            'type': 'message',
            'serverId': server_id,
            'title': title or 'Admin Message',
            'message': message,
            'duration': duration,
            'variant': variant
        })
        log_user_action(session['username'], 'PLAYER_MESSAGE', f'{server_id}: {message}')
        return jsonify({'success': True, 'message': 'Message queued'})
    else:
        return jsonify({'success': False, 'message': 'serverId or broadcast required'})


                                                                

@app.route('/api/status')
@login_required
@require_permission('can_view_dashboard')
def api_status():
    
    sync_server_state_with_system()
    return jsonify(build_runtime_status_payload(include_history=True))

@app.route('/api/start', methods=['POST'])
@login_required
@require_permission('can_control_server')
def api_start():
    
    return jsonify(start_server(session['username']))

@app.route('/api/stop', methods=['POST'])
@login_required
@require_permission('can_control_server')
def api_stop():
    
    return jsonify(stop_server(session['username']))

@app.route('/api/restart', methods=['POST'])
@login_required
@require_permission('can_control_server')
def api_restart():
    
    return jsonify(restart_server(session['username']))

@app.route('/api/console')
@login_required
@require_permission('can_view_console')
def api_console():
    
    return jsonify({'lines': console_lines})

@app.route('/api/command', methods=['POST'])
@login_required
@require_permission('can_send_commands')
def api_command():
    
    global server_process

    if not server_state['running']:
        return jsonify({'success': False, 'message': 'Server is not running'})

    if server_state['attached']:
        return jsonify({'success': False, 'message': 'Cannot send commands to attached process'})

    command = request.json.get('command', '')

    if len(command) > 1000:
        return jsonify({'success': False, 'message': 'Command too long (max 1000 chars)'})

    try:
        if server_process and server_process.stdin:
            server_process.stdin.write(f"{command}\n".encode())
            server_process.stdin.flush()
            add_console_line(f'> {command}', session['username'])
            log_user_action(session['username'], 'COMMAND', command)
            return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/config', methods=['GET', 'POST'])
@admin_required
def api_config():
    
    global config

    if request.method == 'GET':
        return jsonify(config)
    else:
        config.update(request.json)
        save_config(config)
        log_user_action(session['username'], 'UPDATE_CONFIG', 'Success')
        return jsonify({'success': True, 'message': 'Configuration updated'})

                                                         

@app.route('/api/update-status')
def api_update_status():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify(get_update_payload())


@app.route('/api/update-check', methods=['POST'])
def api_update_check():
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    data = request.json or {}
    force = bool(data.get('force', True))
    try:
        check_for_updates(force=force)
    except Exception as e:
        return jsonify({
            'success': False,
            'message': str(e),
            'state': get_update_payload()
        }), 500
    return jsonify({'success': True, 'state': get_update_payload()})


@app.route('/api/update-start', methods=['POST'])
def api_update_start():
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    user = get_user(session['username'])
    if not user or user.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Admin access required'}), 403

    payload = get_update_payload()
    run_state = payload.get('run', {})
    if run_state.get('running'):
        return jsonify({'success': False, 'message': 'Update already running'}), 409

    available = []
    if payload.get('panel', {}).get('available'):
        available.append('panel')
    if payload.get('ragemp', {}).get('available'):
        available.append('ragemp')

    data = request.json or {}
    req_targets = data.get('targets')
    if isinstance(req_targets, str):
        req_targets = [req_targets]
    if isinstance(req_targets, list) and req_targets:
        targets = [t for t in req_targets if t in available]
    else:
        targets = list(available)

    if not targets:
        return jsonify({'success': False, 'message': 'No updates available'}), 400

    stop_server_requested = bool(data.get('stop_server'))
    if server_state['running']:
        if not stop_server_requested:
            return jsonify({'success': False, 'message': 'Server must be stopped before update'}), 400
        stop_result = stop_server(session['username'])
        if not stop_result.get('success'):
            return jsonify({'success': False, 'message': stop_result.get('message', 'Failed to stop server')}), 500

    job = {
        'targets': targets,
        'panel': {
            'version': payload.get('panel', {}).get('latest'),
            'zip_url': payload.get('panel', {}).get('zip_url')
        },
        'ragemp': {
            'version': payload.get('ragemp', {}).get('latest'),
            'archive_url': payload.get('ragemp', {}).get('archive_url'),
            'etag': payload.get('ragemp', {}).get('etag'),
            'last_modified': payload.get('ragemp', {}).get('last_modified')
        },
        'panel_port': PANEL_PORT,
        'server_manager_pid': os.getpid(),
        'restart_mode': _detect_restart_mode()
    }
    _safe_json_save(UPDATE_JOB_FILE, job)
    _safe_json_save(UPDATE_STATUS_FILE, {
        'running': True,
        'finished': False,
        'success': False,
        'progress': 0,
        'step': 'Queued',
        'message': 'Update queued',
        'targets': targets,
        'error': '',
        'log': [],
        'updated_at': time.time()
    })

    try:
        subprocess.Popen(
            [sys.executable, 'updater.py', '--job', str(UPDATE_JOB_FILE)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception as e:
        return jsonify({'success': False, 'message': f'Failed to start updater: {e}'}), 500

    add_console_line(f'=== UPDATE STARTED ({", ".join(targets)}) ===', session['username'])
    return jsonify({'success': True, 'message': 'Update started'})

                                                     

@socketio.on('connect')
def handle_connect():
    
    if 'username' not in session:
        disconnect()
        return

    sync_server_state_with_system()
    emit('console_history', {'lines': console_lines})
    emit('server_status', {'running': server_state['running']})
    emit('stats_update', build_runtime_status_payload(include_history=False))
    emit('update_status', get_update_payload())

                                                   

if __name__ == '__main__':
    initial_port = _resolve_initial_panel_port()
    parser = argparse.ArgumentParser(description='RageAdmin panel server')
    parser.add_argument(
        '--port',
        type=_panel_port_arg,
        default=initial_port,
        help=f'Panel HTTP port (default: {DEFAULT_PANEL_PORT}, or saved/env value if present)'
    )
    args = parser.parse_args()

    PANEL_PORT = args.port
    _persist_panel_port(PANEL_PORT)

    storage.ensure_storage_files()
    add_console_line('=== RAGEADMIN STARTED ===')
    pin = ensure_setup_pin()
    if pin:
        _announce_setup_pin(pin)
    sync_server_state_with_system()
    _append_runtime_sample(force=True, emit_socket=False)

    stats_thread = threading.Thread(target=update_stats, daemon=True)
    stats_thread.start()

    sched_thread = threading.Thread(target=scheduled_restart_thread, daemon=True)
    sched_thread.start()

    update_thread = threading.Thread(target=_update_check_loop, daemon=True)
    update_thread.start()

    discord_runtime.sync_from_config(force=True)

    if panel_config.get('auto_start', False):
        add_console_line('=== AUTO-START ENABLED ===')

        def _auto_start():
            time.sleep(2)
            start_server('SYSTEM')

        threading.Thread(target=_auto_start, daemon=True).start()

    setup_hint = (
        f'  Setup PIN: {pin}\n'
        if pin else
        '  Setup: completed\n'
    )

    print(
        f"""
=======================================================
  RageAdmin

  Access: http://0.0.0.0:{PANEL_PORT}
  Mode: {'production' if PANEL_PRODUCTION_MODE else 'development'}
  Backend: {SOCKETIO_ASYNC_MODE} ({SOCKETIO_ASYNC_REASON})
  Access logs: {'enabled' if PANEL_ACCESS_LOGS else 'disabled'}

{setup_hint}=======================================================
"""
    )

    use_unsafe_werkzeug = (SOCKETIO_ASYNC_MODE == 'threading')
    if PANEL_PRODUCTION_MODE and use_unsafe_werkzeug:
        if SOCKETIO_ASYNC_REASON == 'eventlet_disabled_windows':
            print('[INFO] Windows detected: using threading backend for Socket.IO stability.')
        elif SOCKETIO_ASYNC_REASON == 'default_threading' and os.name == 'nt':
            print('[INFO] Windows detected: using threading backend for Socket.IO stability.')
        elif SOCKETIO_ASYNC_REASON == 'eventlet_missing':
            print('[WARN] eventlet not available; falling back to threading/Werkzeug backend.')
        elif SOCKETIO_ASYNC_REASON == 'forced_threading':
            print('[INFO] PANEL_SOCKETIO_ASYNC_MODE=threading is active.')
        else:
            print(f'[INFO] Socket.IO backend fallback: {SOCKETIO_ASYNC_REASON}.')

    socketio.run(
        app,
        host='0.0.0.0',
        port=PANEL_PORT,
        debug=not PANEL_PRODUCTION_MODE,
        use_reloader=False,
        log_output=PANEL_ACCESS_LOGS,
        allow_unsafe_werkzeug=use_unsafe_werkzeug
    )
