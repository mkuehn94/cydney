from io import BytesIO
import os

from PIL import Image

from server.app import app, encode_frame
from server.tests.demo_data import install_demo_state


def test_old_frame_still_clickable(client):
    install_demo_state(app)
    key = client.post('/api/device/sessions', json={}).json()['id']
    path = f'/api/device/{key}'
    shown = client.get(path + '/frame.json').json()
    session = app.state.sessions[key]
    session.device.revision += 1
    newer = client.get(path + '/frame.json').json()
    assert newer['frame_id'] > shown['frame_id']
    response = client.post(
        path + '/touch?encoding=json',
        json={
            'x': 300,
            'y': 19,
            'revision': shown['revision'],
            'frame_id': shown['frame_id'],
        },
    )
    assert response.status_code == 200 and response.json()['page'] == 'menu'
    assert (
        client.post(
            path + '/touch',
            json={
                'x': 300,
                'y': 19,
                'revision': shown['revision'],
                'frame_id': shown['frame_id'],
            },
        ).status_code
        == 409
    )


def test_old_frame_rejected(client):
    install_demo_state(app)
    key = client.post('/api/device/sessions', json={}).json()['id']
    path = f'/api/device/{key}'
    shown = client.get(path + '/frame.json').json()
    app.state.sessions[key].device.page = 'menu'
    assert (
        client.post(
            path + '/touch',
            json={
                'x': 300,
                'y': 19,
                'revision': shown['revision'],
                'frame_id': shown['frame_id'],
            },
        ).status_code
        == 409
    )


def test_jpeg_size():
    image = Image.frombytes('RGB', (320, 240), os.urandom(320 * 240 * 3))
    content, media = encode_frame(image, 'jpg', 'big')
    assert media == 'image/jpeg' and len(content) <= 48 * 1024
    assert Image.open(BytesIO(content)).size == (320, 240)
