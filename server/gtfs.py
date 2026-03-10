import csv
from datetime import datetime, timedelta
from io import BytesIO, TextIOWrapper
from pathlib import Path
import zipfile
from zoneinfo import ZoneInfo

from .adapters import ATTRIBUTIONS, iso, point, timestamp
from .data import MUNICH, Observation, distance_m

BERLIN = ZoneInfo('Europe/Berlin')
GTFS_URL = 'https://www.mvv-muenchen.de/fileadmin/mediapool/developer/opendata/gesamt_gtfs.zip'


def rows(archive, name, required=True):
    if name not in archive.namelist():
        if required:
            raise ValueError(f'GTFS missing {name}')
        return
    with archive.open(name) as raw, TextIOWrapper(raw, encoding='utf-8-sig', newline='') as stream:
        yield from csv.DictReader(stream)


def seconds(value):
    hour, minute, second = map(int, value.split(':'))
    if not (0 <= hour <= 71 and 0 <= minute < 60 and 0 <= second < 60):
        raise ValueError('bad GTFS time')
    return hour * 3600 + minute * 60 + second


def build_index(content, center=MUNICH, radius_m=3000):
    with zipfile.ZipFile(BytesIO(content)) as archive:
        if sum(info.file_size for info in archive.infolist()) > 1_500_000_000:
            raise ValueError('GTFS zip too big')
        zones = {r.get('agency_timezone') for r in rows(archive, 'agency.txt')}
        if zones != {'Europe/Berlin'}:
            raise ValueError('GTFS timezone is not Europe/Berlin?')
        stops = {}
        for row in rows(archive, 'stops.txt'):
            pos = point((row.get('stop_lon'), row.get('stop_lat')))
            if pos and distance_m(pos, center) <= radius_m:
                stops[row['stop_id']] = {'name': row['stop_name'], 'position': pos, 'events': []}
        routes = {
            r['route_id']: {'name': r.get('route_short_name') or r.get('route_long_name'), 'type': r['route_type']}
            for r in rows(archive, 'routes.txt')
        }
        used = set()
        for row in rows(archive, 'stop_times.txt'):
            stop = stops.get(row['stop_id'])
            if stop is None or row.get('pickup_type') == '1':
                continue
            try:
                departure = seconds(row.get('departure_time') or row.get('arrival_time', ''))
            except (ValueError, TypeError):
                continue
            stop['events'].append((row['trip_id'], departure, row.get('stop_headsign', '')))
            used.add(row['trip_id'])
        trips = {
            r['trip_id']: {'service': r['service_id'], 'route': r['route_id'], 'headsign': r.get('trip_headsign', '')}
            for r in rows(archive, 'trips.txt')
            if r['trip_id'] in used
        }
        services = {trip['service'] for trip in trips.values()}
        calendar = [r for r in rows(archive, 'calendar.txt', required=False) if r['service_id'] in services]
        exceptions = [r for r in rows(archive, 'calendar_dates.txt', required=False) if r['service_id'] in services]
        if not calendar and not exceptions:
            raise ValueError('GTFS has no calendar')
        if not stops:
            raise ValueError('no GTFS stops in this area')
        return {'stops': stops, 'routes': routes, 'trips': trips, 'calendar': calendar, 'exceptions': exceptions}


def active_services(index, day):
    date = day.strftime('%Y%m%d')
    weekday = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')[day.weekday()]
    active = {
        r['service_id'] for r in index['calendar'] if r['start_date'] <= date <= r['end_date'] and r.get(weekday) == '1'
    }
    for row in index['exceptions']:
        if row['date'] == date:
            if row['exception_type'] == '1':
                active.add(row['service_id'])
            elif row['exception_type'] == '2':
                active.discard(row['service_id'])
    return active


def service_instant(day, elapsed):
    # GTFS time = noon - 12h + HH:MM:SS (because of DST)
    #timestamps because of DST
    noon = datetime.combine(day, datetime.min.time().replace(hour=12), BERLIN)
    return datetime.fromtimestamp(noon.timestamp() - 43200 + elapsed, BERLIN)


def departures(index, now, fetched_at):
    today = now.astimezone(BERLIN).date()
    last_dates = [r['end_date'] for r in index['calendar']] + [r['date'] for r in index['exceptions']]
    first_dates = [r['start_date'] for r in index['calendar']] + [r['date'] for r in index['exceptions']]
    if not last_dates or today.strftime('%Y%m%d') > max(last_dates) or today.strftime('%Y%m%d') < min(first_dates):
        raise ValueError('GTFS has no service for today')
    services = []
    for delta in (-2, -1, 0, 1):
        day = today + timedelta(days=delta)
        services.append((service_instant(day, 0).timestamp(), active_services(index, day)))
    now_epoch = now.timestamp()
    items = []
    for stop_id, stop in index['stops'].items():
        if not stop['events']:
            continue
        upcoming = []
        for trip_id, elapsed, stop_headsign in stop['events']:
            trip = index['trips'].get(trip_id)
            if not trip:
                continue
            route = index['routes'].get(trip['route'])
            if not route:
                continue
            for start, active in services:
                if trip['service'] not in active:
                    continue
                departure = start + elapsed
                delta = departure - now_epoch
                if 0 <= delta <= 7200:
                    upcoming.append(
                        {
                            'at': iso(datetime.fromtimestamp(departure, BERLIN)),
                            'route': route['name'],
                            'headsign': stop_headsign or trip['headsign'],
                            'trip_id': trip_id,
                            'minutes': int(delta // 60),
                            'route_type': route['type'],
                            'realtime': False,
                        }
                    )
        upcoming.sort(key=lambda row: (row['at'], row['route'] or ''))
        selected = upcoming[:3]
        value = 'SCHEDULE ONLY / ' + (
            '; '.join(f'{r["route"]} {r["minutes"]}m → {r["headsign"]}' for r in selected)
            if selected
            else 'No departure in next 2h'
        )
        rail = any(int(r['route_type']) in (0, 1, 2, 100, 109, 400, 900) for r in selected)
        key = f'mvv:stop:{stop_id}'
        items.append(
            Observation(
                key,
                'rail' if rail else 'transit',
                stop['name'],
                'STOP',
                tuple(stop['position']),
                value,
                (key,),
                demo=False,
                fetched_at=fetched_at,
                expires_at=iso(now + timedelta(minutes=5)),
                kind='schedule',
                position_kind='stop',
                attribution=(ATTRIBUTIONS['mvv'],),
                details={
                    'departures': selected,
                    'computed_at': iso(now),
                    'timezone': 'Europe/Berlin',
                    'position_note': 'Published stop coordinate. Not a vehicle. No live delay or cancellation data.',
                },
            )
        )
    return items


class GTFSAdapter:
    def __init__(self, directory, center=MUNICH, radius_m=3000, lock=None):
        import asyncio

        self.directory = Path(directory)
        self.center, self.radius_m = center, radius_m
        self.lock = lock or asyncio.Lock()
        self.index = None
        self.fetched_at = None
        self.archive_mtime = None

    async def load(self, download, now):
        async with self.lock:
            return await self._load(download, now)

    async def _load(self, download, now):
        import asyncio

        path = self.directory / 'mvv-gtfs.zip'
        cached_at = timestamp(self.fetched_at)
        disk_epoch = path.stat().st_mtime if path.exists() else 0
        cached_epoch = max(cached_at.timestamp() if cached_at else 0, disk_epoch)
        due = now.timestamp() - cached_epoch > 86400
        if due:
            content = await download(GTFS_URL, max_bytes=50_000_000)
            index = await asyncio.to_thread(build_index, content, self.center, self.radius_m)
            await asyncio.to_thread(departures, index, now, iso(now))
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                await asyncio.to_thread(temporary.write_bytes, content)
                temporary.replace(path)
            except OSError:
                pass  # read only cache, whatever
            self.index, self.fetched_at = index, iso(now)
            self.archive_mtime = path.stat().st_mtime if path.exists() else 0
        elif self.index is None or self.archive_mtime != disk_epoch:
            self.index = await asyncio.to_thread(
                build_index, await asyncio.to_thread(path.read_bytes), self.center, self.radius_m
            )
            self.fetched_at = iso(datetime.fromtimestamp(disk_epoch, now.tzinfo))
            self.archive_mtime = disk_epoch
        items = await asyncio.to_thread(departures, self.index, now, self.fetched_at)
        return items, self.fetched_at
