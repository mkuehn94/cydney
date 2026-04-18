import asyncio
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
from pprint import pprint
import logging
import math
import os
from pathlib import Path
from urllib.parse import quote
import shutil
import time

import httpx

from . import cameras
from .adapters import (
    ATTRIBUTIONS,
    POI_BOUNDS,
    inside,
    iso,
    point,
    osm_query,
    parse_aircraft,
    parse_atp,
    parse_dwd,
    parse_nina,
    parse_osm,
    parse_works,
    place_value,
    timestamp,
    works_dates,
)
from .data import MUNICH, Observation, distance_m
from .ground import parse_departures, parse_gbfs
from .gtfs import GTFSAdapter
from .places import reconcile
from .provenance import real_record
from .transit import parse_vehicle_positions
from .timing import TIMED_LAYERS, timing_status
from .viewport import DEFAULT_VIEW, MUNICH_CITY, MVV_REGION, Viewport, contains, intersects

log = logging.getLogger(__name__)
UTC = timezone.utc


@dataclass(frozen=True)
class Spec:
    id: str
    label: str
    layers: tuple[str, ...]
    interval: int
    stale_after: int
    dynamic: bool = False
    url: str = ''


SPECS = (
    Spec(
        'osm',
        'OpenStreetMap viewport POIs',
        ('places', 'food', 'health', 'amenities', 'parking', 'charging', 'access', 'nature', 'heritage'),
        86400,
        172800,
        url='https://overpass-api.de/api/interpreter',
    ),
    Spec('webcams', 'Public webcams', ('cameras',), 3600, 86400),
    # Spec('autobahn', 'Autobahn webcams', ('cameras',), 3600, 86400),  # always empty??
    Spec('atp', 'AllThePlaces regional import', ('places', 'food', 'health'), 300, 14 * 86400),
    Spec(
        'works', 'Munich roadworks', ('works',), 1800, 7200, url='https://geoportal.muenchen.de/geoserver/mor_wfs/ows'
    ),
    Spec(
        'dwd',
        'DWD München-Stadt observations',
        ('weather',),
        600,
        7200,
        True,
        'https://opendata.dwd.de/weather/weather_reports/poi/10865-BEOB.csv',
    ),
    Spec(
        'nina',
        'BBK / NINA Munich alerts',
        ('alerts',),
        120,
        600,
        True,
        'https://warnung.bund.de/api31/dashboard/091620000000.json',
    ),
    Spec('aircraft', 'ADSB.lol aircraft', ('flights', 'traffic'), 30, 90, True),
    Spec('mvv', 'MVV timetables', ('rail', 'transit'), 60, 172800),
    Spec('mvg', 'MVG departures', ('rail', 'transit'), 30, 90, True),
    Spec('transit_gps', 'Operator GTFS-RT vehicle positions', ('rail', 'transit'), 30, 90, True),
    Spec(
        'myradl',
        'MyRadl bikes',
        ('bikes',),
        60,
        180,
        True,
        'https://gbfs.nextbike.net/maps/gbfs/v2/nextbike_ml/en/free_bike_status.json',
    ),
    Spec(
        'dott',
        'Dott scooters / bikes',
        ('bikes',),
        300,
        600,
        True,
        'https://gbfs.api.ridedott.com/public/v2/munich/free_bike_status.json',
    ),
)


@dataclass(frozen=True)
class Fetch:
    content: bytes
    fetched_at: str


def retry_seconds(error, now, default):
    if isinstance(error, httpx.HTTPStatusError):
        value = error.response.headers.get('retry-after', '')
        if value.isdigit():
            return max(default, int(value))
        # return max(default, (parsedate_to_datetime(value) - now).total_seconds())
    return default


class FeedStore:
    def __init__(self, client, directory=None, enabled=True, *, view=DEFAULT_VIEW, root=None):
        self.client, self.enabled, self.view = client, enabled, view
        self.directory = Path(directory or os.environ.get('CYD_FEED_CACHE', '.cache/feeds'))
        self.root = root or self
        if root is None:
            self.transit_url = os.environ.get('CYD_TRANSIT_VP_URL', '').strip()
            self.transit_attribution = os.environ.get('CYD_TRANSIT_VP_ATTRIBUTION', '').strip()
            self.transit_name = os.environ.get('CYD_TRANSIT_VP_NAME', '').strip() or 'Configured transit operator'
            raw_bounds = json.loads(os.environ.get('CYD_TRANSIT_VP_BOUNDS', json.dumps(MVV_REGION)))
            if (
                not isinstance(raw_bounds, list)
                or len(raw_bounds) != 4
                or not point(raw_bounds[:2])
                or not point(raw_bounds[2:])
                or raw_bounds[1] >= raw_bounds[3]
            ):
                raise ValueError('CYD_TRANSIT_VP_BOUNDS must be [west,south,east,north]')
            self.transit_bounds = tuple(raw_bounds)
        self.records, self.tasks = {}, []
        self.webcam_warning = None
        self.generation = 0
        self.last_access = time.monotonic()
        self.wake = asyncio.Event()
        self.board_wake = asyncio.Event()
        if root is None:
            self.areas = OrderedDict([(view.key, self)])
            self.consumers, self.focuses = {}, {}
            self.downloads = asyncio.Semaphore(5)
            self.osm_slots = asyncio.Semaphore(2)
            self.resources, self.resource_locks, self.provider_cooldowns = {}, {}, {}
            self.retired = set()
            self.gtfs_lock = asyncio.Lock()
        self.gtfs = GTFSAdapter(self.root.directory, view.center, max(3000, view.radius_m), self.root.gtfs_lock)
        self.user_agent = os.environ.get(
            'CYD_FEED_USER_AGENT', 'Cydney/0.5 (local viewport awareness display; low-volume research)'
        )
        for spec in SPECS:
            record = {'items': (), 'fetched_at': None, 'last_attempt': None, 'error': None, 'retry_at': None}
            self.records[spec.id] = record
            path = self.directory / f'{spec.id}.json'
            if not path.exists():
                continue
            try:
                saved = json.loads(path.read_text())
                if saved.get('version') != 1:
                    continue
                items = tuple(
                    Observation(
                        **{
                            **item,
                            'position': tuple(item['position']),
                            'sources': tuple(item['sources']),
                            'attribution': tuple(item.get('attribution', [])),
                        }
                    )
                    for item in saved['items']
                )
                if spec.id in ('osm', 'atp'):
                    items = tuple(
                        replace(item, value=place_value(item.details['tags'])) if item.details.get('tags') else item
                        for item in items
                    )
                if spec.id == 'works':
                    items = tuple(
                        replace(
                            item,
                            starts_at=works_dates(item.details.get('properties', {}))[0],
                            ends_at=works_dates(item.details.get('properties', {}))[1],
                        )
                        for item in items
                    )
                if spec.id == 'transit_gps':
                    items = tuple(
                        i
                        for i in items
                        if i.details.get('provider') == self.root.transit_name
                        and i.details.get('configuration_id') == self.transit_configuration_id
                    )
                rejected = sum(not real_record(item, spec.id) for item in items)
                if rejected:
                    record.update(error='Synthetic/unverified cached records rejected', rejected_count=rejected)
                else:
                    record.update(items=items, fetched_at=saved['fetched_at'])
            except (OSError, ValueError, KeyError, TypeError):
                pass

    @property
    def transit_configuration_id(self):
        import hashlib

        return hashlib.sha256((self.root.transit_url + self.root.transit_attribution).encode()).hexdigest()

    def coverage_note(self, spec):
        bounds = self.view.bounds
        if spec.id == 'transit_gps':
            if not self.root.transit_url or not self.root.transit_attribution:
                return 'No verified local vehicle-position feed configured; departures are not GPS.'
            if not intersects(bounds, self.root.transit_bounds):
                return 'Outside configured transit vehicle-position coverage.'
        if spec.id in ('mvg', 'mvv', 'myradl') and not intersects(bounds, MVV_REGION):
            return 'Provider covers Munich / MVV region only.'
        if spec.id in ('works', 'nina', 'dott') and not intersects(bounds, MUNICH_CITY):
            return 'Provider covers Munich only.'
        if spec.id == 'atp' and not intersects(bounds, POI_BOUNDS):
            return 'Loaded ATP import covers central Munich only; import another region before claiming coverage.'
        if spec.id == 'dwd' and distance_m(self.view.center, MUNICH) > 50000:
            return 'This DWD adapter is München-Stadt station only.'
        return None

    def active(self):
        if time.monotonic() - self.last_access > 60:
            return False
        return self.view.key in self.root.consumers.values() or (self is self.root and not self.root.consumers)

    def follow(self, consumer, center, zoom, size):
        root = self.root
        area = root.areas.get(root.consumers.get(consumer))
        if area is None or not area.view.covers(center, zoom, size):
            area = next(
                (candidate for candidate in reversed(root.areas.values()) if candidate.view.covers(center, zoom, size)),
                None,
            )
        if area is None:
            view = Viewport.camera(center, zoom, size)
            area = root.areas.get(view.key)
            if area is None:
                neighbors = tuple(root.areas.values())
                if len(root.areas) >= 8:
                    victim = None
                    for key, candidate in root.areas.items():
                        if candidate is root:
                            continue
                        users = {owner for owner, area_key in root.consumers.items() if area_key == key}
                        if not candidate.active() or users == {consumer}:
                            victim = key
                            break
                    if victim is None:
                        raise ValueError('too many areas loaded, try again later')
                    retired = root.areas.pop(victim)
                    root.stop_area(retired)
                area = FeedStore(root.client, root.directory / 'areas' / view.key, root.enabled, view=view, root=root)
                area.seed_neighbors(neighbors)
                for previous in reversed(neighbors):
                    if distance_m(previous.gtfs.center, view.center) + view.radius_m <= previous.gtfs.radius_m:
                        area.gtfs = previous.gtfs
                root.areas[view.key] = area
                root.prune_area_cache()
        previous_key = root.consumers.get(consumer)
        old_focuses = set(area.board_centers())
        root.consumers[consumer] = area.view.key
        root.focuses[consumer] = (Viewport.focus(center), time.monotonic())
        if set(area.board_centers()) != old_focuses:
            area.board_wake.set()
        if previous_key != area.view.key and previous_key in root.areas and previous_key not in root.consumers.values():
            root.stop_area(root.areas[previous_key])
        if root.enabled and not area.tasks:
            area.tasks = [
                asyncio.create_task(area.poll(spec, debounce=0.8), name=f'{area.view.key}:{spec.id}') for spec in SPECS
            ]
        area.last_access = time.monotonic()
        area.wake.set()
        root.areas.move_to_end(area.view.key)
        active_gtfs = {id(root.areas[key].gtfs) for key in root.consumers.values() if key in root.areas}
        for cached in root.areas.values():
            if id(cached.gtfs) not in active_gtfs:
                cached.gtfs.index = None
        return area

    def seed_neighbors(self, areas):
        for spec in SPECS:
            if self.records[spec.id]['fetched_at'] or self.coverage_note(spec):
                continue
            regional = spec.id in ('dwd', 'nina')
            neighbors = [
                a
                for a in areas
                if a.records[spec.id]['fetched_at']
                and not a.coverage_note(spec)
                and (regional or intersects(a.view.bounds, self.view.bounds))
            ]
            neighbors.sort(key=lambda a: a.records[spec.id]['fetched_at'], reverse=True)
            enclosing = next(
                (
                    a
                    for a in neighbors
                    if not a.records[spec.id].get('partial')
                    and (regional or contains(a.view.bounds, self.view.bounds))
                    and (spec.id != 'osm' or a.view.detail == 'detail' or self.view.detail == 'overview')
                ),
                None,
            )
            if enclosing:
                self.records[spec.id] = dict(enclosing.records[spec.id])
            elif neighbors:
                items = {}
                for neighbor in neighbors:
                    for item in neighbor.records[spec.id]['items']:
                        if inside(item.position, self.view.bounds):
                            items.setdefault(item.id, item)
                self.records[spec.id] = {
                    **neighbors[0].records[spec.id],
                    'items': tuple(items.values()),
                    'fetched_at': min(a.records[spec.id]['fetched_at'] for a in neighbors),
                    'partial': True,
                }

    def board_centers(self):
        now = time.monotonic()
        centers = [
            center
            for consumer, (center, accessed) in sorted(
                self.root.focuses.items(), key=lambda pair: pair[1][1], reverse=True
            )
            if self.root.consumers.get(consumer) == self.view.key and now - accessed < 60
        ]
        return list(dict.fromkeys(centers))[:3] or [self.view.center]

    def stop_area(self, area):
        for task in area.tasks:
            task.cancel()
            self.root.retired.add(task)
            task.add_done_callback(self.root.retired.discard)
        area.tasks = []

    def release(self, consumer):
        key = self.root.consumers.pop(consumer, None)
        self.root.focuses.pop(consumer, None)
        if key in self.root.areas and key not in self.root.consumers.values():
            self.stop_area(self.root.areas[key])

    def prune_area_cache(self):
        path = self.root.directory / 'areas'
        try:
            directories = sorted(
                (p for p in path.glob('view-*') if p.is_dir() and not p.is_symlink()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for directory in directories[16:]:
                if directory.name not in self.root.areas:
                    shutil.rmtree(directory)
        except OSError:
            pass

    async def start(self):
        if self.enabled and not self.tasks:
            self.tasks = [asyncio.create_task(self.poll(spec, debounce=0.8), name=f'feed:{spec.id}') for spec in SPECS]

    async def close(self):
        tasks = [t for area in self.root.areas.values() for t in area.tasks] + list(self.root.retired)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def download(self, url, *, params=None, data=None, max_bytes=15_000_000, headers=None, timeout=90):
        host = httpx.URL(url).host
        cooldown = self.root.provider_cooldowns.get(host)
        if cooldown and cooldown[0] > time.monotonic():
            raise cooldown[1]
        # print('GET', url, params)
        async with self.root.downloads:
            async with self.client.stream(
                'POST' if data is not None else 'GET',
                url,
                params=params,
                data=data,
                headers={'User-Agent': self.user_agent, 'Accept-Encoding': 'gzip', **(headers or {})},
                timeout=timeout,
                follow_redirects=True,
            ) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as error:
                    if response.status_code == 429 or response.headers.get('retry-after'):
                        delay = retry_seconds(error, datetime.now(UTC), 60)
                        self.root.provider_cooldowns[host] = (time.monotonic() + delay, error)
                        log.info('%s rate limited, waiting %ds', host, delay)
                    raise
                result = bytearray()
                async for chunk in response.aiter_bytes():
                    result.extend(chunk)
                    if len(result) > max_bytes:
                        raise ValueError('response too big')
                return bytes(result)

    async def shared(self, key, seconds, producer):
        root = self.root
        async with root.resource_locks.setdefault(key, asyncio.Lock()):
            saved = root.resources.get(key)
            if saved and saved[0] > time.monotonic():
                if isinstance(saved[1], Exception):
                    raise saved[1]
                return saved[1]
            try:
                value = await producer()
            except Exception as error:
                delay = retry_seconds(error, datetime.now(UTC), min(seconds, 300))
                root.resources[key] = (time.monotonic() + delay, error)
                raise
            root.resources[key] = (time.monotonic() + seconds, value)
            if len(root.resources) > 256:
                expired = [k for k, (expiry, _) in root.resources.items() if expiry < time.monotonic()]
                for old in expired:
                    if not root.resource_locks[old].locked():
                        root.resources.pop(old, None)
                        root.resource_locks.pop(old, None)
            return value

    async def shared_download(self, url, ttl, *, now=None, **kwargs):
        key = ('http', url, json.dumps(kwargs.get('params', {}), sort_keys=True))

        async def receive():
            content = await self.download(url, **kwargs)
            return Fetch(content, iso(now or datetime.now(UTC)))

        return await self.shared(key, ttl, receive)

    async def load(self, spec, now):
        bounds, center = self.view.bounds, self.view.center
        fetched_at = iso(now)
        if spec.id == 'webcams':
            items, failures = await cameras.catalog(self, now, center)
            self.webcam_warning = (
                f'{failures} webcam provider/district feeds unavailable; available cameras retained'
                if failures
                else None
            )
            return [i for i in items if inside(i.position, bounds)], min(
                (i.fetched_at for i in items), default=iso(now)
            )
        if spec.id == 'mvv':
            return await self.shared(
                ('gtfs', self.gtfs.center, self.gtfs.radius_m), 30, lambda: self.gtfs.load(self.download, now)
            )
        if spec.id == 'atp':
            path = Path(os.environ.get('CYD_ATP_FILE', '.cache/imports/alltheplaces.geojson'))
            if not path.exists():
                raise FileNotFoundError('Regional ATP file missing; use python -m server.import_atp')
            if path.stat().st_size > 25_000_000:
                raise ValueError('ATP import > 25 MB')
            content = await asyncio.to_thread(path.read_bytes)
            data = json.loads(content)
            if data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'), list):
                raise ValueError('ATP import is not a FeatureCollection')
            fetched = timestamp(data.get('cydney', {}).get('imported_at')) or datetime.fromtimestamp(
                path.stat().st_mtime, UTC
            )
            items = parse_atp(data['features'], fetched, bounds)
            snapshot_date = data.get('cydney', {}).get('snapshot_date')
            return [replace(item, details={**item.details, 'snapshot_date': snapshot_date}) for item in items], iso(
                fetched
            )
        if spec.id == 'osm':
            query = osm_query(bounds, self.view.detail)
            async with self.root.osm_slots:
                try:
                    content = await self.download(spec.url, data={'data': query})
                except (httpx.TimeoutException, httpx.HTTPStatusError) as error:
                    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500:
                        raise
                    content = await self.download('https://overpass.kumi.systems/api/interpreter', data={'data': query})
            items = await asyncio.to_thread(parse_osm, json.loads(content), now, bounds)
        elif spec.id == 'works':
            west, south, east, north = bounds
            content = await self.download(
                spec.url,
                params={
                    'service': 'WFS',
                    'version': '1.0.0',
                    'request': 'GetFeature',
                    'typeName': 'mor_wfs:baustellen_opendata',
                    'outputFormat': 'application/json',
                    'srsName': 'EPSG:4326',
                    'bbox': f'{west},{south},{east},{north},EPSG:4326',
                    'maxFeatures': '10000',
                },
            )
            items = parse_works(json.loads(content), now, bounds)
        elif spec.id == 'mvg':
            url = 'https://www.mvg.de/api/bgw-pt/v3/stations/nearby'
            stations = {}
            for focus in self.board_centers():
                content = await self.shared_download(
                    url, 3600, now=now, params={'latitude': round(focus[1], 5), 'longitude': round(focus[0], 5)}
                )
                nearby = json.loads(content.content)
                if not isinstance(nearby, list):
                    raise ValueError('bad MVG station list')
                nearby = [
                    station
                    for station in nearby
                    if (p := point((station.get('longitude'), station.get('latitude')))) and inside(p, bounds)
                ]
                nearby.sort(key=lambda station: distance_m(focus, (station['longitude'], station['latitude'])))
                stations.update((station['globalId'], station) for station in nearby[:3])

            async def board(station):
                raw = await self.shared_download(
                    'https://www.mvg.de/api/bgw-pt/v3/departures',
                    30,
                    now=now,
                    params={'globalId': station['globalId'], 'limit': 10},
                )
                return replace(parse_departures(station, json.loads(raw.content), now), fetched_at=raw.fetched_at)

            items = await asyncio.gather(*(board(station) for station in stations.values()))
            return items, min((item.fetched_at for item in items), default=iso(now))
        elif spec.id == 'transit_gps':
            token = os.environ.get('CYD_TRANSIT_VP_TOKEN', '')
            headers = {'Authorization': f'Bearer {token}'} if token else {}
            raw = await self.shared_download(self.root.transit_url, 30, now=now, headers=headers)
            fetched_at = raw.fetched_at
            items = parse_vehicle_positions(
                raw.content, now, bounds, self.root.transit_name, self.root.transit_attribution
            )
            items = [
                replace(i, details={**i.details, 'configuration_id': self.transit_configuration_id}) for i in items
            ]
        elif spec.id == 'dwd':
            content = await self.shared_download(spec.url, 600, now=now, max_bytes=100_000)
            fetched_at = content.fetched_at
            items = parse_dwd(content.content.decode('utf-8-sig'), now)
        elif spec.id == 'nina':
            content = await self.shared_download(spec.url, 120, now=now)
            fetched_at = content.fetched_at
            dashboard = json.loads(content.content)
            if not isinstance(dashboard, list):
                raise ValueError('bad NINA response')

            async def warning_detail(row):
                try:
                    raw = await self.shared_download(
                        'https://warnung.bund.de/api31/warnings/' + quote(str(row['id']), safe='') + '.json',
                        120,
                        now=now,
                    )
                    detail = json.loads(raw.content)
                    return str(row['id']), detail if isinstance(detail, dict) else {}
                except (httpx.HTTPError, ValueError, KeyError):
                    return str(row.get('id', '')), {}

            details = dict(await asyncio.gather(*(warning_detail(row) for row in dashboard[:50])))
            items = parse_nina(dashboard, now, details)
        elif spec.id == 'aircraft':
            radius = min(100, max(5, math.ceil(self.view.radius_m / 1852)))
            url = f'https://api.adsb.lol/v2/point/{center[1]:.5f}/{center[0]:.5f}/{radius}'
            content = await self.shared_download(url, 30, now=now)
            fetched_at = content.fetched_at
            items = parse_aircraft(json.loads(content.content), now, center, radius)
        else:
            content = await self.shared_download(spec.url, spec.interval, now=now)
            fetched_at = content.fetched_at
            items = parse_gbfs(json.loads(content.content), now, spec.id, bounds)
        return [replace(item, fetched_at=fetched_at) for item in items], fetched_at

    async def refresh(self, spec, now=None):
        if self.coverage_note(spec):
            return
        now = now or datetime.now(UTC)
        old = self.records[spec.id]
        self.records[spec.id] = {**old, 'last_attempt': iso(now)}
        try:
            items, fetched_at = await self.load(spec, now)
            ids = [item.id for item in items]
            if len(ids) != len(set(ids)) or any(not real_record(item, spec.id) for item in items):
                raise ValueError('duplicate ids or demo data in live source')
            record = {
                'items': tuple(items),
                'fetched_at': fetched_at,
                'last_attempt': iso(now),
                'error': None,
                'retry_at': None,
                'warning': self.webcam_warning if spec.id == 'webcams' else None,
            }
            self.records[spec.id] = record
            try:
                saved = {'version': 1, 'fetched_at': fetched_at, 'items': [asdict(item) for item in items]}
                # pprint(saved)
                await asyncio.to_thread(self.persist, spec.id, saved)
            except OSError:
                log.warning('cant write feed cache: %s', spec.id)
        except Exception as error:
            # Keep going if one provider fails
            retry = retry_seconds(error, now, min(spec.interval, 300))
            if isinstance(error, httpx.HTTPStatusError):
                message = f'Provider HTTP {error.response.status_code}'
            elif isinstance(error, httpx.HTTPError):
                message = type(error).__name__
            else:
                message = str(error)[:180] or type(error).__name__
            self.records[spec.id] = {
                **old,
                'last_attempt': iso(now),
                'error': message,
                'retry_at': iso(datetime.fromtimestamp(now.timestamp() + retry, UTC)),
            }
            log.warning('Feed %s [%s]: %s', spec.id, self.view.key, message)
        self.generation += 1

    def persist(self, source_id, saved):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f'{source_id}.json'
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(saved, ensure_ascii=False))
        temp.replace(path)

    async def poll(self, spec, debounce=0):
        if self.coverage_note(spec):
            return
        await asyncio.sleep(debounce)
        fetched = timestamp(self.records[spec.id]['fetched_at'])
        if fetched and not self.records[spec.id].get('partial') and spec.id in ('osm', 'works', 'dwd'):
            delay = max(0, spec.interval - (time.time() - fetched.timestamp()))
            if delay:
                await asyncio.sleep(delay)
        while True:
            while not self.active():
                self.wake.clear()
                await self.wake.wait()
            if spec.id == 'mvg':
                self.board_wake.clear()
            await self.refresh(spec)
            retry = timestamp(self.records[spec.id]['retry_at'])
            delay = max(1, retry.timestamp() - time.time()) if retry else spec.interval
            if spec.id == 'mvg' and not retry:
                try:
                    await asyncio.wait_for(self.board_wake.wait(), timeout=delay)
                    await asyncio.sleep(0.8)  # wait until panning is done
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(delay)

    def snapshot(self, now=None):
        now = now or datetime.now(UTC)
        items, statuses = [], []
        for spec in SPECS:
            record = self.records[spec.id]
            fetched = timestamp(record['fetched_at'])
            note = self.coverage_note(spec)
            rejected = sum(not real_record(item, spec.id) for item in record['items'])
            error = record['error'] or ('Synthetic/unverified records blocked' if rejected else None)
            stale = bool(error) or not self.enabled or not fetched or (now - fetched).total_seconds() > spec.stale_after
            available, expired_count = [], 0
            for item in record['items'] if not note and not rejected else ():
                expires = timestamp(item.expires_at)
                if expires and expires <= now:
                    expired_count += 1
                    continue
                if item.layer in TIMED_LAYERS and timing_status(item, now) == 'ended':
                    expired_count += 1
                    continue
                if spec.id not in ('dwd', 'nina') and not inside(item.position, self.view.bounds):
                    continue
                observed = timestamp(item.observed_at)
                item_stale = stale or bool(observed and (now - observed).total_seconds() > spec.stale_after)
                computed = timestamp(item.details.get('computed_at')) if item.kind == 'schedule' else None
                age = max(0, int((now - (observed or computed or fetched or now)).total_seconds() // 60))
                available.append(
                    replace(
                        item,
                        stale=item_stale,
                        age_minutes=age,
                        details={**item.details, 'timing': timing_status(item, now)}
                        if item.layer in TIMED_LAYERS
                        else item.details,
                    )
                )
            items.extend(available)
            expired = bool(record['items']) and expired_count == len(record['items'])
            stale = stale or expired or bool(available and all(item.stale for item in available))
            if spec.id == 'atp':
                unconfigured = not fetched and (record['error'] or '').startswith('Regional ATP file missing')
            elif spec.id == 'transit_gps':
                unconfigured = not self.root.transit_url or not self.root.transit_attribution
            else:
                unconfigured = False

            if not self.enabled:
                status = 'offline'
            elif unconfigured:
                status = 'not configured'
            elif note:
                status = 'outside coverage'
            elif error:
                status = 'error'
            elif not fetched or record.get('partial'):
                status = 'pending'
            elif stale:
                status = 'stale'
            else:
                status = 'ready'

            if spec.id == 'transit_gps':
                attribution = self.root.transit_attribution or 'Operator attribution required before connecting'
            else:
                attribution = ATTRIBUTIONS[spec.id]
            statuses.append(
                {
                    'id': spec.id,
                    'label': spec.label,
                    'layers': spec.layers,
                    'status': status,
                    'coverage_note': note,
                    'warning': record.get('warning'),
                    'dynamic': spec.dynamic,
                    'fetched_at': record['fetched_at'],
                    'last_attempt': record['last_attempt'],
                    'retry_at': record['retry_at'],
                    'error': error,
                    'rejected_count': max(rejected, record.get('rejected_count', 0)),
                    'count': len(available),
                    'cached_count': len(record['items']),
                    'bounds': self.view.bounds,
                    'partial_coverage': bool(record.get('partial')),
                    'stale': stale,
                    'all_cached_records_expired': expired,
                    'attribution': attribution,
                    'refresh_seconds': spec.interval,
                    'stale_after_seconds': spec.stale_after,
                }
            )
        # ages update every 5s
        token = (self.view.key, self.generation, int(now.timestamp() // 5))
        live_stations = {i.details['station_global_id'] for i in items if i.id.startswith('mvg:') and not i.stale}
        items = [
            i
            for i in items
            if not (
                i.id.startswith('mvv:stop:')
                and ':'.join(i.id.removeprefix('mvv:stop:').split(':')[:3]) in live_stations
            )
        ]
        return token, reconcile(items), tuple(statuses)

    def manifest(self):
        _, items, sources = self.snapshot()
        return {
            'mode': 'live',
            'real_data_only': True,
            'synthetic_observations': 0,
            'blocked_records': sum(s['rejected_count'] for s in sources),
            'location': {
                'name': 'Configured Munich home; camera moves independently',
                'position': MUNICH,
                'gps': False,
            },
            'viewport': self.view.describe(),
            'bounds': {'pois': self.view.bounds, 'roadworks': self.view.bounds},
            'observations': len(items),
            'connected_sources': sum(s['status'] == 'ready' for s in sources),
            'live_sources': sum(s['status'] == 'ready' and s['dynamic'] for s in sources),
            'sources': sources,
            'limitations': [
                'Transit VehiclePositions require a configured verified operator feed; live departure boards are stops',
                'Operator vehicle-position source is configured; check source health'
                if self.root.transit_url and self.root.transit_attribution
                else 'No verified in-motion Munich bus/train GPS source is connected; departure pins are stops',
                'Map zoom 12–15 hides stops; zoom 16–18 shows grouped station details',
                'Shared vehicles are available/parked inventory, never rider tracking',
                'Provider coverage follows viewport; outside coverage is not an empty successful feed',
                'Overview zoom imports selected important POIs; zoom in for dense details',
                'ATP requires regional snapshot import; no police radio, private cameras or live satellite imagery',
            ],
        }
