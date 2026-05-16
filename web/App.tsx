import { useCallback, useEffect, useRef, useState } from 'react'
import type { CSSProperties, KeyboardEvent, PointerEvent } from 'react'
import { nativePoint } from './geometry'

type Profile = 'landscape' | 'portrait'
type Session = {
  id: string; shared: boolean; width: number; height: number; page: string; revision: number; zoom: number
  observations: number; live_sources: number; connected_sources: number; enabled: string[]; map_status: string
  fleet: { in_view: { scooters: number; bikes: number; other: number; stale: number }; in_area: { scooters: number; bikes: number; other: number; stale: number } }
  mode: 'live' | 'demo'
  center: [number, number]
  location: { position: [number, number] | null; accuracy_m: number | null; observed_at: string | null; fresh: boolean; mode: 'follow' | 'browse' }
  camera_directory: { id: string; title: string }[]
  camera_view: {
    id: string
    title: string
    url: string
    credit: string
    status: string
    link_only: boolean
    image_box: [number, number, number, number] | null
  } | null
  flight_route: { status: 'available' | 'unavailable'; origin?: { code: string }; destination?: { code: string } } | null
  show_planned: boolean
  timing: { active: number; planned: number; unknown: number }
  regional_alerts: { id: string; title: string; stale: boolean; expires_at: string | null; details: { severity?: string; affected_area?: string } }[]
  viewport: { id: string; center: [number, number]; bounds: number[]; detail: string; overview_limited: boolean }
  sources: {
    id: string
    label: string
    status: string
    count: number
    partial_coverage: boolean
    fetched_at: string | null
    error: string | null
    warning?: string | null
    attribution: string
    coverage_note: string | null
    refresh_seconds: number
  }[]
  card_open: boolean
  selected_object: { id: string; title: string; value: string } | null
  layers: { id: string; label: string; count: number }[]
}
type FrameBundle = Session & { image: string; frame_id: number }
type Frame = { url: string; width: number; height: number; revision: number }
type Input = { kind: string; value?: string; dx?: number; dy?: number; x?: number; y?: number; latitude?: number; longitude?: number; zoom?: number } | { x: number; y: number; pointer?: 'mouse' | 'touch' | 'pen' }

const SHORTCUTS: Record<string, Input> = {
  c: { kind: 'page', value: 'menu' },
  v: { kind: 'page', value: 'cameras' },
  m: { kind: 'page', value: 'map' },
  f: { kind: 'page', value: 'feed' },
  l: { kind: 'page', value: 'layers' },
  i: { kind: 'page', value: 'info' },
  arrowleft: { kind: 'pan', dx: 48, dy: 0 },
  arrowright: { kind: 'pan', dx: -48, dy: 0 },
  arrowup: { kind: 'pan', dx: 0, dy: 48 },
  arrowdown: { kind: 'pan', dx: 0, dy: -48 },
  '+': { kind: 'zoom', value: 'in' },
  '=': { kind: 'zoom', value: 'in' },
  '-': { kind: 'zoom', value: 'out' },
  n: { kind: 'next' },
  p: { kind: 'prev' },
  escape: { kind: 'page', value: 'map' },
}

async function checked(response: Response) {
  if (!response.ok) {
    if (response.status === 409) throw new Error('frame was outdated, tap again')
    if (response.status === 404) throw new Error('session expired')
    const detail = await response.json().catch(() => null)
    throw new Error(typeof detail?.detail === 'string' ? detail.detail : `server returned ${response.status}`)
  }
  return response
}

async function postJSON<T>(url: string, body: object, signal: AbortSignal): Promise<T> {
  const response = await checked(await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  }))
  return response.json()
}

function describeFrame(frame: Frame, session: Session | null) {
  let text = `${frame.width}×${frame.height} CYD ${session?.page ?? ''} frame`
  if (session?.card_open && session.selected_object) {
    text += `; selected ${session.selected_object.title}: ${session.selected_object.value}`
    if (session.flight_route?.status === 'available') {
      text += `; reported route ${session.flight_route.origin?.code} to ${session.flight_route.destination?.code}`
    }
  }
  return text
}

function CameraLinks({ camera, width, height }: { camera: NonNullable<Session['camera_view']>; width: number; height: number }) {
  const box = camera.image_box
  return <>
    {box && <a
      className="camera-image-link" href={camera.url} target="_blank" rel="noreferrer"
      aria-label={`Open ${camera.title} (${camera.credit})`}
      onPointerDown={event => event.stopPropagation()} onClick={event => event.stopPropagation()}
      style={{
        left: `${box[0] / width * 100}%`, top: `${box[1] / height * 100}%`,
        width: `${(box[2] - box[0]) / width * 100}%`, height: `${(box[3] - box[1]) / height * 100}%`,
      }}
    />}
    {camera.link_only && <a
      className="camera-provider-link" href={camera.url} target="_blank" rel="noreferrer"
      onPointerDown={event => event.stopPropagation()} onClick={event => event.stopPropagation()}
      style={{ top: `${105 / height * 100}%` }}
    >open provider</a>}
    <a
      className="camera-source-link" href={camera.url} target="_blank" rel="noreferrer"
      aria-label={`Camera source: ${camera.credit}`}
      onPointerDown={event => event.stopPropagation()} onClick={event => event.stopPropagation()}
      style={{ top: `${(height - 34) / height * 100}%`, height: `${17 / height * 100}%` }}
    />
  </>
}

export default function App() {
  const search = new URLSearchParams(window.location.search)
  const deviceOnly = search.get('device') === '1'
  const [profile, setProfile] = useState<Profile>(search.get('profile') === 'portrait' ? 'portrait' : 'landscape')
  const [scale, setScale] = useState(1)
  const [latitude, setLatitude] = useState(search.get('lat') ?? '48.1816035')
  const [longitude, setLongitude] = useState(search.get('lon') ?? '11.5099957')
  const [session, setSession] = useState<Session | null>(null)
  const [frame, setFrame] = useState<Frame | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusyState] = useState(true)
  const [generation, setGeneration] = useState(0)
  const [watching, setWatching] = useState(false)
  const [gpsError, setGpsError] = useState<string | null>(null)
  const gpsWatchRef = useRef<number | null>(null)
  const gpsFollowRef = useRef(false)
  const gpsPendingRef = useRef<GeolocationPosition | null>(null)
  const busyRef = useRef(false)
  const sessionRef = useRef<Session | null>(null)
  const revisionRef = useRef(0)
  const frameIdRef = useRef<number | undefined>(undefined)
  const frameEtagRef = useRef<string | null>(null)
  const frameTicketRef = useRef(0)
  const refreshRef = useRef<((bundle?: FrameBundle, background?: boolean) => Promise<void>) | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  const pointer = useRef<{ x: number; y: number; id: number; revision: number } | null>(null)
  const screenRef = useRef<HTMLDivElement>(null)
  const setBusy = useCallback((value: boolean) => {
    busyRef.current = value
    setBusyState(value)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    controllerRef.current = controller
    let key: string | null = null
    let interval = 0
    let polling = false
    frameEtagRef.current = null
    frameIdRef.current = undefined
    sessionRef.current = null
    setSession(null)
    setFrame(null)
    setError(null)
    setBusy(true)

    async function refresh(bundle?: FrameBundle, background = false) {
      // console.log('refresh', bundle?.frame_id, background)
      if (!key || controller.signal.aborted) return
      const ticket = ++frameTicketRef.current
      let nextEtag: string | null = null
      let state = bundle
      if (!state) {
        const response = await fetch(`api/device/${key}/frame.json`, {
          signal: controller.signal, cache: 'no-store',
          headers: frameEtagRef.current ? { 'If-None-Match': frameEtagRef.current } : {},
        })
        if (response.status === 304) return
        nextEtag = response.headers.get('ETag')
        if (response.status === 404) {
          setGeneration(value => value + 1)
          return
        }
        state = await (await checked(response)).json() as FrameBundle
      }
      const image = new Image()
      image.src = state.image
      await image.decode()
      if (controller.signal.aborted || ticket !== frameTicketRef.current ||
          (background && (busyRef.current || pointer.current))) return
      frameEtagRef.current = nextEtag
      frameIdRef.current = state.frame_id
      revisionRef.current = state.revision
      setFrame({ url: state.image, width: image.width, height: image.height, revision: state.revision })
      const updated = { ...state, id: key, shared: sessionRef.current?.shared ?? false }
      sessionRef.current = updated
      setSession(updated)
    }
    refreshRef.current = refresh
    async function start() {
      try {
        const search = new URLSearchParams(window.location.search)
        const location: Record<string, number> = {}
        for (const [field, query] of [['latitude', 'lat'], ['longitude', 'lon'], ['zoom', 'zoom']]) {
          if (search.has(query)) location[field] = Number(search.get(query))
        }
        const shared = profile === 'landscape' && search.get('preview') !== '1'
          && (search.get('shared') === '1' || !['lat', 'lon', 'zoom'].some(field => search.has(field)))
        const created = await postJSON<Session>('api/device/sessions', { profile, shared, ...location }, controller.signal)
        key = created.id
        if (controller.signal.aborted) {
          void fetch(`api/device/${key}`, { method: 'DELETE' })
          return
        }
        sessionRef.current = created
        setSession(created)
        await refresh()
        if (controller.signal.aborted) return
        interval = window.setInterval(async () => {
          if (busyRef.current || pointer.current || polling) return
          polling = true
          try {
            await refresh(undefined, true)
            setError(null)
          } catch (reason) {
            if (!controller.signal.aborted) setError((reason as Error).message)
          } finally {
            polling = false
          }
        }, 250)
      } catch (reason) {
        if (!controller.signal.aborted) setError(`cant load frame: ${(reason as Error).message}`)
      } finally {
        if (!controller.signal.aborted) setBusy(false)
      }
    }
    void start()
    return () => {
      controller.abort()
      window.clearInterval(interval)
      if (key) void fetch(`api/device/${key}`, { method: 'DELETE' }).catch(() => {})
    }
  }, [profile, generation, setBusy])

  const stopGPS = useCallback(() => {
    if (gpsWatchRef.current !== null) navigator.geolocation.clearWatch(gpsWatchRef.current)
    gpsWatchRef.current = null
    gpsPendingRef.current = null
    gpsFollowRef.current = false
    setWatching(false)
  }, [])

  function startGPS() {
    if (!navigator.geolocation) {
      setGpsError('no geolocation in this browser')
      return
    }
    if (!window.isSecureContext) {
      setGpsError('location only works with https or localhost')
      return
    }
    if (gpsWatchRef.current !== null) return
    setGpsError(null)
    setWatching(true)
    gpsFollowRef.current = true
    gpsWatchRef.current = navigator.geolocation.watchPosition(
      position => { gpsPendingRef.current = position; setGpsError(null) },
      reason => {
        if (reason.code === 1) {
          setGpsError('Location permission denied')
          stopGPS()
        } else if (reason.code === 2) {
          setGpsError('no location yet...')
        } else {
          setGpsError('location timed out')
        }
      },
      { enableHighAccuracy: true, maximumAge: 0, timeout: 20000 },
    )
  }

  useEffect(() => {
    const timer = window.setInterval(async () => {
      const current = sessionRef.current
      const fix = gpsPendingRef.current
      const controller = controllerRef.current
      if (!fix || !current || !controller || controller.signal.aborted || busyRef.current || pointer.current) return
      gpsPendingRef.current = null
      if (Date.now() - fix.timestamp > 120000) {
        setGpsError('location is too old, waiting for a new one')
        return
      }
      setBusy(true)
      try {
        await postJSON(`api/device/${current.id}/position`, {
          longitude: fix.coords.longitude, latitude: fix.coords.latitude,
          accuracy_m: fix.coords.accuracy, observed_at: new Date(fix.timestamp).toISOString(),
          ...(gpsFollowRef.current ? { follow: true } : {}),
        }, controller.signal)
        if (!controller.signal.aborted) {
          gpsFollowRef.current = false
          await refreshRef.current?.()
          setGpsError(null)
        }
      } catch (reason) {
        if (!controller.signal.aborted) setGpsError((reason as Error).message)
      } finally {
        if (!controller.signal.aborted) setBusy(false)
      }
    }, 250)
    return () => {
      window.clearInterval(timer)
      stopGPS()
    }
  }, [profile, generation, setBusy, stopGPS])

  const send = useCallback(async (input: Input) => {
    const current = sessionRef.current
    const controller = controllerRef.current
    if (!current || !controller || busyRef.current) return
    setBusy(true)
    ++frameTicketRef.current
    try {
      const path = 'kind' in input ? 'action' : 'touch'
      const updated = await postJSON<FrameBundle>(`api/device/${current.id}/${path}?encoding=json`, {
        ...input, revision: revisionRef.current, frame_id: frameIdRef.current,
      }, controller.signal)
      if (controller.signal.aborted) return
      await refreshRef.current?.(updated)
      setError(null)
    } catch (reason) {
      if (!controller.signal.aborted) {
        console.warn('input failed', reason)
        setError((reason as Error).message)
        // refresh but dont resend the touch
        try { await refreshRef.current?.() } catch { /* ignore */ }
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false)
    }
  }, [setBusy])

  useEffect(() => {
    const screen = screenRef.current
    if (!screen) return
    let pending: { value: 'in' | 'out'; x: number; y: number; sessionId: string } | null = null
    let timer = 0
    let running = false
    let cancelled = false
    async function drain() {
      timer = 0
      if (cancelled || !pending) return
      const current = sessionRef.current
      if (!current || current.page !== 'map' || current.id !== pending.sessionId) {
        pending = null
        return
      }
      if (busyRef.current) {
        timer = window.setTimeout(() => void drain(), 60)
        return
      }
      const step = pending
      pending = null
      running = true
      await send({ kind: 'zoom', value: step.value, x: step.x, y: step.y })
      running = false
      if (!cancelled && pending && !timer) timer = window.setTimeout(() => void drain(), 80)
    }
    function wheel(event: WheelEvent) {
      const current = sessionRef.current
      if (!current || current.page !== 'map' || !screen!.querySelector('img') || event.deltaY === 0) return
      // needs a native listener, otherwise preventDefault doesnt work
      event.preventDefault()
      const point = nativePoint(event.clientX, event.clientY, screen!.getBoundingClientRect(), current)
      pending = { value: event.deltaY < 0 ? 'in' : 'out', ...point, sessionId: current.id }
      if (!timer && !running) timer = window.setTimeout(() => void drain(), 80)
    }
    screen.addEventListener('wheel', wheel, { passive: false })
    return () => {
      cancelled = true
      pending = null
      window.clearTimeout(timer)
      screen.removeEventListener('wheel', wheel)
    }
  }, [send])

  function pointerDown(event: PointerEvent<HTMLDivElement>) {
    if (!frame || busyRef.current || event.button !== 0 || !event.isPrimary) return
    event.currentTarget.setPointerCapture(event.pointerId)
    pointer.current = { ...nativePoint(event.clientX, event.clientY, event.currentTarget.getBoundingClientRect(), frame), id: event.pointerId, revision: frame.revision }
  }
  function pointerUp(event: PointerEvent<HTMLDivElement>) {
    const down = pointer.current
    if (!frame || !down || down.id !== event.pointerId) return
    pointer.current = null
    if (busyRef.current || down.revision !== frame.revision) return
    const point = nativePoint(event.clientX, event.clientY, event.currentTarget.getBoundingClientRect(), frame)
    const dx = point.x - down.x, dy = point.y - down.y
    if (Math.hypot(dx, dy) > 12 && session?.page === 'map') void send({ kind: 'pan', dx, dy })
    else if (Math.abs(dx) > 24 && session?.page !== 'map') void send({ kind: dx < 0 ? 'next' : 'prev' })
    else void send({ ...point, pointer: event.pointerType === 'mouse' ? 'mouse' : event.pointerType === 'pen' ? 'pen' : 'touch' })
  }

  function keyDown(event: KeyboardEvent<HTMLDivElement>) {
    const key = event.key.toLowerCase()
    if (key === 'g') {
      event.preventDefault()
      startGPS()
      return
    }
    const input = SHORTCUTS[key]
    if (input) {
      event.preventDefault()
      void send(input)
    }
  }

  const width = profile === 'landscape' ? 320 : 240
  const height = profile === 'landscape' ? 240 : 320
  const effectiveScale = deviceOnly ? 1 : scale
  return <main className={`simulator ${deviceOnly ? 'device-only' : ''}`}>
    {!deviceOnly && <header className="preview-header">
      <span className="wordmark">cydney<span>_</span></span>
      <span className="demo-tag">{
        !session ? 'CONNECTING…'
        : session.mode === 'demo' ? 'DEMO DATA'
        : session.live_sources > 0 ? 'LIVE'
        : session.connected_sources > 0 ? 'STATIC DATA ONLY'
        : 'NO DATA SOURCES'
      }</span>
    </header>}
    {!deviceOnly && <div className="preview-settings">
      <div className="setting-group" aria-label="Display orientation">
        <button aria-pressed={profile === 'landscape'} onClick={() => setProfile('landscape')}>320 × 240</button>
        <button aria-pressed={profile === 'portrait'} onClick={() => setProfile('portrait')}>240 × 320</button>
      </div>
      <div className="setting-group" aria-label="Preview magnification">
        {[1, 2, 3].map(value => <button
          key={value} aria-label={`${value} times preview magnification`}
          aria-pressed={scale === value} onClick={() => setScale(value)}
        >{value}×</button>)}
      </div>
    </div>}
    <section className="device-stage" aria-label="simulator">
      <div className="device-surround" style={{ '--screen-width': `${width * effectiveScale}px`, '--screen-height': `${height * effectiveScale}px` } as CSSProperties}>
        <div ref={screenRef} className="device-screen" data-testid="device-screen" aria-label="CYD touchscreen" role="application" tabIndex={0}
          onPointerDown={pointerDown} onPointerUp={pointerUp} onPointerCancel={() => { pointer.current = null }} onLostPointerCapture={() => { pointer.current = null }}
          onKeyDown={keyDown}>
          {frame ? <img
            src={frame.url} width={frame.width} height={frame.height} draggable={false}
            alt={describeFrame(frame, session)}
          /> : <div className="boot-screen">
            <strong>CYDNEY_</strong>
            <span>{error ? 'SERVER OFFLINE' : 'CONNECTING...'}</span>
          </div>}
          {session?.camera_view && <CameraLinks camera={session.camera_view} width={width} height={height} />}
        </div>
      </div>
      {!deviceOnly && <div className="device-caption"><span>{width} x {height} @ {effectiveScale}x</span><span>{busy ? 'loading...' : 'tap / drag / wheel'}</span></div>}
    </section>
    {error && <div className="connection-error" role="alert">
      <p>{error}</p>
      <button onClick={() => setGeneration(value => value + 1)}>Reconnect</button>
      <code>uv run uvicorn server.app:app --host 127.0.0.1 --port 8000</code>
    </div>}
    {gpsError && <p className="connection-error" role="alert">{gpsError}</p>}
    {!deviceOnly && <>
      <nav className="keyboard-controls" aria-label="controls">
        {Object.entries({ menu: 'C', map: 'M', cameras: 'V', feed: 'F', layers: 'L', info: 'I' }).map(([page, key]) => <button
          key={page} disabled={!frame || busy} onClick={() => void send({ kind: 'page', value: page })}
        >{page === 'menu' ? 'Controls' : page}<kbd>{key}</kbd></button>)}
        <button disabled={!frame || busy} onClick={() => void send({ kind: 'home' })}>Recenter</button>
      </nav>
      {session?.mode === 'live' && <form
        className="location-controls" aria-label="Map location"
        onSubmit={event => {
          event.preventDefault()
          void send({ kind: 'locate', latitude: Number(latitude), longitude: Number(longitude) })
        }}
      >
        <label>Latitude<input
          aria-label="Latitude" type="number" min="-85" max="85" step="any" required
          value={latitude} onChange={event => setLatitude(event.target.value)}
        /></label>
        <label>Longitude<input
          aria-label="Longitude" type="number" min="-180" max="180" step="any" required
          value={longitude} onChange={event => setLongitude(event.target.value)}
        /></label>
        <button disabled={busy || !frame} type="submit">Go</button>
        <button disabled={busy || !frame || watching} type="button" onClick={startGPS}>Use my location</button>
        <button
          disabled={busy || !frame || !session.location.fresh} type="button"
          onClick={() => void send({ kind: 'follow', value: session.location.mode === 'follow' ? 'off' : 'on' })}
        >{session.location.mode === 'follow' ? 'Browse map' : 'Follow location'}</button>
        {watching && <button disabled={busy} type="button" onClick={() => {
          stopGPS()
          void send({ kind: 'follow', value: 'off' })
        }}>Stop location</button>}
        <small>
          Camera {session.center[1].toFixed(5)}, {session.center[0].toFixed(5)} · {session.location.mode === 'follow' ? 'following' : 'browsing'}
        </small>
        <small aria-label="Location status">
          {session.location.position
            ? `${session.location.fresh ? 'Position' : 'OLD position'} ${session.location.position[1].toFixed(5)}, ${session.location.position[0].toFixed(5)}  ±${Math.round(session.location.accuracy_m ?? 0)}m`
            : 'no position yet'}
        </small>
        <button
          disabled={busy || !frame} type="button" aria-pressed={session.show_planned}
          onClick={() => void send({ kind: 'planned' })}
        >Planned {session.show_planned ? 'shown' : 'hidden'} ({session.timing.planned})</button>
        <small aria-label="Available shared vehicles">
          {session.fleet.in_view.scooters} scooters, {session.fleet.in_view.bikes} bikes on screen ({session.fleet.in_area.scooters} / {session.fleet.in_area.bikes} loaded)
        </small>
      </form>}
      <div className="density-strip">
        <div><strong>{session?.observations ?? '-'}</strong><span>objects</span></div>
        <div><strong>{session?.layers.length ?? '-'}</strong><span>layers</span></div>
        <div><strong>{session?.connected_sources ?? 0}</strong><span>feeds</span></div>
      </div>
      {session?.mode === 'live' && <section className="sources" aria-label="Sources">
        <h2>Sources</h2>
        <ul>{session.sources.map(source => <li key={source.id}>
          <strong>{source.label}</strong>: {source.status} · {source.count} records
          {source.error ? ` · ${source.error}` : ''}
          {source.warning ? ` · ${source.warning}` : ''}
          {source.coverage_note ? ` · ${source.coverage_note}` : ''}
          {source.fetched_at ? ` · fetched ${new Date(source.fetched_at).toLocaleString()}` : ''}
          <br /><small>{source.attribution}</small>
        </li>)}</ul>
      </section>}
      <p className="hardware-note">{session?.shared ? 'this is the same screen as the CYD' : 'separate preview, not the CYD screen'}</p>
      <footer className="preview-footer">
        <a href={session?.mode === 'live' ? `?device=1&profile=${profile}&lat=${session.center[1]}&lon=${session.center[0]}&zoom=${session.zoom}&shared=${session.shared ? 1 : 0}` : '?device=1'}>frame only</a>
        <a href="api/docs" target="_blank" rel="noreferrer">api docs</a>
        <span>© OpenStreetMap contributors</span>
      </footer>
    </>}
  </main>
}
