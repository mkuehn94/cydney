import base64
from importlib import import_module

from server.app import app


def test_shared_session(client, monkeypatch):
    module = import_module('server.app')
    renders = []
    original = module.render

    def counted(*args):
        renders.append(1)
        return original(*args)

    monkeypatch.setattr(module, 'render', counted)
    first = client.post('/api/device/sessions', json={'shared': True}).json()
    second = client.post('/api/device/sessions', json={'shared': True}).json()
    assert first['shared'] and first['id'] == second['id']
    path = f'/api/device/{first["id"]}'
    initial = client.get(f'{path}/frame.json').json()
    jpeg = client.get(f'{path}/frame.jpg')
    assert base64.b64decode(initial['image'].split(',')[1]) == jpeg.content
    assert len(renders) == 1
    assert client.get(f'{path}/frame.jpg', headers={'if-none-match': jpeg.headers['etag']}).status_code == 304
    assert len(renders) == 1
    changed = client.post(
        f'{path}/action?encoding=json',
        json={
            'kind': 'zoom',
            'value': 'out',
            'revision': initial['revision'],
        },
    ).json()
    assert changed['zoom'] == initial['zoom'] - 1
    assert client.get(f'{path}/state').json()['zoom'] == changed['zoom']
    assert (
        client.post(
            f'{path}/action',
            json={
                'kind': 'zoom',
                'value': 'in',
                'revision': initial['revision'],
            },
        ).status_code
        == 409
    )
    jpeg = client.get(f'{path}/frame.jpg')
    assert base64.b64decode(changed['image'].split(',')[1]) == jpeg.content
    client.delete(path)
    attached = client.post('/api/device/sessions', json={'shared': True}).json()
    assert attached['id'] == first['id'] and attached['zoom'] == changed['zoom']
    isolated = client.post('/api/device/sessions', json={}).json()
    assert isolated['id'] != first['id'] and not isolated['shared']
    assert app.state.sessions[first['id']].shared
