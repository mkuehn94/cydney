from dataclasses import dataclass

from .fleet import available_class

CLOSE_ZOOM = 16
# ZOOM_FADE = (30, 46, 64, 110, 153, 209, 240)
ZOOM_FADE = (46, 64, 82, 110, 153, 209, 240)  # z12..z18


@dataclass(frozen=True)
class Rule:
    alpha: int
    priority: int
    min_zoom: int = 12
    fade_with_zoom: bool = False
    glyph: bool = True
    individual: bool = False


ALERT = Rule(255, 100, individual=True)
UNKNOWN_VEHICLE = Rule(255, 95)
USEFUL_AMENITY = Rule(150, 45)
MINOR_AMENITY = Rule(100, 25, CLOSE_ZOOM, glyph=False)
DEFAULT = Rule(180, 40)

RULES = {
    'alerts': ALERT,
    'hazards': ALERT,
    'transit': Rule(240, 70, fade_with_zoom=True),
    'rail': Rule(240, 70, fade_with_zoom=True),
    'flights': Rule(235, 88),
    'traffic': Rule(235, 90),
    'bikes': Rule(240, 55, fade_with_zoom=True),
    'parking': Rule(240, 45, fade_with_zoom=True),
    'charging': Rule(240, 50, fade_with_zoom=True),
    'cameras': Rule(230, 86),
    'weather': Rule(230, 86),
    'air': Rule(230, 86),
    'water': Rule(230, 86),
    'utilities': Rule(230, 92),
    'events': Rule(230, 88),
    'news': Rule(255, 98),
    'places': Rule(200, 40, CLOSE_ZOOM, glyph=False),
    'food': Rule(200, 42, CLOSE_ZOOM, glyph=False),
    'health': Rule(110, 30),
    'amenities': MINOR_AMENITY,
    'access': Rule(175, 35, CLOSE_ZOOM, glyph=False),
    'connectivity': Rule(190, 60),
    'nature': Rule(190, 40, glyph=False),
    'astronomy': Rule(255, 95),
    'vessels': Rule(255, 95),
    'works': Rule(110, 30),
    'radio': Rule(210, 78),
    'satellite': Rule(210, 78),
    'heritage': Rule(180, 35, CLOSE_ZOOM, glyph=False),
    'sensors': Rule(255, 95),
}

AMENITY_CODES = {
    'toilets': 'WC',
    'drinking_water': 'H2O',
    'bicycle_repair_station': 'FIX',
    'atm': 'ATM',
}


def amenity_code(item):
    return AMENITY_CODES.get((item.details.get('tags') or {}).get('amenity')) if item.layer == 'amenities' else None


def rule_for(item):
    if item.warning:
        return ALERT  #warnings always visible
    if available_class(item) == 'vehicle':
        return UNKNOWN_VEHICLE
    if item.layer == 'amenities' and amenity_code(item):
        return USEFUL_AMENITY
    return RULES.get(item.layer, DEFAULT)


def overlay_alpha(item, zoom):
    rule = rule_for(item)
    if zoom < rule.min_zoom:
        return 0
    return ZOOM_FADE[max(0, min(6, zoom - 12))] if rule.fade_with_zoom else rule.alpha
