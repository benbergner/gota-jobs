# Data layout on R2

What the jobs here write and the site's Pages Functions read (`functions/_forecast.ts`,
`functions/_places.ts` in the game's repository). Change both together and bump `format`.

## Forecast: `weather/forecast/global/`

`latest.json`: the newest complete run, `{ path: "weather/forecast/global/2026102500/", ...meta }`
(cached 5 minutes). Each run's files lie under its own path and never change; the job keeps the
newest two runs and deletes the older ones.

`<run>/meta.json`:

| key | |
|---|---|
| `format` | 1 |
| `model`, `resolution` | `"ECMWF IFS"`, `0.1` (degrees) |
| `run` | the run's time, `"2026-10-25T00:00:00Z"` |
| `steps` | hours after the run: 0–144 every 3 h, 150–240 every 6 h (65 steps) |
| `fields` | the bytes of a step, in order (below) |
| `record` | bytes per cell: steps × fields (650) |
| `cells` | cells in `forecast.bin` |
| `grid` | `rows`, `cols`, `lat0`, `lon0` (the first cell's centre, north-west), `dlat` (negative), `dlon` |

`<run>/cells.bin`: which grid cells are stored (land and coast, north of 60° S) and where.

- 32-byte header: `u32` magic `GWC1`, `u32` rows, `u32` cols, `f32` lat0, lon0, dlat, dlon, padding.
- A row table: per grid row three `u32`: cells stored in the rows before it, index of its first span,
  number of spans.
- The spans: `u16` pairs, first and one-past-last column of a run of stored cells.

All little-endian. A cell at row `r = round((lat − lat0) / dlat)`, column
`c = round(((lon − lon0) mod 360) / dlon)` is stored if a span of row `r` contains `c`; its record is
number `before[r] + (c − start)` plus the lengths of the row's spans before that one.

`<run>/forecast.bin`: the records, `record` bytes each, in that order. Within one, the steps in order,
each `fields` bytes. 255 is missing in every field.

| field | byte | value |
|---|---|---|
| `temp` | (°C + 60) × 2 | 2 m temperature |
| `precip` | √mm × 20 | precipitation since the step before |
| `snow` | √mm × 20 | of which snowfall (water) |
| `cloud` | % × 2 | total cloud cover |
| `wind` | km/h | 10 m wind |
| `windDir` | ° × 254 / 360 | where it comes from |
| `gust` | km/h | strongest 10 m gust since the step before (0 at step 0) |
| `humidity` | % | 2 m relative humidity (from temperature and dew point) |
| `pressure` | hPa − 900 | mean sea level pressure |
| `cape` | √(J/kg) × 3 | most unstable CAPE (thunderstorm potential) |

## Places: `places/`

From GeoNames (CC BY 4.0): every populated place in DE, AT and CH, elsewhere those of 500 people or
more.

- `slugs.tsv`: `geonameid<TAB>slug` of every place ever given a slug. A slug, once given, is kept.
- `meta.json`: `{ places, sitemaps, listed, split }`; `split` lists the slug prefixes whose places are
  sharded by a letter more.
- `by-slug/<prefix>.json`: `{ slug: place }` for the slugs starting with `<prefix>`: their first three
  letters, or more while the prefix is in `split`. A place: `id` (GeoNames), `n` (local name), `en`
  (English name if different), `lat`, `lon`, `cc` (country), `a1` (state) and `a1en`, `a2` (district),
  `p` (the municipality, for districts and villages), `pop`, `f` (GeoNames feature code), `tz`, `ele`
  (m). Absent keys are empty.
- `search/<abc>.json`: `[name, slug, region, country, population]` for each word of each place's names
  starting with `abc` (normalised: lower case, umlauts spelled out, no accents), most populous first.
- `sitemap/<n>.txt`: the slugs listed in sitemap `n`, one a line.
