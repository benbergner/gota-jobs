#!/usr/bin/env python3
"""
The place index (PLACES.md): GeoNames' populated places, every one worldwide of 500 people or more and
all of them in FULL_COUNTRIES, each with a URL slug that is assigned once and then kept (slugs.tsv:
the previous build's, the new places get new ones). Writes, under places/ (FORMAT.md):

  slugs.tsv              geonameid and slug of every place ever given one
  by-slug/<abc>.json     the places by the first three letters of their slug, four or more where that
                         would be too many (meta.json lists those prefixes); the place pages look them up
  search/<abc>.json      names by their words' first three letters, most populous first (the place search)
  sitemap/<lang>-<n>.txt the pages to list in the sitemaps, by language, 10,000 a file

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
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile

GEONAMES = 'https://download.geonames.org/export/dump'
# every populated place here, villages and town districts too; elsewhere those of 500 people or more
FULL_COUNTRIES = ['DE', 'AT', 'CH', 'LI']
# the language of each full country's local names (the German pages show them)
LOCAL_LANG = {'DE': 'de', 'AT': 'de', 'CH': 'de', 'LI': 'de'}
# German-speaking regions elsewhere (GeoNames admin2): their villages keep their German names too
GERMAN_REGIONS = {'IT.17.BZ'}  # South Tyrol
# populated places, but not historical, abandoned, destroyed or religious ones
KINDS = {'PPL', 'PPLA', 'PPLA2', 'PPLA3', 'PPLA4', 'PPLA5', 'PPLC', 'PPLG', 'PPLL', 'PPLS', 'PPLF', 'PPLX'}
# the more important first where names are equal in population
RANK = {'PPLC': 0, 'PPLA': 1, 'PPLG': 1, 'PPLA2': 2, 'PPLA3': 3, 'PPLA4': 4, 'PPLA5': 5, 'PPL': 6, 'PPLS': 7, 'PPLL': 7, 'PPLF': 7, 'PPLX': 8}
# paths the site uses itself
RESERVED = {'api', 'data', 'assets', 'materials', 'sitemaps', 'weather', 'wetter', 'place', 'index', 'robots', 'favicon'}
# Which pages search engines are asked to index (the record's `x`, the sitemaps): the German pages of
# every place in these countries, elsewhere (and the English pages) of towns of this many people.
# A new site gets few pages indexed; it should ask for those people search for first.
INDEX_DE_COUNTRIES = set(FULL_COUNTRIES)
INDEX_MIN_POP = 50000
SITEMAP_URLS = 10000
# places per by-slug shard at most (where a longer prefix can split it)
SHARD_MAX = 800


def log(*a):
    print(*a, flush=True)


def download(name: str, cache: str) -> bytes:
    path = os.path.join(cache, name.replace('/', '_'))
    if not os.path.exists(path):
        log('downloading', name)
        part = path + '.part'
        if os.path.exists(part):
            os.remove(part)
        # the server resets long downloads now and then: carry on where it stopped
        for attempt in range(10):
            done = os.path.getsize(part) if os.path.exists(part) else 0
            req = urllib.request.Request(f'{GEONAMES}/{name}', headers={'User-Agent': 'gota-places-job', **({'Range': f'bytes={done}-'} if done else {})})
            try:
                with urllib.request.urlopen(req, timeout=300) as res, open(part, 'ab' if res.status == 206 else 'wb') as fh:
                    while chunk := res.read(1 << 20):
                        fh.write(chunk)
                break
            except (OSError, urllib.error.URLError) as e:
                if attempt == 9:
                    raise
                log('download stopped, resuming:', e)
                time.sleep(5 * (attempt + 1))
        os.replace(path + '.part', path)
    with open(path, 'rb') as fh:
        return fh.read()


def zipped_lines(path: str, member: str):
    """A large zipped file's rows, read as a stream."""
    with zipfile.ZipFile(path) as z, z.open(member) as fh:
        for line in io.TextIOWrapper(fh, encoding='utf-8'):
            if line and not line.startswith('#'):
                yield line.rstrip('\n').split('\t')


def best_names(rows, lang: str, ids: set[str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """From alternate-name rows: per GeoNames id the best name in `lang` (preferred, not a short name,
    short) and the shortest (for regions: "Bayern", not "Freistaat Bayern"); colloquial and historic
    names left out."""
    best, shortest = {}, {}
    for r in rows:
        if len(r) < 8 or r[2] != lang or r[6] == '1' or r[7] == '1' or (ids is not None and r[1] not in ids):
            continue
        score = (r[4] == '1', r[5] != '1', -len(r[3]))
        if r[1] not in best or score > best[r[1]][0]:
            best[r[1]] = (score, r[3])
        if r[1] not in shortest or len(r[3]) < len(shortest[r[1]]):
            shortest[r[1]] = r[3]
    return {k: v[1] for k, v in best.items()}, shortest


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
        # names in the country's language
        best, short = best_names(lines(download(f'alternatenames/{cc}.zip', a.cache), f'{cc}.txt'), LOCAL_LANG[cc])
        local.update(best)
        shortest.update(short)
    log('places', len(rows))
    # German names of the places and states elsewhere, for the German pages ("Mailand", "Lombardei")
    abroad = {gid for gid, r in rows.items() if r[8] not in FULL_COUNTRIES} | {v[1] for k, v in admin1.items() if k[:2] not in FULL_COUNTRIES}
    download('alternateNamesV2.zip', a.cache)
    german, german_short = best_names(zipped_lines(os.path.join(a.cache, 'alternateNamesV2.zip'), 'alternateNamesV2.txt'), 'de', abroad)
    log('german names abroad', len(german))

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
        pop = int(r[14] or 0)
        # a German name abroad where German speakers use one: for towns of some size, and everywhere in
        # German-speaking regions; not the old names of villages elsewhere ("Espenkrug" for Osowa, even
        # where GeoNames marks them preferred), nor additions in brackets or names of districts
        german_ok = pop >= INDEX_MIN_POP or f'{cc}.{r[10]}.{r[11]}' in GERMAN_REGIONS
        de = re.sub(r'\s*\(.*\)$', '', german.get(gid, '')) if cc not in FULL_COUNTRIES and german_ok else ''
        de = '' if '/' in de or re.match(r'(Bezirk|Kreis|Landkreis|Distrikt|Provinz|Gemeinde) ', de) else de
        a1_de = (german_short.get(a1[1]) if a1 else None) if cc not in FULL_COUNTRIES else None
        index = [lang for lang, yes in (('de', cc in INDEX_DE_COUNTRIES or pop >= INDEX_MIN_POP), ('en', pop >= INDEX_MIN_POP)) if yes]
        places.append(
            {
                'id': int(gid),
                'n': name_local,
                'en': r[1] if r[1] != name_local and not full else None,
                'de': de if de and de != name_local else None,
                'ascii': r[2] or r[1],
                'lat': round(float(r[4]), 5),
                'lon': round(float(r[5]), 5),
                'cc': cc,
                'a1': region(a1),
                # an English name where it is one ("Bavaria"), not a description ("State of Berlin")
                'a1en': a1[0] if a1 and region(a1) not in a1[0] else None,
                'a1de': a1_de if a1_de and a1_de != region(a1) else None,
                'a2': region(admin2.get(f'{cc}.{r[10]}.{r[11]}')),
                'p': parent if parent and parent != name_local else None,
                'pop': pop,
                'f': r[7],
                'tz': r[17],
                'ele': int(r[15] or r[16] or 0) if (r[15] or r[16]) not in ('', '-9999') else None,
                # the languages whose page search engines should index
                'x': ' '.join(index) or None,
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
        for name in {p['n'], p['en'] or p['n'], p['de'] or p['n']}:
            for key in {w[:3] for w in words(name) if len(w) >= 2}:
                search[key].append([name, p['slug'], region, p['cc'], p['pop']])
    write_dir(os.path.join(out, 'search'), {k: json.dumps(v, ensure_ascii=False, separators=(',', ':')) for k, v in search.items()})

    # sitemaps: per language the pages to index, each line the slug and the languages it is indexed in
    chunks, sitemaps, listed = {}, {}, {}
    for lang in ('de', 'en'):
        rows_ = [f"{p['slug']}\t{p['x']}" for p in places if lang in (p['x'] or '').split()]
        listed[lang] = len(rows_)
        sitemaps[lang] = (len(rows_) + SITEMAP_URLS - 1) // SITEMAP_URLS
        for i in range(0, len(rows_), SITEMAP_URLS):
            chunks[f'{lang}-{i // SITEMAP_URLS}'] = '\n'.join(rows_[i : i + SITEMAP_URLS])
    write_dir(os.path.join(out, 'sitemap'), chunks, '.txt')
    meta = {'places': len(places), 'sitemaps': sitemaps, 'listed': listed, 'split': sorted(split)}
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
