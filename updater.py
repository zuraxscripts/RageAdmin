import argparse
import json
import os
import shutil
import subprocess
import stat
import sys
import tempfile
import time
import tarfile
import urllib.request
import zipfile
import signal
import re
from pathlib import Path
from email.utils import parsedate_to_datetime


ROOT_DIR = Path(__file__).resolve().parent
USER_AGENT = 'RageAdmin-Updater/1.0'
DATA_DIR = ROOT_DIR / 'data'
STATUS_FILE = DATA_DIR / 'update_status.json'
LOG_FILE = DATA_DIR / 'update.log'
DEFAULT_PANEL_PORT = 20000
PANEL_PORT = DEFAULT_PANEL_PORT
RAGEMP_SERVER_ARCHIVE_URL = 'https://cdn.rage.mp/updater/prerelease/server-files/linux_x64.tar.gz'
RAGEMP_SERVER_DIR_NAME = 'ragemp-srv'
RAGEMP_SERVER_EXECUTABLE = 'ragemp-server'
RAGEMP_CONTENT_DIRS = ('packages', 'client_packages', 'maps', 'plugins')


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


DATA_DIR.mkdir(exist_ok=True)


STATUS_TEMPLATE = {
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


_status = dict(STATUS_TEMPLATE)


def _coerce_panel_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PANEL_PORT
    if port < 1 or port > 65535:
        return DEFAULT_PANEL_PORT
    return port


def _set_panel_port(value):
    global PANEL_PORT
    PANEL_PORT = _coerce_panel_port(value)


def _get_panel_host():
    return f'http://127.0.0.1:{PANEL_PORT}'


def _extract_port_from_cmdline(cmdline):
    if not cmdline:
        return None
    for idx, part in enumerate(cmdline):
        if part == '--port' and idx + 1 < len(cmdline):
            return _coerce_panel_port(cmdline[idx + 1])
        if part.startswith('--port='):
            return _coerce_panel_port(part.split('=', 1)[1])
    return None


def _resolve_panel_port(job: dict):
    explicit = job.get('panel_port')
    if explicit is not None:
        return _coerce_panel_port(explicit)

    pid = job.get('server_manager_pid')
    if pid:
        try:
            import psutil
            cmdline = psutil.Process(int(pid)).cmdline()
            parsed = _extract_port_from_cmdline(cmdline)
            if parsed is not None:
                return parsed
        except Exception:
            pass

    env_port = os.getenv('PANEL_PORT') or os.getenv('PORT')
    if env_port:
        return _coerce_panel_port(env_port)

    panel_cfg = _load_panel_config()
    if isinstance(panel_cfg, dict) and panel_cfg.get('panel_port') is not None:
        return _coerce_panel_port(panel_cfg.get('panel_port'))

    return DEFAULT_PANEL_PORT


def _json_load(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8-sig'))
    except Exception:
        pass
    return default


def _json_save(path: Path, payload: dict):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, indent=4), encoding='utf-8')
    os.replace(tmp, path)


def _log(message: str):
    timestamp = time.strftime('%H:%M:%S')
    line = f'[{timestamp}] {message}'
    _status['message'] = message
    _status['log'].append(line)
    if len(_status['log']) > 200:
        _status['log'] = _status['log'][-200:]
    _status['updated_at'] = time.time()
    _json_save(STATUS_FILE, _status)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _set_status(**kwargs):
    _status.update(kwargs)
    _status['updated_at'] = time.time()
    _json_save(STATUS_FILE, _status)


def _download_with_progress(url: str, dest_path: Path, start_pct: int, end_pct: int, label: str):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req) as resp, open(dest_path, 'wb') as f:
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
                _set_status(progress=int(pct))
                _log(f'{label}: {downloaded / (1024 * 1024):.1f} MB')
                last_tick = time.time()
        _set_status(progress=end_pct)
        _log(f'{label}: download complete')


def _find_file(root: Path, filename: str):
    for p in root.rglob(filename):
        return p
    return None


def _should_skip(rel_path: Path, skip_top: set, skip_files: set, skip_any: set):
    parts = rel_path.parts
    if not parts:
        return False
    if any(p in skip_any for p in parts):
        return True
    if parts[0] in skip_top:
        return True
    if len(parts) == 1 and parts[0] in skip_files:
        return True
    return False


def _copy_tree(src_root: Path, dst_root: Path, skip_top: set, skip_files: set, skip_any: set):
    for item in src_root.rglob('*'):
        rel = item.relative_to(src_root)
        if _should_skip(rel, skip_top, skip_files, skip_any):
            continue
        dest = dst_root / rel
        if item.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dest)


def _merge_resources(src_res: Path, dst_res: Path, replace_names: set):
    dst_res.mkdir(parents=True, exist_ok=True)
    for item in src_res.iterdir():
        target = dst_res / item.name
        if item.name in replace_names:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
        else:
            if target.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)


def _merge_server_tree(src_root: Path, dst_root: Path):
    for item in src_root.iterdir():
        dest = dst_root / item.name
        if item.name in {'conf.json', *RAGEMP_CONTENT_DIRS} and dest.exists():
            continue
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)


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
    conf_path.write_text(json.dumps(dict(RAGEMP_DEFAULT_SETTINGS), indent=4), encoding='utf-8')


def _terminate_process_quiet(proc):
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
    if conf_path.exists():
        return

    server_bin = server_dir / RAGEMP_SERVER_EXECUTABLE
    proc = None
    try:
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
    except Exception:
        pass
    finally:
        _terminate_process_quiet(proc)

    _ensure_ragemp_content_dirs(server_dir)
    if not conf_path.exists():
        _write_default_ragemp_conf(conf_path)


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


def _load_panel_config():
    try:
        sys.path.insert(0, str(ROOT_DIR))
        import storage                                
        return storage.load_panel_config()
    except Exception:
        return {}


def _load_server_config():
    try:
        sys.path.insert(0, str(ROOT_DIR))
        import storage                                
        return storage.load_config()
    except Exception:
        return {}


def _save_panel_config(cfg: dict):
    try:
        sys.path.insert(0, str(ROOT_DIR))
        import storage                                
        storage.save_panel_config(cfg)
    except Exception:
        pass


def _save_server_config(cfg: dict):
    try:
        sys.path.insert(0, str(ROOT_DIR))
        import storage                                
        storage.save_config(cfg)
    except Exception:
        pass
def _update_panel_version(version: str):
    if not version:
        return
    cfg = _load_panel_config()
    cfg['panel_version'] = str(version)
    _save_panel_config(cfg)


def _update_ragemp_info(version: str, archive_url: str, etag: str = '', last_modified: str = ''):
    cfg = _load_server_config()
    version = str(version or '').strip()
    formatted = _format_ragemp_build_label(last_modified, etag, archive_url)
    if not version or (_looks_like_header_date_version(version) and formatted):
        version = formatted
    cfg['ragemp_version'] = str(version or '')
    if archive_url:
        cfg['ragemp_archive_url'] = str(archive_url)
    if etag:
        cfg['ragemp_etag'] = str(etag)
    if last_modified:
        cfg['ragemp_last_modified'] = str(last_modified)
    _save_server_config(cfg)


def perform_panel_update(panel_zip_url: str, panel_version: str, start_pct: int, end_pct: int):
    _log('Starting panel update')
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        zip_path = tmpdir / 'panel.zip'
        span = max(1, end_pct - start_pct)
        dl_end = start_pct + int(span * 0.6)
        extract_end = start_pct + int(span * 0.8)
        _set_status(step='Panel', progress=start_pct)
        _download_with_progress(panel_zip_url, zip_path, start_pct, dl_end, 'Panel files')

        extract_dir = tmpdir / 'panel_extract'
        extract_dir.mkdir(parents=True, exist_ok=True)
        _log('Extracting panel files...')
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)
        _set_status(progress=extract_end)

        top_dirs = [p for p in extract_dir.iterdir() if p.is_dir()]
        source_dir = top_dirs[0] if top_dirs else extract_dir

        skip_top = {'data', 'RageMP-Server'}
        skip_files = {'panel_config.json', 'server_config.json', 'panel_version.json', 'update_config.json'}
        skip_any = {'.git', '__pycache__', '.venv', 'venv', 'node_modules'}

        _log('Copying panel files...')
        _copy_tree(source_dir, ROOT_DIR, skip_top, skip_files, skip_any)

    _update_panel_version(panel_version)
    _set_status(progress=end_pct)
    _log('Panel update finished')


def perform_ragemp_update(archive_url: str, version: str, etag: str, last_modified: str, start_pct: int, end_pct: int):
    _log('Starting RageMP update')
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        archive_path = tmpdir / 'ragemp.tar.gz'
        span = max(1, end_pct - start_pct)
        dl_end = start_pct + int(span * 0.6)
        extract_end = start_pct + int(span * 0.75)
        _set_status(step='RageMP', progress=start_pct)
        _download_with_progress(archive_url, archive_path, start_pct, dl_end, 'RageMP files')

        extract_dir = tmpdir / 'server_extract'
        extract_dir.mkdir(parents=True, exist_ok=True)
        _extract_server_archive(archive_path, extract_dir)
        _set_status(progress=extract_end)

        server_root = _locate_ragemp_server_root(extract_dir)
        if not server_root:
            raise RuntimeError('ragemp-server not found in extracted RageMP archive')

        server_cfg = _load_server_config()
        server_path = Path(str(server_cfg.get('server_path', './RageMP-Server/ragemp-srv/ragemp-server')) or './RageMP-Server/ragemp-srv/ragemp-server')
        if not server_path.is_absolute():
            server_path = ROOT_DIR / server_path
        server_path = server_path.resolve()
        server_dir = server_path if server_path.is_dir() else server_path.parent
        if not server_dir.exists():
            raise RuntimeError(f'Server directory not found: {server_dir}')

        _log('Updating server files...')
        _merge_server_tree(server_root, server_dir)

        dst = server_dir / RAGEMP_SERVER_EXECUTABLE
        if dst.exists():
            try:
                os.chmod(dst, 0o755)
            except Exception:
                pass

        _ensure_ragemp_runtime_files(server_dir)

    _update_ragemp_info(version, archive_url, etag, last_modified)
    _set_status(progress=end_pct)
    _log('RageMP update finished')


def _terminate_process(pid: int):
    if not pid:
        return
    try:
        import psutil                                
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        return
    except Exception:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def _restart_panel(job: dict):
    restart_mode = job.get('restart_mode') or 'standalone'
    server_pid = job.get('server_manager_pid')
    panel_port = _resolve_panel_port(job)
    if restart_mode == 'main':
                                                              
        restart_flag = DATA_DIR / 'restart.flag'
        restart_flag.write_text('restart', encoding='utf-8')
        _log('Restart flag created, stopping server manager...')
        _terminate_process(server_pid)
        return

    _log('Starting panel process...')
    cwd = str(ROOT_DIR)
    if (ROOT_DIR / 'main.py').exists():
        cmd = [sys.executable, 'main.py', '--port', str(panel_port)]
    else:
        cmd = [sys.executable, 'server_manager.py', '--port', str(panel_port)]
    try:
        subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    _terminate_process(server_pid)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    args = parser.parse_args()

    job_path = Path(args.job).resolve()
    job = _json_load(job_path, {})
    _set_panel_port(_resolve_panel_port(job))
    targets = job.get('targets') or []

    _status.update(STATUS_TEMPLATE)
    _status['running'] = True
    _status['finished'] = False
    _status['success'] = False
    _status['progress'] = 0
    _status['step'] = 'Starting'
    _status['targets'] = targets
    _json_save(STATUS_FILE, _status)
    _log('Updater started')

    try:
        ranges = {}
        if 'panel' in targets and 'ragemp' in targets:
            ranges['panel'] = (5, 55)
            ranges['ragemp'] = (55, 95)
        elif 'panel' in targets:
            ranges['panel'] = (5, 95)
        elif 'ragemp' in targets:
            ranges['ragemp'] = (5, 95)

        if 'panel' in targets:
            panel = job.get('panel') or {}
            panel_zip = panel.get('zip_url')
            panel_ver = panel.get('version')
            if not panel_zip:
                raise RuntimeError('Panel update requested but zip_url missing')
            start_pct, end_pct = ranges.get('panel', (5, 95))
            perform_panel_update(panel_zip, panel_ver, start_pct, end_pct)

        if 'ragemp' in targets:
            ragemp = job.get('ragemp') or {}
            archive_url = ragemp.get('archive_url') or RAGEMP_SERVER_ARCHIVE_URL
            version = ragemp.get('version') or ''
            etag = ragemp.get('etag') or ''
            last_modified = ragemp.get('last_modified') or ''
            start_pct, end_pct = ranges.get('ragemp', (5, 95))
            perform_ragemp_update(archive_url, version, etag, last_modified, start_pct, end_pct)

        _set_status(progress=100, step='Done', finished=True, success=True, running=False)
        _log('Update completed successfully')
    except Exception as e:
        _set_status(finished=True, success=False, running=False, error=str(e))
        _log(f'Update failed: {e}')
        return

                   
    _restart_panel(job)


if __name__ == '__main__':
    main()
