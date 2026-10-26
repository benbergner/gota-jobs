#!/usr/bin/env python3
"""
The place index (PLACES.md): GeoNames' populated places, every one worldwide of 500 people or more and
all of them in FULL_COUNTRIES, each with a URL slug that is assigned once and then kept (slugs.tsv:
the previous build's, the new places get new ones). Writes, under places/ (FORMAT.md):

  slugs.tsv              geonameid and slug of every place ever given one
  by-slug/<abc>.json     the places by the first three letters of their slug, four or more where that
                         would be too many (meta.json lists those prefixes); the place pages look them up
  search/<abc>.json      names by their words' first three letters, most populous first (the place search)
  sitemap/<n>.txt        the place pages' slugs for the sitemaps, 5,000 places (10,000 URLs) a file

  python places.py --out out                    # downloads GeoNames into cache/, keeps out/places/slugs.tsv
  python places.py --out out --slugs prev.tsv   # with the slugs of the build before
  python places.py --out out --upload           # ... and to R2 (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET)
"""
import argparse
import collections
import concurrent.futures as cf
import hashlib
import io
import json
import os
import re
import unicodedata
import urllib.request
import zipfile

GEONAMES = 'https://download.geonames.org/export/dump'
# every populated place here, villages and town districts too; elsewhere those of 500 people or more
FULL_COUNTRIES = ['DE', 'AT', 'CH']
# the language of each full country's local names (the German pages show them)
LOCAL_LANG = {'DE': 'de', 'AT': 'de', 'CH': 'de'}
# populated places, but not historical, abandoned, destroyed or religious ones
KINDS = {'PPL', 'PPLA', 'PPLA2', 'PPLA3', 'PPLA4', 'PPLA5', 'PPLC', 'PPLG', 'PPLL', 'PPLS', 'PPLF', 'PPLX'}
# the more important first where names are equal in population
RANK = {'PPLC': 0, 'PPLA': 1, 'PPLG': 1, 'PPLA2': 2, 'PPLA3': 3, 'PPLA4': 4, 'PPLA5': 5, 'PPL': 6, 'PPLS': 7, 'PPLL': 7, 'PPLF': 7, 'PPLX': 8}
# paths the site uses itself
RESERVED = {'api', 'data', 'assets', 'materials', 'sitemaps', 'weather', 'wetter', 'place', 'index', 'robots', 'favicon'}
# the sitemaps: all places of these countries, elsewhere towns of this many people
SITEMAP_COUNTRIES = set(FULL_COUNTRIES)
SITEMAP_MIN_POP = 50000
SITEMAP_PLACES = 5000
# places per by-slug shard at most (where a longer prefix can split it)
SHARD_MAX = 800


def log(*a):
    print(*a, flush=True)


def download(name: str, cache: str) -> bytes:
    path = os.path.join(cache, name.replace('/', '_'))
    if not os.path.exists(path):
        log('downloading', name)
        req = urllib.request.Request(f'{GEONAMES}/{name}', headers={'User-Agent': 'gota-places-job'})
        with urllib.request.urlopen(req, timeout=300) as res, open(path + '.part', 'wb') as fh:
            fh.write(res.read())
        os.replace(path + '.part', path)
    with open(path, 'rb') as fh:
        return fh.read()


def lines(data: bytes, member: str | None = None):
    if member:
        data = zipfile.ZipFile(io.BytesIO(data)).read(member)
    for line in io.TextIOWrapper(io.BytesIO(data), encoding='utf-8'):
        if line and not line.startswith('#'):
            yield line.rstrip('\n').split('\t')


def slugify(s: str) -> str:
    s = s.lower()
    for a, b in (('ä', 'ae'), ('ö', 'oe'), ('ü', 'ue'), ('ß', 'ss'), ('æ', 'ae'), ('ø', 'oe'), ('å', 'aa'), ('ł', 'l'), ('đ', 'd'), ('ð', 'd'), ('þ', 'th'), ('ı', 'i')):
        s = s.replace(a, b)
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


def words(s: str) -> list[str]:
    """As the search normalises them (src/search.ts: lower case, umlauts spelled out, no accents)."""
    return [w for w in slugify(s).split('-') if w]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='out')
    ap.add_argument('--cache', default='cache')
    ap.add_argument('--slugs', help="the previous build's slugs.tsv (default: <out>/places/slugs.tsv if there; with --upload, R2's)")
    ap.add_argument('--upload', action='store_true')
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    out = os.path.join(a.out, 'places')
    os.makedirs(out, exist_ok=True)

    countries = {r[0]: r[4] for r in lines(download('countryInfo.txt', a.cache)) if len(r) > 4}
    admin1 = {r[0]: (r[1], r[3]) for r in lines(download('admin1CodesASCII.txt', a.cache))}
    admin2 = {r[0]: (r[1], r[3]) for r in lines(download('admin2Codes.txt', a.cache))}

    rows = {}
    for r in lines(download('cities500.zip', a.cache), 'cities500.txt'):
        if r[8] not in FULL_COUNTRIES and r[7] in KINDS:
            rows[r[0]] = r
    gemeinden = {}
    local = {}
    shortest = {}
    for cc in FULL_COUNTRIES:
        for r in lines(download(f'{cc}.zip', a.cache), f'{cc}.txt'):
            if r[6] == 'P' and r[7] in KINDS:
                rows[r[0]] = r
            elif r[7] == 'ADM4':
                gemeinden[(r[8], r[10], r[11], r[12], r[13])] = r[1]
        # names in the country's language: preferred ones first (of regions the shortest: "Bayern", not "Freistaat Bayern")
        lang = LOCAL_LANG[cc]
        best = {}
        for r in lines(download(f'alternatenames/{cc}.zip', a.cache), f'{cc}.txt'):
            if len(r) < 8 or r[2] != lang or r[6] == '1' or r[7] == '1':  # colloquial, historic
                continue
            score = (r[4] == '1', r[5] != '1', -len(r[3]))  # preferred, not a short name, short
            if r[1] not in best or score > best[r[1]][0]:
                best[r[1]] = (score, r[3])
            if r[1] not in shortest or len(r[3]) < len(shortest[r[1]]):
                shortest[r[1]] = r[3]
        local.update({k: v[1] for k, v in best.items()})
    log('places', len(rows))

    def region(entry):
        """A region's name: the shortest in the local language ("Berlin", not "Land Berlin"), else GeoNames'."""
        return (shortest.get(entry[1]) or entry[0]) if entry else None

    places = []
    for gid, r in rows.items():
        cc = r[8]
        a1 = admin1.get(f'{cc}.{r[10]}')
        name_local = local.get(gid, r[1]) if cc in FULL_COUNTRIES else r[1]
        # the municipality, as "Schwerin" (not "Schwerin, Landeshauptstadt")
        parent = (gemeinden.get((cc, r[10], r[11], r[12], r[13])) or '').split(',')[0].strip()
        # a town named without its official addition ("Werder" for "Werder (Havel)"): the full name
        full = r[7] != 'PPLX' and parent.startswith(name_local + ' ')
        if full:
            name_local = parent
        places.append(
            {
                'id': int(gid),
                'n': name_local,
                'en': r[1] if r[1] != name_local and not full else None,
                'ascii': r[2] or r[1],
                'lat': round(float(r[4]), 5),
                'lon': round(float(r[5]), 5),
                'cc': cc,
                'a1': region(a1),
                # an English name where it is one ("Bavaria"), not a description ("State of Berlin")
                'a1en': a1[0] if a1 and region(a1) not in a1[0] else None,
                'a2': region(admin2.get(f'{cc}.{r[10]}.{r[11]}')),
                'p': parent if parent and parent != name_local else None,
                'pop': int(r[14] or 0),
                'f': r[7],
                'tz': r[17],
                'ele': int(r[15] or r[16] or 0) if (r[15] or r[16]) not in ('', '-9999') else None,
            }
        )

    # slugs: the previous build's kept, then the new places most important first
    prev_path = a.slugs or os.path.join(out, 'slugs.tsv')
    prev_data = open(prev_path, 'rb').read() if os.path.exists(prev_path) else None
    if a.upload and not a.slugs:
        # the published slugs, so that no place's URL changes (none there: the first build)
        s3, bucket = r2()
        try:
            prev_data = s3.get_object(Bucket=bucket, Key='places/slugs.tsv')['Body'].read()
        except s3.exceptions.NoSuchKey:
            prev_data = None
    prev = {int(r[0]): r[1] for r in lines(prev_data)} if prev_data else {}
    log('slugs before', len(prev))
    taken = set(prev.values()) | RESERVED
    places.sort(key=lambda p: (-p['pop'], RANK.get(p['f'], 9), p['id']))
    for p in places:
        if p['id'] in prev:
            p['slug'] = prev[p['id']]
            continue
        base = slugify(p['n'] if p['cc'] in FULL_COUNTRIES else p['ascii']) or str(p['id'])
        tries = [base] + [f'{base}-{slugify(q)}' for q in (p['p'], p['a2'], p['a1en'] or p['a1'], countries.get(p['cc'])) if q and slugify(q) and slugify(q) != base]
        p['slug'] = next((s for s in tries if s not in taken), f'{base}-{p["id"]}')
        taken.add(p['slug'])
        prev[p['id']] = p['slug']
    with open(os.path.join(out, 'slugs.tsv'), 'w') as fh:
        fh.write('# geonameid\tslug: assigned once, never changed\n')
        for gid, slug in sorted(prev.items()):
            fh.write(f'{gid}\t{slug}\n')

    # by slug: small shards, as a place page reads one in its few milliseconds of CPU
    split = set()
    counts = collections.Counter()
    for n in range(3, 8):
        counts = collections.Counter(shard_key(p['slug'], split) for p in places)
        big = {k for k, c in counts.items() if c > SHARD_MAX and len(k) == n}
        if not big:
            break
        split |= big
    shards = collections.defaultdict(dict)
    for p in places:
        rec = {k: v for k, v in p.items() if v is not None and k not in ('ascii', 'slug')}
        shards[shard_key(p['slug'], split)][p['slug']] = rec
    write_dir(os.path.join(out, 'by-slug'), {k: json.dumps(v, ensure_ascii=False, separators=(',', ':')) for k, v in shards.items()})

    # search: [name, slug, region, country, population], by the first three letters of each word of the name
    search = collections.defaultdict(list)
    for p in places:  # most populous first
        # a district or village by its municipality, a town by its state
        region = (p['p'] if p['f'] == 'PPLX' or not p['a1'] else p['a1']) or ''
        for name in {p['n'], p['en'] or p['n']}:
            for key in {w[:3] for w in words(name) if len(w) >= 2}:
                search[key].append([name, p['slug'], region, p['cc'], p['pop']])
    write_dir(os.path.join(out, 'search'), {k: json.dumps(v, ensure_ascii=False, separators=(',', ':')) for k, v in search.items()})

    # sitemaps
    listed = [p['slug'] for p in places if p['cc'] in SITEMAP_COUNTRIES or p['pop'] >= SITEMAP_MIN_POP]
    chunks = {str(i // SITEMAP_PLACES): '\n'.join(listed[i : i + SITEMAP_PLACES]) for i in range(0, len(listed), SITEMAP_PLACES)}
    write_dir(os.path.join(out, 'sitemap'), chunks, '.txt')
    meta = {'places': len(places), 'sitemaps': len(chunks), 'listed': len(listed), 'split': sorted(split)}
    with open(os.path.join(out, 'meta.json'), 'w') as fh:
        json.dump(meta, fh)
    log(meta, 'shards', len(shards), 'search keys', len(search))
    if a.upload:
        upload(a.out)


def shard_key(slug: str, split: set[str]) -> str:
    """The slug's first three letters, a letter more for each prefix that is split (as functions/_places.ts)."""
    key = slug[:3]
    while key in split and len(slug) > len(key):
        key = slug[: len(key) + 1]
    return key


def write_dir(path: str, files: dict[str, str], ext: str = '.json'):
    os.makedirs(path, exist_ok=True)
    for f in os.listdir(path):
        os.remove(os.path.join(path, f))
    for k, v in files.items():
        with open(os.path.join(path, k + ext), 'w') as fh:
            fh.write(v)


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


def upload(root: str):
    """Everything under <root>/places to R2: new and changed files, then the files gone."""
    s3, bucket = r2()

    existing = {}
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix='places/'):
        for o in page.get('Contents', []):
            existing[o['Key']] = o['ETag'].strip('"')
    local = {}
    for d, _, fs in os.walk(os.path.join(root, 'places')):
        for f in fs:
            path = os.path.join(d, f)
            local[os.path.relpath(path, root).replace(os.sep, '/')] = path

    def put(key: str):
        body = open(local[key], 'rb').read()
        if existing.get(key) == hashlib.md5(body).hexdigest():
            return 0
        ctype = 'application/json' if key.endswith('.json') else 'text/plain; charset=utf-8'
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=ctype, CacheControl='public, max-age=86400')
        return 1

    with cf.ThreadPoolExecutor(16) as pool:
        n = sum(pool.map(put, sorted(local)))
    gone = [k for k in existing if k not in local]
    for i in range(0, len(gone), 1000):
        s3.delete_objects(Bucket=bucket, Delete={'Objects': [{'Key': k} for k in gone[i : i + 1000]]})
    log('uploaded', n, 'changed of', len(local), 'deleted', len(gone))


if __name__ == '__main__':
    main()
