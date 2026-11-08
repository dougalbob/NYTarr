from copy import deepcopy
from config import DEFAULTS
import pytest
from web import create_app


@pytest.fixture
def ui(tmp_path):
    cfg = deepcopy(DEFAULTS)
    cfg['prowlarr']['url'] = 'http://prowlarr:9696'
    cfg['sabnzbd']['url'] = 'http://sab:8080'
    cfg['prowlarr']['api_key'] = 'secret-prowlarr'
    cfg['sabnzbd']['api_key'] = 'secret-sab'
    saved, triggered = [], []
    state = {'downloads': {
        '2026-11-08': {'status': 'completed', 'file': '/archive/paper.pdf'},
        '2026-11-07': {'status': 'failed', 'title': '<script>alert(1)</script>'},
    }}
    log = tmp_path / 'test.log'
    log.write_text('\n'.join(str(i) for i in range(600)))
    app = create_app(lambda: deepcopy(cfg), saved.append, lambda: state,
                     str(log), lambda: triggered.append(True), lambda: None)
    app.config['TESTING'] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session['csrf_token'] = 'test-token'
    return client, saved, triggered


def test_pages(ui):
    client, _, _ = ui
    for path in ['/', '/settings', '/logs', '/static/app.css', '/static/app.js']:
        assert client.get(path).status_code == 200
    html = client.get('/').text
    assert 'name="viewport"' in html
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html


def test_filters(ui):
    client, _, _ = ui
    assert '2026-11-07' not in client.get('/?status=completed').text
    assert '2026-11-08' not in client.get('/?q=missing').text
    assert 'No editions match' in client.get('/?q=missing').text


def form():
    return dict(csrf_token='test-token', prowlarr_url='http://prowlarr:9696', prowlarr_api_key='',
                prowlarr_indexer='', sab_url='http://sab:8080', sab_api_key='',
                sab_category='nytarr', start_hour='12', interval_hours='2',
                download_folder='/downloads', media_folder='/media', backfill_days='5')


def test_settings_preserve_secrets(ui):
    client, saved, _ = ui
    assert 'secret-prowlarr' not in client.get('/settings').text
    assert 'secret-sab' not in client.get('/settings').text
    assert client.post('/settings', data=form()).status_code == 302
    assert saved[0]['prowlarr']['api_key'] == 'secret-prowlarr'
    assert saved[0]['sabnzbd']['api_key'] == 'secret-sab'
    assert saved[0]['backfill']['days'] == 5


@pytest.mark.parametrize('field,value', [('start_hour','24'), ('interval_hours','0'), ('backfill_days','-1')])
def test_invalid_settings(ui, field, value):
    client, saved, _ = ui
    data = form()
    data[field] = value
    assert 'Error saving settings' in client.post('/settings', data=data).text
    assert not saved


def test_search(ui):
    client, _, triggered = ui
    assert client.post('/search-now', data={'csrf_token': 'test-token'}).status_code == 302
    assert triggered == [True]


def test_logs(ui):
    client, _, _ = ui
    html = client.get('/logs').text
    assert '\n99\n' not in html
    assert '\n599' in html


def test_empty(tmp_path):
    cfg = deepcopy(DEFAULTS)
    app = create_app(lambda: cfg, lambda cfg: None, lambda: {'downloads': {}},
                     str(tmp_path / 'missing'), lambda: None, lambda: None)
    assert 'Your archive starts here' in app.test_client().get('/').text


def test_csrf(ui):
    client, saved, triggered = ui
    assert client.post('/settings', data={}).status_code == 400
    assert client.post('/search-now').status_code == 400
    assert not saved and not triggered


def test_auth(ui, monkeypatch):
    client, _, _ = ui
    monkeypatch.setenv('WEB_PASSWORD', 'test-password')
    assert client.get('/').status_code == 401
    assert client.get('/health').status_code == 200
    assert client.get('/', auth=('admin', 'test-password')).status_code == 200
