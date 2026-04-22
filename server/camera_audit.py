import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

import httpx

from . import cameras
from .camera_fallbacks import failure
from .feeds import FeedStore


async def audit(feeds, items, *, concurrency=2, emit=None):
    slots = asyncio.Semaphore(concurrency)

    async def check(item):
        async with slots:
            start = time.monotonic()
            result = {'id': item.id, 'title': item.title}
            try:
                image, fetched = await asyncio.wait_for(cameras.snapshot(feeds, item, datetime.now(timezone.utc)), 50)
                kind = image.info.get('cydney_capture_kind') or (
                    'video' if item.details.get('media_kind') == 'video' else 'snapshot'
                )
                result.update(
                    outcome=kind,
                    dimensions=list(image.size),
                    fetched_at=fetched,
                    status=image.info.get('cydney_capture_status', kind.upper() + ' / capture time unverified'),
                )
            except Exception as error:
                status, _ = failure(error)
                result.update(outcome='unavailable', status=status)
            result['seconds'] = round(time.monotonic() - start, 3)
            if emit:
                emit(result)
            return result

    return await asyncio.gather(*(check(item) for item in items))


async def main(args):
    def emit(record):
        print(json.dumps(record, ensure_ascii=False), flush=True)

    with tempfile.TemporaryDirectory(prefix='cydney-camera-audit-') as directory:
        async with httpx.AsyncClient() as client:
            feeds = FeedStore(client, Path(directory), enabled=True)
            center = (args.longitude, args.latitude)
            items, failures = await asyncio.wait_for(cameras.catalog(feeds, datetime.now(timezone.utc), center), 60)
            items = cameras.nearby(items, center)
            if args.provider:
                items = tuple(item for item in items if item.id.split(':')[1] in args.provider)
            emit(
                {
                    'audit_started_at': datetime.now(timezone.utc).isoformat(),
                    'center': center,
                    'radius_km': 50,
                    'camera_count': len(items),
                    'directory_failures': failures,
                }
            )
            results = await audit(feeds, items, concurrency=args.concurrency, emit=emit)
            counts = Counter(result['outcome'] for result in results)
            emit(
                {
                    'summary': dict(counts),
                    'total': len(results),
                    'current_captures': counts['snapshot'] + counts['video'],
                    'degraded_images': counts['preview'] + counts['archive'],
                    'unavailable': counts['unavailable'],
                }
            )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--longitude', type=float, default=11.5761)
    parser.add_argument('--latitude', type=float, default=48.1374)
    parser.add_argument('--provider', action='append', help='Optional exact provider ID; repeatable')
    parser.add_argument('--concurrency', type=int, choices=range(1, 5), default=2)
    args = parser.parse_args()
    if not -180 <= args.longitude <= 180 or not -85 <= args.latitude <= 85:
        parser.error('Coordinates outside supported map bounds')
    asyncio.run(main(args))
