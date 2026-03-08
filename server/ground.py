from datetime import datetime, timedelta, timezone
import math

from .adapters import ATTRIBUTIONS, clean, inside, iso, point
from .data import Observation
from .fleet import vehicle_class

UTC = timezone.utc


def epoch(value, milliseconds=False):
    try:
        return datetime.fromtimestamp(float(value) / (1000 if milliseconds else 1), UTC)
    except Exception:
        return None


def parse_departures(station, data, now):
    if not isinstance(data, list):
        raise ValueError('bad MVG departures')
    position = point((station.get('longitude'), station.get('latitude')))
    if not position:
        raise ValueError('MVG station without coords')
    departures = []
    for row in data:
        planned = epoch(row.get('plannedDepartureTime'), milliseconds=True)
        realtime = row.get('realtime') is True
        predicted = epoch(row.get('realtimeDepartureTime'), milliseconds=True) if realtime else None
        at = predicted or planned
        if not at or not -120 <= (at - now).total_seconds() <= 7200:
            continue
        departures.append(
            {
                'route': clean(row.get('label')),
                'headsign': clean(row.get('destination')),
                'planned_at': iso(planned) if planned else None,
                'at': iso(at),
                'minutes': max(0, math.ceil((at - now).total_seconds() / 60)),
                'realtime': realtime,
                'delay_minutes': row.get('delayInMinutes') if realtime else None,
                'cancelled': bool(row.get('cancelled')),
                'platform': row.get('platform'),
                'messages': row.get('messages', []),
            }
        )
    departures.sort(key=lambda row: row['at'])
    selected = departures[:5]
    parts = []
    for row in selected:
        status = 'CANCELLED' if row['cancelled'] else f'{row["minutes"]}m'
        delay = (
            f' {row["delay_minutes"]:+}m'
            if isinstance(row['delay_minutes'], (int, float)) and row['delay_minutes']
            else ''
        )
        parts.append(f'{row["route"]} {status}{delay} {"RT" if row["realtime"] else "SCHED"} → {row["headsign"]}')
    key = f'mvg:stop:{station["globalId"]}'
    rail = any(t in ('SBAHN', 'UBAHN', 'TRAM') for t in station.get('transportTypes', []))
    return Observation(
        key,
        'rail' if rail else 'transit',
        clean(station['name']),
        'STOP',
        position,
        'DEPARTURES / ' + ('; '.join(parts) or 'No departures in next 2h'),
        (key,),
        demo=False,
        fetched_at=iso(now),
        expires_at=iso(now + timedelta(seconds=120)),
        kind='observation' if any(row['realtime'] for row in selected) else 'schedule',
        position_kind='stop',
        attribution=(ATTRIBUTIONS['mvg'],),
        details={
            'departures': selected,
            'station_global_id': station['globalId'],
            'computed_at': iso(now),
            'position_note': 'position of the station, not the vehicle. RT = live, SCHED = timetable',
            'observation_time_note': 'no timestamp from MVG, fetched_at is when we got it',
        },
    )


def parse_gbfs(data, now, provider, bounds):
    if not isinstance(data.get('data', {}).get('bikes'), list):
        raise ValueError('bad GBFS response')
    updated = epoch(data.get('last_updated'))
    if not updated or updated > now + timedelta(minutes=1):
        raise ValueError('GBFS timestamp missing')
    ttl = max(0, int(data.get('ttl', 60)))
    expires = updated + timedelta(seconds=max(300, ttl * 2))
    if expires <= now:
        raise ValueError('GBFS snapshot expired')
    items = []
    for row in data['data']['bikes']:
        position = point((row.get('lon'), row.get('lat')))
        # only the parked ones
        if not position or not inside(position, bounds) or row.get('is_reserved') or row.get('is_disabled'):
            continue
        vehicle_type = clean(row.get('vehicle_type_id') or '')
        category = vehicle_class(provider, vehicle_type)
        key = f'gbfs:{provider}:{row["bike_id"]}'
        fuel = row.get('current_fuel_percent')
        range_m = row.get('current_range_meters')
        value = 'AVAILABLE'
        if isinstance(fuel, (int, float)) and 0 <= fuel <= 1:
            value += f' / battery {round(fuel * 100)}%'
        if isinstance(range_m, (int, float)):
            value += f' / range {range_m / 1000:.1f}km'
        reported = epoch(row.get('last_reported'))
        title = f'{"MyRadl" if provider == "myradl" else "Dott"} {category}'
        items.append(
            Observation(
                key,
                'bikes',
                title,
                {'scooter': 'SCOOT', 'bike': 'BIKE', 'vehicle': 'VEH'}[category],
                position,
                value,
                (key,),
                demo=False,
                is_vehicle=True,
                observed_at=iso(updated),
                fetched_at=iso(now),
                expires_at=iso(expires),
                kind='availability',
                position_kind='available',
                attribution=(ATTRIBUTIONS[provider],),
                details={
                    'vehicle_type': vehicle_type,
                    'vehicle_class': category,
                    'station_id': row.get('station_id'),
                    'snapshot_at': iso(updated),
                    'position_reported_at': iso(reported) if reported else None,
                    'availability_only': True,
                    'position_note': 'parked and free to rent, ids can change',
                    'provider_ttl_seconds': ttl,
                },
            )
        )
    return items
