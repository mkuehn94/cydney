import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from server.adapters import inside, osm_query
from server.data import MUNICH
from server.feeds import FeedStore
from server.viewport import DEFAULT_VIEW, Viewport, contains, screen_bounds


@pytest.mark.parametrize('center', [MUNICH, (9.9937, 53.5511), (179.999, 50), (-179.999, 50), (0, 84.9)])
def test_dateline(center):
    for zoom in (12, 15, 18):
        area = Viewport.camera(center, zoom, (320, 240))
        assert contains(area.bounds, screen_bounds(center, zoom, (320, 240)))
        assert area.covers(center, zoom, (320, 240)) and inside(center, area.bounds)
        assert area.detail == ('overview' if zoom <= 13 else 'detail')
    area = Viewport.camera((179.999, 48), 12, (320, 240))
    query = osm_query(area.bounds, area.detail)
    assert area.bounds[0] > area.bounds[2] and ',-180,' in query
    assert ',180,' in query or ',180)' in query
    assert '["entrance"]' not in query and '["entrance"]' in osm_query(DEFAULT_VIEW.bounds, 'detail')


def test_cooldown(tmp_path):
    async def run():
        calls, failed = [], False

        def upstream(request):
            calls.append(request)
            return httpx.Response(429, headers={'Retry-After': '120'}) if failed else httpx.Response(200, json=[])

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            store = FeedStore(client, tmp_path, enabled=False)
            now = datetime.now(timezone.utc)
            url = 'https://api.adsb.lol/v2/point/48.13743/11.57549/5'
            first = await store.shared_download(url, 10, now=now)
            cached = await store.shared_download(url, 10, now=now + timedelta(seconds=5))
            assert cached.fetched_at == first.fetched_at == now.isoformat() and len(calls) == 1
            store.resources.clear()
            failed = True
            for target in (url, 'https://api.adsb.lol/v2/point/48.14/11.58/5'):
                with pytest.raises(httpx.HTTPStatusError):
                    await store.shared_download(target, 10)
            assert len(calls) == 2

    asyncio.run(run())
