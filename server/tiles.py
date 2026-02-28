import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import time

import httpx
from PIL import Image, ImageDraw

MAX_LAT = 85.05112878


def world_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    size = 256 * 2**zoom
    return (lon + 180) / 360 * size, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * size


def coordinate(x: float, y: float, zoom: int) -> tuple[float, float]:
    size = 256 * 2**zoom
    return (x / size * 360 - 180 + 180) % 360 - 180, math.degrees(
        math.atan(math.sinh(math.pi * (1 - 2 * max(0, min(size, y)) / size)))
    )


@dataclass
class Tile:
    image: Image.Image | None
    status: str


class TileCache:
    def __init__(self, client: httpx.AsyncClient, directory: Path | None = None, enabled: bool = True):
        self.client = client
        self.directory = directory or Path('.cache/tiles')
        self.enabled = enabled
        self.locks: dict[str, asyncio.Lock] = {}
        self.failed_until: dict[str, float] = {}
        self.downloads = asyncio.Semaphore(4)
        self.memory = OrderedDict()
        self.maps = OrderedDict()
        self.pending = {}
        self.buffering = {}
        self.generation = 0
        self.user_agent = os.environ.get('CYD_TILE_USER_AGENT', 'Cydney/0.2 (local CYD prototype)')

    def remember(self, key, image, meta):
        self.memory[key] = (image, meta)
        self.memory.move_to_end(key)
        while len(self.memory) > 128:
            self.memory.popitem(last=False)

    def cached_tile(self, zoom: int, x: int, y: int) -> tuple[Tile, dict]:
        if not self.enabled:
            return Tile(None, 'offline'), {}
        if not 0 <= y < 2**zoom:
            return Tile(None, 'unavailable'), {}
        key = f'{zoom}-{x % 2**zoom}-{y}'
        if key in self.memory:
            self.memory.move_to_end(key)
            image, meta = self.memory[key]
        else:
            try:
                with Image.open(self.directory / f'{key}.png') as opened:
                    image = opened.convert('RGB')
            except OSError:
                return Tile(None, 'unavailable'), {}
            try:
                meta = json.loads((self.directory / f'{key}.json').read_text())
            except (OSError, ValueError):
                meta = {}
            self.remember(key, image, meta)
        status = 'cached' if meta.get('expires_at', 0) > time.time() else 'stale'
        return Tile(image, status), meta

    async def get(self, zoom: int, x: int, y: int) -> Tile:
        if not self.enabled:
            return Tile(None, 'offline')
        if not 0 <= y < 2**zoom:
            return Tile(None, 'unavailable')
        x %= 2**zoom
        key = f'{zoom}-{x}-{y}'
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            path = self.directory / f'{key}.png'
            meta_path = self.directory / f'{key}.json'
            tile, meta = self.cached_tile(zoom, x, y)
            cached = tile.image
            if tile.status == 'cached':
                return tile
            if self.failed_until.get(key, 0) > time.time():
                return Tile(cached, 'stale' if cached else 'unavailable')
            headers = {'User-Agent': self.user_agent}
            if meta.get('etag'):
                headers['If-None-Match'] = meta['etag']
            if meta.get('last_modified'):
                headers['If-Modified-Since'] = meta['last_modified']
            try:
                # TODO satellite imagery as second basemap (sentinel-2? esri?)
                async with self.downloads:
                    response = await self.client.get(
                        f'https://tile.openstreetmap.org/{zoom}/{x}/{y}.png', headers=headers
                    )
                if response.headers.get('x-blocked'):
                    raise ValueError('blocked by tile server')
                if response.status_code == 304 and cached is not None:
                    image = cached
                else:
                    response.raise_for_status()
                    with Image.open(BytesIO(response.content)) as opened:
                        if opened.size != (256, 256):
                            raise ValueError('tile is not 256x256')
                        image = opened.convert('RGB')
                cache_control = response.headers.get('cache-control', '')
                max_age = re.search(r'max-age=(\d+)', cache_control)
                ttl = int(max_age.group(1)) if max_age else 7 * 86400
                ttl = max(0, ttl - int(response.headers.get('age') or 0))
                if 'no-cache' in cache_control:
                    ttl = 0
                metadata = {
                    'expires_at': time.time() + ttl,
                    'etag': response.headers.get('etag', meta.get('etag')),
                    'last_modified': response.headers.get('last-modified', meta.get('last_modified')),
                }
                try:
                    if 'no-store' in cache_control:
                        self.memory.pop(key, None)
                        path.unlink(missing_ok=True)
                        meta_path.unlink(missing_ok=True)
                    else:
                        self.remember(key, image, metadata)
                        self.directory.mkdir(parents=True, exist_ok=True)
                        image.save(path)
                        meta_path.write_text(json.dumps(metadata))
                except OSError:
                    pass  # no disk cache then
                return Tile(image, 'ready')
            except (httpx.HTTPError, OSError, ValueError) as error:
                print("tile", zoom, x, y, "failed:", repr(error))
                self.failed_until[key] = time.time() + 60
                return Tile(cached, 'stale' if cached else 'unavailable')
            finally:
                if len(self.locks) > 512:
                    self.locks = {k: v for k, v in self.locks.items() if v.locked()}
                    self.failed_until = {k: v for k, v in self.failed_until.items() if v > time.time()}

    def buffer_view(self, center, zoom, width, height, margin=128):
        if not self.enabled:
            return
        cx, cy = world_pixel(*center, zoom)
        left, top = round(cx - width / 2 - margin), round(cy - height / 2 - margin)
        positions = [
            (x, y)
            for x in range(left // 256, (left + width + 2 * margin - 1) // 256 + 1)
            for y in range(top // 256, (top + height + 2 * margin - 1) // 256 + 1)
            if 0 <= y < 2**zoom
        ]
        positions.sort(key=lambda p: (p[0] * 256 + 128 - cx) ** 2 + (p[1] * 256 + 128 - cy) ** 2)
        for x, y in positions:
            if len(self.buffering) >= 2:
                break
            x %= 2**zoom
            key = f'{zoom}-{x}-{y}'
            lock = self.locks.get(key)
            if key in self.buffering or (lock and lock.locked()) or self.failed_until.get(key, 0) > time.time():
                continue
            if self.cached_tile(zoom, x, y)[0].status == 'cached':
                continue

            async def load(x=x, y=y, zoom=zoom, key=key):
                try:
                    await asyncio.sleep(0.05)  # visible tiles first
                    await self.get(zoom, x, y)
                finally:
                    self.buffering.pop(key, None)

            self.buffering[key] = asyncio.create_task(load(), name='osm-buffer:' + key)

    async def close(self):
        tasks = list(self.pending.values()) + list(self.buffering.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        self.buffering.clear()

    async def map_image(
        self, center: tuple[float, float], zoom: int, width: int, height: int, *, wait=True
    ) -> tuple[Image.Image, str]:
        cx, cy = world_pixel(*center, zoom)
        left, top = round(cx - width / 2), round(cy - height / 2)
        positions = [
            (x, y)
            for x in range(left // 256, (left + width - 1) // 256 + 1)
            for y in range(top // 256, (top + height - 1) // 256 + 1)
        ]
        if not wait and self.enabled:
            key = (tuple(center), zoom, width, height)
            cached = self.maps.get(key)
            if cached and cached[2] > time.monotonic():
                self.maps.move_to_end(key)
                return cached[:2]
            local = [self.cached_tile(zoom, x, y)[0] for x, y in positions]
            if all(tile.status == 'cached' for tile in local):
                return self.compose(positions, local, left, top, width, height)
            if key not in self.pending:

                async def load():
                    try:
                        image, status = await self.map_image(center, zoom, width, height)
                        self.maps[key] = (image, status, time.monotonic() + 30)
                        while len(self.maps) > 16:
                            self.maps.popitem(last=False)
                        self.generation += 1
                    finally:
                        self.pending.pop(key, None)

                self.pending[key] = asyncio.create_task(load())
            if cached:
                return cached[:2]
            image, status = self.compose(positions, local, left, top, width, height)
            return image, 'loading' if status == 'unavailable' else status
        tiles = await asyncio.gather(*(self.get(zoom, x, y) for x, y in positions))
        return self.compose(positions, tiles, left, top, width, height)

    @staticmethod
    def compose(positions, tiles, left, top, width, height):
        image = Image.new('RGB', (width, height), '#192726')
        draw = ImageDraw.Draw(image)
        for x in range(0, width, 16):
            draw.line((x, 0, x, height), fill='#263632')
        for y in range(0, height, 16):
            draw.line((0, y, width, y), fill='#263632')
        for (x, y), tile in zip(positions, tiles):
            if tile.image:
                image.paste(tile.image, (x * 256 - left, y * 256 - top))
        states = {tile.status for tile in tiles}
        status = (
            'offline'
            if states == {'offline'}
            else 'unavailable'
            if any(tile.image is None for tile in tiles)
            else 'stale'
            if 'stale' in states
            else 'ready'
        )
        return image, status
