import json
import re

import httpx

from .adapters import clean

SOURCE = 'adsbdb / callsign route lookup'
URL = 'https://api.adsbdb.com/v0/callsign/'


def callsign(item):
    value = item.details.get('callsign')
    return value if isinstance(value, str) and re.fullmatch(r'[A-Z0-9]{2,8}', value) else None


def airport(row):
    if not isinstance(row, dict):
        return None
    code = row.get('iata_code') or row.get('icao_code')
    if not isinstance(code, str) or not re.fullmatch(r'[A-Z0-9]{3,4}', code):
        return None
    return {'code': code, 'name': clean(row.get('name') or code), 'city': clean(row.get('municipality') or '')}


def parse_route(data, expected, fetched_at):
    response = data.get('response') if isinstance(data, dict) else None
    route = response.get('flightroute') if isinstance(response, dict) else None
    if not isinstance(route, dict) or expected not in (
        route.get('callsign'),
        route.get('callsign_icao'),
        route.get('callsign_iata'),
    ):
        raise ValueError('Route callsign mismatch or unavailable')
    origin, destination = airport(route.get('origin')), airport(route.get('destination'))
    if not origin or not destination:
        raise ValueError('no airports for route')
    return {
        'status': 'available',
        'callsign': expected,
        'origin': origin,
        'destination': destination,
        'source': SOURCE,
        'url': URL + expected,
        'fetched_at': fetched_at,
        'note': 'Reported callsign route; may differ from current operation or diversions.',
    }


async def lookup(feeds, item, now):
    ident = callsign(item)
    unavailable = {'status': 'unavailable', 'callsign': ident, 'source': SOURCE}
    if not ident or not feeds.enabled:
        return unavailable
    try:
        raw = await feeds.shared_download(URL + ident, 900, now=now, max_bytes=100_000, timeout=5)
        return parse_route(json.loads(raw.content), ident, raw.fetched_at)
    except Exception:
        return unavailable


def route_label(route):
    if route and route.get('status') == 'available':
        return route['origin']['code'] + ' → ' + route['destination']['code']
    return 'Route unavailable'
