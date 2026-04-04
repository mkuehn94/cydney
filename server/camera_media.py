import asyncio
from html import unescape
import json
import re
import sys
from urllib.parse import urlsplit

from .camera_fallbacks import CameraUnavailable


async def command(*args, timeout=20):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode or not output:
            raise ValueError('stream unavailable')
        return output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def valid_video(provider, url):
    if not isinstance(url, str) or len(url) > 8192:
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
    except ValueError:
        return False
    if provider == 'michaeli':
        return bool(re.fullmatch(r'(?:[a-z0-9-]+\.)+googlevideo\.com', parsed.hostname or ''))
    if provider.startswith('feratel-'):
        return bool(
            re.fullmatch(r'sts[a-z0-9]+\.feratel\.(?:com|co\.at)', parsed.hostname or '')
            and re.fullmatch(r'/streams/[A-Za-z0-9_/-]+\.mp4', parsed.path)
        )
    if provider.startswith('wetter-'):
        return (
            bool(re.fullmatch(r'sts[a-z0-9]+\.feratel\.(?:com|co\.at)', parsed.hostname or ''))
            and bool(re.fullmatch(r'/streams/[A-Za-z0-9_/-]+\.mp4', parsed.path))
        ) or (
            bool(re.fullmatch(r'[a-z0-9-]+\.livespotting\.com', parsed.hostname or ''))
            and bool(re.fullmatch(r'/[A-Za-z0-9_/-]+\.m3u8', parsed.path))
        )
    return False


def wetter_video(content, ident):
    match = re.search(r"mediaItems:\s*'([^']+)'", content)
    if not match:
        if re.search(r'id=["\']livecam-offline["\']', content):
            raise CameraUnavailable('OFFLINE / stream disabled', 300)
        raise ValueError('stream metadata unavailable')
    # json inside a js string inside json. thanks
    items = json.loads(json.loads('"' + match[1] + '"'))
    for item in items:
        if item.get('id') == ident:
            for source in item.get('sources', []):
                if valid_video('wetter-' + ident, source.get('src')):
                    return source['src']
    raise ValueError('stream metadata unavailable')


async def video_snapshot(feeds, item, now):
    from .cameras import camera_url, decode_snapshot

    provider = item.id.split(':')[1]

    async def resolve():
        if provider == 'michaeli':
            raw = await feeds.shared_download(
                'https://xn--mnchen-3ya.webcam/scripts/michaelianger.min.js',
                300,
                now=now,
                max_bytes=500_000,
                timeout=12,
            )
            match = re.search(r'YOUTUBE_VIDEO="([A-Za-z0-9_-]{11})"', raw.content.decode())
            if not match:
                raise ValueError('stream metadata unavailable')
            raw_url = await command(
                sys.executable,
                '-m',
                'yt_dlp',
                '--no-playlist',
                '--no-warnings',
                '--socket-timeout',
                '8',
                '--retries',
                '0',
                '-f',
                'bestvideo[height<=480][protocol*=m3u8]/best[height<=480]/bestvideo[height<=480]/best',
                '--get-url',
                'https://www.youtube.com/watch?v=' + match[1],
                timeout=12,
            )
            return raw_url.decode().strip().splitlines()[0]
        raw = await feeds.shared_download(camera_url(item.id), 120, now=now, max_bytes=2_000_000, timeout=12)
        text = raw.content.decode('utf-8')
        if provider.startswith('wetter-'):
            return wetter_video(text, item.id.split(':')[-1])
        ident = item.id.split(':')[-1]
        match = re.search(
            r'https://sts[a-z0-9]+\.feratel\.(?:com|co\.at)/streams/latest/1/'
            + re.escape(ident)
            + r'\.mp4(?:\?[^"\s<]*)?',
            unescape(text),
        )
        if not match:
            raise ValueError('stream metadata unavailable')
        return match[0]

    url = await feeds.shared(('camera-video-url', item.id), 120, resolve)
    if not valid_video(provider, url):
        raise ValueError('weird stream url')
    # no local files, limit size + runtime
    content = await command(
        'ffmpeg',
        '-nostdin',
        '-hide_banner',
        '-loglevel',
        'error',
        '-threads',
        '1',
        '-protocol_whitelist',
        'https,http,tcp,tls,crypto',
        '-rw_timeout',
        '10000000',
        '-i',
        url,
        '-map',
        '0:v:0',
        '-frames:v',
        '1',
        '-an',
        '-vf',
        'scale=960:540:force_original_aspect_ratio=decrease',
        '-threads',
        '1',
        '-f',
        'image2pipe',
        '-vcodec',
        'mjpeg',
        'pipe:1',
        timeout=18,
    )
    return await asyncio.to_thread(decode_snapshot, content), now.isoformat()
