# HomeQuest — Israeli supermarket price sync

A small Python job that keeps Israeli supermarket prices in a Supabase (Postgres)
database, so the HomeQuest shopping list can compare what a basket costs at each chain.

It runs **once a day** from GitHub Actions. There is no server to maintain.

## Why this repo is separate and public

The HomeQuest app itself lives in a **private** repo and stays there. Only this scraper
is public, for one reason: GitHub Actions minutes are unlimited on public repositories
and capped on private ones. Putting the daily job here means it can never run the app
repo out of build minutes — and the app's source never has to be exposed for that.

The two repos share nothing but the database:

```
this repo  ──scrapes──>  Supabase  <──reads──  HomeQuest app (private repo)
```

Nothing here is secret. The one credential — the Supabase secret key — lives in
*Settings → Secrets and variables → Actions*, never in the code.

## Where the data comes from

Israeli law (the Price Transparency regulations) requires every supermarket chain to
publish machine-readable price files for each of its branches. This job downloads those
official files via the maintained [`il-supermarket-scraper`](https://pypi.org/project/il-supermarket-scraper/)
library, parses them, and upserts `(barcode, chain) -> price` rows.

Chains currently covered: Rami Levy, Shufersal, Victory, Yohananof, Osher Ad,
Mahsani HaShuk, Yaynot Bitan / Carrefour.

## Setup (one time)

1. **Add the secret** — *Settings → Secrets and variables → Actions → New repository secret*

   | Name | Value |
   | --- | --- |
   | `SUPABASE_SERVICE_KEY` | the `sb_secret_…` key from the Supabase dashboard |

   Optionally add a **variable** (not a secret) `SUPABASE_URL` to point at a different
   project; the script defaults to the production one.

2. **Create the database objects** — run `supabase_schema.sql` once in the Supabase SQL
   editor. It creates the `prices` table and the `compare_basket` function the app calls.

3. **Run it once by hand** — *Actions → Daily Price Sync → Run workflow*.

## Reading a run

Every run writes a per-chain table into the job summary, commits it to
[`logs/last-sync.json`](logs/), and uploads it as an artifact:

| Chain | Files | Parsed | Upserted | Status |
| --- | ---: | ---: | ---: | --- |
| רמי לוי | 3 | 9,300 | 9,300 | OK |
| יינות ביתן / קארפור | 3 | 0 | 0 | no products |

- **Files** — how many files actually landed on disk.
- **Parsed** — how many products came out of them.

Files but zero parsed means a **parser** problem (an unrecognised XML layout or file
naming scheme), not a download problem. That distinction is the whole point of the two
columns.

A chain that parses nothing raises a warning annotation. The run fails outright only
when *no* chain produced rows — a silent no-op must never look like success, which is
exactly how an earlier version of this job rotted unnoticed for 71 days.

## Running one chain

Use the `chains` input on a manual run, or `SYNC_CHAINS` locally:

```bash
SYNC_CHAINS=YAYNO_BITAN_AND_CARREFOUR,MAHSANI_ASHUK_NEW_SOURCE python sync_prices.py
```

## Running locally

**Linux or WSL only** — the scraper library imports `fcntl`, which does not exist on
Windows.

```bash
pip install -r requirements.txt
cp .env.example .env      # paste the real secret key into it
SUPABASE_SERVICE_KEY=sb_secret_... python sync_prices.py
```

## Adding a chain

Add its `ScraperFactory` enum name to `PILOT_CHAINS` and a Hebrew label to
`CHAIN_LABELS`, both in `sync_prices.py`. Nothing else changes.
