import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import time

from .tiles import world_pixel

MARGIN = 128


def map_identity(device):
    x, y = world_pixel(*device.center, device.zoom)
    x, y = round(x), round(y)
    key = hashlib.blake2s(json.dumps((x, y, device.zoom, device.size)).encode(), digest_size=8).hexdigest()
    return key, {'X-Map-Key': key, 'X-Map-X': str(x), 'X-Map-Y': str(y), 'X-Map-Zoom': str(device.zoom)}


def encode_preview(image):
    buffer = BytesIO()
    for quality in (55, 40, 25, 15):
        buffer.seek(0)
        buffer.truncate()
        image.save(buffer, 'JPEG', quality=quality, subsampling=2)
        if buffer.tell() <= 48 * 1024:
            return buffer.getvalue()
    raise ValueError('map preview too big')


@dataclass
class Preview:
    body: bytes | None = None
    task: asyncio.Task | None = None
    due: float = 0


class MapPreviewStore:
    def __init__(self, tiles):
        self.tiles = tiles
        self.entries = OrderedDict()
        self.slots = asyncio.Semaphore(2)

    def get(self, device):
        key, headers = map_identity(device)
        if not self.tiles.enabled:
            return None, headers
        saved = self.entries.setdefault(key, Preview())
        self.entries.move_to_end(key)
        if saved.task is None and time.monotonic() >= saved.due:
            center, zoom = device.center, device.zoom
            width, height = device.size

            async def load():
                try:
                    async with self.slots:
                        image, status = await asyncio.wait_for(
                            self.tiles.map_image(center, zoom, width + 2 * MARGIN, height + 2 * MARGIN), 12
                        )
                        if status not in ('ready', 'stale'):
                            raise ValueError('border tiles missing')
                        saved.body = await asyncio.to_thread(encode_preview, image)
                    saved.due = time.monotonic() + 120
                except Exception:
                    saved.due = time.monotonic() + 5
                finally:
                    saved.task = None

            saved.task = asyncio.create_task(load(), name='map-border:' + key)
        while len(self.entries) > 12:
            _, old = self.entries.popitem(last=False)
            if old.task:
                old.task.cancel()
        return saved.body, headers

    async def close(self):
        tasks = [entry.task for entry in self.entries.values() if entry.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
