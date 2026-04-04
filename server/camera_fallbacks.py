import asyncio
from datetime import datetime
from html import unescape
import re
from urllib.parse import urlsplit

import httpx


class CameraUnavailable(ValueError):
    def __init__(self, message, retry_after=60):
        super().__init__(message)
        self.retry_after = retry_after


def failure(error):
    if isinstance(error, CameraUnavailable):
        return str(error), error.retry_after
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        if code in (404, 410):
            return 'OFFLINE / provider image removed (HTTP ' + str(code) + ')', 300
        if code in (401, 403):
            return 'UNAVAILABLE / provider denied access (HTTP ' + str(code) + ')', 60
        if code == 429:
            return 'UNAVAILABLE / provider rate limit; retrying', 60
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return 'UNAVAILABLE / provider timeout; retrying', 15
    return 'UNAVAILABLE / provider capture failed; retrying', 10


def describe(image, kind, status, retry_after=5):
    # thumbnail() keeps .info, so the source info survives
    image.info['cydney_capture_kind'] = kind
    image.info['cydney_capture_status'] = status
    image.info['cydney_refresh_seconds'] = retry_after
    return image


async def read_image(feeds, url, now, *, ttl=5):
    from .cameras import decode_snapshot

    raw = await feeds.shared_download(url, ttl, now=now, max_bytes=2_000_000, timeout=12)
    return await asyncio.to_thread(decode_snapshot, raw.content), raw.fetched_at


async def feratel_snapshot(feeds, item, now):
    from .cameras import camera_url

    ident = item.id.split(':')[-1]
    raw = await feeds.shared_download(camera_url(item.id), 120, now=now, max_bytes=2_000_000, timeout=12)
    # page also lists nearby cams, only take our's
    text = unescape(raw.content.decode('utf-8'))
    pattern = re.compile(
        r'https://wtvpict(?:fc)?\.feratel\.com/picture/[0-9]+/' + re.escape(ident) + r'\.jpeg(?:\?[^"\s<>]*)?'
    )
    # og:image is often 4K, take the video thumbnail first
    thumbnails = re.findall(r'"thumbnailUrl"\s*:\s*"([^"]+)"', text)
    url = next((url for url in thumbnails if pattern.fullmatch(url)), None)
    if url is None:
        match = pattern.search(text)
        if not match:
            raise CameraUnavailable('UNAVAILABLE / no snapshot')
        url = match[0]
    parsed = urlsplit(url)
    # the old *fc hosts redirect to html, use this one
    url = 'https://wtvpict.feratel.com' + parsed.path
    if parsed.query:
        url += '?' + parsed.query
    image, fetched = await read_image(feeds, url, now)
    return describe(image, 'snapshot', 'SNAPSHOT / checks every 5s'), fetched


async def siemens_snapshot(feeds, item, now):
    from .cameras import camera_url

    raw = await feeds.shared_download(camera_url(item.id), 120, now=now, max_bytes=100_000, timeout=12)
    text = raw.content.decode('utf-8')
    # php page is a viewer not a jpeg, get the image path out of it
    if not re.search(
        r'<img\b[^>]*\bsrc=["\'](?:https://munich-wetter\.de)?'
        r'/WebCam/annke/snapshot/webcam-muc\.jpg["\']',
        text,
        re.I,
    ):
        raise CameraUnavailable('UNAVAILABLE / no snapshot')
    return await read_image(feeds, 'https://munich-wetter.de/WebCam/annke/snapshot/webcam-muc.jpg', now)


async def terra_archive(feeds, item, now):
    from .cameras import camera_url

    page = camera_url(item.id)
    raw = await feeds.shared_download(page + '01func/get_current.php', 300, now=now, max_bytes=1000, timeout=8)
    path = raw.content.decode('ascii').strip()
    match = re.fullmatch(r'([0-9]{4}/[0-9]{2}/[0-9]{2})/([0-9]{4})m\.jpg', path)
    if not match:
        raise CameraUnavailable('OFFLINE / no image', 300)
    try:
        date = datetime.strptime(match[1] + match[2], '%Y/%m/%d%H%M')
    except ValueError as error:
        raise CameraUnavailable('OFFLINE / invalid archive date', 300)
    image, fetched = await read_image(feeds, page + path, now, ttl=300)
    return describe(image, 'archive', f'ARCHIVE {date:%Y-%m-%d} / NOT LIVE', 300), fetched


async def provider_preview(feeds, item, now):
    from .cameras import camera_url

    provider, ident = item.id.split(':')[1:]
    if provider.startswith('wetter-'):
        raw = await feeds.shared_download(camera_url(item.id), 120, now=now, max_bytes=2_000_000, timeout=12)
        text = unescape(raw.content.decode('utf-8'))
        url = f'https://cs3.wettercomassets.com/thumbnails/variants/{ident}/16x9_player.jpg'
        if url not in text:
            raise CameraUnavailable('UNAVAILABLE / no preview')
        offline = bool(re.search(r'id=["\']livecam-offline["\']', text))
    elif provider == 'michaeli':
        # preview from the owner
        url = 'https://xn--mnchen-3ya.webcam/thumbnails/youtube@100x.webp'
        offline = False
    else:
        raise CameraUnavailable('UNAVAILABLE / no preview')
    image, fetched = await read_image(feeds, url, now, ttl=60)
    status = 'OFFLINE / provider preview; NOT LIVE' if offline else 'PREVIEW / video unavailable; NOT LIVE'
    return describe(image, 'preview', status, 60), fetched
