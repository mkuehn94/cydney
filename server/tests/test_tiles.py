import asyncio
from io import BytesIO

import httpx
from PIL import Image

from server.tiles import TileCache


def test_tile_cache(tmp_path):
    async def run():
        calls = []
        buffer = BytesIO()
        Image.new('RGB', (256, 256), 'green').save(buffer, 'PNG')

        def handler(request):
            calls.append(request)
            if request.headers.get('if-none-match') == 'tile-1':
                return httpx.Response(304, headers={'cache-control': 'max-age=0', 'etag': 'tile-1'})
            return httpx.Response(
                200, content=buffer.getvalue(), headers={'cache-control': 'max-age=0', 'etag': 'tile-1'}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = TileCache(client, tmp_path)
            first = await cache.get(15, 1, 1)
            second = await cache.get(15, 1, 1)
            assert first.image.getpixel((0, 0)) == second.image.getpixel((0, 0))
            assert len(calls) == 2
            assert calls[1].headers['if-none-match'] == 'tile-1'
            assert calls[0].headers['user-agent'].startswith('Cydney/')
            assert 'cache-control' not in calls[0].headers

    asyncio.run(run())


def test_disk_tile(tmp_path):
    async def run():
        count = 0
        buffer = BytesIO()
        Image.new('RGB', (256, 256), 'blue').save(buffer, 'PNG')

        def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(200, content=buffer.getvalue(), headers={'cache-control': 'max-age=3600'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = TileCache(client, tmp_path)
            await cache.get(15, 1, 1)
            assert (await cache.get(15, 1, 1)).status == 'cached'
            assert count == 1

    asyncio.run(run())


def test_tile_backoff(tmp_path):
    async def run():
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(503)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = TileCache(client, tmp_path)
            assert (await cache.get(15, 1, 1)).image is None
            assert (await cache.get(15, 1, 1)).image is None
            assert count == 1
            image, status = await cache.map_image((13.4, 52.5), 15, 320, 152)
            assert image.size == (320, 152)
            assert status == 'unavailable'
            await cache.close()

    asyncio.run(run())


def test_offline(tmp_path):
    async def run():
        async with httpx.AsyncClient() as client:
            cache = TileCache(client, tmp_path, enabled=False)
            image, status = await cache.map_image((13.4, 52.5), 15, 320, 152)
            assert status == 'offline'
            assert image.size == (320, 152)

    asyncio.run(run())
