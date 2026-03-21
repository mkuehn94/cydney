import gzip
from io import BytesIO
import json
import tarfile

from server.data import MUNICH
from server.import_atp import import_snapshot


def test_import_geojson(tmp_path):
    feature = {
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': MUNICH},
        'properties': {'@id': 'branch-42', '@spider': 'test_de', 'name': 'Branch'},
    }
    foreign = {**feature, 'geometry': {'type': 'Point', 'coordinates': [0, 0]}}
    source = tmp_path / 'snapshot.geojson.gz'
    with gzip.open(source, 'wt') as stream:
        json.dump({'type': 'FeatureCollection', 'features': [foreign, feature, feature]}, stream)
    output = tmp_path / 'regional.geojson'
    assert import_snapshot(source, output, '2026-11-07') == 1
    data = json.loads(output.read_text())
    assert data['features'][0]['properties']['@id'] == 'branch-42'
    assert data['cydney']['snapshot_date'] == '2026-11-07'
    assert data['cydney']['imported_at']


def test_import_tar(tmp_path):
    feature = {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': MUNICH}, 'properties': {'name': 'Test'}}
    content = (json.dumps(feature) + '\n').encode()
    source = tmp_path / 'snapshot.tar.gz'
    with tarfile.open(source, 'w:gz') as archive:
        info = tarfile.TarInfo('../../escape.ndjson')
        info.size = len(content)
        archive.addfile(info, BytesIO(content))
    assert import_snapshot(source, tmp_path / 'regional.geojson') == 1
    assert not (tmp_path.parent / 'escape.ndjson').exists()
