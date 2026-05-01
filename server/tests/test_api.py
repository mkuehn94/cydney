from datetime import datetime, timedelta, timezone
from io import BytesIO

from PIL import Image
import pytest

from server.app import app
from server.data import DEFAULT_CENTER, DEFAULT_ZOOM
from server.tests.demo_data import install_demo_state


def act(client, key, kind, **fields):
    revision = client.get(f'/api/device/{key}/state').json()['revision']
    return client.post(f'/api/device/{key}/action', json={'kind': kind, 'revision': revision, **fields})


@pytest.mark.parametrize('profile,size', [('landscape', (320, 240)), ('portrait', (240, 320))])
def test_frame_and_touch(client, profile, size):
    install_demo_state(app)
    key = client.post('/api/device/sessions', json={'profile': profile}).json()['id']
    path = f'/api/device/{key}'
    assert client.post(f'{path}/touch', json={'x': 10, 'y': 10, 'revision': 0}).status_code == 409
    for encoding, media in [('png', 'image/png'), ('jpg', 'image/jpeg')]:
        frame = client.get(f'{path}/frame.{encoding}')
        assert frame.status_code == 200 and frame.headers['content-type'] == media
        assert frame.headers['x-data-mode'] == 'demo'
        assert Image.open(BytesIO(frame.content)).size == size
    big = client.get(f'{path}/frame.rgb565').content
    little = client.get(f'{path}/frame.rgb565?byte_order=little').content
    assert len(big) == len(little) == size[0] * size[1] * 2
    assert big[::2] == little[1::2] and big[1::2] == little[::2]
    frame = client.get(f'{path}/frame.png')
    assert client.get(f'{path}/frame.png', headers={'if-none-match': frame.headers['etag']}).status_code == 304
    tap = {'x': size[0] - 20, 'y': 20, 'revision': 0}
    assert client.post(f'{path}/touch', json=tap).json()['page'] == 'menu'
    assert client.post(f'{path}/touch', json=tap).status_code == 409
    assert client.post(f'{path}/action', json={'kind': 'home', 'revision': 0}).status_code == 409
    assert act(client, key, 'toggle', value='invalid').status_code == 422
    revision = client.get(f'{path}/state').json()['revision']
    moved = client.post(f'{path}/action?encoding=jpg', json={'kind': 'page', 'value': 'map', 'revision': revision})
    assert moved.status_code == 200 and moved.headers['content-type'] == 'image/jpeg'
    assert moved.headers['x-frame-page'] == 'map' and Image.open(BytesIO(moved.content)).size == size
    tap['revision'] = int(moved.headers['x-frame-revision'])
    menu = client.post(f'{path}/touch?encoding=jpg', json=tap)
    assert menu.status_code == 200 and menu.headers['x-frame-page'] == 'menu'
    assert Image.open(BytesIO(menu.content)).size == size
    assert client.post(f'{path}/touch?encoding=jpg', json=tap).status_code == 409
    assert client.delete(path).status_code == 204
    assert client.get(f'{path}/state').status_code == 404


def test_start_position(client):
    for body in ({}, {'shared': True}, {'profile': 'portrait'}):
        session = client.post('/api/device/sessions', json=body).json()
        assert session['center'] == session['home'] == [11.5099957, 48.1816035]
        assert session['zoom'] == DEFAULT_ZOOM == 18
        key = session['id']
        assert act(client, key, 'pan', dx=100).json()['center'] != list(DEFAULT_CENTER)
        assert act(client, key, 'home').json()['center'] == list(DEFAULT_CENTER)


def test_position(client):
    state = client.post('/api/device/sessions', json={}).json()
    key = state['id']
    assert state['location']['position'] is None
    assert act(client, key, 'follow', value='on').status_code == 422
    now = datetime.now(timezone.utc)
    fix = {'longitude': 11.59, 'latitude': 48.15, 'accuracy_m': 18.5, 'observed_at': now.isoformat(), 'follow': True}
    state = client.post(f'/api/device/{key}/position', json=fix).json()
    assert state['center'] == [11.59, 48.15] and state['location']['fresh']
    assert state['location']['mode'] == 'follow' and state['home'] == list(DEFAULT_CENTER)
    center = act(client, key, 'pan', dx=40).json()['center']
    fix.pop('follow')
    fix.update(longitude=11.595, observed_at=(now + timedelta(seconds=1)).isoformat())
    state = client.post(f'/api/device/{key}/position', json=fix).json()
    assert state['center'] == center and state['location']['mode'] == 'browse'
    assert act(client, key, 'follow', value='on').json()['center'] == [11.595, 48.15]
    assert client.post(f'/api/device/{key}/position', json=fix).status_code == 409
    for changes in (
        {'accuracy_m': -1},
        {'latitude': 90},
        {'observed_at': (now - timedelta(minutes=3)).isoformat()},
        {'observed_at': '2026-05-01T12:00:00'},
    ):
        assert client.post(f'/api/device/{key}/position', json={**fix, **changes}).status_code == 422
