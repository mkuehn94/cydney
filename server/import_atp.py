import argparse
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import tarfile

from .adapters import POI_BOUNDS, inside, point


def features(stream, ndjson=False):
    if ndjson:
        while line := stream.readline(8_000_001):
            if len(line) > 8_000_000:
                raise ValueError('single ATP feature > 8 MB ?!')
            if line.strip():
                yield json.loads(line)
    else:
        import ijson

        yield from ijson.items(stream, 'features.item', use_float=True)


def read_snapshot(path):
    if tarfile.is_tarfile(path):
        with tarfile.open(path, 'r|*') as archive:
            for member in archive:
                if not member.isfile() or not member.name.endswith(('.geojson', '.json', '.ndjson', '.jsonl')):
                    continue
                with archive.extractfile(member) as stream:
                    yield from features(stream, member.name.endswith(('.ndjson', '.jsonl')))
    else:
        opener = gzip.open if path.suffix == '.gz' else open
        name = path.name.removesuffix('.gz')
        with opener(path, 'rb') as stream:
            yield from features(stream, name.endswith(('.ndjson', '.jsonl')))


def import_snapshot(path, output, snapshot_date=None):
    selected, seen = [], set()
    for feature in read_snapshot(Path(path)):
        geometry = feature.get('geometry') or {}
        pos = point(geometry.get('coordinates')) if geometry.get('type') == 'Point' else None
        if not pos or not inside(pos):
            continue
        serialized = json.dumps(feature, sort_keys=True, ensure_ascii=False)
        if serialized in seen:
            continue
        seen.add(serialized)
        selected.append(feature)
        if len(selected) > 20000:
            raise ValueError('more than 20000 features, something is wrong with the snapshot')
    payload = {
        'type': 'FeatureCollection',
        'features': selected,
        'cydney': {
            'imported_at': datetime.now(timezone.utc).isoformat(),
            'snapshot_date': snapshot_date,
            'input_file': Path(path).name,
            'bounds': POI_BOUNDS,
            'license': 'AllThePlaces / CC0',
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode()) > 25_000_000:
        raise ValueError('output > 25 MB')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.tmp')
    temporary.write_text(encoded)
    temporary.replace(output)
    return len(selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path, help='Published AllThePlaces snapshot, downloaded separately')
    parser.add_argument('--output', type=Path, default=Path('.cache/imports/alltheplaces.geojson'))
    parser.add_argument('--snapshot-date', help='Published snapshot date; import time is not scrape/observation time')
    args = parser.parse_args()
    count = import_snapshot(args.snapshot, args.output, args.snapshot_date)
    print(f'{count} Munich features -> {args.output}')


if __name__ == '__main__':
    main()
