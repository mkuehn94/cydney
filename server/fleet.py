def vehicle_class(provider, type_id):
    if type_id == 'dott_scooter':
        return 'scooter'
    if provider == 'myradl' or type_id == 'dott_bicycle':
        return 'bike'
    return 'vehicle'


def available_class(item):
    if item.kind != 'availability' or item.layer != 'bikes':
        return None
    explicit = item.details.get('vehicle_class')
    if explicit in ('scooter', 'bike', 'vehicle'):
        return explicit
    # old caches have no vehicle_class
    provider = item.id.split(':')[1] if item.id.startswith('gbfs:') else ''
    return vehicle_class(provider, item.details.get('vehicle_type'))


def fleet_counts(items):
    counts = {'scooters': 0, 'bikes': 0, 'other': 0, 'stale': 0}
    for item in items:
        kind = available_class(item)
        if kind:
            counts[{'scooter': 'scooters', 'bike': 'bikes', 'vehicle': 'other'}[kind]] += 1
            counts['stale'] += int(item.stale)
    return counts
