import asyncio
from io import BytesIO
import json
import re

from PIL import Image, ImageOps

from .adapters import clean, inside, point
from .data import MUNICH, Observation, distance_m
from .camera_media import video_snapshot
from .camera_fallbacks import feratel_snapshot, provider_preview, siemens_snapshot, terra_archive
from .camera_regions import REGIONS, identity, parse_directory, provider_url, valid_image

PROVIDER = 'https://www.foto-webcam.eu'
TFL_DIRECTORY = 'https://api.tfl.gov.uk/Place/Type/JamCam'
CALTRANS_DIRECTORY = 'https://cwwp2.dot.ca.gov/data/d{district}/cctv/cctvStatusD{district:02}.json'
ATTRIBUTION = 'Foto-Webcam.eu / camera operators; linked embedding under camera-specific terms'
TFL_ATTRIBUTION = (
    'Powered by TfL Open Data; Contains OS data © Crown copyright and database rights 2016; '
    'Geomni UK Map data © and database rights 2019'
)
CALTRANS_ATTRIBUTION = 'Caltrans / California Department of Transportation; public CCTV data'


def camera_url(key):
    if re.fullmatch(r'webcam:foto-webcam:[a-z0-9-]{1,100}', key):
        return f'{PROVIDER}/webcam/{key.split(":")[-1]}/'
    if re.fullmatch(r'webcam:tfl:[0-9]{5}\.[0-9]{5}', key):
        return f'https://tfl.gov.uk/traffic/status/?camera={key.split(":")[-1]}'
    if re.fullmatch(r'webcam:caltrans:(?:[1-9]|1[0-2]):[0-9]{1,8}', key):
        return 'https://quickmap.dot.ca.gov/'
    return provider_url(key)


def camera_credit(key):
    camera_url(key)
    if key.startswith('webcam:tfl:'):
        return 'Powered by TfL Open Data'
    if key.startswith('webcam:caltrans:'):
        return 'Caltrans / dot.ca.gov'
    if key.startswith('webcam:foto-webcam:'):
        return 'www.foto-webcam.eu'
    if key.startswith('webcam:foto-net:'):
        return 'www.foto-webcam.net'
    if key.startswith('webcam:foto-org:'):
        return 'www.foto-webcam.org'
    if key.startswith('webcam:terra:'):
        return 'Terra-HD / camera operator'
    return identity(key)[1].credit


def foto_camera(row, fetched_at):
    slug = row.get('id', '')
    if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9-]{1,100}', slug):
        return None
    key = f'webcam:foto-webcam:{slug}'
    url = camera_url(key)
    position = point((row.get('longitude'), row.get('latitude')))
    if not position or row.get('offline') or row.get('hidden') or row.get('link') != url:
        return None
    interval = row.get('captureInterval')
    interval = interval if isinstance(interval, int) and 60 <= interval <= 86400 else None
    return Observation(
        key,
        'cameras',
        clean(row.get('name') or slug),
        'CAM',
        position,
        'Public webcam / periodic snapshots / '
        + (f'provider interval {interval // 60} min' if interval else 'capture cadence unknown'),
        (key,),
        demo=False,
        fetched_at=fetched_at,
        kind='listing',
        position_kind='listing',
        attribution=(ATTRIBUTION,),
        details={
            'url': url,
            'capture_interval_seconds': interval,
            'direction_degrees': row.get('direction'),
            'position_note': 'Camera coordinates published by provider; not image coverage.',
        },
    )


def parse_camera(content, slug, fetched_at):
    # old single page format
    marker = '{"id":' + json.dumps(slug) + ','
    offset = content.find(marker)
    if offset < 0:
        raise ValueError('webcam metadata unavailable')
    row, _ = json.JSONDecoder().raw_decode(content[offset:])
    return foto_camera(row, fetched_at)


def parse_foto_directory(content, fetched_at):
    marker = re.search(r'var\s+metadata\s*=\s*new Object\(\s*(\{)', content)
    if not marker:
        raise ValueError('webcam metadata unavailable')
    data, _ = json.JSONDecoder().raw_decode(content[marker.start(1) :])
    if not isinstance(data, dict) or not isinstance(data.get('cams'), list):
        raise ValueError('foto-webcam list broken')
    return [item for row in data['cams'] if isinstance(row, dict) and (item := foto_camera(row, fetched_at))]


def parse_tfl(data, fetched_at):
    if not isinstance(data, list):
        raise ValueError('TfL list broken')
    items = []
    for row in data:
        if not isinstance(row, dict):
            continue
        ident = str(row.get('id', '')).removeprefix('JamCams_')
        if not re.fullmatch(r'[0-9]{5}\.[0-9]{5}', ident):
            continue
        props = {p.get('key'): p.get('value') for p in row.get('additionalProperties', []) if isinstance(p, dict)}
        position = point((row.get('lon'), row.get('lat')))
        image_url = f'https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/{ident}.jpg'
        if not position or props.get('available') != 'true' or props.get('imageUrl') != image_url:
            continue
        key = f'webcam:tfl:{ident}'
        items.append(
            Observation(
                key,
                'cameras',
                clean(row.get('commonName') or ident),
                'CAM',
                position,
                'TfL traffic camera / periodic snapshot / capture cadence unknown',
                (key,),
                demo=False,
                fetched_at=fetched_at,
                kind='listing',
                position_kind='listing',
                attribution=(TFL_ATTRIBUTION,),
                details={
                    'url': camera_url(key),
                    'image_url': image_url,
                    'capture_interval_seconds': None,
                    'direction': clean(props.get('view')),
                    'position_note': 'Published camera location; not image coverage.',
                },
            )
        )
    return items


def parse_caltrans(data, district, fetched_at):
    if not isinstance(data, dict) or not isinstance(data.get('data'), list):
        raise ValueError('caltrans list broken')
    items = []
    for entry in data['data']:
        if not isinstance(entry, dict) or not isinstance(entry.get('cctv'), dict):
            continue
        row = entry['cctv']
        location = row.get('location') or {}
        static = (row.get('imageData') or {}).get('static') or {}
        ident = str(row.get('index', ''))
        position = point((location.get('longitude'), location.get('latitude')))
        image_url = static.get('currentImageURL', '')
        if (
            not re.fullmatch(r'[0-9]{1,8}', ident)
            or not position
            or row.get('inService') != 'true'
            or str(location.get('district')) != str(district)
            or not caltrans_image_url(image_url, district)
        ):
            continue
        key = f'webcam:caltrans:{district}:{ident}'
        items.append(
            Observation(
                key,
                'cameras',
                clean(location.get('locationName') or ident),
                'CAM',
                position,
                'Caltrans highway camera / periodic snapshot / capture cadence unknown',
                (key,),
                demo=False,
                fetched_at=fetched_at,
                kind='listing',
                position_kind='listing',
                attribution=(CALTRANS_ATTRIBUTION,),
                details={
                    'url': camera_url(key),
                    'image_url': image_url,
                    'capture_interval_seconds': None,
                    'direction': clean(location.get('direction')),
                    'district': district,
                    'position_note': 'Published camera location; directory recordTimestamp is not image capture time.',
                },
            )
        )
    return items


def caltrans_image_url(url, district):
    return isinstance(url, str) and bool(
        re.fullmatch(
            rf'https://cwwp2\.dot\.ca\.gov/data/d{district}/cctv/image/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+\.jpg', url
        )
    )


async def catalog(feeds, now, center=MUNICH):
    async def foto():
        raw = await feeds.shared_download(PROVIDER + '/', 3600, now=now, max_bytes=2_000_000)
        return parse_foto_directory(raw.content.decode('utf-8'), raw.fetched_at)

    async def tfl():
        raw = await feeds.shared_download(TFL_DIRECTORY, 3600, now=now, max_bytes=4_000_000)
        return parse_tfl(json.loads(raw.content), raw.fetched_at)

    async def caltrans(district):
        url = CALTRANS_DIRECTORY.format(district=district)
        raw = await feeds.shared_download(url, 3600, now=now, max_bytes=8_000_000)
        return parse_caltrans(json.loads(raw.content), district, raw.fetched_at)

    async def regional(provider, region):
        raw = await feeds.shared_download(region.url, region.ttl, now=now, max_bytes=4_000_000)
        return parse_directory(provider, raw.content, raw.fetched_at)

    jobs = [foto()]
    jobs.extend(regional(provider, region) for provider, region in REGIONS.items() if inside(center, region.bounds))
    if distance_m(center, (-0.1276, 51.5072)) <= 110000:
        jobs.append(tfl())
    # 12 districts
    if inside(center, (-125, 32, -113, 43)):
        jobs.extend(caltrans(d) for d in range(1, 13))
    rows = await asyncio.gather(*jobs, return_exceptions=True)
    items = {item.id: item for row in rows if not isinstance(row, Exception) for item in row}
    failures = sum(isinstance(row, Exception) for row in rows)
    if not items and failures:
        raise ValueError('webcam list unavailable')
    return list(items.values()), failures


def nearby(items, center):
    return tuple(
        sorted(
            (i for i in items if distance_m(center, i.position) <= 50000), key=lambda i: distance_m(center, i.position)
        )
    )


async def snapshot(feeds, item, now):
    camera_url(item.id)
    provider = item.id.split(':')[1]
    if provider.startswith('feratel-') and provider in REGIONS:
        return await feratel_snapshot(feeds, item, now)
    if provider == 'siemens':
        return await siemens_snapshot(feeds, item, now)
    if provider in REGIONS and (provider.startswith('wetter-') or provider == 'michaeli'):
        try:
            return await video_snapshot(feeds, item, now)
        except Exception:
            return await provider_preview(feeds, item, now)
    if item.id.startswith('webcam:foto-webcam:'):
        url = camera_url(item.id) + 'current/720.jpg'
    elif item.id.startswith('webcam:tfl:'):
        url = f'https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/{item.id.split(":")[-1]}.jpg'
    elif item.id.startswith('webcam:caltrans:'):
        url = item.details.get('image_url')
        if not caltrans_image_url(url, int(item.id.split(':')[2])):
            raise ValueError('weird image url')
    else:
        provider, region = identity(item.id)
        if provider == 'sg':
            # singapore urls change, look it up again
            raw = await feeds.shared_download(region.url, region.ttl, now=now, max_bytes=4_000_000)
            current = next((i for i in parse_directory(provider, raw.content, raw.fetched_at) if i.id == item.id), None)
            if current is None:
                raise ValueError('camera not found')
            url = current.details['image_url']
        else:
            url = item.details.get('image_url')
        if not valid_image(provider, url):
            raise ValueError('weird image url')
    try:
        raw = await feeds.shared_download(url, 5, now=now, max_bytes=2_000_000, timeout=12)
        return await asyncio.to_thread(decode_snapshot, raw.content), raw.fetched_at
    except Exception:
        if provider != 'terra':
            raise
        return await terra_archive(feeds, item, now)


def decode_snapshot(content):
    with Image.open(BytesIO(content)) as image:
        if image.format not in ('JPEG', 'WEBP') or image.width * image.height > 8_000_000:
            raise ValueError('bad webcam image')
        image.load()
        return ImageOps.exif_transpose(image).convert('RGB')


def image_box(size, image):
    fitted = ImageOps.contain(image, (size[0], size[1] - 96))
    x, y = (size[0] - fitted.width) // 2, 36 + (size[1] - 96 - fitted.height) // 2
    return fitted, (x, y, x + fitted.width, y + fitted.height)
