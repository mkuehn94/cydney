from datetime import datetime, timezone
import json
import math

import pytest

from server import cameras
from server.camera_regions import REGIONS, parse_directory
from server.provenance import real_record

NOW = datetime.now(timezone.utc).isoformat()


def sample(provider):
    if provider == 'hk':
        return b'<image-list><image><key>H429F</key><description>Aberdeen</description><longitude>114.1505</longitude><latitude>22.24845</latitude><url>https://tdcctv.data.one.gov.hk/H429F.JPG</url></image></image-list>'
    if provider == 'sg':
        data = {
            'items': [
                {
                    'cameras': [
                        {
                            'camera_id': '2701',
                            'location': {'longitude': 103.77, 'latitude': 1.44},
                            'image': 'https://images.data.gov.sg/api/traffic-images/2026/11/abc-123.jpg',
                        }
                    ]
                }
            ]
        }
    elif provider == 'austin':
        data = [
            {
                'camera_id': '1',
                'camera_status': 'TURNED_ON',
                'location_name': 'Congress',
                'location': {'coordinates': [-97.74, 30.27]},
                'screenshot_address': 'https://cctv.austinmobility.io/image/1.jpg',
            }
        ]
    elif provider == 'oregon':
        data = {
            'features': [
                {
                    'properties': {
                        'publishedImageId': '392',
                        'title': 'Portland',
                        'longitude': -122.67,
                        'latitude': 45.52,
                        'filename': 'Portland_pid1.jpg',
                    }
                }
            ]
        }
    elif provider == 'wsdot':
        data = {
            'features': [
                {
                    'attributes': {
                        'CameraID': 1003,
                        'CameraTitle': 'Seattle',
                        'ImageURL': 'https://images.wsdot.wa.gov/sw/005vc00320.jpg',
                    },
                    'geometry': {
                        'x': math.radians(-122.33) * 6378137,
                        'y': math.asinh(math.tan(math.radians(47.60))) * 6378137,
                    },
                }
            ]
        }
    elif provider == 'bayern':
        data = {
            'groups': [
                {
                    'groups': None,
                    'webcams': [
                        {
                            'id': 1373,
                            'route': 'B304',
                            'direction': 'Dachau',
                            'lon': 11.473386,
                            'lat': 48.222152,
                            'url': '1373.jpg',
                        }
                    ],
                }
            ]
        }
    elif provider == 'terra':
        return (
            'locations[1] = ' + json.dumps(['0', '1', 'Museum', '../deutschesmuseum', 48.12975, 11.582016, '1']) + ';'
        ).encode()
    elif provider in ('foto-net', 'foto-org'):
        data = {
            'cams': [
                {
                    'id': 'woerthsee',
                    'name': 'Wörthsee',
                    'longitude': 11.1964,
                    'latitude': 48.06485,
                    'link': REGIONS[provider].page + 'webcam/woerthsee/',
                    'offline': False,
                }
            ]
        }
    elif provider.startswith('feratel-'):
        return (
            '<script type="application/ld+json">'
            + json.dumps(
                {
                    '@graph': [
                        {
                            '@id': REGIONS[provider].page + '#place',
                            'name': 'München',
                            'geo': {'longitude': 11.576938, 'latitude': 48.136781},
                        }
                    ]
                }
            )
            + '</script>'
        ).encode()
    elif provider == 'mrsv':
        return (
            b'<div class="awb-openstreet-map-marker awb-openstreet-map-marker-1" '
            b'data-latitude="47.9916481" data-longitude="11.337999484601568"></div>'
            b'<img data-orig-src="https://mrsv-online.de/webcam/webcam-mrsv.jpg?vers=20261110">'
        )
    elif provider == 'siemens':
        return "München-Oberföhring 11° 37' 46'' Ost / 48° 10' 21'' Nord".encode()
    elif provider == 'michaeli':
        return 'Michaelianger <span>48° 07\' 25" N&ensp;•&ensp;11° 38\' 11" E</span>'.encode()
    elif provider == 'pliening':
        return (
            b'<a href="https://www.openstreetmap.org/?mlat=48.1987&amp;mlon=11.7980&amp;zoom=12">Location</a>'
            b'<img src="/lib/exe/fetch.php?w=160&amp;media=wview:current_small.jpg">'
        )
    elif provider.startswith('wetter-'):
        return b'<meta itemprop="latitude" content="48.1306"><meta itemprop="longitude" content="11.5458">'
    else:
        raise AssertionError(provider)
    return json.dumps(data).encode()


@pytest.mark.parametrize('provider', REGIONS)
def test_regional_cams(provider):
    (item,) = parse_directory(provider, sample(provider), NOW)
    assert real_record(item, 'webcams') and item.observed_at is None
    assert item.fetched_at == NOW and item.attribution
    assert cameras.camera_url(item.id).startswith('https://') and cameras.camera_credit(item.id)
    if REGIONS[provider].link_only:
        assert item.details['link_only'] and item.details['image_url'] is None
    if provider == 'wsdot':
        assert item.position == pytest.approx((-122.33, 47.60))
    with pytest.raises(ValueError):
        parse_directory(provider, b'<html>Feed unavailable</html>', NOW)
