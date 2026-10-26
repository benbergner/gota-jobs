# gota data jobs

Scheduled jobs that keep the data of [gota](https://github.com/benbergner/gota)'s place pages fresh
on its R2 bucket. Public so that GitHub Actions' minutes are free. The layout they write is in
[FORMAT.md](FORMAT.md).

| Job | Schedule | What |
|---|---|---|
| [forecast.py](forecast.py) | twice a day | ECMWF IFS open data at 0.1°, 10 days, land cells, as one point-major file |
| [places.py](places.py) | monthly | GeoNames populated places, URL slugs (assigned once, kept), search and sitemap lists |

Secrets: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (an R2 API token with write
access to the bucket only); variable `R2_BUCKET` (default `world-data`).

Locally:

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python forecast.py --out out --bbox 5.8,47.2,15.1,55.1 --max-step 48   # a small test file
.venv/bin/python places.py --out out
```

Data: ECMWF open data, © ECMWF, CC BY 4.0. GeoNames, CC BY 4.0.

The source of these files is `jobs/` in the gota repository (pushed here with
`git subtree push --prefix jobs <this repo> main`).
