from collections import Counter, defaultdict
from dataclasses import replace
import unicodedata

from .data import distance_m


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


def branch_key(item):
    tags = item.details.get('tags', {})
    fields = tuple(normalized(tags.get(field)) for field in ('name', 'addr:street', 'addr:housenumber'))
    return (item.layer, *fields) if all(fields) else None


def same_branch(first, second):
    key = branch_key(first)
    if not key or key != branch_key(second):
        return False
    if distance_m(first.position, second.position) > 30:
        return False
    first_tags = first.details.get('tags', {})
    second_tags = second.details.get('tags', {})
    for field in ('addr:postcode', 'addr:unit', 'level'):
        first_value, second_value = first_tags.get(field), second_tags.get(field)
        if first_value and second_value and normalized(first_value) != normalized(second_value):
            return False
    return True


def reconcile(items):
    items = list(items)
    osm, atp = defaultdict(list), defaultdict(list)
    for item in items:
        key = branch_key(item)
        if key and item.id.startswith('osm:'):
            osm[key].append(item)
        elif key and item.id.startswith('atp:'):
            atp[key].append(item)
    replacements, removed = {}, set()
    for key, branches in atp.items():
        matches = [(imported, mapped) for imported in branches for mapped in osm[key] if same_branch(imported, mapped)]
        import_matches = Counter(imported.id for imported, _ in matches)
        osm_matches = Counter(mapped.id for _, mapped in matches)
        for imported, mapped in matches:
            if import_matches[imported.id] != 1 or osm_matches[mapped.id] != 1:
                continue
            replacements[mapped.id] = replace(
                mapped,
                sources=mapped.sources + imported.sources,
                attribution=tuple(dict.fromkeys(mapped.attribution + imported.attribution)),
                details={
                    **mapped.details,
                    'match_rule': 'unique same-name/full-street-address within 30m; compatible unit/level',
                    'records': [
                        {'source': mapped.id, 'fetched_at': mapped.fetched_at, 'fields': mapped.details},
                        {'source': imported.id, 'fetched_at': imported.fetched_at, 'fields': imported.details},
                    ],
                    'field_policy': 'OSM display fields retained; ATP fields remain separately attributed',
                },
                stale=mapped.stale or imported.stale,
            )
            removed.add(imported.id)
    return tuple(replacements.get(item.id, item) for item in items if item.id not in removed)
