import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from server.adapters import parse_aircraft, parse_atp, parse_osm
from server.data import MUNICH
from server.feeds import FeedStore, SPECS
from server.places import reconcile

NOW = datetime(2026, 11, 9, 10, tzinfo=timezone.utc)


def aircraft(now=NOW):
    return {
        'now': now.timestamp() * 1000,
        'ac': [
            {
                'hex': 'abc123',
                'flight': ' TEST42 ',
                'lat': MUNICH[1],
                'lon': MUNICH[0],
                'seen_pos': 2,
                'alt_baro': 10000,
                'gs': 240,
                'track': 90,
                't': 'A320',
            }
        ],
    }


def test_osm_atp_merge():
    tags = {
        'name': 'Branch',
        'shop': 'supermarket',
        'addr:street': 'Teststraße',
        'addr:housenumber': '1',
        'addr:unit': '1',
    }
    (osm,) = parse_osm(
        {
            'elements': [
                {
                    'type': 'node',
                    'id': 42,
                    'lon': MUNICH[0],
                    'lat': MUNICH[1],
                    'tags': {**tags, 'opening_hours': 'Mo-Fr 08:00-18:00'},
                }
            ]
        },
        NOW,
    )
    (atp,) = parse_atp(
        [
            {
                'type': 'Feature',
                'geometry': {'type': 'Point', 'coordinates': MUNICH},
                'properties': {**tags, '@spider': 'test_de', '@id': '42', 'opening_hours': '24/7'},
            }
        ],
        NOW,
    )
    (merged,) = reconcile([osm, atp])
    assert merged.id == osm.id and merged.sources == osm.sources + atp.sources
    assert '24/7' not in merged.value
    assert merged.details['records'][1]['fields']['tags']['opening_hours'] == '24/7'
    for different in (
        replace(atp, position=(11.58, 48.14)),
        replace(atp, details={'tags': {'name': 'Branch'}}),
        replace(atp, details={'tags': {**tags, 'addr:unit': '2'}}),
    ):
        assert len(reconcile([osm, different])) == 2
    assert len(reconcile([osm, replace(osm, id='osm:node/43'), atp])) == 3


def test_parse_aircraft():
    (item,) = parse_aircraft(aircraft(), NOW)
    assert item.is_vehicle and not item.demo and item.value.startswith('3048m barometric')
    assert item.observed_at == (NOW - timedelta(seconds=2)).isoformat()
    assert item.position_kind == 'observed' and item.details['track_degrees'] == 90
    with pytest.raises(ValueError, match='clock'):
        parse_aircraft(aircraft(NOW - timedelta(minutes=10)), NOW)
    for changes in ({'seen_pos': 90}, {'category': 'C0', 'alt_baro': 'ground'}):
        data = aircraft()
        data['ac'][0].update(changes)
        assert parse_aircraft(data, NOW) == []


def test_stale_cache(tmp_path):
    async def run():
        calls, fail = [], False

        def upstream(request):
            calls.append(request)
            return httpx.Response(503 if fail else 200, json=aircraft(), headers={'Retry-After': '180'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            store = FeedStore(client, tmp_path)
            spec = next(s for s in SPECS if s.id == 'aircraft')
            await store.refresh(spec, NOW)
            assert not store.snapshot(NOW)[1][0].stale
            store.snapshot(NOW)
            assert len(calls) == 1
            fail = True
            store.resources.clear()
            await store.refresh(spec, NOW + timedelta(seconds=30))
            assert store.snapshot(NOW + timedelta(seconds=30))[1][0].stale
            assert store.records['aircraft']['retry_at'] == (NOW + timedelta(seconds=210)).isoformat()
            restarted = FeedStore(client, tmp_path, enabled=False)
            assert restarted.snapshot(NOW + timedelta(seconds=40))[1][0].stale
            assert restarted.snapshot(NOW + timedelta(minutes=3))[1] == ()

    asyncio.run(run())


def test_pollers(tmp_path):
    async def run():
        calls = []

        def upstream(request):
            calls.append(request)
            return httpx.Response(200, json=[])

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            store = FeedStore(client, tmp_path)
            url = 'https://example.test/feed'
            first, second = await asyncio.gather(store.shared_download(url, 30), store.shared_download(url, 30))
            assert first == second and len(calls) == 1
            area = store.follow('device', (9.9937, 53.5511), 14, (320, 240))
            tasks = list(area.tasks)
            assert tasks and store.follow('other', area.view.center, 14, (320, 240)) is area
            store.release('device')
            assert area.tasks == tasks
            store.release('other')
            await asyncio.sleep(0)
            assert not area.tasks and all(task.done() for task in tasks)
            assert store.follow('device', area.view.center, 14, (320, 240)) is area and area.tasks
            await store.close()

    asyncio.run(run())
