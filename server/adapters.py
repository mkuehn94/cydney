from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import csv
import hashlib
from io import StringIO
import math
import re

from .data import MUNICH, Observation, distance_m
from .timing import parse_time as timestamp

UTC = timezone.utc
# west, south, east, north
POI_BOUNDS = (11.558, 48.126, 11.593, 48.149)
CITY_BOUNDS = (11.535, 48.110, 11.616, 48.165)
ATTRIBUTIONS = {
    'webcams': (
        'Foto-Webcam.eu / camera operators; Powered by TfL Open Data; '
        'Contains OS data © Crown copyright and database rights 2016; '
        'Geomni UK Map data © and database rights 2019; Caltrans; '
        'Hong Kong Transport Department / DATA.GOV.HK; LTA / data.gov.sg; '
        'Oregon DOT / TripCheck; Washington State DOT; City of Austin; '
        'BayernInfo / Bayerische Staatsbauverwaltung; Terra-HD; '
        'Foto-Webcam.net; Foto-Webcam.org; feratel; wetter.com; '
        'Hans van Gelderen / Michaelianger webcam; Private Wetterstation Pliening / Django Nausch; '
        'Münchener Ruder- und Segelverein Bayern; Johann Siemens / AWEKAS'
    ),
    'osm': '© OpenStreetMap contributors / ODbL',
    'atp': 'AllThePlaces / CC0',
    'works': 'Landeshauptstadt München / dl-by-de/2-0',
    'dwd': 'Deutscher Wetterdienst / DWD Open Data',
    'nina': 'BBK / warnung.bund.de; originating authority per alert',
    'aircraft': 'ADSB.lol / ODbL',
    'mvv': 'Münchner Verkehrs- und Tarifverbund (MVV) / CC BY 4.0',
    'mvg': 'Münchner Verkehrsgesellschaft (MVG) / public departure API',
    'myradl': 'MyRadl / nextbike GBFS; public available vehicles, no riders',
    'dott': 'Dott GBFS; public available vehicles, no riders',
}


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def point(value) -> tuple[float, float] | None:
    try:
        lon, lat = map(float, value)
        if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -85 <= lat <= 85:
            return lon, lat
    except (TypeError, ValueError):
        pass
    return None


def inside(position, bounds=POI_BOUNDS):
    longitude = (
        bounds[0] <= position[0] <= bounds[2]
        if bounds[0] <= bounds[2]
        else position[0] >= bounds[0] or position[0] <= bounds[2]
    )
    return longitude and bounds[1] <= position[1] <= bounds[3]


def clean(value, limit=300):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', str(value or ''))).strip()[:limit]


def place_layer(tags):
    amenity = tags.get('amenity')
    if amenity in ('cafe', 'restaurant', 'fast_food', 'bar', 'pub', 'ice_cream', 'food_court'):
        return 'food'
    if amenity in ('pharmacy', 'hospital', 'clinic', 'doctors', 'dentist') or tags.get('healthcare'):
        return 'health'
    if amenity == 'charging_station':
        return 'charging'
    if amenity in ('parking', 'bicycle_parking', 'parking_entrance'):
        return 'parking'
    if tags.get('entrance') or tags.get('highway') in ('steps', 'elevator'):
        return 'access'
    if tags.get('natural') or tags.get('leisure') in ('park', 'playground'):
        return 'nature'
    if tags.get('historic') or tags.get('tourism'):
        return 'heritage'
    if tags.get('shop'):
        return 'places'
    return 'amenities'


def place_value(tags):
    parts = [tags.get('shop') or tags.get('amenity') or tags.get('tourism') or tags.get('entrance') or 'Place']
    address = ' '.join(filter(None, [tags.get('addr:street'), tags.get('addr:housenumber')]))
    if address:
        parts.append(address)
    # just show the hours as text, no open/closed logic
    if tags.get('opening_hours'):
        parts.append('Hours: ' + clean(tags['opening_hours']))
    elif tags.get('shop') or place_layer(tags) in ('food', 'health'):
        parts.append('Hours unknown')
    for key, label in (
        ('wheelchair', 'Wheelchair'),
        ('fee', 'Fee'),
        ('access', 'Access'),
        ('cuisine', 'Cuisine'),
        ('drinking_water', 'Drinking water'),
        ('capacity', 'Capacity'),
        ('socket:type2:output', 'Type2 power'),
        ('socket:type2', 'Type2 sockets'),
        ('internet_access', 'Internet'),
        ('operator', 'Operator'),
    ):
        if tags.get(key):
            parts.append(f'{label}: {clean(tags[key], 100)}')
    return ' / '.join(parts)


def osm_query(bounds=POI_BOUNDS, detail='detail'):
    from .viewport import boxes

    filters = [
        '["shop"]',
        '["amenity"]',
        '["tourism"]',
        '["historic"]',
        '["entrance"]',
        '["highway"="elevator"]',
        '["leisure"~"^(park|playground)$"]',
        '["natural"="tree"]',
    ]
    if detail == 'overview':
        filters = [
            '["amenity"~"^(hospital|pharmacy|charging_station|toilets|drinking_water|bicycle_repair_station|atm)$"]',
            '["shop"="supermarket"]',
            '["tourism"="attraction"]',
        ]
    queries = ''.join(
        f'nwr{tag}({south},{west},{north},{east});' for west, south, east, north in boxes(bounds) for tag in filters
    )
    return '[out:json][timeout:45];(' + queries + ');out center tags meta;'


def parse_osm(data, now, bounds=POI_BOUNDS):
    if not isinstance(data.get('elements'), list) or data.get('remark'):
        raise ValueError('overpass response incomplete')
    items = []
    for row in data['elements']:
        pos = point((row.get('lon'), row.get('lat'))) or point(
            (row.get('center', {}).get('lon'), row.get('center', {}).get('lat'))
        )
        tags = row.get('tags', {})
        if not pos or not inside(pos, bounds) or not tags:
            continue
        key = f'osm:{row["type"]}/{row["id"]}'
        layer = place_layer(tags)
        title = (
            tags.get('name')
            or tags.get('brand')
            or tags.get('shop')
            or tags.get('amenity')
            or tags.get('entrance')
            or tags.get('tourism')
            or 'OSM feature'
        )
        items.append(
            Observation(
                key,
                layer,
                clean(title),
                layer[:4].upper(),
                pos,
                place_value(tags),
                (key,),
                demo=False,
                fetched_at=iso(now),
                kind='listing',
                position_kind='listing' if row['type'] == 'node' else 'representative',
                attribution=(ATTRIBUTIONS['osm'],),
                details={
                    'tags': tags,
                    'edited_at': row.get('timestamp'),
                    'position_note': 'OSM node'
                    if row['type'] == 'node'
                    else 'OSM bounding-box center; not an entrance',
                },
            )
        )
    return items


def parse_atp(features, now, bounds=POI_BOUNDS):
    items = []
    for row in features:
        geometry, tags = row.get('geometry') or {}, row.get('properties') or {}
        pos = point(geometry.get('coordinates')) if geometry.get('type') == 'Point' else None
        if not pos or not inside(pos, bounds):
            continue
        spider = str(tags.get('@spider') or 'unknown')
        reference = (
            tags.get('@id') or tags.get('ref') or hashlib.sha256(repr(sorted(tags.items())).encode()).hexdigest()[:20]
        )
        key = f'atp:{spider}:{reference}'
        layer = place_layer(tags)
        items.append(
            Observation(
                key,
                layer,
                clean(tags.get('name') or tags.get('brand') or spider),
                'SHOP',
                pos,
                place_value(tags),
                (key,),
                demo=False,
                fetched_at=iso(now),
                kind='listing',
                position_kind='listing',
                attribution=(ATTRIBUTIONS['atp'],),
                details={'tags': tags},
            )
        )
    return items


def geometry_points(geometry):
    def descend(value):
        pos = point(value) if isinstance(value, (list, tuple)) and len(value) == 2 else None
        if pos:
            yield pos
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield from descend(child)

    return list(descend((geometry or {}).get('coordinates', [])))


def works_dates(tags):
    def local_date(key, end=False):
        try:
            day = datetime.strptime(tags.get(key) or '', '%d.%m.%Y')
            if end:
                day += timedelta(days=1)  # last day is included
            return iso(day.replace(tzinfo=ZoneInfo('Europe/Berlin')))
        except (ValueError, TypeError):
            return None

    return local_date('beginn_datum_kombiniert'), local_date('ende_datum_kombiniert', end=True)


def parse_works(data, now, bounds=CITY_BOUNDS):
    if data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'), list):
        raise ValueError('bad roadworks geojson')
    if data.get('numberMatched', data.get('totalFeatures', 0)) not in (None, 'unknown'):
        if int(data.get('numberMatched', data.get('totalFeatures', 0))) > len(data['features']):
            raise ValueError('roadworks response is cut off')
    items = []
    for row in data['features']:
        tags = row.get('properties') or {}
        positions = geometry_points(row.get('geometry'))
        if not positions:
            continue
        pos = (
            (min(p[0] for p in positions) + max(p[0] for p in positions)) / 2,
            (min(p[1] for p in positions) + max(p[1] for p in positions)) / 2,
        )
        if not inside(pos, bounds):
            continue
        starts_at, ends_at = works_dates(tags)
        if ends_at and timestamp(ends_at) <= now:
            continue
        key = f'munich:works:{row.get("id") or hashlib.sha256(repr(row).encode()).hexdigest()[:20]}'
        parts = [
            tags.get('beeintraechtigung'),
            tags.get('beschreibung'),
            tags.get('beginn_datum_kombiniert'),
            tags.get('ende_datum_kombiniert'),
        ]
        items.append(
            Observation(
                key,
                'works',
                clean(tags.get('strasse_hausnr') or 'Munich roadworks'),
                'WORK',
                pos,
                ' / '.join(clean(part) for part in parts if part),
                (key,),
                demo=False,
                fetched_at=iso(now),
                starts_at=starts_at,
                ends_at=ends_at,
                kind='listing',
                position_kind='representative',
                attribution=(ATTRIBUTIONS['works'],),
                details={
                    'properties': tags,
                    'geometry': row.get('geometry'),
                    'position_note': 'Source geometry bounding-box center; published activity dates, not a live worksite observation',
                },
            )
        )
    return items


def parse_nina(data, now, warnings=None):
    if not isinstance(data, list):
        raise ValueError('bad NINA response')
    items = []
    for row in data:
        payload = row.get('payload') or {}
        warning = (warnings or {}).get(row['id']) or {}
        infos = warning.get('info') or []
        info = next((i for i in infos if i.get('language', '').startswith('de')), infos[0] if infos else {})
        if warning.get('status', payload.get('status', 'Actual')) != 'Actual':
            continue  # test warnings
        expires = timestamp(info.get('expires') or payload.get('expires') or row.get('expires'))
        if expires and expires <= now:
            continue
        if (warning.get('msgType') or payload.get('msgType') or row.get('msgType')) == 'Cancel':
            continue
        key = f'bbk:{row["id"]}'
        start = timestamp(
            info.get('onset')
            or info.get('effective')
            or payload.get('effective')
            or warning.get('sent')
            or payload.get('sent')
            or row.get('sent')
        )
        severity = clean(
            info.get('severity')
            or payload.get('data', {}).get('severity')
            or payload.get('severity')
            or row.get('severity')
            or 'Unknown'
        )
        areas = info.get('area') or []
        area = clean(' / '.join(a.get('areaDesc', '') for a in areas)) or 'München municipality'
        items.append(
            Observation(
                key,
                'alerts',
                clean(
                    info.get('headline')
                    or payload.get('data', {}).get('headline')
                    or row.get('i18nTitle', {}).get('de')
                    or 'Official Munich alert'
                ),
                'WARN',
                MUNICH,
                'Munich citywide notice; exact incident location unavailable / '
                + clean(info.get('description') or payload.get('type') or row.get('type')),
                (key,),
                demo=False,
                warning=True,
                observed_at=payload.get('sent') or row.get('sent'),
                fetched_at=iso(now),
                expires_at=iso(expires) if expires else None,
                starts_at=iso(start) if start else None,
                ends_at=iso(expires) if expires else None,
                kind='alert',
                position_kind='citywide',
                attribution=(ATTRIBUTIONS['nina'],),
                details={
                    'dashboard': row,
                    'severity': severity,
                    'affected_area': area,
                    'warning_detail': warning,
                    'detail_available': bool(info),
                    'position_note': 'Administrative coverage: München (091620000000). Regional banner; city anchor is NOT incident coordinates.',
                },
            )
        )
    return items


def parse_dwd(text, now):
    rows = list(csv.reader(StringIO(text), delimiter=';'))
    if len(rows) < 4 or 'dry_bulb_temperature_at_2_meter_above_ground' not in rows[0]:
        raise ValueError('DWD csv looks different')
    names = rows[0]
    records = []
    for row in rows[3:]:
        try:
            observed = datetime.strptime(row[0] + ' ' + row[1], '%d.%m.%y %H:%M').replace(tzinfo=UTC)
            if observed <= now + timedelta(minutes=5):
                records.append((observed, dict(zip(names, row))))
        except (ValueError, IndexError):
            continue
    if not records:
        raise ValueError('nothing usable in DWD csv')
    observed, values = max(records, key=lambda pair: pair[0])
    parts = []
    for key, suffix in [
        ('dry_bulb_temperature_at_2_meter_above_ground', 'C'),
        ('mean_wind_speed_during last_10_min_at_10_meters_above_ground', 'km/h wind'),
        ('precipitation_amount_last_hour', 'mm rain/1h'),
    ]:
        value = values.get(key, '---')
        if value not in ('---', '', '-999'):
            parts.append(value.replace(',', '.') + suffix)
    if not parts:
        raise ValueError('DWD row has no values')
    return [
        Observation(
            'dwd:10865',
            'weather',
            'DWD München-Stadt',
            'WX',
            (11.5429, 48.1632),
            ' / '.join(parts),
            ('dwd:WMO10865',),
            demo=False,
            observed_at=iso(observed),
            fetched_at=iso(now),
            expires_at=iso(observed + timedelta(hours=6)),
            kind='observation',
            position_kind='station',
            attribution=(ATTRIBUTIONS['dwd'],),
            details={'station': 'München-Stadt / WMO 10865', 'measurements': values},
        )
    ]


def parse_aircraft(data, now, center=MUNICH, radius_nm=25):
    if not isinstance(data.get('ac'), list):
        raise ValueError('bad adsb.lol response')
    # print(len(data['ac']), 'aircraft')
    provider_now = float(data.get('now') or now.timestamp())
    # adsb.lol sends ms, some others secconds
    if provider_now > 100_000_000_000:
        provider_now /= 1000
    reference = datetime.fromtimestamp(provider_now, UTC)
    if abs((reference - now).total_seconds()) > 120:
        raise ValueError('ADSB.lol response timestamp too far from server clock')
    items = []
    for row in data['ac']:
        pos = point((row.get('lon'), row.get('lat')))
        try:
            seen = float(row.get('seen_pos', 99999))
        except (TypeError, ValueError):
            continue
        if not pos or not math.isfinite(seen) or seen < 0 or seen > 60 or distance_m(pos, center) > radius_nm * 1852:
            continue
        category = str(row.get('category') or '')
        surface = category in ('C1', 'C2')  # C1/C2 = emergency / service vehicles on the ground
        if category.startswith('C') and not surface:
            continue  # Could be a beacon or a tower -> skip
        key = f'adsblol:{row["hex"]}'
        observed = reference - timedelta(seconds=seen)
        altitude = row.get('alt_baro')
        altitude_text = (
            f'{round(altitude * 0.3048)}m barometric'
            if isinstance(altitude, (int, float))
            else str(altitude or 'altitude unknown')
        )
        try:
            track = float(row.get('track'))
            track = track % 360 if math.isfinite(track) and 0 <= track <= 360 else None
        except (TypeError, ValueError):
            track = None
        callsign = clean(row.get('flight') or '').upper()
        title = clean(row.get('flight') or row.get('r') or row['hex'])
        if surface:
            title = 'Surface vehicle ' + title
        items.append(
            Observation(
                key,
                'traffic' if surface else 'flights',
                title,
                'GRND' if surface else clean(row.get('t') or 'AIR', 6),
                pos,
                f'{altitude_text} / {row.get("gs", "?")}kt / '
                + (f'{track:g}° track' if track is not None else 'track unknown'),
                (key,),
                demo=False,
                is_vehicle=True,
                observed_at=iso(observed),
                fetched_at=iso(now),
                expires_at=iso(observed + timedelta(minutes=2)),
                kind='observation',
                position_kind='observed',
                attribution=(ATTRIBUTIONS['aircraft'],),
                details={
                    'callsign': callsign or None,
                    'track_degrees': track,
                    'registration': row.get('r'),
                    'aircraft_type': row.get('t'),
                    'emitter_category': category,
                    'explicit_surface_vehicle': surface,
                    'position_age_seconds': seen,
                },
            )
        )
    return items
