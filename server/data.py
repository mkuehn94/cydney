from dataclasses import dataclass, field
import math

HOME = (13.4185, 52.4975)  # old demo position (berlin)
MUNICH = (11.57549, 48.13743)  # munich center
DEFAULT_CENTER = (11.5099957, 48.1816035)  # lon, lat !!
DEFAULT_ZOOM = 18


@dataclass(frozen=True)
class Layer:
    id: str
    label: str
    code: str
    color: str
    source: str


# Layers without a connected adapter are marked planned; see feeds.SPECS.
PLANNED = 'Planned — no data source connected yet'

LAYERS = [
    Layer('alerts', 'Public warnings', '!', '#ff9c68', 'Official warning feeds'),
    Layer('transit', 'Bus / tram stops & vehicles', 'B', '#b49aff', 'MVV GTFS / operator realtime when available'),
    Layer('rail', 'Rail / departures', 'U', '#ceadff', 'MVV GTFS / rail operator feeds'),
    Layer('flights', 'Aircraft / altitude', 'A', '#7fd5ff', 'ADSB.lol community ADS-B feed'),
    Layer('traffic', 'Traffic / closures', 'T', '#ffc569', 'ADSB.lol ground aircraft — no road traffic source connected yet'),
    Layer('bikes', 'Bikes / scooters', 'b', '#b4e878', 'GBFS feeds'),
    Layer('parking', 'Parking spaces', 'P', '#8bb9ff', 'OpenStreetMap parking features'),
    Layer('charging', 'EV charging', 'E', '#77ead2', 'OpenStreetMap charging stations'),
    Layer('cameras', 'Public cameras', 'C', '#78d5d0', 'Publicly accessible webcams'),
    Layer('weather', 'Rain / wind / UV', 'W', '#f0d983', 'DWD open weather observations'),
    Layer('air', 'Air / pollen / noise', 'Q', '#a6dd93', PLANNED),
    Layer('water', 'River / flood gauges', '~', '#69c7ed', PLANNED),
    Layer('utilities', 'Power / water outages', 'X', '#ffad86', PLANNED),
    Layer('events', 'Markets / local events', '*', '#ee98d4', PLANNED),
    Layer('news', 'Local public notices', 'N', '#f2c4a0', PLANNED),
    Layer('places', 'Shops / businesses', 'S', '#ddf77e', 'OSM + AllThePlaces'),
    Layer('food', 'Food / cafes / hours', 'F', '#e3d894', 'OSM + AllThePlaces'),
    Layer('health', 'Pharmacies / health', 'H', '#f69bb5', 'OSM + AllThePlaces'),
    Layer('amenities', 'Toilets / water / bins', 'o', '#c9dfaa', 'OpenStreetMap'),
    Layer('access', 'Access / lifts / steps', '=', '#9ccfe6', 'OpenStreetMap'),
    Layer('connectivity', 'Public Wi-Fi / outages', 'I', '#c5b1f6', PLANNED),
    Layer('nature', 'Trees / wildlife reports', 'n', '#87c98c', 'OpenStreetMap'),
    Layer('astronomy', 'ISS / sky visibility', '+', '#c2c5ff', PLANNED),
    Layer('vessels', 'Boats / ferries', 'V', '#78bdec', PLANNED),
    Layer('works', 'Construction / permits', 'K', '#e5ad71', 'Munich open data roadworks'),
    Layer('hazards', 'Fire / lightning / flood', '!', '#fc827a', PLANNED),
    Layer('radio', 'Amateur radio repeaters', 'R', '#d7b2f0', PLANNED),
    Layer('satellite', 'Imagery acquisition age', 's', '#a0b8d2', PLANNED),
    Layer('heritage', 'History / landmarks', 'h', '#d7bf96', 'OpenStreetMap'),
    Layer('sensors', 'Public city sensors', 'v', '#86c6b9', PLANNED),
]
BY_LAYER = {layer.id: layer for layer in LAYERS}

# Layers backed by an adapter in feeds.SPECS; planned layers stay available
# in the layer menu but are off by default and report 'not connected'.
CONNECTED_LAYERS = frozenset(layer.id for layer in LAYERS if layer.source != PLANNED)


@dataclass(frozen=True)
class Observation:
    id: str
    layer: str
    title: str
    short: str
    position: tuple[float, float]
    value: str
    sources: tuple[str, ...]
    age_minutes: int = 0
    warning: bool = False
    demo: bool = True  # adapters set this to False
    area_radius_m: float | None = None
    is_vehicle: bool = False
    observed_at: str | None = None
    fetched_at: str | None = None
    expires_at: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None  # exclusive
    stale: bool = False
    kind: str = 'demo'
    position_kind: str = 'demo'
    attribution: tuple[str, ...] = ()
    details: dict = field(default_factory=dict, compare=False, hash=False)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, math.radians(b[0] - a[0])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.atan2(math.sqrt(h), math.sqrt(max(0, 1 - h)))


def distance_label(meters: float) -> str:
    return f'{round(meters)}m' if meters < 1000 else f'{meters / 1000:.1f}km'
