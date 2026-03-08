from .adapters import point, timestamp
from .data import BY_LAYER

SOURCE_PREFIXES = {
    'webcams': 'webcam:',
    'osm': 'osm:',
    'atp': 'atp:',
    'works': 'munich:works:',
    'dwd': 'dwd:',
    'nina': 'bbk:',
    'aircraft': 'adsblol:',
    'mvv': 'mvv:stop:',
    'mvg': 'mvg:stop:',
    'myradl': 'gbfs:myradl:',
    'dott': 'gbfs:dott:',
    'transit_gps': 'gtfsrt:',
}
KINDS = {'listing', 'observation', 'schedule', 'availability', 'alert'}
POSITIONS = {'listing', 'observed', 'representative', 'station', 'stop', 'citywide', 'available'}


def real_record(item, source=None):
    prefix = SOURCE_PREFIXES.get(source)
    if not all(isinstance(value, str) for value in (item.kind, item.position_kind, item.layer)):
        return False
    if not isinstance(item.details, dict) or point(item.position) is None:
        return False
    if item.demo or item.kind not in KINDS or item.position_kind not in POSITIONS or item.layer not in BY_LAYER:
        return False
    if not isinstance(item.id, str) or not item.id.startswith((prefix,) if prefix else tuple(SOURCE_PREFIXES.values())):
        return False
    if (
        not item.sources
        or not all(isinstance(s, str) and s for s in item.sources)
        or not item.attribution
        or not all(isinstance(a, str) and a.strip() for a in item.attribution)
        or timestamp(item.fetched_at) is None
    ):
        return False
    if any(s.lower().startswith(('demo:', 'fixture:', 'test:')) for s in item.sources):
        return False
    if item.observed_at and timestamp(item.observed_at) is None:
        return False
    if any(value and timestamp(value) is None for value in (item.expires_at, item.starts_at, item.ends_at)):
        return False
    if item.is_vehicle and item.kind == 'observation':
        return item.position_kind == 'observed' and timestamp(item.observed_at) is not None
    if item.kind == 'availability':
        return (
            item.position_kind == 'available'
            and timestamp(item.observed_at) is not None
            and timestamp(item.expires_at) is not None
        )
    if item.kind == 'schedule':
        return (
            item.position_kind == 'stop'
            and timestamp(item.details.get('computed_at')) is not None
            and timestamp(item.expires_at) is not None
        )
    return True
