import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from server.aircraft import URL, lookup, route_label, parse_route
from server.adapters import parse_aircraft
from server.app import app
from server.feeds import FeedStore
from server.tests.test_api import act
from server.tests.test_feeds import aircraft

NOW = datetime.now(timezone.utc)
ROUTE = {
    'response': {
        'flightroute': {
            'callsign_icao': 'TEST42',
            'origin': {'icao_code': 'EDDM', 'iata_code': 'MUC', 'name': 'Munich Airport'},
            'destination': {'icao_code': 'EGLL', 'iata_code': 'LHR', 'name': 'Heathrow Airport'},
        }
    }
}


@pytest.mark.parametrize('track,expected', [(90, 90), (360, 0), (-1, None), ('bad', None), (float('nan'), None)])
def test_ground_course(track, expected):
    data = aircraft(NOW)
    data['ac'][0]['track'] = track
    (item,) = parse_aircraft(data, NOW)
    assert item.details['track_degrees'] == expected
    assert ('track unknown' in item.value) == (expected is None)


def test_route_cache(tmp_path):
    async def run():
        requests = []

        def upstream(request):
            requests.append(str(request.url))
            return httpx.Response(200, json=ROUTE) if request.url.path.endswith('TEST42') else httpx.Response(503)

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            feeds = FeedStore(client, tmp_path)
            (item,) = parse_aircraft(aircraft(NOW), NOW)
            result = await lookup(feeds, item, NOW)
            assert route_label(result) == 'MUC → LHR' and result['origin']['name'] == 'Munich Airport'
            assert result['url'] == URL + 'TEST42'
            assert await lookup(feeds, item, NOW) == result and len(requests) == 1
            for callsign in ('https://localhost', '', 'UNKNOWN'):
                missing = await lookup(feeds, replace(item, details={'callsign': callsign}), NOW)
                assert missing['status'] == 'unavailable' and route_label(missing) == 'Route unavailable'
            assert len(requests) == 2
            wrong = {'response': {'flightroute': {**ROUTE['response']['flightroute'], 'callsign_icao': 'OTHER1'}}}
            with pytest.raises(ValueError, match='mismatch'):
                parse_route(wrong, 'TEST42', NOW.isoformat())

    asyncio.run(run())


def test_route_in_frame(client, monkeypatch):
    async def lookup_route(feeds, item, now):
        return parse_route(ROUTE, item.details['callsign'], now.isoformat())

    monkeypatch.setattr('server.app.aircraft.lookup', lookup_route)
    (item,) = parse_aircraft(aircraft(NOW), NOW)
    app.state.feeds.records['aircraft'].update(items=(item,), fetched_at=NOW.isoformat())
    app.state.feeds.generation += 1
    key = client.post(
        '/api/device/sessions',
        json={
            'longitude': item.position[0],
            'latitude': item.position[1],
            'zoom': 15,
        },
    ).json()['id']
    assert act(client, key, 'select', value=item.id).status_code == 200
    assert client.get(f'/api/device/{key}/frame.png').status_code == 200
    assert route_label(app.state.sessions[key].device.selected_flight_route) == 'MUC → LHR'
