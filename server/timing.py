from datetime import datetime, timezone

TIMED_LAYERS = frozenset({'works', 'events', 'news', 'alerts', 'hazards', 'utilities', 'traffic'})


def parse_time(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except:
        return None


def timing_status(item, now=None):
    now = now or datetime.now(timezone.utc)
    start, end = parse_time(item.starts_at), parse_time(item.ends_at)
    if start and end and end <= start:
        return 'unknown'
    if end and end <= now:
        return 'ended'
    if start and start > now:
        return 'planned'
    if start and (end or item.kind == 'alert'):
        return 'active'
    return 'unknown'


def timing_label(item, now=None):
    if item.layer not in TIMED_LAYERS or item.demo or item.is_vehicle:
        return ''
    return {'active': 'ACTIVE', 'planned': 'PLANNED', 'unknown': 'TIMING UNKNOWN', 'ended': 'ENDED'}[
        timing_status(item, now)
    ]
