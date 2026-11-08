"""Validated configuration and atomic persistence. Environment owns deployment settings."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import yaml

DEFAULTS = {
    'prowlarr': {'url': '', 'api_key': '', 'indexer_name': ''},
    'sabnzbd': {'url': '', 'api_key': '', 'category': 'nytarr'},
    'schedule': {'search_start_hour': 12, 'search_interval_hours': 2},
    'paths': {'download_folder': '/downloads', 'media_folder': '/media'},
    'backfill': {'days': 3}, 'timezone': 'UTC',
}
ENV = {
    'PROWLARR_URL': ('prowlarr', 'url'), 'PROWLARR_API_KEY': ('prowlarr', 'api_key'),
    'PROWLARR_INDEXER': ('prowlarr', 'indexer_name'),
    'SABNZBD_URL': ('sabnzbd', 'url'), 'SABNZBD_API_KEY': ('sabnzbd', 'api_key'),
    'SABNZBD_CATEGORY': ('sabnzbd', 'category'),
    'DOWNLOAD_FOLDER': ('paths', 'download_folder'), 'MEDIA_FOLDER': ('paths', 'media_folder'),
}


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate(cfg):
    for section, key, low, high in [('schedule', 'search_start_hour', 0, 23),
                                   ('schedule', 'search_interval_hours', 1, 24),
                                   ('backfill', 'days', 0, 30)]:
        value = cfg[section][key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{section}.{key} must be an integer between {low} and {high}.')
    ZoneInfo(cfg['timezone'])
    for service in ('prowlarr', 'sabnzbd'):
        url = cfg[service]['url']
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(f'{service} URL must be HTTP(S), without credentials, query or fragment.')
    for value in cfg['paths'].values():
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('Library paths must be absolute container paths.')
    if Path(cfg['paths']['download_folder']).resolve() == Path(cfg['paths']['media_folder']).resolve():
        raise ValueError('Download and archive folders must differ.')
    return cfg


class ConfigStore:
    def __init__(self, directory):
        self.path = Path(directory) / 'config.yml'
        self.lock = threading.RLock()
        if not self.path.exists():
            atomic_write(self.path, yaml.safe_dump(DEFAULTS, sort_keys=False))
        self.load()

    def load(self):
        with self.lock:
            raw = yaml.safe_load(self.path.read_text())
            if not isinstance(raw, dict):
                raise ValueError('config.yml must contain a mapping.')
            cfg = copy.deepcopy(DEFAULTS)
            for key in cfg:
                if key in raw:
                    if isinstance(cfg[key], dict):
                        cfg[key].update(raw[key])
                    else:
                        cfg[key] = raw[key]
            for name, (section, key) in ENV.items():
                if name in os.environ:
                    cfg[section][key] = os.environ[name]
            if 'TZ' in os.environ:
                cfg['timezone'] = os.environ['TZ']
            return validate(cfg)

    def save(self, cfg):
        """Only routine preferences can be changed through the web application."""
        with self.lock:
            current = self.load()
            current['schedule'] = copy.deepcopy(cfg['schedule'])
            current['backfill'] = copy.deepcopy(cfg['backfill'])
            validate(current)
            raw = yaml.safe_load(self.path.read_text())
            raw['schedule'] = current['schedule']
            raw['backfill'] = current['backfill']
            # Remove migrated values only when explicitly supplied by the environment.
            for name, (section, key) in ENV.items():
                if name in os.environ:
                    raw.get(section, {}).pop(key, None)
            if 'TZ' in os.environ:
                raw.pop('timezone', None)
            atomic_write(self.path, yaml.safe_dump(raw, sort_keys=False))


def ready(cfg):
    return all(cfg[s][k] for s in ('prowlarr', 'sabnzbd') for k in ('url', 'api_key'))
