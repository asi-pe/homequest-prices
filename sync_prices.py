"""
HomeQuest — supermarket price sync.

Runs once a day. For each pilot chain it:
  1. Downloads the official price-transparency files (the chains are legally required
     to publish these) using the maintained `il-supermarket-scraper` library.
  2. Parses them into (barcode, name, price) rows.
  3. Upserts a lean copy into Supabase (Postgres) — NOT Firestore (too expensive at
     millions of rows).

The app later sends a shopping list and a Postgres function returns a ranked
basket comparison (cheapest chain first).

Scheduled by .github/workflows/prices-sync.yml (daily). It is NOT run by the
pikud-haoref microservice any more — see that workflow's header for why.

Env vars:
  SUPABASE_SERVICE_KEY  required. The Supabase SECRET key (sb_secret_...). Never commit it.
  SUPABASE_URL          optional, defaults to DEFAULT_SUPABASE_URL below.
  SYNC_CHAINS           optional. Comma-separated chain enums to sync (blank = all),
                        handy for re-running just the chains that are still broken.
  FILES_PER_CHAIN       optional, default 3. How many branch files to pull per chain;
                        more branches means a fuller catalogue but a longer run.

Exit code is 1 when no chain synced at all, so a silent failure shows up as a red
run instead of stale data nobody notices.

Run locally (Linux/WSL — the scraper library needs fcntl, it will not run on Windows):
  pip install -r requirements.txt
  SUPABASE_SERVICE_KEY=... python sync_prices.py
"""

import io
import os
import sys
import glob
import gzip
import json
import time
import tempfile
import xml.etree.ElementTree as ET

from supabase import create_client

# --- Pilot chains -----------------------------------------------------------
# Names must match il_supermarket_scarper.ScraperFactory enum members.
# To add a chain later: add its enum name here. Nothing else changes.
PILOT_CHAINS = [
    "RAMI_LEVY",
    "SHUFERSAL",
    "VICTORY_NEW_SOURCE",
    "YOHANANOF",
    "OSHER_AD",
    "MAHSANI_ASHUK_NEW_SOURCE",
    "YAYNO_BITAN_AND_CARREFOUR",  # יינות ביתן + קארפור — same publisher/source
]

# Human-readable label per chain (shown in the app). Keep keys == PILOT_CHAINS.
CHAIN_LABELS = {
    "RAMI_LEVY": "רמי לוי",
    "SHUFERSAL": "שופרסל",
    "VICTORY_NEW_SOURCE": "ויקטורי",
    "YOHANANOF": "יוחננוף",
    "OSHER_AD": "אושר עד",
    "MAHSANI_ASHUK_NEW_SOURCE": "מחסני השוק",
    "YAYNO_BITAN_AND_CARREFOUR": "יינות ביתן / קארפור",
}

BATCH_SIZE = 1000  # rows per upsert call

# How many PriceFull files (= branches) to pull per chain. See download_chain().
# Measured on 2026-09-02: three branch files cost 13-17s for a healthy chain, i.e.
# roughly 5s per branch, not the ~60s first assumed. Branch coverage is therefore
# cheap and the old default of 3 was leaving most of each catalogue on the floor.
# 40 branches is ~3.5 min/chain, ~25 min for all seven — well inside the CI timeout.
FILES_PER_CHAIN = int(os.environ.get("FILES_PER_CHAIN") or 40)

# The project URL is not a secret (the app ships it in client code), so it has a
# default and only the service key has to be configured on the host.
DEFAULT_SUPABASE_URL = "https://echyvokwnbhxbrgsoodt.supabase.co"

# Written next to the script so CI can upload it as an artifact.
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_report.json")


def get_supabase():
    url = os.environ.get("SUPABASE_URL") or DEFAULT_SUPABASE_URL
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not key:
        sys.exit(
            "ERROR: SUPABASE_SERVICE_KEY is not set.\n"
            "  CI:    add it under Settings -> Secrets and variables -> Actions.\n"
            "  Local: SUPABASE_SERVICE_KEY=sb_secret_... python sync_prices.py"
        )
    return create_client(url, key)


def chains_to_sync():
    """PILOT_CHAINS, optionally narrowed by the SYNC_CHAINS env var."""
    raw = (os.environ.get("SYNC_CHAINS") or "").strip()
    if not raw:
        return list(PILOT_CHAINS)
    wanted = [c.strip().upper() for c in raw.split(",") if c.strip()]
    unknown = [c for c in wanted if c not in PILOT_CHAINS]
    if unknown:
        sys.exit("ERROR: unknown chain(s) in SYNC_CHAINS: " + ", ".join(unknown))
    return wanted


# Chains whose scraper class routes through laibcatalog.co.il. The library walks
# every branch with a separate getfiles call (~140 requests, each returning
# thousands of entries), which hangs and gives up after ~9 minutes with 0 files —
# that alone burned 18 of the 21 minutes of a full run. The API itself is fine, so
# these two are fetched directly. Verified 2026-09-02: getfiles returns 70+
# pricefull entries per chain and a single branch file parses to ~8,600 products.
LAIBCATALOG_BASE = "https://laibcatalog.co.il"
LAIBCATALOG_CHAINS = {
    "VICTORY_NEW_SOURCE": ["7290696200003"],
    "MAHSANI_ASHUK_NEW_SOURCE": ["7290661400001"],
}


def download_chain_laibcatalog(chain_name, dest_dir):
    """Download the newest PriceFull file per branch straight from the API."""
    import json
    import urllib.parse
    import urllib.request

    def _get(url, attempts=3):
        # The host is occasionally slow to answer; a transient timeout must not cost
        # us the whole chain for the day.
        last = None
        for i in range(attempts):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.read()
            except Exception as e:  # noqa: BLE001 - retried below, re-raised if final
                last = e
                if i < attempts - 1:
                    time.sleep(3 * (i + 1))
        raise last

    downloaded = 0
    for edi in LAIBCATALOG_CHAINS[chain_name]:
        listing_url = (
            f"{LAIBCATALOG_BASE}/webapi/api/getfiles?"
            + urllib.parse.urlencode({"edi": edi})
        )
        try:
            entries = json.loads(_get(listing_url))
        except Exception as e:  # noqa: BLE001 - a bad listing must not kill the chain
            print(f"  laibcatalog listing failed for {edi}: {e}")
            continue

        # A malformed response can come back as a bare string rather than a list.
        if not isinstance(entries, list):
            print(f"  laibcatalog returned no usable listing for {edi}")
            continue

        full = [
            e for e in entries
            if isinstance(e, dict) and str(e.get("fileType", "")).lower() == "pricefull"
        ]
        # Newest first, then one file per branch: the listing holds several days.
        full.sort(key=lambda e: str(e.get("fileDate", "")), reverse=True)

        seen_branches = set()
        for entry in full:
            if downloaded >= FILES_PER_CHAIN:
                break
            branch = entry.get("branchNumber")
            if branch in seen_branches:
                continue
            seen_branches.add(branch)

            name = entry.get("fileName")
            if not name:
                continue
            try:
                blob = _get(f"{LAIBCATALOG_BASE}/webapi/{edi}/{name}")
            except Exception as e:  # noqa: BLE001 - skip one file, keep the rest
                print(f"  download failed for {name}: {e}")
                continue
            with open(os.path.join(dest_dir, name), "wb") as fh:
                fh.write(blob)
            downloaded += 1

    print(f"  laibcatalog: downloaded {downloaded} branch files")


def download_chain(chain_name, dest_dir):
    """Download the latest price files for one chain into dest_dir.

    Note the import module is `il_supermarket_scarper` (spelled "scarper") even
    though the pip package is `il-supermarket-scraper`.
    """
    if chain_name in LAIBCATALOG_CHAINS:
        download_chain_laibcatalog(chain_name, dest_dir)
        return

    from il_supermarket_scarper import ScarpingTask

    # Only price files matter for us (not promos/stores).
    task = ScarpingTask(
        enabled_scrapers=[chain_name],
        files_types=["PRICE_FULL_FILE"],
        output_configuration={
            "output_mode": "disk",
            "base_storage_path": dest_dir,
        },
        status_configuration={
            "database_type": "json",
            "base_path": os.path.join(dest_dir, "status"),
        },
    )
    # `limit` (max files to download) belongs to start() in this library version.
    #
    # It used to be hardcoded to 3, with a comment claiming "a few PriceFull files
    # already hold the full catalogue". That is wrong: under the price-transparency
    # rules each PriceFull file is ONE BRANCH, so limit=3 samples three arbitrary
    # branches and calls the result the chain's catalogue. Two chains' product counts
    # were therefore not comparable at all — they measured which branches we happened
    # to grab, not what the chains actually stock.
    #
    # Raising it costs wall-clock time roughly linearly, and the CI job has a hard
    # timeout, so the real value is an empirical trade-off rather than "all of them".
    # It is configurable so it can be measured instead of guessed.
    #
    # start() runs in a BACKGROUND thread — join() blocks until the download has
    # actually finished, otherwise we would parse an empty folder and get 0 products.
    task.start(limit=FILES_PER_CHAIN)
    task.join()


def _text(elem, *tags):
    """Return the first non-empty text among the given child tag names."""
    for tag in tags:
        node = elem.find(tag)
        if node is not None and node.text and node.text.strip():
            return node.text.strip()
    return None


def parse_price_file(path):
    """Yield (barcode, name, price) from one price XML (or .gz) file.

    The transparency format is not perfectly uniform across chains, so we read a
    few possible tag spellings for each field (ItemCode/Barcode, ItemName, ItemPrice).
    """
    try:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rb") as fh:
            tree = ET.parse(fh)
    except (ET.ParseError, OSError):
        return  # skip unreadable file, don't crash the whole run

    root = tree.getroot()
    # Items live under <Items><Item>...</Item></Items> (tag casing varies).
    for item in root.iter():
        if item.tag.lower() != "item":
            continue
        barcode = _text(item, "ItemCode", "Itemcode", "Barcode")
        name = _text(item, "ItemName", "Itemname", "ManufacturerItemDescription")
        price = _text(item, "ItemPrice", "Itemprice", "Price")
        if not barcode or not price:
            continue
        try:
            price_val = float(price)
        except ValueError:
            continue
        if price_val <= 0:
            continue
        yield barcode.strip(), (name or "").strip(), price_val


def collect_rows(chain_name, folder):
    """Parse every price file in folder into deduped Supabase rows for a chain.

    For a given barcode we keep the LAST price seen (files are same-day), so a
    barcode maps to one price per chain.
    """
    label = CHAIN_LABELS[chain_name]
    by_barcode = {}
    patterns = ["**/*PriceFull*.xml", "**/*PriceFull*.gz", "**/*pricefull*.xml", "**/*pricefull*.gz"]
    seen_files = set()
    for pat in patterns:
        for f in glob.glob(os.path.join(folder, pat), recursive=True):
            if f in seen_files:
                continue
            seen_files.add(f)
            for barcode, name, price in parse_price_file(f):
                by_barcode[barcode] = {
                    "barcode": barcode,
                    "name": name,
                    "chain": chain_name,
                    "chain_label": label,
                    "price": price,
                }

    # Diagnostics. Several chains download fine but parse to 0 products, and without
    # these two numbers you cannot tell "downloaded nothing" from "downloaded files
    # this glob does not recognise".
    all_files = [
        f for f in glob.glob(os.path.join(folder, "**", "*"), recursive=True)
        if os.path.isfile(f) and "/status/" not in f.replace(os.sep, "/")
    ]
    stats = {
        "files_downloaded": len(all_files),
        "files_matched": len(seen_files),
        "sample_filenames": sorted(os.path.basename(f) for f in all_files)[:5],
    }
    return list(by_barcode.values()), stats


def upsert_rows(sb, rows):
    """Upsert rows into the `prices` table in batches (conflict on barcode+chain)."""
    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        sb.table("prices").upsert(batch, on_conflict="barcode,chain").execute()
        total += len(batch)
    return total


def write_report(results):
    """Persist the per-chain outcome and render it into the CI job summary.

    The old setup printed to stdout on a host nobody watched, so a chain that returned
    0 products looked exactly like a chain that worked. Now every run leaves a table
    behind, in the log and in the GitHub run summary.
    """
    with io.open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)

    lines = [
        "| Chain | Files | Parsed | Upserted | Seconds | Status |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in results:
        status = r["error"] or ("OK" if r["upserted"] else "no products")
        lines.append(
            f"| {r['label']} | {r['files_downloaded']} | {r['parsed']} "
            f"| {r['upserted']} | {r.get('seconds', 0)} | {status} |"
        )
    # The knob that decides catalogue coverage vs. run time — record what produced
    # these numbers, otherwise the table cannot be compared between runs.
    lines.append("")
    lines.append(f"`FILES_PER_CHAIN={FILES_PER_CHAIN}`")
    table = "\n".join(lines)
    print("\n" + table)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with io.open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## Price sync\n\n" + table + "\n")


def main():
    sb = get_supabase()
    results = []

    for chain in chains_to_sync():
        label = CHAIN_LABELS[chain]
        print(f"\n=== {label} ({chain}) ===")
        started = time.time()
        entry = {
            "chain": chain, "label": label, "files_downloaded": 0,
            "files_matched": 0, "parsed": 0, "upserted": 0,
            "sample_filenames": [], "error": None,
        }

        with tempfile.TemporaryDirectory() as tmp:
            try:
                download_chain(chain, tmp)
                rows, stats = collect_rows(chain, tmp)
                entry.update(stats)
                entry["parsed"] = len(rows)
                print(f"  downloaded {stats['files_downloaded']} files "
                      f"({stats['files_matched']} matched), parsed {len(rows)} products")
                if rows:
                    entry["upserted"] = upsert_rows(sb, rows)
                    print(f"  upserted {entry['upserted']} rows")
                else:
                    # Not fatal on its own — the other chains still have to run — but it
                    # has to show up in the run's annotations instead of scrolling past.
                    print(f"::warning title=No products::{label} ({chain}) parsed 0 products "
                          f"from {stats['files_downloaded']} downloaded files")
            except Exception as e:  # noqa: BLE001 — one chain must not kill the rest
                entry["error"] = f"{type(e).__name__}: {e}"
                print(f"::warning title=Chain failed::{label} ({chain}): {entry['error']}")

        entry["seconds"] = round(time.time() - started, 1)
        results.append(entry)

    write_report(results)

    ok = [r for r in results if r["upserted"] > 0]
    total = sum(r["upserted"] for r in results)
    print(f"\nDone. {len(ok)}/{len(results)} chains synced, {total} rows upserted.")

    if not ok:
        # Hard failure: red run + a GitHub notification. This is the exact case that
        # rotted silently for 71 days, so it must never exit 0 again.
        print("::error title=Price sync failed::No chain produced any rows.")
        sys.exit(1)


if __name__ == "__main__":
    main()
