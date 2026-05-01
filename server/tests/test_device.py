from dataclasses import replace
from datetime import datetime, timezone

from PIL import Image, ImageDraw
import pytest

from server.adapters import parse_aircraft
from server.aircraft import parse_route, route_label
from server.cartography import Layout, Region, Symbol, cluster_text, layout_map, paint_aircraft, symbol_radius
from server.data import LAYERS, MUNICH, Observation
from server.display import RULES, overlay_alpha
from server.fleet import fleet_counts
from server.ground import parse_gbfs
from server.device import Device, PROFILES, apply_action, render, touch
from server.tests.demo_data import DemoDevice
from server.tests.test_feeds import aircraft
from server.tiles import coordinate, world_pixel

NOW = datetime.now(timezone.utc)


@pytest.mark.parametrize('profile', PROFILES)
def test_pages_and_touch(profile):
    device = DemoDevice(profile=profile)
    for direction, expected in (('out', 14), ('in', 15)):
        frame = render(device, None, NOW)
        control = next(hit for hit in frame.hits if hit.kind == 'zoom' and hit.value == direction)
        x0, y0, x1, y1 = control.box
        touch(device, frame, (x0 + x1) // 2, (y0 + y1) // 2)
        assert device.zoom == expected and device.page == 'map'
    frame = render(device, None, NOW)
    assert {hit.value for hit in frame.hits if hit.kind == 'zoom'} == {'in', 'out'}
    represented = [item.id for symbol in frame.layout.symbols for item in symbol.members]
    assert sorted(represented) == sorted(key for key, _, _ in frame.points)
    assert len(represented) == len(set(represented))
    assert len(frame.layout.symbols) < len(frame.points) / 2
    symbol = next(s for s in frame.layout.symbols if len(s.members) > 1 and s.x < device.size[0] - 40 and s.y > 40)
    touch(device, frame, round(symbol.x), round(symbol.y))
    assert device.zoom == 15 and device.card_open
    assert {item.id for item in symbol.members} <= set(device.selected)
    for page in ('map', 'menu', 'feed', 'layers', 'info'):
        apply_action(device, 'page', page)
        frame = render(device, None, NOW)
        assert frame.image.size == PROFILES[profile] and frame.image.mode == 'RGB'
        assert all(
            0 <= h.box[0] < h.box[2] <= device.size[0] and 0 <= h.box[1] < h.box[3] <= device.size[1]
            for h in frame.hits
        )
    apply_action(device, 'select', 'bus-m29')
    assert render(device, None, NOW).image.size == device.size
    for direction in ('in', 'out'):
        x, y = device.size[0] - 40, 70
        cx, cy = world_pixel(*device.center, device.zoom)
        anchor = coordinate(cx + x - device.size[0] / 2, cy + y - device.size[1] / 2, device.zoom)
        apply_action(device, 'zoom', direction, x=x, y=y)
        cx, cy = world_pixel(*device.center, device.zoom)
        assert coordinate(cx + x - device.size[0] / 2, cy + y - device.size[1] / 2, device.zoom) == pytest.approx(
            anchor
        )


def test_aircraft_marker():
    (item,) = parse_aircraft(aircraft(NOW), NOW)
    device = Device(center=item.position, observations=(item,), map_status='ready')
    frame = render(device, None, NOW)
    (symbol,) = frame.layout.symbols
    assert symbol_radius(symbol) == 20
    touch(device, frame, round(symbol.x + 10), round(symbol.y + 9), 'mouse')
    assert device.card_open and device.selected == [item.id]
    for track, nose in [(0, (40, 20)), (90, (60, 40)), (270, (20, 40))]:
        image = Image.new('RGBA', (80, 80))
        paint_aircraft(ImageDraw.Draw(image), 40, 40, replace(item, details={'track_degrees': track}), (255, 0, 0), 255)
        assert image.getpixel(nose) == (255, 0, 0, 255)
    route = {
        'callsign_icao': 'TEST42',
        'origin': {'icao_code': 'EDDM', 'iata_code': 'MUC', 'name': 'Munich'},
        'destination': {'icao_code': 'EGLL', 'iata_code': 'LHR', 'name': 'Heathrow'},
    }
    device.flight_route = {
        **parse_route({'response': {'flightroute': route}}, 'TEST42', NOW.isoformat()),
        'aircraft_id': item.id,
    }
    assert route_label(device.selected_flight_route) == 'MUC → LHR'
    device.observations = (replace(item, details={'callsign': 'OTHER1'}),)
    assert device.selected_flight_route is None and route_label(None) == 'Route unavailable'


def test_display_rules():
    assert set(RULES) == {layer.id for layer in LAYERS}
    base = Observation('fixture', 'health', 'Fixture', 'H', MUNICH, '', ('fixture',), demo=False)
    for layer in ('health', 'works'):
        item = replace(base, layer=layer)
        assert overlay_alpha(item, 12) == 110 and overlay_alpha(replace(item, warning=True), 12) == 255
    for layer in ('places', 'food', 'access', 'heritage'):
        assert not layout_map((replace(base, layer=layer),), MUNICH, 15, (320, 240), {layer}).symbols
    stop = replace(base, id='stop', layer='rail', kind='schedule', position_kind='stop')
    train = replace(
        stop, id='train', kind='observation', position_kind='observed', is_vehicle=True, observed_at=NOW.isoformat()
    )
    scooter, bike = parse_gbfs(
        {
            'last_updated': NOW.timestamp(),
            'ttl': 300,
            'data': {
                'bikes': [
                    {'bike_id': kind, 'lon': MUNICH[0], 'lat': MUNICH[1], 'vehicle_type_id': f'dott_{kind}'}
                    for kind in ('scooter', 'bicycle')
                ]
            },
        },
        NOW,
        'dott',
        (11.5, 48.1, 11.8, 48.2),
    )
    parked = tuple(replace(item, id=f'{item.id}-{n}') for item in (scooter, bike) for n in range(3))
    device = Device(center=MUNICH, zoom=15, observations=parked + (stop, train))
    assert fleet_counts(parked) == device.visible_fleet == {'scooters': 3, 'bikes': 3, 'other': 0, 'stale': 0}
    for zoom in (12, 16, 18):
        layout = layout_map(device.observations, MUNICH, zoom, device.size, device.enabled)
        assert any(s.members == (train,) for s in layout.symbols)
        assert any(stop in s.members for s in layout.symbols) == (zoom >= 16)
        inventory = [s for s in layout.symbols if s.members[0].kind == 'availability']
        assert sum(len(s.members) for s in inventory) == 6
        assert len(inventory) == (6 if zoom == 18 else 2)
        if zoom < 17:
            assert {cluster_text(s) for s in inventory} == {'SC3', 'BK3'}
    device.enabled.remove('bikes')
    assert device.visible_fleet['scooters'] == device.visible_fleet['bikes'] == 0


def test_hit_testing():
    device = DemoDevice()
    bus = device.by_id['bus-m29']
    weather = device.by_id['weather-0']
    frame = render(device, None, NOW)
    frame.layout = Layout([], [Symbol((bus,), 80, 80, 75)], [Region(weather, 160, 120, 1000)])
    touch(device, frame, 60, 60, 'mouse')
    assert not device.selected and device.page == 'map'
    touch(device, frame, 90, 80, 'mouse')
    assert not device.selected
    touch(device, frame, 90, 80, 'touch')
    assert device.card_open and device.selected == [bus.id]
    card = render(device, None, NOW)
    revision = device.revision
    x, y, _, _ = card.card_box
    touch(device, card, x + 10, y + 30, 'mouse')
    assert device.revision == revision and device.card_open
    apply_action(device, 'pan', dx=20)
    assert not device.selected and not device.card_open
