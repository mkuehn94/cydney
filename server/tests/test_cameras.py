from datetime import datetime, timezone
from io import BytesIO
import json

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from server import cameras
from server.app import app
from server.device import Device, PROFILES, apply_action, render, touch
from server.provenance import real_record

NOW = datetime.now(timezone.utc)


def foto_row(slug):
    return {
        'id': slug,
        'name': 'Published camera',
        'latitude': 48.1743533,
        'longitude': 11.5529664,
        'link': cameras.camera_url(f'webcam:foto-webcam:{slug}'),
        'captureInterval': 600,
        'offline': False,
    }


def metadata(slug):
    return '<script>var directory=' + json.dumps([foto_row(slug)], separators=(',', ':')) + ';</script>'


def directory(rows):
    return '<script>var metadata= new Object(' + json.dumps({'cams': rows}) + ');</script>'


def camera():
    return cameras.parse_camera(metadata('tum-olympiapark'), 'tum-olympiapark', NOW.isoformat())


def jpeg():
    buffer = BytesIO()
    Image.new('RGB', (720, 405), '#aaccee').save(buffer, 'JPEG')
    return buffer.getvalue()


def test_foto_webcam():
    item = camera()
    assert real_record(item, 'webcams')
    assert item.position == (11.5529664, 48.1743533)
    assert item.kind == 'listing' and item.observed_at is None
    assert item.details['capture_interval_seconds'] == 600
    with pytest.raises(ValueError, match='metadata unavailable'):
        cameras.parse_camera('<html>No camera data</html>', 'muenchen', NOW.isoformat())
    assert (
        cameras.parse_camera(
            metadata('muenchen').replace('"offline":false', '"offline":true'), 'muenchen', NOW.isoformat()
        )
        is None
    )
    assert cameras.nearby([item], (9.9937, 53.5511)) == ()
    with pytest.raises(ValueError, match='Unknown public webcam'):
        cameras.camera_url('https://localhost/private.jpg')
    with pytest.raises(OSError):
        cameras.decode_snapshot(b'<html>Upstream error</html>')


@pytest.mark.parametrize('profile', PROFILES)
def test_camera_page(profile):
    item = camera()
    device = Device(profile=profile, center=item.position, observations=(item,), camera_directory=(item,))
    apply_action(device, 'page', 'cameras')
    frame = render(device, None, NOW)
    hit = next(h for h in frame.hits if h.kind == 'camera')
    touch(device, frame, hit.box[0] + 10, hit.box[1] + 10)
    assert device.page == 'camera' and device.camera == item
    device.camera_image = cameras.decode_snapshot(jpeg())
    image, box = cameras.image_box(device.size, device.camera_image)
    assert abs(image.width / image.height - 16 / 9) < 0.02
    assert 0 <= box[0] < box[2] <= device.size[0] and 36 <= box[1] < box[3] <= device.size[1] - 60
    frame = render(device, None, NOW)
    assert frame.image.size == device.size
    assert frame.image.getpixel((box[0] + 2, box[1] + 2)) == device.camera_image.getpixel((2, 2))


def test_cameras_offline(monkeypatch, tmp_path):
    monkeypatch.setenv('CYD_FEEDS', '0')
    monkeypatch.setenv('CYD_TILES', '0')
    monkeypatch.setenv('CYD_FEED_CACHE', str(tmp_path))
    with TestClient(app) as client:
        state = client.post('/api/device/sessions', json={}).json()
        key = state['id']
        client.post(
            f'/api/device/{key}/action', json={'kind': 'page', 'value': 'cameras', 'revision': state['revision']}
        )
        assert client.get(f'/api/device/{key}/frame.png').status_code == 200
        assert client.get(f'/api/device/{key}/state').json()['camera_directory'] == []
