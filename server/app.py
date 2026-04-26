import asyncio
import base64
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import logging
from pathlib import Path
import os
import time
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import aircraft, cameras
from .data import DEFAULT_CENTER, DEFAULT_ZOOM
from .feeds import FeedStore
from .fleet import fleet_counts
from .device import Device, Frame, PROFILES, apply_action, render, touch
from .timing import TIMED_LAYERS, parse_time, timing_status
from .tiles import TileCache
from .camera_store import CameraStore
from .map_preview import MARGIN, MapPreviewStore, map_identity


@dataclass
class Session:
    device: Device
    frame: Frame | None = None
    last_revision: int = -1
    accessed: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    shared: bool = False
    frame_token: tuple | None = None
    encoded: dict = field(default_factory=dict)
    frame_id: int = 0
    input_epoch: int = 0
    history: OrderedDict = field(default_factory=OrderedDict)
    route_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(timeout=4, follow_redirects=False) as client:
        app.state.tiles = TileCache(client, enabled=os.environ.get('CYD_TILES', '1') != '0')
        app.state.previews = MapPreviewStore(app.state.tiles)
        app.state.sessions = OrderedDict()
        app.state.mode = os.environ.get('CYD_MODE', 'live')
        if app.state.mode != 'live':
            raise ValueError('only CYD_MODE=live is supported')
        app.state.feeds = FeedStore(client, enabled=os.environ.get('CYD_FEEDS', '1') != '0')
        app.state.cameras = CameraStore(app.state.feeds)
        await app.state.feeds.start()
        try:
            yield
        finally:
            tasks = [s.route_task for s in app.state.sessions.values() if s.route_task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.state.cameras.close()
            await app.state.feeds.close()
            await app.state.previews.close()
            await app.state.tiles.close()


app = FastAPI(
    title='cydney',
    version='0.4.0',
    lifespan=lifespan,
    root_path=os.environ.get('CYD_ROOT_PATH', ''),
    docs_url='/api/docs',
    openapi_url='/api/openapi.json',
    redoc_url=None,
)


class Location(BaseModel):
    longitude: float | None = Field(default=None, ge=-180, le=180)
    latitude: float | None = Field(default=None, ge=-85, le=85)
    zoom: int | None = Field(default=None, ge=12, le=18)

    @model_validator(mode='after')
    def paired_coordinates(self):
        if (self.longitude is None) != (self.latitude is None):
            raise ValueError('need lon and lat')
        return self


class CreateSession(Location):
    model_config = ConfigDict(extra='forbid')
    profile: Literal['landscape', 'portrait'] = 'landscape'
    shared: bool = False


class Touch(BaseModel):
    frame_id: int | None = Field(default=None, ge=1)
    x: int = Field(ge=0, lt=320)
    y: int = Field(ge=0, lt=320)
    pointer: Literal['mouse', 'touch', 'pen'] = 'touch'
    revision: int = Field(ge=0)


class Action(Location):
    frame_id: int | None = Field(default=None, ge=1)
    kind: Literal[
        'page',
        'zoom',
        'home',
        'pan',
        'toggle',
        'all',
        'next',
        'prev',
        'select',
        'inspect',
        'dismiss',
        'locate',
        'follow',
        'planned',
        'alerts',
        'camera',
    ]
    value: str = Field(default='', max_length=100)
    dx: int = Field(default=0, ge=-640, le=640)
    dy: int = Field(default=0, ge=-640, le=640)
    x: int | None = Field(default=None, ge=0, lt=320)
    y: int | None = Field(default=None, ge=0, lt=320)
    revision: int = Field(ge=0)


class PositionFix(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-85, le=85)
    accuracy_m: float = Field(gt=0, le=1_000_000)
    observed_at: datetime
    follow: bool | None = None

    @model_validator(mode='after')
    def recent_position(self):
        if self.observed_at.tzinfo is None:
            raise ValueError('timestamp needs a timezone')
        if not -30 <= (datetime.now(timezone.utc) - self.observed_at).total_seconds() <= 120:
            raise ValueError('position too old or in the future')
        return self


def get_session(request: Request, key: str) -> Session:
    sessions = request.app.state.sessions
    now = time.monotonic()
    for expired in [
        token
        for token, session in sessions.items()
        if not session.shared and now - session.accessed > 1800 and not session.lock.locked()
    ]:
        sessions.pop(expired)
        request.app.state.feeds.release(expired)
    if key not in sessions:
        raise HTTPException(404, 'session not found')
    session = sessions[key]
    session.accessed = now
    sessions.move_to_end(key)
    return session


def view_stamp(device):
    return (device.page, device.center, device.zoom, device.card_open, tuple(device.selected))


def input_frame(session, body):
    if body.frame_id is not None:
        saved = session.history.get(body.frame_id)
        if (
            saved
            and saved[1] == body.revision
            and saved[2] == session.input_epoch
            and saved[3] == view_stamp(session.device)
        ):
            return saved[0]
    elif (
        body.revision == session.device.revision
        and session.last_revision == body.revision
        and session.frame is not None
    ):
        return session.frame
    raise HTTPException(409, 'stale touch, fetch a new frame')


@app.middleware('http')
async def document_cache(request, call_next):
    response = await call_next(request)
    if response.headers.get('content-type', '').startswith('text/html'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def sync_data(device: Device, feeds: FeedStore, initial=False):
    if device.mode == 'demo':
        return
    try:
        area = feeds.follow(device.feed_consumer, device.center, device.zoom, device.size)
    except ValueError as error:
        raise HTTPException(503, str(error))
    device.viewport = area.view.describe()
    now = datetime.now(timezone.utc)
    token = (area.view.key, area.generation, int(now.timestamp() // 5))
    if token == device.data_token:
        return
    token, items, sources = area.snapshot(now)
    changed = items != device.observations or sources != device.source_statuses
    device.observations, device.source_statuses, device.data_token = items, sources, token
    device.selected = [key for key in device.selected if key in device.by_id]
    device.selected_index = min(device.selected_index, max(0, len(device.selected) - 1))
    if not device.selected:
        device.card_open = False
        if device.page == 'detail':
            device.page = 'map'
    if not initial and changed:
        device.revision += 1


def summary(device: Device):
    selected = device.selected_object
    camera = device.camera if device.page == 'camera' else None
    camera_view = None
    if camera:
        camera_view = {
            'id': camera.id,
            'title': camera.title,
            'url': cameras.camera_url(camera.id),
            'credit': cameras.camera_credit(camera.id),
            'status': device.camera_status,
            'link_only': bool(camera.details.get('link_only')),
            'image_kind': (
                device.camera_image.info.get('cydney_capture_kind')
                or ('video' if camera.details.get('media_kind') == 'video' else 'snapshot')
            )
            if device.camera_image
            else None,
            'fetched_at': device.camera_fetched_at,
            'image_box': cameras.image_box(device.size, device.camera_image)[1] if device.camera_image else None,
        }
    timing = dict.fromkeys(('active', 'planned', 'unknown'), 0)
    for item in device.observations:
        if item.layer in TIMED_LAYERS and not item.is_vehicle:
            state = timing_status(item)
            if state in timing:
                timing[state] += 1
    return {
        'profile': device.profile,
        'width': device.size[0],
        'height': device.size[1],
        'page': device.page,
        'revision': device.revision,
        'zoom': device.zoom,
        'center': device.center,
        'enabled': sorted(device.enabled),
        'demo': device.mode == 'demo',
        'mode': device.mode,
        'home': device.home,
        'real_data_only': device.mode == 'live',
        'location': {
            'position': device.position,
            'accuracy_m': device.position_accuracy_m,
            'observed_at': device.position_observed_at,
            'fresh': device.position_fresh(),
            'mode': 'follow' if device.following else 'browse',
        },
        'camera_directory': [asdict(i) for i in device.camera_directory],
        'camera_view': camera_view,
        'show_planned': device.show_planned,
        'timing': timing,
        'regional_alerts': [asdict(i) for i in device.regional_alerts],
        'synthetic_observations': sum(item.demo for item in device.observations),
        'observations': len(device.observations),
        'fleet': {'in_view': device.visible_fleet, 'in_area': fleet_counts(device.observations)},
        'live_sources': sum(source['status'] == 'ready' and source['dynamic'] for source in device.source_statuses),
        'connected_sources': sum(source['status'] == 'ready' for source in device.source_statuses),
        'sources': device.source_statuses,
        'viewport': device.viewport,
        'map_status': device.map_status,
        'offset': device.offset,
        'selected': selected.id if selected else None,
        'card_open': device.card_open,
        'flight_route': device.selected_flight_route,
        'selected_object': asdict(selected) if selected else None,
        'layers': device.layer_summaries,
    }


@app.get('/api/health')
async def health(request: Request):
    manifest = (
        {'mode': 'demo', 'live_sources': 0, 'connected_sources': 0}
        if request.app.state.mode == 'demo'
        else request.app.state.feeds.manifest()
    )
    return {'status': 'ok', **manifest, 'real_data_only': request.app.state.mode == 'live', 'profiles': PROFILES}


@app.get('/api/sources')
async def sources(request: Request, device: str | None = None):
    if request.app.state.mode == 'demo':
        return {'mode': 'demo', 'live_sources': 0, 'connected_sources': 0, 'sources': []}
    return feed_area(request, device).manifest()


def feed_area(request: Request, key: str | None = None):
    feeds = request.app.state.feeds
    if key is None:
        return feeds
    device = get_session(request, key).device
    try:
        return feeds.follow(key, device.center, device.zoom, device.size)
    except ValueError as error:
        raise HTTPException(503, str(error))


@app.post('/api/device/sessions', status_code=201)
async def create_session(body: CreateSession, request: Request):
    sessions = request.app.state.sessions
    shared = body.shared and request.app.state.mode == 'live'
    if shared:
        existing = next(
            ((key, s) for key, s in sessions.items() if s.shared and s.device.profile == body.profile), None
        )
        if existing:
            key, session = existing
            session.accessed = time.monotonic()
            async with session.lock:
                return {'id': key, 'shared': True, **summary(session.device)}
    if len(sessions) >= 64:
        # full, kick an idle one
        idle = next(
            (key for key, session in sessions.items() if not session.shared and not session.lock.locked()), None
        )
        if idle is None:
            raise HTTPException(503, 'too many sessions')
        sessions.pop(idle)
        print("session limit, dropped", idle)
        request.app.state.feeds.release(idle)
    key = uuid4().hex
    # print('new session', key, body)
    if request.app.state.mode == 'live':
        center = (body.longitude, body.latitude) if body.longitude is not None else DEFAULT_CENTER
        device = Device(
            profile=body.profile,
            mode='live',
            home=DEFAULT_CENTER,
            center=center,
            zoom=body.zoom or DEFAULT_ZOOM,
            observations=(),
            feed_consumer=key,
        )
        sync_data(device, request.app.state.feeds, initial=True)
    else:
        device = request.app.state.demo_factory(profile=body.profile)
    sessions[key] = Session(device, shared=shared)
    return {'id': key, 'shared': shared, **summary(device)}


@app.delete('/api/device/{key}', status_code=204)
async def delete_session(key: str, request: Request):
    session = get_session(request, key)
    async with session.lock:
        if not session.shared:
            if session.route_task:
                session.route_task.cancel()
            request.app.state.sessions.pop(key, None)
            request.app.state.feeds.release(key)
    return Response(status_code=204)


@app.get('/api/device/{key}/state')
async def state(key: str, request: Request):
    session = get_session(request, key)
    async with session.lock:
        return summary(session.device)


@app.get('/api/observations')
async def observations(request: Request, device: str | None = None):
    if request.app.state.mode == 'demo':
        return {
            'mode': 'demo',
            'live_sources': 0,
            'items': [asdict(item) for item in request.app.state.demo_observations],
        }
    area = feed_area(request, device)
    _, items, _ = area.snapshot()
    return {**area.manifest(), 'items': [asdict(item) for item in items]}


def encode_frame(image, encoding, byte_order):
    if encoding == 'rgb565':
        body = bytearray()
        pixels = image.tobytes()
        for offset in range(0, len(pixels), 3):
            red, green, blue = pixels[offset : offset + 3]
            pixel = (red >> 3) << 11 | (green >> 2) << 5 | (blue >> 3)
            body.extend(pixel.to_bytes(2, byte_order))
        return bytes(body), 'application/octet-stream'

    buffer = BytesIO()
    if encoding == 'jpg':
        for quality in (80, 70, 60, 50, 40):
            buffer.seek(0)
            buffer.truncate()
            image.save(buffer, 'JPEG', quality=quality, subsampling=2)
            if buffer.tell() <= 48 * 1024:
                break
        return buffer.getvalue(), 'image/jpeg'
    image.save(buffer, 'PNG', optimize=True)
    return buffer.getvalue(), 'image/png'


async def frame_response(session: Session, encoding: str, request: Request, byte_order='big'):
    started = time.monotonic()
    device = session.device
    sync_data(device, request.app.state.feeds)
    tiles = request.app.state.tiles
    camera_token = request.app.state.cameras.update(device) if device.page in ('camera', 'cameras') else None
    token = (
        device.revision,
        session.input_epoch,
        tiles.generation if device.page == 'map' else 0,
        int(time.time() // 30),
        camera_token,
    )
    if token != session.frame_token:
        item = device.selected_object
        if item and (device.card_open or device.page == 'detail') and item.layer == 'flights' and not item.demo:
            if (
                not device.flight_route or device.flight_route.get('aircraft_id') != item.id
            ) and session.route_task is None:

                async def load_route(selected=item):
                    try:
                        route = await aircraft.lookup(request.app.state.feeds, selected, datetime.now(timezone.utc))
                        async with session.lock:
                            device.flight_route = {**route, 'aircraft_id': selected.id}
                            device.revision += 1
                    finally:
                        session.route_task = None

                session.route_task = asyncio.create_task(load_route(), name='flight-route:' + item.id)
        basemap = None
        if device.page == 'map':
            basemap, device.map_status = await tiles.map_image(device.center, device.zoom, *device.size, wait=False)
        session.frame = await asyncio.to_thread(render, device, basemap, datetime.now(ZoneInfo('Europe/Berlin')))
        session.last_revision = device.revision
        session.frame_token = token
        session.encoded.clear()
        session.frame_id += 1
        session.history[session.frame_id] = (session.frame, device.revision, session.input_epoch, view_stamp(device))
        while len(session.history) > 12:
            session.history.popitem(last=False)
    image_encoding = 'jpg' if encoding == 'json' else encoding
    cache_key = (image_encoding, byte_order)
    if cache_key not in session.encoded:
        content, media_type = await asyncio.to_thread(encode_frame, session.frame.image, image_encoding, byte_order)
        session.encoded[cache_key] = (content, media_type, '"' + hashlib.sha256(content).hexdigest() + '"')
    content, media_type, etag = session.encoded[cache_key]
    if encoding == 'json':
        etag = etag[:-1] + f'-{session.frame_id}"'
    headers = {
        'ETag': etag,
        'Cache-Control': 'private, no-cache',
        'X-Frame-Revision': str(device.revision),
        'X-Frame-ID': str(session.frame_id),
        'Server-Timing': f'frame;dur={(time.monotonic() - started) * 1000:.1f}',
        'X-Frame-Width': str(device.size[0]),
        'X-Frame-Height': str(device.size[1]),
        'X-Data-Mode': device.mode,
        'X-Observations': str(len(device.observations)),
        'X-Live-Sources': str(sum(s['status'] == 'ready' and s['dynamic'] for s in device.source_statuses)),
        'X-Connected-Sources': str(sum(s['status'] == 'ready' for s in device.source_statuses)),
        'X-Map-Status': device.map_status,
        'X-Frame-Page': device.page,
        'X-Card-Open': 'true' if device.card_open else 'false',
    }
    if device.page == 'map':
        headers.update(map_identity(device)[1])
        tiles.buffer_view(device.center, device.zoom, *device.size)
    if encoding == 'rgb565':
        headers['X-Pixel-Format'] = f'RGB565-{byte_order}-endian'
    if request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers=headers)
    if encoding == 'json':
        return JSONResponse(
            {
                **summary(device),
                'frame_id': session.frame_id,
                'image': 'data:image/jpeg;base64,' + base64.b64encode(content).decode(),
            },
            headers=headers,
        )
    return Response(content, media_type=media_type, headers=headers)


@app.get('/api/device/{key}/frame.{encoding}')
async def frame(
    key: str,
    encoding: Literal['png', 'jpg', 'rgb565', 'json'],
    request: Request,
    byte_order: Literal['big', 'little'] = 'big',
):
    session = get_session(request, key)
    async with session.lock:
        return await frame_response(session, encoding, request, byte_order)


@app.get('/api/device/{key}/map-preview.jpg')
async def map_preview(key: str, request: Request, map_key: str):
    session = get_session(request, key)
    async with session.lock:
        device = session.device
        if device.page != 'map' or map_key != map_identity(device)[0]:
            raise HTTPException(409, 'map moved, fetch a new frame')
        content, headers = request.app.state.previews.get(device)
        headers.update({'X-Preview-Margin': str(MARGIN), 'Cache-Control': 'private, no-store'})
        if content is None:
            return Response(status_code=202 if request.app.state.tiles.enabled else 204, headers=headers)
        return Response(content, media_type='image/jpeg', headers=headers)


@app.post('/api/device/{key}/touch')
async def tap(key: str, body: Touch, request: Request, encoding: Literal['jpg', 'json'] | None = None):
    session = get_session(request, key)
    async with session.lock:
        shown = input_frame(session, body)
        try:
            zoom = session.device.zoom
            touch(session.device, shown, body.x, body.y, body.pointer)
            session.input_epoch += 1
            logging.getLogger('uvicorn.error').info(
                'Input %s tap x=%s y=%s zoom=%s->%s', key[:8], body.x, body.y, zoom, session.device.zoom
            )
            if encoding is None:
                sync_data(session.device, request.app.state.feeds)
        except ValueError as error:
            raise HTTPException(422, str(error))
        if encoding is None:
            return summary(session.device)
        return await frame_response(session, encoding, request)


@app.post('/api/device/{key}/position')
async def position(key: str, body: PositionFix, request: Request):
    session = get_session(request, key)
    async with session.lock:
        device = session.device
        previous = parse_time(device.position_observed_at)
        if previous and body.observed_at <= previous:
            raise HTTPException(409, 'position is older than the last one')
        device.position = (body.longitude, body.latitude)
        device.position_accuracy_m = body.accuracy_m
        device.position_observed_at = body.observed_at.astimezone(timezone.utc).isoformat()
        if body.follow is not None:
            device.following = body.follow
        if device.following:
            device.center = device.position
            device.selected, device.selected_index, device.card_open = [], 0, False
        device.revision += 1
        session.input_epoch += 1
        sync_data(device, request.app.state.feeds)
        return summary(device)


@app.post('/api/device/{key}/action')
async def action(key: str, body: Action, request: Request, encoding: Literal['jpg', 'json'] | None = None):
    session = get_session(request, key)
    async with session.lock:
        if body.frame_id is not None:
            input_frame(session, body)
        elif body.revision != session.device.revision:
            raise HTTPException(409, 'stale action, fetch a new frame')
        try:
            zoom = session.device.zoom
            if body.kind == 'locate':
                if body.longitude is None:
                    raise ValueError('need lon and lat')
                session.device.following = False
                session.device.center = (body.longitude, body.latitude)
                session.device.zoom = body.zoom or session.device.zoom
                apply_action(session.device, 'page', 'map')
            else:
                apply_action(session.device, body.kind, body.value, body.dx, body.dy, body.x, body.y)
            session.input_epoch += 1
            logging.getLogger('uvicorn.error').info(
                'Input %s %s %s dx=%s dy=%s zoom=%s->%s',
                key[:8],
                body.kind,
                body.value,
                body.dx,
                body.dy,
                zoom,
                session.device.zoom,
            )
            if encoding is None:
                sync_data(session.device, request.app.state.feeds)
        except ValueError as error:
            raise HTTPException(422, str(error))
        if encoding is None:
            return summary(session.device)
        return await frame_response(session, encoding, request)


# serve the built frontend
dist = Path(__file__).resolve().parent.parent / 'dist'
if dist.is_dir():
    app.mount('/', StaticFiles(directory=dist, html=True), name='simulator')
