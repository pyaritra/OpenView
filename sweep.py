#!/usr/bin/env python3
"""
Rolling global sweep of Mapillary street-level coverage.

WHAT IT DOES
  Walks the world tile-by-tile a little each day, recording WHERE imagery
  exists, how recent it is, and whether it's 360 - never the images
  themselves. Over repeated daily runs it completes a full global pass,
  then starts the next one, so you build up a time series of coverage.

  It is resumable: a cursor file remembers how far the current pass got,
  so each run picks up where the last stopped and no run exceeds the
  Mapillary tile budget (50k requests/day per app).

HOW IT STAYS CHEAP
  It enumerates the globe at a coarse zoom (BASE_Z) and only descends to
  the fine zoom (TARGET_Z) inside tiles that actually contain coverage -
  empty ocean / wilderness is skipped after a single cheap probe.

OUTPUT (all under OUTDIR, committed to the repo by the workflow)
  cursor.json                      {cursor, pass, pass_started}
  state/<BASE_Z>_<bx>_<by>.json    latest coverage under that base tile:
                                     { "Z/X/Y": [seq_count, newest_day, pano_count], ... }
  changes/<YYYY-MM-DD>.jsonl       one row per detected change:
                                     {"t":"Z/X/Y","old":[...]|null,"new":[...]|null}
                                     old=null -> newly appeared
                                     new=null -> coverage removed

  This is the ARCHIVE plane only. The website still opens imagery from the
  LIVE Mapillary plane on click, so click-to-view never goes stale.

TUNING (env vars, no code edits needed)
  SWEEP_TARGET_Z  finer = more detail but a longer full pass
                    z10 ~ town (fast)   z11 ~ district (default)   z12+ ~ street (slow)
  SWEEP_BASE_Z    coarse enumeration level (default 7)
  SWEEP_BUDGET    max tile fetches per run (default 45000, under the 50k cap)
"""
import os, sys, json, datetime, urllib.request, urllib.error

try:
    import mapbox_vector_tile
except ImportError:
    sys.exit("Missing dependency. Run: pip install mapbox-vector-tile")

TOKEN     = os.environ.get("MAPILLARY_TOKEN", "")
BASE_Z    = int(os.environ.get("SWEEP_BASE_Z", "7"))
TARGET_Z  = int(os.environ.get("SWEEP_TARGET_Z", "11"))
BUDGET    = int(os.environ.get("SWEEP_BUDGET", "45000"))
OUTDIR    = os.environ.get("SWEEP_OUT", "archive")
URL       = "https://tiles.mapillary.com/maps/vtp/mly1_public/2/{z}/{x}/{y}?access_token={t}"

fetched = 0  # tiles requested this run


class BudgetReached(Exception):
    pass


def today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def fetch_tile(z, x, y):
    """Return raw .pbf bytes, or None for empty/missing. Raises BudgetReached at the cap."""
    global fetched
    if fetched >= BUDGET:
        raise BudgetReached()
    fetched += 1
    url = URL.format(z=z, x=x, y=y, t=TOKEN)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:           # rate limited -> stop cleanly, resume next run
            raise BudgetReached()
        return None                 # 204/404/etc -> treat as empty
    except Exception:
        return None


def decode(raw):
    if not raw:
        return None
    try:
        return mapbox_vector_tile.decode(raw)
    except Exception:
        return None


def has_coverage(raw):
    d = decode(raw)
    if not d:
        return False
    seq = d.get("sequence")
    return bool(seq and seq.get("features"))


def target_stats(raw):
    """At TARGET_Z summarise the sequence layer -> [count, newest_day, pano_count]."""
    d = decode(raw)
    feats = (d.get("sequence") or {}).get("features", []) if d else []
    if not feats:
        return None
    newest, panos = 0, 0
    for f in feats:
        p = f.get("properties", {}) or {}
        ca = p.get("captured_at") or 0
        if ca > newest:
            newest = ca
        if p.get("is_pano"):
            panos += 1
    return [len(feats), int(newest // 86400000), panos]


def descend(z, x, y, out):
    """Fetch (z,x,y); if it has coverage, recurse into children down to TARGET_Z."""
    raw = fetch_tile(z, x, y)
    if not has_coverage(raw):
        return
    if z >= TARGET_Z:
        st = target_stats(raw)
        if st:
            out["{}/{}/{}".format(z, x, y)] = st
        return
    for dx in (0, 1):
        for dy in (0, 1):
            descend(z + 1, 2 * x + dx, 2 * y + dy, out)


def state_path(bx, by):
    return os.path.join(OUTDIR, "state", "{}_{}_{}.json".format(BASE_Z, bx, by))


def load_cursor():
    p = os.path.join(OUTDIR, "cursor.json")
    if os.path.exists(p):
        return json.load(open(p))
    return {"cursor": 0, "pass": 1, "pass_started": today()}


def save_cursor(s):
    os.makedirs(OUTDIR, exist_ok=True)
    json.dump(s, open(os.path.join(OUTDIR, "cursor.json"), "w"))


def diff(prev, cur):
    """Yield change rows between two {tile: stats} dicts."""
    for k, v in cur.items():
        if k not in prev:
            yield {"t": k, "old": None, "new": v}
        elif prev[k] != v:
            yield {"t": k, "old": prev[k], "new": v}
    for k, v in prev.items():
        if k not in cur:
            yield {"t": k, "old": v, "new": None}


def run():
    n = 1 << BASE_Z
    total = n * n
    s = load_cursor()
    i = s["cursor"]
    date = today()
    changes, processed = [], 0

    while i < total:
        if fetched >= BUDGET:
            break
        bx, by = i % n, i // n
        out = {}
        try:
            descend(BASE_Z, bx, by, out)
        except BudgetReached:
            break  # leave cursor on this tile so we resume it next run

        sp = state_path(bx, by)
        prev = json.load(open(sp)) if os.path.exists(sp) else {}
        changes.extend(diff(prev, out))
        if out:
            os.makedirs(os.path.dirname(sp), exist_ok=True)
            json.dump(out, open(sp, "w"), separators=(",", ":"))
        elif os.path.exists(sp):
            os.remove(sp)

        i += 1
        processed += 1

    wrapped = False
    if i >= total:
        i, s["pass"], s["pass_started"], wrapped = 0, s["pass"] + 1, date, True
    s["cursor"] = i
    save_cursor(s)

    if changes:
        cp = os.path.join(OUTDIR, "changes", "{}.jsonl".format(date))
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        with open(cp, "a") as fh:
            for c in changes:
                fh.write(json.dumps(c, separators=(",", ":")) + "\n")

    print("date={} base_tiles_done={} fetched={} changes={} cursor={}/{} pass={} wrapped={}".format(
        date, processed, fetched, len(changes), i, total, s["pass"], wrapped))


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("Set MAPILLARY_TOKEN (your MLY|... client token) in the environment.")
    run()
