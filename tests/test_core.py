from copy import deepcopy
from datetime import date, datetime
import json
import logging
import pytest
import pytz
import app
from config import ConfigStore, DEFAULTS


@pytest.fixture
def config(tmp_path, monkeypatch):
    from config import ENV
    for name in [*ENV, 'TZ']:
        monkeypatch.delenv(name, raising=False)
    store = ConfigStore(tmp_path)
    monkeypatch.setattr(app, 'STATE_PATH', str(tmp_path / 'grabberstate.json'))
    monkeypatch.setattr(app, 'store', store)
    app.stop_event.clear()
    cfg = deepcopy(DEFAULTS)
    cfg['paths'] = {'download_folder': str(tmp_path / 'downloads'), 'media_folder': str(tmp_path / 'media')}
    return cfg


def test_first_run_and_environment(tmp_path, monkeypatch, config):
    monkeypatch.setenv('PROWLARR_API_KEY', 'private-token')
    store = ConfigStore(tmp_path / 'fresh')
    cfg = store.load()
    assert cfg['prowlarr']['api_key'] == 'private-token'
    cfg['backfill']['days'] = 10
    store.save(cfg)
    assert store.load()['backfill']['days'] == 10
    assert 'private-token' not in store.path.read_text()


def test_invalid_config(tmp_path, config):
    store = ConfigStore(tmp_path / 'invalid')
    cfg = store.load()
    cfg['schedule']['search_interval_hours'] = 0
    with pytest.raises(ValueError):
        store.save(cfg)
    assert store.load()['schedule']['search_interval_hours'] == 2


def test_corrupt_history_fails_closed(config):
    from pathlib import Path
    Path(app.STATE_PATH).write_text('not json')
    with pytest.raises(json.JSONDecodeError):
        app.load_state()


def test_resume_without_resubmit(config, monkeypatch):
    app.save_state({'downloads': {'2026-11-08': {'status': 'downloading', 'nzo_id': 'SAB1'}}})
    monkeypatch.setattr(app, 'sab_job_status', lambda *args: 'queue')
    monkeypatch.setattr(app, 'prowlarr_search', lambda *args: pytest.fail('Must not search again'))
    assert not app.process_date(config, date(2026, 11, 8))
    assert app.load_state()['downloads']['2026-11-08']['nzo_id'] == 'SAB1'


def test_ambiguous_submission_not_retried(config, monkeypatch):
    monkeypatch.setattr(app, 'prowlarr_search', lambda *args: [{'title': 'The New York Times - 08 November 2026', 'downloadUrl': 'http://example.test/a', 'size': 1}])
    calls = []
    def send(*args):
        assert app.load_state()['downloads']['2026-11-08']['status'] == 'submitting'
        calls.append(True)
        return None
    monkeypatch.setattr(app, 'send_to_sabnzbd', send)
    app.process_date(config, date(2026, 11, 8))
    app.process_date(config, date(2026, 11, 8))
    assert calls == [True]
    assert app.load_state()['downloads']['2026-11-08']['status'] == 'review'


def test_import_after_completion(config, monkeypatch):
    from pathlib import Path
    source = Path(config['paths']['download_folder'])
    source.mkdir()
    pdf = source / 'The New York Times - 08 November 2026.pdf'
    pdf.write_bytes(b'%PDF-1.7 test')
    app.save_state({'downloads': {'2026-11-08': {'status': 'downloading', 'nzo_id': 'SAB1'}}})
    monkeypatch.setattr(app, 'sab_job_status', lambda *args: 'history_ok')
    assert app.process_date(config, date(2026, 11, 8))
    assert not pdf.exists()
    assert Path(app.load_state()['downloads']['2026-11-08']['file']).read_bytes() == b'%PDF-1.7 test'


def test_monitor_outside_backfill(config, monkeypatch):
    config['prowlarr'].update(url='http://p', api_key='p')
    config['sabnzbd'].update(url='http://s', api_key='s')
    monkeypatch.setattr(app, 'load_config', lambda: config)
    app.save_state({'downloads': {'2020-01-01': {'status': 'downloading', 'nzo_id': 'SAB1'}}})
    seen = []
    monkeypatch.setattr(app, 'sab_job_status', lambda cfg, job: seen.append(job) or 'unknown')
    app.monitor_downloads()
    assert seen == ['SAB1']
    assert app.load_state()['downloads']['2020-01-01']['status'] == 'downloading'


def test_schedule_timezone_and_window(config):
    config['timezone'] = 'Europe/London'
    trigger = app.search_trigger(config)
    now = pytz.timezone('Europe/London').localize(datetime(2026, 7, 1, 9))
    first = trigger.get_next_fire_time(None, now)
    assert first.hour == 12 and first.utcoffset().total_seconds() == 3600
    late = now.replace(hour=23)
    assert trigger.get_next_fire_time(None, late).day == 2


def test_no_credential_in_error(config, monkeypatch, caplog):
    import requests
    def failure(*args, **kwargs):
        raise requests.RequestException('http://server/?apikey=DO-NOT-LOG')
    monkeypatch.setattr(app.requests, 'post', failure)
    with caplog.at_level(logging.ERROR):
        app.send_to_sabnzbd(config, 'http://example.test', 'edition')
    assert 'DO-NOT-LOG' not in caplog.text


def test_missing_indexer_does_not_fall_back(config, monkeypatch):
    config['prowlarr']['indexer_name'] = 'missing'
    class Response:
        def raise_for_status(self): pass
        def json(self): return []
    calls = []
    monkeypatch.setattr(app.requests, 'get', lambda *a, **kw: calls.append(a[0]) or Response())
    assert app.prowlarr_search(config, 'query') == []
    assert len(calls) == 1


def test_legacy_config_preserved(tmp_path, config, monkeypatch):
    import yaml
    path = tmp_path / 'legacy'
    path.mkdir()
    legacy = deepcopy(config)
    legacy['prowlarr']['api_key'] = 'old-secret'
    (path / 'config.yml').write_text(yaml.safe_dump(legacy))
    store = ConfigStore(path)
    assert store.load()['prowlarr']['api_key'] == 'old-secret'
    monkeypatch.setenv('PROWLARR_API_KEY', 'new-secret')
    store.save(store.load())
    assert 'old-secret' not in (path / 'config.yml').read_text()
    assert 'new-secret' not in (path / 'config.yml').read_text()
    assert store.load()['paths'] == config['paths']


def test_failed_job_is_not_resubmitted(config, monkeypatch):
    app.save_state({'downloads': {'2026-11-08': {'status': 'failed', 'nzo_id': 'SAB1'}}})
    monkeypatch.setattr(app, 'prowlarr_search', lambda *a: pytest.fail('Must not retry failed job'))
    assert not app.process_date(config, date(2026, 11, 8))
