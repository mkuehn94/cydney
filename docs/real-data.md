# Data sources

Everything on the map is real data, there is no demo mode in the normal server anymore (fake data is only used in the tests). If a source is down that part of the map just stays empty.

Start position is set in [`server/data.py`](../server/data.py) (`DEFAULT_CENTER`, `DEFAULT_ZOOM`). Zoom goes from 12 to 18.

## Sources

| Source | What | Refresh |
| --- | --- | --- |
| [OSM / Overpass](https://overpass-api.de/) | shops, food, toilets, parking, charging ... | once a day per area |
| [ADSB.lol](https://adsb.lol/) | aircraft positions | 30 s |
| [adsbdb](https://www.adsbdb.com/) | origin / destination of a flight by callsign | on tap, cached 15 min |
| MVG | departures at stops, with delays | 30 s |
| MVV GTFS | timetable | daily |
| MyRadl / Dott | free bikes and scooters | 60 s |
| [Munich roadworks](https://opendata.muenchen.de/dataset/baustellen_4_weeks_opendata) | roadworks | 30 min |
| DWD | weather München-Stadt | 10 min |
| BBK / NINA | warnings | 2 min |
| webcams | see [cameras](camera-sources.md) | list 1 h, image when opened |
| AllThePlaces | stores, optional import | - |

Good to know:

- MVG only gives departures per stop, NOT where the bus or train is. For real positions you need a GTFS-RT feed (`CYD_TRANSIT_VP_URL`), I haven't found a public one for Munich yet.
- bikes and scooters are only the parked ones you could rent right now
- the route of an aircraft comes from the callsign and can be wrong (codeshares etc)
- the Munich sources obviously don't work if you pan to Hamburg, they show "outside coverage" then
- A new area takes a moment to load the first time

## AllThePlaces

There is no API, only snapshots to download from [here](https://www.alltheplaces.xyz/builds.html):

```sh
uv run python -m server.import_atp /path/to/output.tar.gz --snapshot-date YYYY-MM-DD
```

It filters to central Munich and writes `.cache/imports/alltheplaces.geojson`. Entries that are also in OSM (same name + address, less than 30 m apart) get merged.

## API

Frames and touch are described in [the protocol doc](website-prototype.md#protocol).

| Endpoint | What |
| --- | --- |
| `POST /api/device/sessions` | new session, optional `longitude`, `latitude`, `zoom` |
| `GET /api/device/{id}/state` | current state of the session |
| `GET /api/device/{id}/frame.json` | state + frame as data URL |
| `POST /api/device/{id}/position` | set your GPS position (the browser does that) |
| `GET /api/sources?device={id}` | status of all sources |
| `GET /api/observations?device={id}` | all records in the current area |
| `GET /api/health` | |

## Config

| Variable | Default | |
| --- | --- | --- |
| `CYD_ROOT_PATH` | | URL prefix behind a proxy, e.g. `/cydney` |
| `CYD_FEEDS` | `1` | `0` = no feed requests |
| `CYD_TILES` | `1` | `0` = no tile requests |
| `CYD_FEED_CACHE` | `.cache/feeds` | |
| `CYD_ATP_FILE` | `.cache/imports/alltheplaces.geojson` | |
| `CYD_FEED_USER_AGENT`, `CYD_TILE_USER_AGENT` | | set those when you host it |

## Licenses

OSM and ADSB.lol: ODbL. MVV: CC BY 4.0. Munich roadworks: dl-de/by-2-0. ATP and MyRadl: CC0. For DWD, BBK, MVG and Dott check their terms yourself before you host this publicly.

Also read the [OSM tile policy](https://operations.osmfoundation.org/policies/tiles/), tiles are cached on disk and only loaded for what you look at. If you have more than a few users use another tile provider.
