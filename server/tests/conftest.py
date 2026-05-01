from fastapi.testclient import TestClient
import pytest

from server.app import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    for name, value in {
        'CYD_MODE': 'live',
        'CYD_FEEDS': '0',
        'CYD_TILES': '0',
        'CYD_FEED_CACHE': str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)
    with TestClient(app) as instance:
        yield instance
