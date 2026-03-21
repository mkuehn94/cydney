from datetime import timedelta
import hashlib

from google.transit import gtfs_realtime_pb2 as rt

from .adapters import clean, inside, iso, point
from .data import Observation
from .ground import epoch

# TODO find an operator that actually publishes this for munich, DB maybe?


def parse_vehicle_positions(content, now, bounds, provider, attribution):
    feed = rt.FeedMessage()
    feed.ParseFromString(content)
    if not feed.IsInitialized() or not feed.header.HasField('timestamp'):
        raise ValueError('GTFS-RT without timestamp')
    if feed.header.incrementality != rt.FeedHeader.FULL_DATASET:
        raise ValueError('differential GTFS-RT not supported')
    updated = epoch(feed.header.timestamp)
    if not updated or not -30 <= (now - updated).total_seconds() <= 120:
        raise ValueError('GTFS-RT timestamp too old (or in the future)')
    namespace = hashlib.sha256(provider.encode()).hexdigest()[:12]
    items = []
    for entity in feed.entity:
        if entity.is_deleted or not entity.HasField('vehicle'):
            continue
        vehicle = entity.vehicle
        if not vehicle.HasField('position') or not vehicle.HasField('timestamp'):
            continue
        observed = epoch(vehicle.timestamp)
        pos = point((vehicle.position.longitude, vehicle.position.latitude))
        if not observed or not -30 <= (now - observed).total_seconds() <= 90 or not pos or not inside(pos, bounds):
            continue
        if vehicle.trip.schedule_relationship == rt.TripDescriptor.CANCELED:
            continue
        key = f'gtfsrt:{namespace}:{entity.id}'
        route = clean(vehicle.trip.route_id)
        label = clean(vehicle.vehicle.label or vehicle.vehicle.id or entity.id)
        parts = ['REPORTED VEHICLE POSITION', f'Route {route}' if route else 'Route unknown']
        items.append(
            Observation(
                key,
                'transit',
                label or 'Transit vehicle',
                'VEH',
                pos,
                ' / '.join(parts),
                (key,),
                demo=False,
                is_vehicle=True,
                observed_at=iso(observed),
                fetched_at=iso(now),
                expires_at=iso(observed + timedelta(seconds=120)),
                kind='observation',
                position_kind='observed',
                attribution=(attribution,),
                details={
                    'route_id': route,
                    'trip_id': clean(vehicle.trip.trip_id),
                    'vehicle_id': clean(vehicle.vehicle.id),
                    'feed_timestamp': iso(updated),
                    'provider': provider,
                    'position_note': 'Provider VehiclePosition; no interpolation. Route ID is not a public route name.',
                },
            )
        )
    return items
