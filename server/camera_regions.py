from dataclasses import dataclass
import json
import math
import re
import xml.etree.ElementTree as ET
from html import unescape
from urllib.parse import parse_qs, urlsplit

from .adapters import clean, point
from .data import Observation


@dataclass(frozen=True)
class Region:
    url: str
    bounds: tuple
    page: str
    credit: str
    image_pattern: str
    id_pattern: str = r'[A-Za-z0-9_-]{1,100}'
    ttl: int = 3600
    link_only: bool = False


REGIONS = {
    'bayern': Region(
        'https://map.bayerninfo.de/cam/listOfWebcamsV3.json',
        (9, 47, 14, 51),
        'https://www.bayerninfo.de/de/karte',
        'BayernInfo / Bayerische Staatsbauverwaltung',
        r'https://map\.bayerninfo\.de/cam/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.(?:jpg|jpeg)',
        id_pattern=r'[0-9]{1,10}',
    ),
    'terra': Region(
        'https://terra-hd.com/map/map.js',
        (5, 45, 17, 56),
        'https://terra-hd.com/',
        'Terra-HD / camera operator; linked embedding, full image preserved',
        r'https://terra-hd\.com/[a-z0-9-]+/img/current/816\.jpg',
        id_pattern=r'[a-z0-9-]{1,100}',
    ),
    'foto-net': Region(
        'https://www.foto-webcam.net/include/metadata.php',
        (8, 46, 15, 50),
        'https://www.foto-webcam.net/',
        'Foto-Webcam.net / camera operators',
        r'https://www\.foto-webcam\.net/webcam/[a-z0-9-]+/current/720\.jpg',
        id_pattern=r'[a-z0-9-]{1,100}',
    ),
    'foto-org': Region(
        'https://www.foto-webcam.org/include/metadata.php',
        (8, 46, 15, 50),
        'https://www.foto-webcam.org/',
        'Foto-Webcam.org / camera operators',
        r'https://www\.foto-webcam\.org/webcam/[a-z0-9-]+/current/720\.jpg',
        id_pattern=r'[a-z0-9-]{1,100}',
    ),
    'hk': Region(
        'https://static.data.gov.hk/td/traffic-snapshot-images/code/Traffic_Camera_Locations_En.xml',
        (113.7, 22, 114.6, 22.7),
        'https://www.td.gov.hk/en/route_enquiry/traffic_snapshot_images/index.html',
        'Hong Kong Transport Department / DATA.GOV.HK',
        r'https://tdcctv\.data\.one\.gov\.hk/[A-Z0-9]+\.JPG',
    ),
    'sg': Region(
        'https://api.data.gov.sg/v1/transport/traffic-images',
        (103.5, 1.1, 104.2, 1.6),
        'https://data.gov.sg/datasets?query=traffic%20images',
        'Land Transport Authority / data.gov.sg / Singapore Open Data Licence',
        r'https://images\.data\.gov\.sg/api/traffic-images/[0-9]{4}/[0-9]{2}/[a-f0-9-]+\.jpg',
        ttl=60,
    ),
    'oregon': Region(
        'https://www.tripcheck.com/Scripts/map/data/cctvinventory.js',
        (-125, 41.5, -116, 46.6),
        'https://www.tripcheck.com/',
        'Oregon Department of Transportation / TripCheck',
        r'https://www\.tripcheck\.com/RoadCams/cams/[A-Za-z0-9_% .()-]+\.jpg',
    ),
    'wsdot': Region(
        'https://data.wsdot.wa.gov/travelcenter/Cameras.json',
        (-125, 45.4, -116.5, 49.5),
        'https://wsdot.com/travel/real-time/map/',
        'Washington State Department of Transportation / WSDOT',
        r'https://images\.wsdot\.wa\.gov/[A-Za-z0-9_/-]+\.(?:jpg|JPG)',
    ),
    'austin': Region(
        'https://data.austintexas.gov/resource/b4k4-adkb.json?$limit=5000',
        (-98.2, 29.9, -97.2, 30.9),
        'https://data.austintexas.gov/Transportation-and-Mobility/Traffic-Cameras/b4k4-adkb',
        'City of Austin Transportation and Public Works / Open Data',
        r'https://cctv\.austinmobility\.io/image/[0-9]+\.jpg',
    ),
}

# panorama pages only have the position, image comes later
for ident, slug in [('3017', 'marienplatz-altes-rathaus'), ('3015', 'hauptbahnhof'), ('3016', 'olympiaturm')]:
    url = f'https://www.feratel.com/webcams/deutschland/bayern/muenchen-{slug}-{ident}'
    REGIONS[f'feratel-{ident}'] = Region(
        url, (11, 47.7, 12.2, 48.7), url, 'feratel media technologies / camera operator', r'(?!)'
    )

for ident, slug in [
    ('60476b87a35be', 'muenchen-marienplatz-frauenkirche'),
    ('69bd0136e63b2ed1670a4d54', 'muenchen-paulskirche-kirche-st-paul-an-der-theresienwiese-oktoberfest-muenchen'),
    ('677995c451e8e7093b036683', 'muenchen-theresienwiese'),
    ('677995bed5e51e755b0ff503', 'muenchen-deutsche-eiche'),
    ('51efa5fa990fd', 'muenchen-parkstadt-schwabing-wetter-com-redaktion'),
]:
    url = f'https://www.wetter.com/hd-live-webcams/deutschland/{slug}/{ident}/'
    REGIONS[f'wetter-{ident}'] = Region(url, (11, 47.7, 12.2, 48.7), url, 'wetter.com / camera operator', r'(?!)')

REGIONS['michaeli'] = Region(
    'https://xn--mnchen-3ya.webcam/',
    (11, 47.7, 12.2, 48.7),
    'https://xn--mnchen-3ya.webcam/',
    'Hans van Gelderen / Michaelianger webcam',
    r'(?!)',
    id_pattern='michaelianger',
)
REGIONS['pliening'] = Region(
    'https://wetter.nausch.org/',
    (11, 47.7, 12.2, 48.7),
    'https://webcam.nausch.org/',
    'Private Wetterstation Pliening / Django Nausch',
    r'https://webcam\.nausch\.org/control/cam[12]\.jpg',
    id_pattern='(?:west|east)',
)
REGIONS['mrsv'] = Region(
    'https://mrsv.bayern/kontakt/',
    (11, 47.7, 12.2, 48.7),
    'https://mrsv.bayern/neue-webcam-live/',
    'Münchener Ruder- und Segelverein Bayern / Starnberg',
    r'https://mrsv-online\.de/webcam/webcam-mrsv\.jpg',
    id_pattern='lake',
)
REGIONS['siemens'] = Region(
    'https://www.awekas.at/de/station.php4?id=12391',
    (11, 47.7, 12.2, 48.7),
    'https://munich-wetter.de/WebCam/annke/snapshot/WebCam-Muc.php',
    'Johann Siemens / Munich-Oberföhring weather webcam; station position from AWEKAS',
    r'https://munich-wetter\.de/WebCam/annke/snapshot/WebCam-Muc\.php',
    id_pattern='oberfoehring',
)


def identity(key):
    parts = key.split(':')
    if len(parts) != 3 or parts[0] != 'webcam' or parts[1] not in REGIONS:
        raise ValueError('Unknown public webcam')
    region = REGIONS[parts[1]]
    if not re.fullmatch(region.id_pattern, parts[2]):
        raise ValueError('Unknown public webcam')
    return parts[1], region


def provider_url(key):
    provider, region = identity(key)
    slug = key.split(':')[-1]
    if provider == 'terra':
        return f'https://terra-hd.com/{slug}/'
    if provider in ('foto-net', 'foto-org'):
        return region.page + f'webcam/{slug}/'
    return region.page


def valid_image(provider, url):
    return isinstance(url, str) and bool(re.fullmatch(REGIONS[provider].image_pattern, url))


def listing(provider, ident, title, position, image, fetched_at, direction=None, position_note=None):
    region = REGIONS[provider]
    ident = str(ident or '')
    position = point(position)
    video = provider.startswith(('feratel-', 'wetter-')) or provider == 'michaeli'
    if (
        not re.fullmatch(region.id_pattern, ident)
        or not position
        or (not region.link_only and not video and not valid_image(provider, image))
    ):
        return None
    if not (
        region.bounds[0] <= position[0] <= region.bounds[2] and region.bounds[1] <= position[1] <= region.bounds[3]
    ):
        return None
    key = f'webcam:{provider}:{ident}'
    return Observation(
        key,
        'cameras',
        clean(title or ident),
        'CAM',
        position,
        (
            'Public camera / open provider / image reuse not enabled'
            if region.link_only
            else 'Public webcam / periodic snapshot / capture time unverified'
        ),
        (key,),
        demo=False,
        fetched_at=fetched_at,
        kind='listing',
        position_kind='listing',
        attribution=(region.credit,),
        details={
            'url': provider_url(key),
            'image_url': None if region.link_only else image,
            'link_only': region.link_only,
            'media_kind': 'video' if video else 'snapshot',
            'capture_interval_seconds': None,
            'direction': clean(direction),
            'position_source_url': region.url,
            'position_note': clean(position_note) or 'Published camera location; not image coverage.',
        },
    )


def parse_directory(provider, content, fetched_at):
    items = []

    def add(*args, **kwargs):
        item = listing(provider, *args, fetched_at, **kwargs)
        if item:
            items.append(item)

    if provider in ('mrsv', 'siemens'):
        text = unescape(content.decode('utf-8'))
        note = 'Published operator site/station marker; exact camera mounting position unverified.'
        if provider == 'mrsv':
            # coords from the contact page map
            marker = re.search(
                r'<div\b[^>]*class="[^"]*awb-openstreet-map-marker[^\"]*"'
                r'[^>]*data-latitude="([^"]+)"[^>]*data-longitude="([^"]+)"',
                text,
            )
            if marker and 'https://mrsv-online.de/webcam/webcam-mrsv.jpg' in text:
                add(
                    'lake',
                    'Starnberg / MRSV sailing club / lake view',
                    (marker.group(2), marker.group(1)),
                    'https://mrsv-online.de/webcam/webcam-mrsv.jpg',
                    direction='Southeast / Berg and Leoni',
                    position_note=note,
                )
        else:
            coords = re.search(
                r'(\d+)°\s*(\d+)\'\s*(\d+)\'\'\s*Ost\s*/\s*'
                r'(\d+)°\s*(\d+)\'\s*(\d+)\'\'\s*Nord',
                text,
            )
            if coords and 'München-Oberföhring' in text:
                lon, lon_m, lon_s, lat, lat_m, lat_s = map(int, coords.groups())
                if all(v < 60 for v in (lon_m, lon_s, lat_m, lat_s)):
                    add(
                        'oberfoehring',
                        'Munich-Oberföhring / private weather webcam',
                        (lon + lon_m / 60 + lon_s / 3600, lat + lat_m / 60 + lat_s / 3600),
                        'https://munich-wetter.de/WebCam/annke/snapshot/WebCam-Muc.php',
                        position_note=note,
                    )
        if not items:
            raise ValueError('no camera coords')
        return items
    if provider in ('michaeli', 'pliening'):
        text = unescape(content.decode('utf-8'))
        if provider == 'michaeli':
            # map center is somewhere else, the sunrise section has the real position
            coords = re.search(
                r'(\d+)°\s*(\d+)\'\s*(\d+)"\s*N[^<]*?'
                r'(\d+)°\s*(\d+)\'\s*(\d+)"\s*E',
                text,
            )
            if coords and 'Michaelianger' in text:
                lat, lat_m, lat_s, lon, lon_m, lon_s = map(int, coords.groups())
                if lat_m < 60 and lat_s < 60 and lon_m < 60 and lon_s < 60:
                    add(
                        'michaelianger',
                        'Michaelianger / private park webcam',
                        (lon + lon_m / 60 + lon_s / 3600, lat + lat_m / 60 + lat_s / 3600),
                        None,
                    )
        else:
            marker = re.search(r'href="(https://www\.openstreetmap\.org/\?mlat=[^"]+)"', text)
            if marker:
                query = parse_qs(urlsplit(marker.group(1)).query)
                position = (query.get('mlon', [None])[0], query.get('mlat', [None])[0])
                for ident, filename, direction in [
                    ('west', 'current_small.jpg', 'West / München'),
                    ('east', 'current_2_small.jpg', 'East / Markt Schwaben'),
                ]:
                    if re.search(r'<img\b[^>]+src="[^"]*media=wview:' + re.escape(filename) + r'"', text):
                        image_url = 'https://webcam.nausch.org/control/' + (
                            'cam1.jpg' if ident == 'west' else 'cam2.jpg'
                        )
                        add(
                            ident,
                            f'Pliening / private weather webcam / {direction}',
                            position,
                            image_url,
                            direction=direction,
                        )
        if not items:
            raise ValueError('no camera coords')
        return items
    if provider == 'terra':
        text = content.decode('utf-8')
        matches = re.findall(r'locations\[\d+\]\s*=\s*(\[[^\n;]+\])\s*;', text)
        if not matches:
            raise ValueError('terra-hd list broken')
        for match in matches:
            row = json.loads(match)
            if len(row) != 7 or row[0:2] != ['0', '1'] or row[6] != '1':
                continue  # private / external / offline
            slug = row[3].removeprefix('../')
            if row[3] != '../' + slug or not re.fullmatch(r'[a-z0-9-]{1,100}', slug):
                continue
            add(slug, row[2], (row[5], row[4]), f'https://terra-hd.com/{slug}/img/current/816.jpg')
        return list({item.id: item for item in items}.values())
    if provider.startswith('feratel-') or provider.startswith('wetter-'):
        text = content.decode('utf-8')
        if provider.startswith('feratel-'):
            scripts = re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', text, re.S)

            def visit(value):
                if isinstance(value, dict):
                    if value.get('@id') == REGIONS[provider].page + '#place':
                        geo = value.get('geo') or {}
                        title = re.search(r'<title>(.*?)</title>', text, re.S)
                        add(
                            provider.split('-')[1],
                            title.group(1) if title else value.get('name'),
                            (geo.get('longitude'), geo.get('latitude')),
                            None,
                        )
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)

            for script in scripts:
                visit(json.loads(script))
        else:
            lat = re.search(r'<meta\s+itemprop="latitude"\s+content="([^"]+)"', text)
            lon = re.search(r'<meta\s+itemprop="longitude"\s+content="([^"]+)"', text)
            title = re.search(r'<title>(.*?)</title>', text, re.S)
            if lat and lon:
                add(
                    provider.split('-')[1],
                    title.group(1) if title else 'München panorama',
                    (lon.group(1), lat.group(1)),
                    None,
                )
        if not items:
            raise ValueError('no camera coords')
        return list({item.id: item for item in items}.values())
    if provider == 'hk':
        root = ET.fromstring(content)
        if root.tag != 'image-list':
            raise ValueError('hong kong list broken')
        for row in root.findall('image'):
            add(
                row.findtext('key'),
                row.findtext('description'),
                (row.findtext('longitude'), row.findtext('latitude')),
                row.findtext('url'),
            )
        return list({item.id: item for item in items}.values())
    # some old WSDOT titles are not utf8
    data = json.loads(content.decode('utf-8', errors='replace'))
    if provider == 'bayern':
        if not isinstance(data, dict) or not isinstance(data.get('groups'), list):
            raise ValueError('bayerninfo list broken')

        def group(node):
            for row in node.get('webcams') or []:
                add(
                    row.get('id'),
                    ' / '.join(str(row.get(k) or '') for k in ('route', 'direction')),
                    (row.get('lon'), row.get('lat')),
                    'https://map.bayerninfo.de/cam/' + str(row.get('url', '')),
                )
            for child in node.get('groups') or []:
                group(child)

        group(data)
    elif provider in ('foto-net', 'foto-org'):
        if not isinstance(data, dict) or not isinstance(data.get('cams'), list):
            raise ValueError('foto-webcam list broken')
        for row in data['cams']:
            slug = row.get('id', '')
            if (
                not isinstance(slug, str)
                or not re.fullmatch(r'[a-z0-9-]{1,100}', slug)
                or row.get('offline')
                or row.get('hidden')
                or row.get('link') != REGIONS[provider].page + f'webcam/{slug}/'
            ):
                continue  # partner cams, offline, hidden
            add(
                slug,
                row.get('name'),
                (row.get('longitude'), row.get('latitude')),
                REGIONS[provider].page + f'webcam/{slug}/current/720.jpg',
            )
    elif provider == 'sg':
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise ValueError('singapore list broken')
        for batch in data['items']:
            for row in batch.get('cameras', []):
                loc = row.get('location') or {}
                add(
                    row.get('camera_id'),
                    'LTA camera ' + str(row.get('camera_id', '')),
                    (loc.get('longitude'), loc.get('latitude')),
                    row.get('image'),
                )
    elif provider == 'austin':
        if not isinstance(data, list):
            raise ValueError('austin list broken')
        for row in data:
            if row.get('camera_status') == 'TURNED_ON':
                add(
                    row.get('camera_id'),
                    row.get('location_name'),
                    (row.get('location') or {}).get('coordinates'),
                    row.get('screenshot_address'),
                )
    else:
        if not isinstance(data, dict) or not isinstance(data.get('features'), list):
            raise ValueError('camera list broken')
        for row in data['features']:
            props = row.get('properties') or row.get('attributes') or {}
            geometry = row.get('geometry') or {}
            if provider == 'oregon':
                filename = props.get('filename') or ''
                add(
                    props.get('publishedImageId'),
                    props.get('title'),
                    (props.get('longitude'), props.get('latitude')),
                    'https://www.tripcheck.com/RoadCams/cams/' + filename,
                )
            elif provider == 'wsdot':
                try:
                    x, y = float(geometry['x']), float(geometry['y'])
                    if not math.isfinite(x) or not math.isfinite(y) or abs(y) > 20_040_000:
                        continue
                    position = (math.degrees(x / 6378137), math.degrees(math.atan(math.sinh(y / 6378137))))
                except (KeyError, TypeError, ValueError):
                    continue
                add(props.get('CameraID'), props.get('CameraTitle'), position, props.get('ImageURL'))
    return list({item.id: item for item in items}.values())
