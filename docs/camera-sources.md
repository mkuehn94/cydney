# Cameras

Where the webcams come from. All of them work without an API key. Most are NOT video, just a snapshot that gets replaced every few minutes.

Cameras show up within 50 km of the map center, press `V` or open Cameras in the menu. The image is only fetched when you open one.

## Worldwide

| Provider | Where | Notes |
| --- | --- | --- |
| [Foto-Webcam.eu](https://www.foto-webcam.eu/) | Alps, Austria, south Germany | ~300 cams, really nice quality |
| [TfL JamCams](https://api.tfl.gov.uk/Place/Type/JamCam) | London | ~800 traffic cams |
| [Caltrans](https://cwwp2.dot.ca.gov/documentation/cctv/cctv.htm) | California | ~3400, split into 12 districts |
| [Hong Kong TD](https://static.data.gov.hk/td/traffic-snapshot-images/code/Traffic_Camera_Locations_En.xml) | Hong Kong | ~1000 |
| [Singapore LTA](https://api.data.gov.sg/v1/transport/traffic-images) | Singapore | only a few, URLs change all the time |
| [Oregon TripCheck](https://www.tripcheck.com/) | Oregon | ~750 |
| [WSDOT](https://data.wsdot.wa.gov/travelcenter/Cameras.json) | Washington | ~1600 |

## Munich

Around 96 cams within 50 km.

| Provider | Nearby | Notes |
| --- | --- | --- |
| [BayernInfo](https://map.bayerninfo.de/) | 61 | road cams |
| [Terra-HD](https://terra-hd.com/map/) | 13 | Deutsches Museum, Freising, Rosenheim ... |
| [Foto-Webcam.eu](https://www.foto-webcam.eu/) | 6 | Olympiapark, Großhadern, Dachau |
| [wetter.com](https://www.wetter.com/hd-live-webcams/) | 5 | Marienplatz, Theresienwiese. Video, we grab one frame with ffmpeg |
| [Feratel](https://www.feratel.com/) | 3 | Marienplatz, Hauptbahnhof, Olympiaturm |
| Foto-Webcam.net / .org | 3 | Wörthsee, Tegernsee |
| some private ones | 5 | weather stations, a sailing club, a park cam on youtube (yt-dlp) |

A few of them are offline most of the time (annoying). Then you get the last preview image of the provider with a NOT LIVE label, or nothing.

To check which ones currently work:

```sh
uv run python -m server.camera_audit --longitude 11.5761 --latitude 48.1374
```

## Didn't work

- Transport for NSW: list works, images are just "temporarily unavailable" pages
- Queensland, Utah DOT: 404 / 403
- Windy: needs an API key
- Autobahn API: returns empty lists for A8 / A9
- Munich Airport, earthTV, Panomax: found no usable coordinates

Please keep the credit and the link to the provider on the image if you reuse this, most of them require that.
