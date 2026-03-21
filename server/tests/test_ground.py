from datetime import datetime, timedelta, timezone

import pytest

from server.adapters import iso
from server.data import MUNICH
from server.ground import parse_departures, parse_gbfs

NOW = datetime(2026, 11, 9, 16, tzinfo=timezone.utc)
BOUNDS = (9.8, 53.4, 10.1, 53.7)


def test_departures():
    station = {
        'globalId': 'de:09162:2',
        'name': 'Marienplatz',
        'longitude': MUNICH[0],
        'latitude': MUNICH[1],
        'transportTypes': ['SBAHN'],
    }
    ms = NOW.timestamp() * 1000
    rows = [
        {
            'label': 'S3',
            'destination': 'Holzkirchen',
            'plannedDepartureTime': ms + 60_000,
            'realtimeDepartureTime': ms + 240_000,
            'realtime': True,
            'delayInMinutes': 3,
            'cancelled': True,
        },
        {
            'label': 'U6',
            'destination': 'Garching',
            'plannedDepartureTime': ms + 120_000,
            'realtimeDepartureTime': ms + 600_000,
            'realtime': False,
            'delayInMinutes': 8,
        },
    ]
    item = parse_departures(station, rows, NOW)
    assert item.position == MUNICH and item.position_kind == 'stop' and not item.is_vehicle
    assert item.observed_at is None and item.fetched_at == iso(NOW)
    assert 'CANCELLED' in item.value and '+3m RT' in item.value and '2m SCHED' in item.value
    scheduled, live = item.details['departures']
    assert not scheduled['realtime'] and scheduled['delay_minutes'] is None
    assert live['realtime'] and live['cancelled']
    assert 'not the vehicle' in item.details['position_note']


def gbfs(now=NOW):
    return {
        'last_updated': now.timestamp(),
        'ttl': 60,
        'data': {
            'bikes': [
                {
                    'bike_id': 'public-42',
                    'lat': MUNICH[1],
                    'lon': MUNICH[0],
                    'is_reserved': False,
                    'is_disabled': False,
                    'last_reported': (now - timedelta(hours=1)).timestamp(),
                    'current_fuel_percent': 0.7,
                    'current_range_meters': 12000,
                },
                {'bike_id': 'reserved', 'lat': MUNICH[1], 'lon': MUNICH[0], 'is_reserved': True},
                {'bike_id': 'disabled', 'lat': MUNICH[1], 'lon': MUNICH[0], 'is_disabled': True},
            ]
        },
    }


def test_gbfs():
    (item,) = parse_gbfs(gbfs(), NOW, 'myradl', (11.5, 48.1, 11.6, 48.2))
    assert item.is_vehicle and not item.demo
    assert item.kind == 'availability' and item.position_kind == 'available'
    assert item.observed_at == iso(NOW)
    assert item.details['position_reported_at'] == iso(NOW - timedelta(hours=1))
    assert item.details['availability_only'] and 'AVAILABLE' in item.value
    assert 'battery 70%' in item.value and 'range 12.0km' in item.value
    assert parse_gbfs(gbfs(), NOW, 'dott', BOUNDS) == []
    with pytest.raises(ValueError, match='expired'):
        parse_gbfs(gbfs(NOW - timedelta(minutes=10)), NOW, 'myradl', (11.5, 48.1, 11.6, 48.2))
