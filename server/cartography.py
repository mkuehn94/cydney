from dataclasses import dataclass
import math

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageFont

from .data import BY_LAYER, HOME, Observation
from .fleet import available_class
from .display import amenity_code, overlay_alpha, rule_for
from .tiles import world_pixel

FONT = ImageFont.load_default(size=10)
# carefull: GTFS/MVG pins are stops, not vehicles
VEHICLE_LAYERS = frozenset({'transit', 'rail', 'flights', 'vessels'})
STOP_DETAIL_ZOOM = 16
FLEET_DETAIL_ZOOM = 17


def is_vehicle(item: Observation) -> bool:
    return item.is_vehicle or (item.demo and item.layer in VEHICLE_LAYERS)


def individual_marker(item: Observation, zoom: int) -> bool:
    return (not item.demo and rule_for(item).individual) or (
        is_vehicle(item) and (not available_class(item) or zoom >= FLEET_DETAIL_ZOOM)
    )


def group_category(item: Observation):
    inventory = available_class(item)
    return ('inventory', inventory) if inventory else ('poi', item.layer, amenity_code(item))


def is_transit_stop(item: Observation) -> bool:
    tags = item.details.get('tags') or {}
    return (
        (item.layer in ('rail', 'transit', 'vessels') and item.position_kind in ('stop', 'station'))
        or tags.get('railway') in ('station', 'halt', 'tram_stop', 'platform', 'subway_entrance')
        or tags.get('highway') == 'bus_stop'
        or tags.get('public_transport') in ('station', 'platform', 'stop_position')
        or tags.get('amenity') in ('bus_station', 'ferry_terminal')
    )


def has_reported_position(item: Observation) -> bool:
    return bool(
        item.is_vehicle
        and item.kind == 'observation'
        and item.position_kind == 'observed'
        and item.observed_at
        and not item.stale
    )


PRIORITIES = {
    'alerts': 100,
    'hazards': 100,
    'utilities': 90,
    'traffic': 85,
    'flights': 80,
    'transit': 75,
    'rail': 75,
    'vessels': 65,
    'health': 55,
    'weather': 50,
    'water': 45,
    'events': 45,
    'charging': 40,
    'access': 40,
    'air': 35,
    'bikes': 35,
    'cameras': 35,
}


def priority(item: Observation) -> int:
    if not item.demo:
        return rule_for(item).priority
    # old demo path, still used by the touch tests
    return max(90 if item.warning else 0, PRIORITIES.get(item.layer, 20))


def opacity(importance: int) -> int:
    return 240 if importance >= 85 else 215 if importance >= 65 else 145 if importance >= 40 else 75


# TODO clusters jump around a bit when zooming
def cluster_radius(zoom: int) -> int:
    # return 48 if zoom < 17 else 0
    return {12: 60, 13: 54, 14: 48, 15: 38, 16: 28, 17: 18}.get(zoom, 0)


@dataclass(frozen=True)
class Point:
    item: Observation
    x: float
    y: float


@dataclass(frozen=True)
class Symbol:
    members: tuple[Observation, ...]
    x: float
    y: float
    importance: int
    anchor: tuple[float, float] | None = None  # real postion of fanned out markers

    @property
    def color(self):
        if not self.members[0].demo and self.members[0].warning:
            return BY_LAYER['alerts'].color
        category = available_class(self.members[0])
        return {'scooter': '#58e6ff', 'bike': '#c5ff61', 'vehicle': '#e4edff'}.get(
            category, BY_LAYER[self.members[0].layer].color
        )


@dataclass(frozen=True)
class Region:
    item: Observation
    x: float
    y: float
    radius: float


@dataclass
class Layout:
    points: list[Point]
    symbols: list[Symbol]
    regions: list[Region]


def project(position, center, zoom, size):
    cx, cy = world_pixel(*center, zoom)
    px, py = world_pixel(*position, zoom)
    world_size = 256 * 2**zoom
    dx = (px - cx + world_size / 2) % world_size - world_size / 2
    return size[0] / 2 + dx, size[1] / 2 + py - cy


def layout_map(observations, center, zoom, size, enabled, home=HOME) -> Layout:
    width, height = size
    points, regions = [], []
    for item in observations:
        if item.layer not in enabled:
            continue
        if not item.demo:
            if item.warning and item.position_kind == 'citywide':
                continue  # regional notices go into the banner
            if overlay_alpha(item, zoom) == 0:
                continue
            if zoom < STOP_DETAIL_ZOOM and not rule_for(item).individual:
                if is_transit_stop(item) or (item.layer in VEHICLE_LAYERS and not has_reported_position(item)):
                    continue  # departures belong to stops
        x, y = project(item.position, center, zoom, size)
        if item.area_radius_m is not None and not is_vehicle(item):
            meters_per_pixel = math.cos(math.radians(item.position[1])) * 40075016.686 / (256 * 2**zoom)
            radius = item.area_radius_m / meters_per_pixel
            if x + radius >= 0 and y + radius >= 0 and x - radius < width and y - radius < height:
                regions.append(Region(item, x, y, radius))
                if not item.demo and priority(item) >= 85 and 0 <= x < width and 0 <= y < height:
                    points.append(Point(item, x, y))
        elif 0 <= x < width and 0 <= y < height:
            points.append(Point(item, x, y))

    remaining = sorted(
        (point for point in points if not individual_marker(point.item, zoom)),
        key=lambda point: (-priority(point.item), point.item.id),
    )
    # moving things stay single, parked ones get clustered
    symbols = [
        Symbol(
            (point.item,), point.x, point.y, max(35 if point.item.kind == 'availability' else 75, priority(point.item))
        )
        for point in sorted(points, key=lambda entry: entry.item.id)
        if individual_marker(point.item, zoom)
    ]
    radius = cluster_radius(zoom)
    categories = {point.item.id: group_category(point.item) for point in remaining}
    while remaining:
        seed, *tail = remaining
        # measure from the first point, otherwise a chain of neighbours eats everything
        members = [seed]
        remaining = []
        category = categories[seed.item.id]
        reach = radius * (1.4 if available_class(seed.item) else 1)
        for point in tail:
            same_category = seed.item.demo or categories[point.item.id] == category
            if radius and same_category and math.hypot(point.x - seed.x, point.y - seed.y) <= reach:
                members.append(point)
            else:
                remaining.append(point)
        symbols.append(
            Symbol(
                tuple(point.item for point in members),
                sum(point.x for point in members) / len(members),
                sum(point.y for point in members) / len(members),
                priority(seed.item),
            )
        )

    if zoom == 18:
        # same coords = still overlapping at max zoom, fan them out
        remaining = symbols
        symbols = []
        while remaining:
            seed, *tail = remaining
            close = [seed] + [other for other in tail if math.hypot(other.x - seed.x, other.y - seed.y) < 16]
            remaining = [other for other in tail if other not in close]
            if len(close) == 1:
                symbols.append(seed)
                continue
            for index, symbol in enumerate(close):
                ring, slot = divmod(index, 8)
                count = min(8, len(close) - ring * 8)
                angle = slot * math.tau / count - math.pi / 2
                reach = 22 + ring * 20
                x = max(10, min(width - 10, seed.x + math.cos(angle) * reach))
                y = max(18, min(height - 10, seed.y + math.sin(angle) * reach))
                symbols.append(Symbol(symbol.members, x, y, symbol.importance, (symbol.x, symbol.y)))
    symbols = separate_symbols(symbols, size, project(home, center, zoom, size))
    return Layout(points, symbols, regions)


def separate_symbols(symbols, size, home):
    width, height = size
    occupied = [
        (width - 40, 0, width, 40),
        (width - 84, height - 52, width, height - 12),
        (0, 0, 45, 19),
        (home[0] - 12, home[1] - 12, home[0] + 12, home[1] + 12),
    ]
    placed = []
    offsets = [(0, 0)]
    for reach, steps in ((12, 8), (24, 12), (36, 16), (60, 20)):
        offsets += [
            (math.cos(index * math.tau / steps) * reach, math.sin(index * math.tau / steps) * reach)
            for index in range(steps)
        ]
    for symbol in sorted(symbols, key=lambda entry: (-entry.importance, entry.members[0].id)):
        radius = symbol_radius(symbol) + 1
        chosen = None
        for dx, dy in offsets:
            x, y = symbol.x + dx, symbol.y + dy
            box = (x - radius - 2, y - radius - 2, x + radius + 2, y + radius + 2)
            if box[0] < 0 or box[1] < 1 or box[2] >= width or box[3] >= height - 12:
                continue
            if not any(intersects(box, other) for other in occupied):
                chosen = x, y
                break
        if chosen is None:
            chosen = (max(radius, min(width - radius - 1, symbol.x)), max(radius, min(height - radius - 13, symbol.y)))
        x, y = chosen
        anchor = symbol.anchor
        if (x, y) != (symbol.x, symbol.y) and anchor is None:
            anchor = symbol.x, symbol.y
        occupied.append((x - radius - 2, y - radius - 2, x + radius + 2, y + radius + 2))
        placed.append(Symbol(symbol.members, x, y, symbol.importance, anchor))
    return placed


def paint_regions(image: Image.Image, regions: list[Region], zoom: int = 15):
    # merge per layer so overlaps dont get darker
    regions = [r for r in regions if r.item.demo or overlay_alpha(r.item, zoom) > 0]
    for layer_id in sorted({region.item.layer for region in regions}):
        mask = Image.new('L', image.size)
        draw = ImageDraw.Draw(mask)
        for region in regions:
            if region.item.layer == layer_id:
                x, y, radius = region.x, region.y, region.radius
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
        color = ImageColor.getrgb(BY_LAYER[layer_id].color)
        tint = Image.new('RGBA', image.size, color + (0,))
        tint.putalpha(mask.point(lambda value: value * 18 // 255))
        image = Image.alpha_composite(image.convert('RGBA'), tint)
        edge = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(3)))
        edge_alpha = max(
            55 if r.item.demo else overlay_alpha(r.item, zoom) for r in regions if r.item.layer == layer_id
        )
        tint.putalpha(edge.point(lambda value: value * edge_alpha // 255))
        image = Image.alpha_composite(image, tint)
    return image.convert('RGB')


def intersects(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def cluster_text(symbol: Symbol) -> str:
    count = str(len(symbol.members))
    if symbol.members[0].demo:
        return count
    category = available_class(symbol.members[0])
    item = symbol.members[0]
    code = '!' if item.warning else amenity_code(item) or BY_LAYER[item.layer].code
    prefix = {'scooter': 'SC', 'bike': 'BK', 'vehicle': 'AV'}.get(category, code)
    return prefix + count


def aircraft_marker(item):
    return item.layer == 'flights' and item.is_vehicle and not item.demo


def symbol_radius(symbol: Symbol) -> int:
    if len(symbol.members) == 1 and aircraft_marker(symbol.members[0]):
        return 20
    if len(symbol.members) > 1:
        return 11 if symbol.members[0].demo else max(11, math.ceil(FONT.getlength(cluster_text(symbol)) / 2) + 3)
    if available_class(symbol.members[0]):
        return 5
    glyph = not symbol.members[0].demo and rule_for(symbol.members[0]).glyph
    if glyph:
        return max(6, math.ceil(FONT.getlength(marker_code(symbol.members[0])) / 2) + 2)
    return 6 if symbol.importance >= 65 else 3


def marker_code(item):
    if item.warning or item.layer in ('alerts', 'hazards'):
        return '!'
    if item.position_kind == 'stop' or (item.kind == 'schedule' and item.layer in ('rail', 'transit')):
        return 'S'
    if item.is_vehicle and item.layer == 'rail':
        return 'T'
    return amenity_code(item) or BY_LAYER[item.layer].code


def symbol_alpha(symbol, zoom):
    if all(item.demo for item in symbol.members):
        alpha = opacity(symbol.importance)
        return min(alpha, 100) if all(item.stale for item in symbol.members) else alpha
    return max(overlay_alpha(item, zoom) for item in symbol.members)


def paint_aircraft(draw, x, y, item, color, alpha):
    track = item.details.get('track_degrees')
    ink = color + (alpha,)
    if not isinstance(track, (int, float)) or not math.isfinite(track):
        # no heading -> round marker
        draw.ellipse((x - 14, y - 14, x + 14, y + 14), fill=(12, 25, 29, alpha), outline=ink, width=2)
        draw.text((x, y), 'A?', font=FONT, fill=ink, anchor='mm')
        return
    angle = math.radians(track)

    def rotate(px, py):
        return (
            round(x + px * math.cos(angle) - py * math.sin(angle)),
            round(y + px * math.sin(angle) + py * math.cos(angle)),
        )

    hull = [
        (0, -15),
        (2, -10),
        (2, -4),
        (14, 3),
        (14, 6),
        (2, 2),
        (2, 10),
        (6, 13),
        (6, 15),
        (0, 13),
        (-6, 15),
        (-6, 13),
        (-2, 10),
        (-2, 2),
        (-14, 6),
        (-14, 3),
        (-2, -4),
        (-2, -10),
    ]
    draw.polygon([rotate(*p) for p in hull], fill=ink, outline=(12, 25, 29, alpha), width=1)
    draw.line([rotate(0, -12), rotate(0, -20)], fill=ink, width=2)
    draw.line([rotate(-3, -17), rotate(0, -20), rotate(3, -17)], fill=ink, width=2)


def paint_available(draw, x, y, category, color, alpha):
    draw.rounded_rectangle((x - 5, y - 5, x + 5, y + 5), radius=2, fill=(12, 25, 29, alpha))
    ink = color + (alpha,)
    for wheel in (x - 3, x + 3):
        draw.ellipse((wheel - 1, y + 2, wheel + 1, y + 4), outline=ink)
    if category == 'scooter':
        draw.line((x - 3, y + 1, x + 3, y + 1, x + 1, y - 3, x + 3, y - 3), fill=ink)
    elif category == 'bike':
        draw.line((x - 3, y + 2, x - 1, y - 2, x + 2, y + 2, x - 3, y + 2), fill=ink)
        draw.line((x + 3, y + 2, x + 1, y - 3, x + 3, y - 3), fill=ink)
    else:
        draw.text((x - 3, y - 5), '?', font=FONT, fill=ink)


def pick_regions(regions: list[Region], x: int, y: int, padding: int) -> list[Region]:
    result = []
    for region in regions:
        if abs(math.hypot(region.x - x, region.y - y) - region.radius) > padding:
            continue
        if any(
            other.item.layer == region.item.layer
            and other is not region
            and math.hypot(other.x - x, other.y - y) < other.radius - padding
            for other in regions
        ):
            continue  # edge is inside the merged fill
        result.append(region)
    return sorted(result, key=lambda region: region.radius)


def paint_symbols(image: Image.Image, symbols: list[Symbol], zoom: int, reserved: list[tuple]):
    overlay = Image.new('RGBA', image.size)
    draw = ImageDraw.Draw(overlay)
    symbols = [symbol for symbol in symbols if symbol_alpha(symbol, zoom) > 0]
    occupied = list(reserved)
    for symbol in symbols:
        radius = symbol_radius(symbol)
        occupied.append((symbol.x - radius - 2, symbol.y - radius - 2, symbol.x + radius + 2, symbol.y + radius + 2))
    for symbol in sorted(symbols, key=lambda symbol: symbol.importance):
        x, y = round(symbol.x), round(symbol.y)
        color = ImageColor.getrgb(symbol.color)
        category = available_class(symbol.members[0])
        alpha = symbol_alpha(symbol, zoom)
        if symbol.anchor:
            draw.line((symbol.anchor[0], symbol.anchor[1], x, y), fill=color + (min(alpha, 80),))
        if len(symbol.members) == 1 and aircraft_marker(symbol.members[0]):
            paint_aircraft(draw, x, y, symbol.members[0], color, alpha)
        elif category and len(symbol.members) == 1:
            paint_available(draw, x, y, category, color, alpha)
        elif len(symbol.members) > 1:
            radius = symbol_radius(symbol)
            draw.ellipse(
                (x - radius, y - 11, x + radius, y + 11), fill=(20, 35, 38, alpha), outline=color + (alpha,), width=1
            )
            text = cluster_text(symbol)
            draw.text((x - draw.textlength(text, font=FONT) / 2, y - 5), text, font=FONT, fill=color + (alpha,))
        elif symbol.importance >= 65 or (not symbol.members[0].demo and rule_for(symbol.members[0]).glyph):
            scheduled = symbol.members[0].position_kind == 'stop' or symbol.members[0].kind == 'schedule'
            shape = draw.rectangle if scheduled else draw.ellipse
            radius = symbol_radius(symbol)
            shape((x - radius, y - 6, x + radius, y + 6), fill=(19, 35, 37, alpha), outline=color + (alpha,))
            item = symbol.members[0]
            text = marker_code(item)
            draw.text((x - draw.textlength(text, font=FONT) / 2, y - 5), text, font=FONT, fill=color + (alpha,))
        else:
            draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=color + (alpha,))
        if not symbol.members[0].demo and any(item.stale for item in symbol.members):
            r = symbol_radius(symbol)
            draw.rectangle((x + r - 2, y - 4, x + r, y - 2), fill=(255, 179, 72, 255))
    labels = 0
    for symbol in sorted(symbols, key=lambda symbol: (-symbol.importance, symbol.members[0].id)):
        item = symbol.members[0]
        alpha = symbol_alpha(symbol, zoom)
        poi_label = not item.demo and item.layer in ('places', 'food', 'health', 'charging', 'parking') and zoom >= 16
        important_label = not item.demo and priority(item) >= 85
        if (
            alpha < 140
            or (zoom < 17 and not poi_label and not important_label)
            or (symbol.importance < 65 and not poi_label)
            or labels >= 6
        ):
            continue
        if len(symbol.members) > 1:
            if not poi_label:
                continue
            text = {'places': 'SHOPS', 'food': 'FOOD', 'health': 'HEALTH', 'charging': 'CHARGE', 'parking': 'PARK'}[
                item.layer
            ]
        else:
            text = item.title[:15] if poi_label else item.short[:9]
        tw = math.ceil(draw.textlength(text, font=FONT))
        for direction in (1, -1):
            reach = symbol_radius(symbol) + 4
            x = round(symbol.x + reach) if direction == 1 else round(symbol.x - reach - tw)
            y = round(symbol.y - 6)
            box = (x - 2, y - 1, x + tw + 2, y + 12)
            if box[0] < 0 or box[1] < 18 or box[2] >= image.width or box[3] >= image.height:
                continue
            if any(intersects(box, previous) for previous in occupied):
                continue
            draw.rounded_rectangle(box, radius=2, fill=(15, 29, 32, min(alpha, 190)))
            draw.text((x, y), text, font=FONT, fill=ImageColor.getrgb(symbol.color) + (alpha,))
            occupied.append(box)
            labels += 1
            break
    return Image.alpha_composite(image.convert('RGBA'), overlay).convert('RGB')
