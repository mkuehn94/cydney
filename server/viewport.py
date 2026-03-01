from dataclasses import dataclass
import hashlib
import math

from .adapters import POI_BOUNDS, inside
from .data import MUNICH, distance_m
from .tiles import coordinate, world_pixel

MUNICH_CITY = (11.35, 48.0, 11.8, 48.3)
MVV_REGION = (10.7, 47.4, 12.7, 49.0)


def boxes(bounds):
    west, south, east, north = bounds
    return [(west, south, east, north)] if west <= east else [(west, south, 180, north), (-180, south, east, north)]


def intersects(a, b):
    return any(
        max(x[0], y[0]) <= min(x[2], y[2]) and max(x[1], y[1]) <= min(x[3], y[3]) for x in boxes(a) for y in boxes(b)
    )


def screen_bounds(center, zoom, size, padding=0):
    cx, cy = world_pixel(*center, zoom)
    west, north = coordinate(cx - size[0] / 2 - padding, cy - size[1] / 2 - padding, zoom)
    east, south = coordinate(cx + size[0] / 2 + padding, cy + size[1] / 2 + padding, zoom)
    return west, south, east, north


def contains(outer, inner):
    return all(inside((lon, lat), outer) for lon in (inner[0], inner[2]) for lat in (inner[1], inner[3]))


@dataclass(frozen=True)
class Viewport:
    key: str
    center: tuple[float, float]
    bounds: tuple[float, float, float, float]
    detail: str = 'detail'

    @staticmethod
    def focus(center):
        # ~300m cells
        x, y = world_pixel(*center, 15)
        return coordinate((math.floor(x / 96) + 0.5) * 96, (math.floor(y / 96) + 0.5) * 96, 15)

    @classmethod
    def camera(cls, center, zoom, size):
        focus = cls.focus(center)
        detail = 'overview' if zoom <= 13 else 'detail'
        padding = max(max(size) * 0.7 + 48, 96 * 2 ** (zoom - 15))
        bounds = screen_bounds(focus, zoom, size, padding)
        identity = ','.join(f'{v:.6f}' for v in (*focus, *bounds)) + detail
        if detail == 'overview':
            identity += '-poi2'  # old overview caches only had stops
        return cls('view-' + hashlib.sha256(identity.encode()).hexdigest()[:16], focus, bounds, detail)

    @property
    def radius_m(self):
        return max(
            distance_m(self.center, (lon, lat))
            for lon in (self.bounds[0], self.bounds[2])
            for lat in (self.bounds[1], self.bounds[3])
        )

    def covers(self, center, zoom, size):
        detail = 'overview' if zoom <= 13 else 'detail'
        return self.detail == detail and contains(self.bounds, screen_bounds(center, zoom, size))

    def describe(self):
        return {
            'id': self.key,
            'center': self.center,
            'bounds': self.bounds,
            'detail': self.detail,
            'scope': 'padded camera viewport; not GPS',
            'overview_limited': self.detail == 'overview',
        }


DEFAULT_VIEW = Viewport('munich', MUNICH, POI_BOUNDS)
