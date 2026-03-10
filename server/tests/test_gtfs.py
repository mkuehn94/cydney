import asyncio
import os
from datetime import datetime, timezone, date, timedelta
from io import BytesIO
import zipfile


from server.adapters import iso
from server.gtfs import GTFSAdapter, active_services, build_index, departures, service_instant

NOW = datetime(2026, 11, 9, 23, 5, tzinfo=timezone.utc)


def archive():
    files = {
        'agency.txt': 'agency_name,agency_url,agency_timezone\nMVV,https://example.test,Europe/Berlin\n',
        'stops.txt': 'stop_id,stop_name,stop_lat,stop_lon\nA,Marienplatz,48.13743,11.57549\nB,Elsewhere,0,0\n',
        'routes.txt': 'route_id,route_short_name,route_type\nR,U3,1\n',
        'trips.txt': 'route_id,service_id,trip_id,trip_headsign\nR,MONDAY,T,Fuerstenried\nR,CANCELLED,X,Cancelled\n',
        'stop_times.txt': 'trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT,24:10:00,24:10:00,A,1\nX,24:12:00,24:12:00,A,2\nT,24:20:00,24:20:00,B,3\n',
        'calendar.txt': 'service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nMONDAY,1,0,0,0,0,0,0,20261101,20261201\nCANCELLED,1,1,1,1,1,1,1,20261101,20261201\n',
        'calendar_dates.txt': 'service_id,date,exception_type\nCANCELLED,20261109,2\nEXTRA,20261110,1\n',
    }
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as result:
        for name, content in files.items():
            result.writestr(name, content)
    return stream.getvalue()


def test_gtfs_departures():
    index = build_index(archive())
    assert list(index['stops']) == ['A']
    assert active_services(index, date(2026, 11, 9)) == {'MONDAY'}
    assert active_services(index, date(2026, 11, 10)) == {'CANCELLED'}
    (item,) = departures(index, NOW, iso(NOW))
    assert not item.demo and not item.is_vehicle
    assert item.kind == 'schedule' and item.position_kind == 'stop'
    assert item.observed_at is None and 'SCHEDULE ONLY' in item.value
    (departure,) = item.details['departures']
    assert departure['minutes'] == 5 and departure['route'] == 'U3'
    assert departure['at'] == '2026-11-09T23:10:00+00:00' and not departure['realtime']
    assert 'Cancelled' not in item.value


def test_gtfs_cache(tmp_path):
    async def scenario():
        calls = []

        async def download(url, **kwargs):
            calls.append(url)
            return archive()

        adapter = GTFSAdapter(tmp_path)
        first, _ = await adapter.load(download, NOW)
        second, _ = await adapter.load(download, NOW + timedelta(minutes=1))
        assert first[0].details['departures'][0]['minutes'] == 5
        assert second[0].details['departures'][0]['minutes'] == 4
        assert len(calls) == 1
        os.utime(tmp_path / 'mvv-gtfs.zip', (NOW.timestamp(), NOW.timestamp()))
        restarted = GTFSAdapter(tmp_path)
        assert (await restarted.load(download, NOW))[0]
        assert len(calls) == 1

    asyncio.run(scenario())


def test_gtfs_dst():
    assert service_instant(date(2026, 10, 25), 4 * 3600).isoformat() == '2026-10-25T04:00:00+01:00'
    assert service_instant(date(2026, 3, 29), 4 * 3600).isoformat() == '2026-03-29T04:00:00+02:00'
