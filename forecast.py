#!/usr/bin/env python3
"""
The global forecast (PLACES.md): the latest ECMWF IFS run at 0.1° from ECMWF's open data, as one
point-major file, each land cell's 10 days in one record, so one byte-range read returns a place's
forecast. Written to R2 under weather/forecast/global/<run>/ with latest.json pointing at it; the
run before is deleted by the next job (the retry some two hours later). The layout is in FORMAT.md.

  python forecast.py --out out                 # the latest run, into out/
  python forecast.py --out out --upload        # ... and to R2 (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET)
  python forecast.py --out out --bbox 5.8,47.2,15.1,55.1 --max-step 48   # a small test file
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

import eccodes
import numpy as np

# The AWS mirror first (no limit on parallel downloads), ECMWF's own server if a file isn't there.
SOURCES = ['https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com', 'https://data.ecmwf.int/forecasts']
# 3-hourly to 6 days, then 6-hourly to 10 days (the model has little skill beyond)
STEPS = list(range(0, 145, 3)) + list(range(150, 241, 6))
PARAMS = ['2t', '2d', 'tp', 'sf', 'tcc', '10u', '10v', '10fg', 'msl', 'mucape']
# the record's fields per step, in this order (FORMAT.md has their encodings)
FIELDS = ['temp', 'precip', 'snow', 'cloud', 'wind', 'windDir', 'gust', 'humidity', 'pressure', 'cape']
MISSING = 255
PREFIX = 'weather/forecast/global'


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def fetch(path: str, byte_range: tuple[int, int] | None = None, tries: int = 10) -> bytes:
    """A file (or its bytes start..end inclusive), trying the sources in turn and waiting longer each round
    (the mirror throttles the newest run when everyone fetches it)."""
    sources = list(SOURCES)
    last: Exception | None = None
    for attempt in range(tries):
        base = sources[attempt % len(sources)]
        req = urllib.request.Request(f'{base}/{path}', headers={'User-Agent': 'gota-forecast-job'})
        if byte_range:
            req.add_header('Range', f'bytes={byte_range[0]}-{byte_range[1]}')
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                return res.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                sources.remove(base)  # not there
                if not sources:
                    break
                continue
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        if attempt % len(sources) == len(sources) - 1:
            time.sleep(min(30, 2 ** (attempt // len(sources))) * (0.5 + random.random()))
    raise RuntimeError(f'{path}: {last}')


def step_path(run: dt.datetime, step: int, ext: str) -> str:
    return f'{run:%Y%m%d}/{run:%H}z/ifs/0p1/oper/{run:%Y%m%d%H}0000-{step}h-oper-fc-sfc.{ext}'


def latest_run(now: dt.datetime) -> dt.datetime:
    """The newest 00 or 12 UTC run whose last step we need is out."""
    run = now.replace(minute=0, second=0, microsecond=0, hour=0 if now.hour < 12 else 12)
    for _ in range(6):
        try:
            fetch(step_path(run, STEPS[-1], 'index'), tries=4)
            return run
        except RuntimeError:
            run -= dt.timedelta(hours=12)
    raise RuntimeError('no complete run in the last three days')


def download_step(run: dt.datetime, step: int, params: list[str]) -> dict[str, bytes]:
    """The step's GRIB messages of `params`, by byte range from the index."""
    index = [json.loads(line) for line in fetch(step_path(run, step, 'index')).decode().splitlines() if line.strip()]
    out = {}
    for e in index:
        if e['param'] in params and e.get('levtype') == 'sfc' and e['param'] not in out:
            out[e['param']] = fetch(step_path(run, step, 'grib2'), (e['_offset'], e['_offset'] + e['_length'] - 1))
    return out


def decode(msg: bytes) -> tuple[np.ndarray, dict]:
    """A GRIB message's values (rows from the north) and its grid."""
    gid = eccodes.codes_new_from_message(msg)
    try:
        ni, nj = eccodes.codes_get(gid, 'Ni'), eccodes.codes_get(gid, 'Nj')
        grid = {
            'rows': nj,
            'cols': ni,
            'lat0': eccodes.codes_get(gid, 'latitudeOfFirstGridPointInDegrees'),
            'lon0': eccodes.codes_get(gid, 'longitudeOfFirstGridPointInDegrees'),
            'dlat': -eccodes.codes_get(gid, 'jDirectionIncrementInDegrees'),
            'dlon': eccodes.codes_get(gid, 'iDirectionIncrementInDegrees'),
        }
        if eccodes.codes_get(gid, 'jScansPositively') or eccodes.codes_get(gid, 'iScansNegatively'):
            raise RuntimeError('unexpected scanning mode')
        values = eccodes.codes_get_values(gid).astype(np.float32)
        return values, grid
    finally:
        eccodes.codes_release(gid)


def select_cells(lsm: np.ndarray, grid: dict, bbox: list[float] | None) -> np.ndarray:
    """Land and coast (land within one cell), north of 60° S; within `bbox` if given."""
    land = lsm.reshape(grid['rows'], grid['cols']) >= 0.02
    near = land.copy()
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            near |= np.roll(np.roll(land, dr, 0), dc, 1)
    lat = grid['lat0'] + grid['dlat'] * np.arange(grid['rows'])
    near &= (lat > -60)[:, None]
    if bbox:
        lon = (grid['lon0'] + grid['dlon'] * np.arange(grid['cols']) + 180) % 360 - 180
        w, s, e, n = bbox
        near &= ((lat >= s) & (lat <= n))[:, None] & ((lon >= w) & (lon <= e))[None, :]
    return near


def cells_index(mask: np.ndarray, grid: dict) -> bytes:
    """FORMAT.md, cells.bin: the grid, then per row the cells before it and its spans of selected columns."""
    rows, cols = mask.shape
    table = np.zeros((rows, 3), np.uint32)
    spans: list[tuple[int, int]] = []
    before = 0
    for r in range(rows):
        row = mask[r].astype(np.int8)
        edges = np.flatnonzero(np.diff(np.concatenate(([0], row, [0]))))
        pairs = list(zip(edges[::2].tolist(), edges[1::2].tolist()))
        table[r] = (before, len(spans), len(pairs))
        spans.extend(pairs)
        before += int(row.sum())
    header = np.array([0x31435747, rows, cols], np.uint32).tobytes()  # 'GWC1'
    header += np.array([grid['lat0'], grid['lon0'], grid['dlat'], grid['dlon']], np.float32).tobytes()
    header += b'\0' * (32 - len(header))
    return header + table.tobytes() + np.array(spans, np.uint16).reshape(-1, 2).tobytes()


def q(x: np.ndarray, lo: float = 0, hi: float = 254) -> np.ndarray:
    """Rounded into a byte, 255 left for missing."""
    out = np.clip(np.rint(x), lo, hi)
    out[~np.isfinite(x)] = MISSING
    return out.astype(np.uint8)


def encode(f: dict[str, np.ndarray], prev: dict[str, np.ndarray] | None) -> np.ndarray:
    """One step of the selected cells as FIELDS bytes (FORMAT.md)."""
    n = len(f['2t'])
    t = f['2t'] - 273.15
    td = f['2d'] - 273.15
    rh = 100 * np.exp(17.625 * td / (243.04 + td) - 17.625 * t / (243.04 + t))
    # accumulated since the run's start (m): the step's share
    precip = (f['tp'] - prev['tp']) * 1000 if prev else np.zeros(n, np.float32)
    snow = (f['sf'] - prev['sf']) * 1000 if prev else np.zeros(n, np.float32)
    u, v = f['10u'], f['10v']
    wind = np.hypot(u, v) * 3.6
    direction = (np.degrees(np.arctan2(-u, -v)) + 360) % 360  # where it comes from
    gust = f['10fg'] * 3.6 if '10fg' in f else np.full(n, np.nan, np.float32)
    cape = f['mucape'] if 'mucape' in f else np.full(n, np.nan, np.float32)
    return np.stack(
        [
            q((t + 60) * 2),
            q(np.sqrt(np.maximum(precip, 0)) * 20),
            q(np.sqrt(np.maximum(snow, 0)) * 20),
            q(f['tcc'] * 200),
            q(wind),
            q(direction * 254 / 360) % 254,  # 360° wraps to 0
            q(gust),
            q(rh, 0, 100),
            q(f['msl'] / 100 - 900),
            q(np.sqrt(np.maximum(cape, 0)) * 3),
        ],
        axis=1,
    )


def build(run: dt.datetime, out_dir: str, steps: list[int], bbox: list[float] | None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    log('run', f'{run:%Y-%m-%d %H} UTC,', len(steps), 'steps')
    first = download_step(run, 0, ['lsm'])
    lsm, grid = decode(first['lsm'])
    mask = select_cells(lsm, grid, bbox)
    idx = np.flatnonzero(mask.ravel())
    log('cells', len(idx), 'of', mask.size)
    with open(os.path.join(out_dir, 'cells.bin'), 'wb') as fh:
        fh.write(cells_index(mask, grid))

    record = np.zeros((len(idx), len(steps), len(FIELDS)), np.uint8)
    prev = None
    with cf.ThreadPoolExecutor(8) as pool:
        futures = [pool.submit(download_step, run, s, PARAMS) for s in steps]
        for i, (s, fut) in enumerate(zip(steps, futures)):
            msgs = fut.result()
            fields = {p: decode(m)[0][idx] for p, m in msgs.items()}
            missing = {'2t', '2d', 'tp', 'sf', 'tcc', '10u', '10v', 'msl'} - fields.keys()
            if missing:
                raise RuntimeError(f'step {s}: no {sorted(missing)}')
            record[:, i, :] = encode(fields, prev)
            prev = {'tp': fields['tp'], 'sf': fields['sf']}
            futures[i] = None  # its bytes can go
            log('step', s)
    record.tofile(os.path.join(out_dir, 'forecast.bin'))
    meta = {
        'format': 1,
        'model': 'ECMWF IFS',
        'resolution': 0.1,
        'run': run.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'steps': steps,
        'fields': FIELDS,
        'record': len(steps) * len(FIELDS),
        'cells': int(len(idx)),
        'grid': grid,
    }
    with open(os.path.join(out_dir, 'meta.json'), 'w') as fh:
        json.dump(meta, fh)
    log('written', os.path.getsize(os.path.join(out_dir, 'forecast.bin')) // 2**20, 'MB')
    return meta


def r2():
    """The R2 bucket (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET) as an S3 client and bucket name."""
    import boto3

    s3 = boto3.client(
        's3',
        endpoint_url=f'https://{os.environ["R2_ACCOUNT_ID"]}.r2.cloudflarestorage.com',
        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
        region_name='auto',
    )
    return s3, os.environ.get('R2_BUCKET', 'world-data')


def upload(out_dir: str, run: dt.datetime, meta: dict):
    from boto3.s3.transfer import TransferConfig

    s3, bucket = r2()
    key = f'{PREFIX}/{run:%Y%m%d%H}/'
    config = TransferConfig(multipart_threshold=64 * 2**20, multipart_chunksize=128 * 2**20, max_concurrency=8)
    for name, ctype in [('cells.bin', 'application/octet-stream'), ('forecast.bin', 'application/octet-stream'), ('meta.json', 'application/json')]:
        s3.upload_file(os.path.join(out_dir, name), bucket, key + name, ExtraArgs={'ContentType': ctype, 'CacheControl': 'public, max-age=31536000, immutable'}, Config=config)
        log('uploaded', key + name)
    latest = {'path': key, **meta}
    s3.put_object(Bucket=bucket, Key=f'{PREFIX}/latest.json', Body=json.dumps(latest).encode(), ContentType='application/json', CacheControl='public, max-age=300')
    log('latest ->', key)
    # the run before stays a while: the site reads latest.json once a minute, and may still read it
    prune(keep=2)


def prune(keep: int):
    """Deletes all runs but the one latest.json points at and, with keep=2, the newest other (the run
    before, which the site may still read for a minute). The next job, finding nothing new, leaves one."""
    s3, bucket = r2()
    current = json.loads(s3.get_object(Bucket=bucket, Key=f'{PREFIX}/latest.json')['Body'].read())['path']
    runs = sorted(p['Prefix'] for p in s3.list_objects_v2(Bucket=bucket, Prefix=f'{PREFIX}/', Delimiter='/').get('CommonPrefixes', []))
    others = [r for r in runs if r != current]
    for old in others[: len(others) - (keep - 1)] if keep > 1 else others:
        objs = s3.list_objects_v2(Bucket=bucket, Prefix=old).get('Contents', [])
        if objs:
            s3.delete_objects(Bucket=bucket, Delete={'Objects': [{'Key': o['Key']} for o in objs]})
        log('deleted', old)


def published_run() -> str | None:
    """The run latest.json on R2 points at."""
    s3, bucket = r2()
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=f'{PREFIX}/latest.json')['Body'].read())['run']
    except s3.exceptions.NoSuchKey:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='out')
    ap.add_argument('--run', help='YYYYMMDDHH (default: the latest complete 00/12 UTC run)')
    ap.add_argument('--max-step', type=int, default=STEPS[-1])
    ap.add_argument('--bbox', help='west,south,east,north: only these cells (for tests)')
    ap.add_argument('--upload', action='store_true')
    a = ap.parse_args()
    run = dt.datetime.strptime(a.run, '%Y%m%d%H').replace(tzinfo=dt.timezone.utc) if a.run else latest_run(dt.datetime.now(dt.timezone.utc))
    if a.upload and not a.run and published_run() == run.strftime('%Y-%m-%dT%H:%M:%SZ'):
        log('run', f'{run:%Y-%m-%d %H} UTC is published already')
        prune(keep=1)
        return
    steps = [s for s in STEPS if s <= a.max_step]
    meta = build(run, a.out, steps, [float(x) for x in a.bbox.split(',')] if a.bbox else None)
    if a.upload:
        if a.bbox or a.max_step != STEPS[-1]:
            sys.exit('not uploading a partial forecast')
        upload(a.out, run, meta)


if __name__ == '__main__':
    main()
