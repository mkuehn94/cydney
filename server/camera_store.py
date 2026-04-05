import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import time

from . import cameras
from .adapters import inside
from .camera_regions import REGIONS
from .camera_fallbacks import CameraUnavailable, failure
from .data import distance_m


@dataclass
class Capture:
    image: object = None
    fetched_at: str | None = None
    status: str = 'LOADING / first capture in background'
    due: float = 0
    generation: int = 0
    task: asyncio.Task | None = None


class CameraStore:
    def __init__(self, feeds):
        self.feeds = feeds
        self.captures = OrderedDict()
        self.directories = OrderedDict()
        self.slots = asyncio.Semaphore(2)

    def directory(self, device):
        if not self.feeds.enabled:
            device.camera_directory_status = 'Camera network access disabled'
            return 0
        center = device.center
        key = (
            tuple(p for p, r in REGIONS.items() if inside(center, r.bounds)),
            distance_m(center, (-0.1276, 51.5072)) <= 110000,
            inside(center, (-125, 32, -113, 43)),
        )
        saved = self.directories.setdefault(
            key, {'items': (), 'status': 'Loading camera directory...', 'due': 0, 'task': None, 'generation': 0}
        )
        self.directories.move_to_end(key)
        if saved['task'] is None and time.monotonic() >= saved['due']:

            async def load():
                try:
                    rows, failures = await asyncio.wait_for(
                        cameras.catalog(self.feeds, datetime.now(timezone.utc), center), 25
                    )
                    saved['items'] = tuple(rows)
                    saved['status'] = (
                        'Some providers unavailable; available cameras retained'
                        if failures
                        else 'Within 50 km / capture when opened'
                    )
                    saved['due'] = time.monotonic() + 300
                except Exception:
                    saved['status'] = 'Directory unavailable / retrying; cached entries retained'
                    saved['due'] = time.monotonic() + 15
                finally:
                    saved['generation'] += 1
                    saved['task'] = None

            saved['task'] = asyncio.create_task(load(), name='camera-directory')
        # keep the open camera even if the directoy fails
        selected = device.camera
        rows = cameras.nearby(saved['items'], center)
        if rows or saved['due']:
            device.camera_directory = rows
            if device.page == 'camera' and selected and selected not in rows:
                device.camera_directory += (selected,)
        device.camera_directory_status = saved['status']
        self._trim(self.directories, 24, lambda value: value['task'])
        return saved['generation']

    def update(self, device):
        generation = self.directory(device)
        if device.page != 'camera':
            return (generation, device.camera_directory_status)
        item = device.camera
        if not self.feeds.enabled or item is None:
            device.camera_status = (
                'Camera unavailable / network access disabled' if not self.feeds.enabled else 'camera not found'
            )
            return (generation, device.camera_status)
        saved = self.captures.setdefault(item.id, Capture())
        self.captures.move_to_end(item.id)
        if saved.task is None and time.monotonic() >= saved.due:

            async def capture():
                try:
                    async with self.slots:
                        image, fetched = await asyncio.wait_for(
                            cameras.snapshot(self.feeds, item, datetime.now(timezone.utc)), 50
                        )
                    kind = image.info.get('cydney_capture_kind')
                    if (
                        kind in ('preview', 'archive')
                        and saved.image is not None
                        and saved.image.info.get('cydney_capture_kind') not in ('preview', 'archive')
                    ):
                        raise CameraUnavailable('STALE / refresh failed', 15)
                    # dont replace a live image with some old preview
                    image.thumbnail((960, 720))
                    saved.image, saved.fetched_at = image, fetched
                    video = item.details.get('media_kind') == 'video'
                    saved.status = image.info.get('cydney_capture_status') or (
                        'VIDEO CAPTURE / checks every 5s' if video else 'SNAPSHOT / checks every 5s'
                    )
                    saved.due = time.monotonic() + image.info.get('cydney_refresh_seconds', 5)
                except Exception as error:
                    status, delay = failure(error)
                    if saved.image is not None:
                        kind = saved.image.info.get('cydney_capture_kind')
                        status = (
                            saved.image.info['cydney_capture_status'] + ' / STALE'
                            if kind in ('preview', 'archive')
                            else 'STALE / refresh failed; retrying'
                        )
                    saved.status = status
                    saved.due = time.monotonic() + delay
                finally:
                    saved.generation += 1
                    saved.task = None

            saved.task = asyncio.create_task(capture(), name='camera-capture:' + item.id)
        device.camera_image, device.camera_fetched_at, device.camera_status = (
            saved.image,
            saved.fetched_at,
            saved.status,
        )
        self._trim(self.captures, 32, lambda value: value.task)
        return (generation, item.id, saved.generation)

    @staticmethod
    def _trim(entries, limit, task):
        if len(entries) <= limit:
            return
        for key in tuple(entries):
            if len(entries) <= limit:
                break
            if task(entries[key]) is None:
                entries.pop(key)

    async def close(self):
        tasks = [s.task for s in self.captures.values() if s.task]
        tasks += [s['task'] for s in self.directories.values() if s['task']]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
