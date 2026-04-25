from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
import math

from PIL import Image, ImageDraw, ImageFont

from . import aircraft, cameras
from .data import BY_LAYER, CONNECTED_LAYERS, DEFAULT_CENTER, DEFAULT_ZOOM, LAYERS, Observation, distance_label, distance_m
from .cartography import Layout, layout_map, paint_regions, paint_symbols, pick_regions, project, symbol_radius
from .tiles import coordinate, world_pixel
from .fleet import fleet_counts
from .display import RULES
from .timing import TIMED_LAYERS, parse_time, timing_label, timing_status

PROFILES = {'landscape': (320, 240), 'portrait': (240, 320)}
BG, PANEL, TEXT, MUTED, ACCENT = '#101a1d', '#1e2c30', '#edf5e9', '#9bafa9', '#dfff7b'
FONT = ImageFont.load_default(size=11)
SMALL = ImageFont.load_default(size=10)
BOLD = ImageFont.load_default(size=12)
NAV_H, TOP_H = 32, 16


@dataclass
class Device:
    profile: str = 'landscape'
    page: str = 'map'
    center: tuple[float, float] = DEFAULT_CENTER
    zoom: int = DEFAULT_ZOOM
    enabled: set[str] = field(default_factory=lambda: set(CONNECTED_LAYERS))
    offset: int = 0
    selected: list[str] = field(default_factory=list)
    selected_index: int = 0
    revision: int = 0
    map_status: str = 'not loaded'
    card_open: bool = False
    mode: str = 'live'
    home: tuple[float, float] = DEFAULT_CENTER
    observations: tuple[Observation, ...] = ()
    source_statuses: tuple[dict, ...] = ()
    data_token: tuple | None = None
    feed_consumer: str = ''
    viewport: dict = field(default_factory=dict)
    show_planned: bool = False
    position: tuple[float, float] | None = None
    position_accuracy_m: float | None = None
    position_observed_at: str | None = None
    following: bool = False
    camera_directory: tuple[Observation, ...] = ()
    camera_id: str = ''
    camera_image: Image.Image | None = None
    camera_fetched_at: str | None = None
    camera_status: str = 'not loaded'
    camera_directory_status: str = 'not loaded'
    flight_route: dict = field(default_factory=dict)

    @property
    def selected_object(self):
        return self.by_id.get(self.selected[self.selected_index]) if self.selected else None

    @property
    def selected_flight_route(self):
        item = self.selected_object
        if (
            item
            and self.flight_route.get('aircraft_id') == item.id
            and self.flight_route.get('callsign') == aircraft.callsign(item)
        ):
            return self.flight_route
        return None

    @property
    def camera(self):
        return next((i for i in self.camera_directory if i.id == self.camera_id), None)

    def position_fresh(self, now=None):
        from datetime import timezone

        now = now or datetime.now(timezone.utc)
        observed = parse_time(self.position_observed_at)
        return bool(self.position and observed and -30 <= (now - observed).total_seconds() <= 120)

    @property
    def regional_alerts(self):
        severity = {'extreme': 0, 'severe': 1, 'moderate': 2, 'minor': 3}
        return sorted(
            (
                i
                for i in self.observations
                if not i.demo and i.warning and i.position_kind == 'citywide' and timing_status(i) != 'ended'
            ),
            key=lambda i: (severity.get(str(i.details.get('severity', '')).lower(), 4), i.id),
        )

    @property
    def map_observations(self):
        visible = []
        for item in self.observations:
            if not item.demo and item.layer in TIMED_LAYERS:
                status = timing_status(item)
                if status == 'ended':
                    continue
                if status == 'planned' and not self.show_planned and not item.warning:
                    continue
            visible.append(item)
        return tuple(visible)

    def __post_init__(self):
        if self.mode not in ('live', 'demo'):
            raise ValueError('unknown mode')
        if self.mode == 'live' and any(item.demo for item in self.observations):
            raise ValueError('demo data in live mode')

    @property
    def by_id(self):
        return {item.id: item for item in self.observations}

    @property
    def layer_summaries(self):
        counts = Counter(item.layer for item in self.observations)
        summaries = []
        for layer in LAYERS:
            if self.mode == 'demo':
                status = 'demo / not connected'
            else:
                statuses = dict.fromkeys(
                    source['status'] for source in self.source_statuses if layer.id in source['layers']
                )
                status = ', '.join(statuses) or 'not connected'
            summaries.append({**asdict(layer), 'count': counts[layer.id], 'status': status})
        return summaries

    @property
    def badge(self):
        if self.mode == 'demo':
            return 'DEMO'
        return 'DATA' if any(source['status'] == 'ready' for source in self.source_statuses) else 'WAIT'

    @property
    def size(self):
        return PROFILES[self.profile]

    @property
    def slots(self):
        if self.page == 'cameras':
            return (self.size[1] - 112) // 32
        return (self.size[1] - TOP_H - NAV_H - 32) // 32

    @property
    def items(self):
        return sorted(
            (item for item in self.observations if item.layer in self.enabled),
            key=lambda item: (not item.warning, distance_m(self.home, item.position), item.id),
        )

    @property
    def visible_fleet(self):
        def in_view(item):
            if item.kind != 'availability' or item.layer not in self.enabled:
                return False
            x, y = project(item.position, self.center, self.zoom, self.size)
            return 0 <= x < self.size[0] and 0 <= y < self.size[1]

        return fleet_counts(item for item in self.observations if in_view(item))


def apply_action(
    device: Device, kind: str, value: str = '', dx: int = 0, dy: int = 0, x: int | None = None, y: int | None = None
):
    if kind == 'page':
        if value not in ('map', 'menu', 'feed', 'layers', 'info', 'cameras'):
            raise ValueError('unknown page')
        device.page, device.offset = value, 0
    elif kind == 'camera':
        item = next(
            (i for i in (*device.camera_directory, *device.observations) if i.id == value and i.layer == 'cameras'),
            None,
        )
        if item is None:
            raise ValueError('Unknown public webcam')
        cameras.camera_url(item.id)
        if item.id != device.camera_id:
            device.camera_image, device.camera_fetched_at = None, None
        device.camera_id, device.page = item.id, 'camera'
        device.card_open = False
        if item not in device.camera_directory:
            device.camera_directory += (item,)
    elif kind == 'zoom':
        if value not in ('in', 'out'):
            raise ValueError('unknown zoom')
        if (x is None) != (y is None):
            raise ValueError('zoom needs x AND y')
        width, height = device.size
        if x is not None and not (0 <= x < width and 0 <= y < height):
            raise ValueError('zoom anchor outside of display')
        zoom = max(12, min(18, device.zoom + (1 if value == 'in' else -1)))
        if x is not None and zoom != device.zoom:
            device.following = False
            cx, cy = world_pixel(*device.center, device.zoom)
            scale = 2 ** (zoom - device.zoom)
            ax, ay = x - width / 2, y - height / 2
            device.center = coordinate((cx + ax) * scale - ax, (cy + ay) * scale - ay, zoom)
        device.zoom = zoom
        device.page = 'map'
    elif kind == 'planned':
        device.show_planned = not device.show_planned
    elif kind == 'follow':
        if value not in ('on', 'off', ''):
            raise ValueError('follow needs on/off')
        following = value == 'on' if value else not device.following
        if following and not device.position_fresh():
            raise ValueError('no fresh position to follow')
        device.following = following
        if following:
            device.center = device.position
        device.page = 'map'
    elif kind == 'alerts':
        device.selected = [i.id for i in device.regional_alerts]
        if not device.selected:
            raise ValueError('no alerts')
        device.selected_index, device.page, device.card_open = 0, 'detail', False
    elif kind == 'home':
        device.following = False
        device.center, device.page = device.home, 'map'
    elif kind == 'pan':
        device.following = False
        x, y = world_pixel(*device.center, device.zoom)
        device.center = coordinate(x - dx, y - dy, device.zoom)
        device.page = 'map'
    elif kind == 'toggle':
        if value not in BY_LAYER:
            raise ValueError('unknown layer')
        if value in device.enabled:
            device.enabled.remove(value)
        else:
            device.enabled.add(value)
    elif kind == 'all':
        device.enabled = set() if device.enabled else set(BY_LAYER)
    elif kind in ('next', 'prev'):
        delta = 1 if kind == 'next' else -1
        if device.page == 'detail' or device.card_open:
            device.selected_index = (device.selected_index + delta) % max(1, len(device.selected))
        else:
            if device.page == 'layers':
                count = len(LAYERS)
            elif device.page == 'cameras':
                count = len(device.camera_directory)
            else:
                count = len(device.items)
            pages = max(1, math.ceil(count / device.slots))
            device.offset = (device.offset + delta) % pages
    elif kind == 'select':
        if value not in device.by_id:
            raise ValueError('unknown observation')
        device.selected, device.selected_index, device.page = [value], 0, 'detail'
        device.card_open = False
    elif kind == 'inspect':
        if not device.card_open or not device.selected:
            raise ValueError('nothing selected')
        device.page, device.card_open = 'detail', False
    elif kind == 'dismiss':
        device.selected, device.selected_index, device.card_open = [], 0, False
        device.page = 'map'
    else:
        raise ValueError('unknown action')
    if kind in ('page', 'zoom', 'home', 'pan', 'toggle', 'all', 'planned', 'follow'):
        device.selected, device.selected_index, device.card_open = [], 0, False
    device.revision += 1


@dataclass
class Hit:
    box: tuple[int, int, int, int]
    kind: str
    value: str = ''


@dataclass
class Frame:
    image: Image.Image
    hits: list[Hit]
    points: list[tuple[str, int, int]]
    layout: Layout | None = None
    card_box: tuple[int, int, int, int] | None = None


def fit(draw: ImageDraw.ImageDraw, text: str, width: int, font=FONT):
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + '..', font=font) > width:
        text = text[:-1]
    return text + '..'


def wrap(draw, text, width, font=FONT):
    words, lines, line = text.split(), [], ''
    for word in words:
        candidate = f'{line} {word}'.strip()
        if line and draw.textlength(candidate, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def render(device: Device, basemap: Image.Image | None, now: datetime) -> Frame:
    width, height = device.size
    image = Image.new('RGB', device.size, BG)
    draw = ImageDraw.Draw(image)
    hits: list[Hit] = []
    points = []
    layout = None
    card_box = None

    def label(x, y, text, fill=TEXT, font=FONT):
        draw.text((x, y), text, fill=fill, font=font)

    def button(box, text, kind, value='', active=False):
        x0, y0, x1, y1 = box
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill='#354132' if active else PANEL, outline='#43524c')
        text_width = draw.textlength(text, font=FONT)
        label(x0 + (x1 - x0 - text_width) / 2, y0 + (y1 - y0 - 12) / 2, text, ACCENT if active else TEXT)
        hits.append(Hit(box, kind, value))

    if device.page != 'map':
        draw.rectangle((0, 0, width, TOP_H - 1), fill='#29362b')
        label(4, 2, 'CYDNEY', ACCENT, SMALL)
        label(57, 2, device.badge, '#ffc787', SMALL)
        label(94, 2, f'{len(device.enabled)}/{len(LAYERS)}L', MUTED, SMALL)
        label(width - 38, 2, now.strftime('%H:%M'), TEXT, SMALL)

    if device.page == 'map':
        layout = layout_map(
            device.map_observations, device.center, device.zoom, device.size, device.enabled, device.home
        )
        image = basemap.copy() if basemap else Image.new('RGB', device.size, '#192726')
        image = paint_regions(image, layout.regions, device.zoom)
        hx, hy = project(device.home, device.center, device.zoom, device.size)
        reserved = [
            (width - 108, 0, width, 40),
            (width - 84, height - 52, width, height - 12),
            (0, 0, 45, 18),
            (hx - 12, hy - 12, hx + 12, hy + 12),
        ]
        if device.regional_alerts:
            reserved.append((0, 39, width, 99))
        image = paint_symbols(image, layout.symbols, device.zoom, reserved)
        draw = ImageDraw.Draw(image)
        points = [(point.item.id, round(point.x), round(point.y)) for point in layout.points]
        if 0 <= hx < width and 0 <= hy < height:
            draw.ellipse((hx - 8, hy - 8, hx + 8, hy + 8), fill='#111f22', outline=ACCENT, width=2)
            if device.mode == 'demo':
                draw.line((hx - 4, hy, hx + 4, hy), fill=ACCENT)
                draw.line((hx, hy - 4, hx, hy + 4), fill=ACCENT)
            else:
                # no gps -> house at home position
                draw.line((hx - 4, hy, hx, hy - 4, hx + 4, hy), fill=ACCENT)
                draw.rectangle((hx - 3, hy, hx + 3, hy + 4), outline=ACCENT)
        if device.position:
            px, py = project(device.position, device.center, device.zoom, device.size)
            fresh = device.position_fresh(now)
            color = '#70dfff' if fresh else '#ffc787'
            if 0 <= px < width and 0 <= py < height:
                radius = (device.position_accuracy_m or 0) / (
                    math.cos(math.radians(device.position[1])) * 40075016.686 / (256 * 2**device.zoom)
                )
                overlay = Image.new('RGBA', device.size)
                accuracy = ImageDraw.Draw(overlay)
                if radius <= 2 * max(device.size):
                    accuracy.ellipse(
                        (px - radius, py - radius, px + radius, py + radius),
                        fill=(112, 223, 255, 28),
                        outline=(112, 223, 255, 100),
                    )
                else:
                    accuracy.rectangle((0, 0, width, height), fill=(112, 223, 255, 28))
                image = Image.alpha_composite(image.convert('RGBA'), overlay).convert('RGB')
                draw = ImageDraw.Draw(image)
                draw.ellipse((px - 5, py - 5, px + 5, py + 5), fill=color if fresh else BG, outline=color, width=2)
                draw.line((px - 8, py, px + 8, py), fill=color)
                draw.line((px, py - 8, px, py + 8), fill=color)
        credit = '© OpenStreetMap contributors'
        draw.text((3, height - 11), credit, font=SMALL, fill='#edf5e9', stroke_width=1, stroke_fill='#111c20')
        draw.rounded_rectangle((3, 3, 42, 17), radius=3, fill=BG)
        label(7, 5, device.badge, '#ffc787', SMALL)
        if device.mode != 'demo':
            stale_count = sum(item.stale for item in device.observations)
            source_errors = sum(source['status'] in ('error', 'offline', 'stale') for source in device.source_statuses)
            pending = sum(source['status'] == 'pending' for source in device.source_statuses)
            if stale_count or source_errors or pending:
                draw.rectangle((46, 3, min(width - 108, 176), 17), fill=BG)
                status = (
                    f'LOAD {pending} / {source_errors} feeds !'
                    if pending
                    else f'{stale_count} stale / {source_errors} feeds !'
                )
                label(49, 5, fit(draw, status, width - 157, SMALL), '#ffc787', SMALL)
        if device.map_status != 'ready':
            draw.rounded_rectangle((3, 37, min(width - 42, 170), 50), radius=2, fill='#553c24')
            label(6, 38, fit(draw, f'OSM {device.map_status.upper()}', width - 52, SMALL), '#ffe1a4', SMALL)
        if device.mode == 'live':
            gps = ('FOLLOW' if device.following else 'BROWSE') + (
                ' / NO GPS'
                if not device.position
                else f' / {"GPS" if device.position_fresh(now) else "GPS STALE"} ±{device.position_accuracy_m:.0f}m'
            )
            draw.rectangle((3, 20, min(width - 108, 211), 34), fill=BG)
            label(6, 22, fit(draw, gps, width - 118, SMALL), '#70dfff' if device.position_fresh(now) else MUTED, SMALL)
        button((width - 108, 4, width - 76, 36), '-', 'zoom', 'out')
        button((width - 72, 4, width - 40, 36), '+', 'zoom', 'in')
        button((width - 36, 4, width - 4, 36), '...', 'page', 'menu')
        if device.regional_alerts:
            alert = device.regional_alerts[0]
            draw.rectangle((2, 40, width - 3, 98), fill='#582718', outline='#ff9c68', width=2)
            severity = str(alert.details.get('severity', 'Unknown')).upper()
            label(
                6,
                43,
                fit(
                    draw,
                    f'! {"STALE / " if alert.stale else ""}{severity} / {len(device.regional_alerts)} ALERTS',
                    width - 12,
                    SMALL,
                ),
                '#ffe1b8',
                SMALL,
            )
            label(6, 56, fit(draw, alert.title, width - 12, BOLD), TEXT, BOLD)
            label(
                6,
                71,
                fit(draw, alert.details.get('affected_area', 'München municipality'), width - 12, SMALL),
                '#ffe1b8',
                SMALL,
            )
            end = parse_time(alert.expires_at)
            expiry = end.astimezone(now.tzinfo).strftime('%d.%m %H:%M %Z') if end else 'unknown'
            label(6, 84, fit(draw, f'Until {expiry}', width - 12, SMALL), '#ffe1b8', SMALL)
            hits.append(Hit((2, 40, width - 2, 99), 'alerts'))
        if device.card_open and device.selected:
            item = device.selected_object
            symbol = next((symbol for symbol in layout.symbols if item in symbol.members), None)
            # card goes to the other half, otherwise it covers the marker
            top = (102 if device.regional_alerts else 38) if symbol and symbol.y > height / 2 else height - 95
            card_box = (4, top, width - 4, top + 80)
            draw.rounded_rectangle((4, top, width - 5, top + 79), radius=5, fill=BG, outline=BY_LAYER[item.layer].color)
            if symbol:
                sx, sy = symbol.x, symbol.y
                radius = symbol_radius(symbol) + 2
                draw.ellipse((sx - radius, sy - radius, sx + radius, sy + radius), outline=ACCENT, width=2)
            label(10, top + 6, fit(draw, item.title, width - 62, BOLD), BY_LAYER[item.layer].color, BOLD)
            status = (
                'DEMO'
                if item.demo
                else ('STALE ' if item.stale else '') + (timing_label(item, now) or item.kind.upper())
            )
            subtitle = f'{status} / {item.position_kind}'
            if item.layer == 'flights' and not item.demo:
                subtitle = (
                    ('STALE / ' if item.stale else '')
                    + aircraft.route_label(device.selected_flight_route)
                    + ' / adsbdb'
                )
            label(10, top + 20, fit(draw, subtitle, width - 62, SMALL), MUTED, SMALL)
            label(10, top + 32, fit(draw, item.value, width - 22, SMALL), TEXT, SMALL)
            button((width - 40, top + 4, width - 8, top + 36), 'X', 'dismiss')
            if len(device.selected) > 1:
                button((8, top + 44, 48, top + 76), '<', 'prev')
                button(
                    (48, top + 44, width - 48, top + 76),
                    f'INFO / {device.selected_index + 1} of {len(device.selected)}',
                    'inspect',
                )
                button((width - 48, top + 44, width - 8, top + 76), '>', 'next')
            else:
                button(
                    (8, top + 44, width - 8, top + 76),
                    'VIEW CAMERA' if item.layer == 'cameras' else 'FULL INFO',
                    'camera' if item.layer == 'cameras' else 'inspect',
                    item.id if item.layer == 'cameras' else '',
                )
    elif device.page == 'menu':
        label(6, 28, 'MAP CONTROLS', ACCENT, BOLD)
        button((width - 36, 20, width - 4, 52), 'X', 'page', 'map')
        controls = [
            ('FEED', 'page', 'feed'),
            ('LAYERS', 'page', 'layers'),
            ('ZOOM -', 'zoom', 'out'),
            ('ZOOM +', 'zoom', 'in'),
            ('RECENTER', 'home', ''),
            ('INFO', 'page', 'info'),
        ]
        for index, (text, kind, value) in enumerate(controls):
            col, row = index % 2, index // 2
            x, y = 4 + col * (width // 2), 60 + row * 36
            button((x, y, x + width // 2 - 8, y + 32), text, kind, value)
        button((4, 168, width // 2 - 4, 200), 'PLANNED ON' if device.show_planned else 'PLANNED OFF', 'planned')
        button((width // 2 + 4, 168, width - 4, 200), 'BROWSE' if device.following else 'FOLLOW GPS', 'follow')
        button((4, 204, width - 4, 236), 'CAMERAS', 'page', 'cameras')
    elif device.page == 'cameras':
        rows = device.camera_directory
        label(5, 20, 'CAMERAS NEARBY', ACCENT, SMALL)
        if not rows:
            label(5, 45, 'no cameras nearby', TEXT, SMALL)
        label(5, height - 76, fit(draw, device.camera_directory_status, width - 10, SMALL), MUTED, SMALL)
        for index, item in enumerate(rows[device.offset * device.slots : (device.offset + 1) * device.slots]):
            y = 36 + index * 32
            view_type = ' / OPEN SITE' if item.details.get('link_only') else ''
            button(
                (4, y, width - 4, y + 32),
                fit(
                    draw,
                    f'{item.title}{view_type} / {distance_label(distance_m(device.center, item.position))}',
                    width - 18,
                ),
                'camera',
                item.id,
            )
        button((0, height - 64, width // 2, height - 32), '< PREV', 'prev')
        button((width // 2, height - 64, width, height - 32), 'NEXT >', 'next')
    elif device.page == 'camera':
        label(
            4, 20, fit(draw, device.camera.title if device.camera else 'Public webcam', width - 42, SMALL), TEXT, SMALL
        )
        button((width - 36, 0, width - 4, 32), 'X', 'page', 'cameras')
        if device.camera_image is not None:
            fitted, box = cameras.image_box(device.size, device.camera_image)
            image.paste(fitted, box[:2])
        elif device.camera and device.camera.details.get('link_only'):
            label(6, 65, 'only on the provider website', TEXT, SMALL)
            label(6, 84, 'Tap OPEN PROVIDER below.', ACCENT, SMALL)
        else:
            loading = device.camera_status.startswith('LOADING')
            label(6, 65, 'loading camera...' if loading else 'no image :(', TEXT, SMALL)
            if loading:
                label(6, 84, 'can take a few seconds', MUTED, SMALL)
        label(4, height - 58, fit(draw, device.camera_status, width - 8, SMALL), '#ffbe89', SMALL)
        fetched = parse_time(device.camera_fetched_at)
        if not (device.camera and device.camera.details.get('link_only')):
            label(
                4,
                height - 45,
                f'Fetched {fetched.astimezone(now.tzinfo).strftime("%H:%M:%S") if fetched else "unknown"}',
                MUTED,
                SMALL,
            )
        credit = cameras.camera_credit(device.camera.id) if device.camera else 'Public webcam'
        label(4, height - 32, fit(draw, 'OPEN PROVIDER / ' + credit, width - 8, SMALL), ACCENT, SMALL)
        button((0, height - 17, width, height), 'BACK TO MAP', 'page', 'map')
    elif device.page in ('feed', 'layers'):
        rows = device.layer_summaries if device.page == 'layers' else device.items
        pages = max(1, math.ceil(len(rows) / device.slots))
        offset = device.offset % pages
        if not rows:
            label(6, 42, 'NOTHING ENABLED', ACCENT)
            label(6, 60, 'turn something on in LAYERS', MUTED, SMALL)
        for index, item in enumerate(rows[offset * device.slots : (offset + 1) * device.slots]):
            y = TOP_H + index * 32
            draw.rectangle((0, y, width - 1, y + 31), fill=BG if index % 2 else PANEL)
            if device.page == 'layers':
                color = item['color']
                key = item['id']
                label(5, y + 3, fit(draw, item['label'], width - 62), color)
                label(5, y + 18, fit(draw, f'{item["count"]} / {item["status"]}', width - 55, SMALL), MUTED, SMALL)
                state = 'OFF' if key not in device.enabled else 'ON'
                if (
                    device.mode != 'demo'
                    and state == 'ON'
                    and key in ('places', 'food', 'access', 'heritage')
                    and device.zoom < RULES[key].min_zoom
                ):
                    state = f'z{RULES[key].min_zoom}+'
                label(width - 39, y + 10, state, ACCENT if key in device.enabled else MUTED)
                hits.append(Hit((0, y, width, y + 32), 'toggle', key))
            else:
                color = BY_LAYER[item.layer].color
                label(5, y + 2, fit(draw, item.title, width - 60), color)
                label(width - 51, y + 3, distance_label(distance_m(device.home, item.position)), MUTED, SMALL)
                value = (
                    ('STALE / ' if item.stale else '')
                    + (timing_label(item, now) + ' / ' if timing_label(item, now) else '')
                    + item.value
                )
                label(5, y + 18, fit(draw, value, width - 10, SMALL), '#ffc787' if item.stale else TEXT, SMALL)
                hits.append(Hit((0, y, width, y + 32), 'select', item.id))
        button((0, height - 64, width // 2, height - 32), f'< {offset + 1}/{pages}', 'prev')
        button((width // 2, height - 64, width, height - 32), 'NEXT >', 'next')
    elif device.page == 'detail' and device.selected:
        item = device.selected_object
        lines: list[tuple[str, str]] = []
        lines += [(line, BY_LAYER[item.layer].color) for line in wrap(draw, item.title, width - 10, BOLD)[:2]]
        status = (
            'DEMO' if item.demo else ('STALE / ' if item.stale else '') + (timing_label(item, now) or item.kind.upper())
        )
        if item.layer == 'flights' and not item.demo:
            route = device.selected_flight_route
            lines += [(aircraft.route_label(route), ACCENT)]
            if route and route.get('status') == 'available':
                for label_text, name in (('From', 'origin'), ('To', 'destination')):
                    airport = route[name]
                    lines += [
                        (line, TEXT)
                        for line in wrap(draw, f'{label_text}: {airport["code"]} / {airport["name"]}', width - 10)[:2]
                    ]
                lines += [('adsbdb callsign lookup; diversions may differ', '#ffbe89')]
        if item.position_kind == 'citywide':
            lines += [
                (f'Severity: {item.details.get("severity", "Unknown")}' + (' / STALE' if item.stale else ''), '#ffc787')
            ]
            lines += [
                (line, TEXT)
                for line in wrap(
                    draw, 'Area: ' + item.details.get('affected_area', 'München municipality'), width - 10
                )[:2]
            ]
            end = parse_time(item.expires_at)
            lines += [(f'Until: {end.astimezone(now.tzinfo).strftime("%d.%m %H:%M %Z") if end else "unknown"}', TEXT)]
            lines += [('regional, no exact position', '#ffc787')]
        else:
            lines += [(f'{distance_label(distance_m(device.home, item.position))} from configured HOME', TEXT)]
        lines += [(f'{BY_LAYER[item.layer].label} / {status}', MUTED)]
        if not item.demo:
            lines += [(f'Position: {item.position_kind} / age {item.age_minutes}m', '#ffbe89')]
            lines += [
                (fit(draw, item.observed_at or 'time unknown', width - 10, SMALL), MUTED)
            ]
        lines += [(line, TEXT) for line in wrap(draw, item.value, width - 10)[:3]]
        if item.area_radius_m is not None:
            lines += [(f'Demo footprint: {item.area_radius_m:.0f}m radius', '#ffbe89')]
        if item.demo:
            lines += [(f'Age: {item.age_minutes}m at demo snapshot', MUTED)]
        lines += [(f'Sources: {len(item.sources)}', ACCENT)]
        lines += [(fit(draw, source, width - 10, SMALL), MUTED) for source in item.sources]
        lines += [
            (
                'DEMO DATA'
                if item.demo
                else fit(draw, ' | '.join(item.attribution), width - 10, SMALL),
                '#ffbe89',
            )
        ]
        if item.layer == 'cameras':
            lines += [
                (
                    'open provider website'
                    if item.details.get('link_only')
                    else 'snapshots, open viewer below',
                    '#ffbe89',
                )
            ]
        for index, (text, color) in enumerate(lines):
            if TOP_H + index * 13 + 12 <= height - 64:
                label(5, TOP_H + index * 13 + 1, text, color, SMALL if index > 0 else BOLD)
        if item.layer == 'cameras':
            button((0, height - 64, width, height - 32), 'VIEW CAMERA', 'camera', item.id)
        else:
            button(
                (0, height - 64, width // 2, height - 32),
                f'< {device.selected_index + 1}/{len(device.selected)}',
                'prev',
            )
            button((width // 2, height - 64, width, height - 32), 'NEARBY >', 'next')
    else:
        info = [
            ('CYDNEY v0.3', ACCENT),
            (f'{width}x{height} native / RGB565', TEXT),
            (f'{len(LAYERS)} layers / {len(device.observations)} objects', TEXT),
            (
                f'feeds: {sum(s["status"] == "ready" for s in device.source_statuses)} / {device.mode.upper()}',
                '#ffc787',
            ),
            (f'map: {device.map_status}', MUTED),
            ('ESP gets images; sends touch.', TEXT),
            ('All measurements are examples.' if device.mode == 'demo' else 'S=stop; GPS only when reported.', MUTED),
            ('MyRadl CC0; stops at zoom 16+', MUTED),
            ('MVG / Dott; API: full credits', MUTED),
            ('OSM / ADSB.lol ODbL; MVV CC BY', MUTED),
            ('City dl-by-de; DWD / BBK; ATP CC0', MUTED),
        ]
        for index, (text, color) in enumerate(info):
            label(5, TOP_H + 3 + index * 14, fit(draw, text, width - 10), color)
        button((0, height - 64, width, height - 32), 'ALL LAYERS OFF' if device.enabled else 'ALL LAYERS ON', 'all')
    if device.page not in ('map', 'menu', 'camera'):
        for index, page in enumerate(('map', 'feed', 'layers', 'info')):
            button(
                (index * width // 4, height - NAV_H, (index + 1) * width // 4, height),
                page.upper(),
                'page',
                page,
                device.page == page,
            )
    return Frame(image, hits, points, layout, card_box)


def touch(device: Device, frame: Frame, x: int, y: int, pointer: str = 'touch'):
    width, height = device.size
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError('touch outside of display')
    # print('touch', x, y, device.page)
    # buttons first, then markers
    for hit in reversed(frame.hits):
        x0, y0, x1, y1 = hit.box
        if x0 <= x < x1 and y0 <= y < y1:
            # print(hit.kind, hit.value)
            apply_action(device, hit.kind, hit.value)
            return
    if device.page == 'map' and frame.layout is not None:
        if frame.card_box:
            x0, y0, x1, y1 = frame.card_box
            if x0 <= x < x1 and y0 <= y < y1:
                return  # tap on the card
        # labels in the tiles are just pixels, only markers can be selected
        hx, hy = project(device.home, device.center, device.zoom, device.size)
        if (x < 172 and y >= height - 11) or (x < 45 and y < 19) or math.hypot(x - hx, y - hy) <= 9:
            if device.card_open:
                apply_action(device, 'dismiss')
            return
        padding = 2 if pointer == 'mouse' else 6
        nearby = []
        for index, symbol in enumerate(frame.layout.symbols):
            distance = math.hypot(symbol.x - x, symbol.y - y)
            if distance <= symbol_radius(symbol) + padding:
                nearby.append((distance, index, symbol))
        nearby.sort(key=lambda entry: (entry[0], -entry[2].importance, entry[1]))
        if nearby:
            selected = list(dict.fromkeys(item.id for _, _, candidate in nearby for item in candidate.members))
        else:
            if device.card_open:
                apply_action(device, 'dismiss')
                return
            selected = [
                region.item.id for region in pick_regions(frame.layout.regions, x, y, 2 if pointer == 'mouse' else 5)
            ]
        if selected:
            device.selected = selected
            device.selected_index, device.card_open = 0, True
            device.revision += 1
