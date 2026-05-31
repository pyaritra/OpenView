#!/usr/bin/env python3
"""
Rolling global sweep of Mapillary street-level coverage (concurrent + time-capped).

Each run walks a slice of the globe, recording WHERE coverage exists, how recent
it is, and whether it's 360 - never the images. A cursor remembers progress so
runs resume where the last stopped; a full global pass spans ~2-3 weeks of daily
runs, then repeats.

Stops at the first of: tile budget (SWEEP_BUDGET, < Mapillary's 50k/day) or wall
clock (SWEEP_MAX_SECONDS). Saves the cursor after every chunk, so a cut-short run
still makes progress. Aborts immediately with a clear message if the token is bad.

OUTPUT (under SWEEP_OUT, default ./archive)
  cursor.json                      {cursor, pass, pass_started}
  state/<BASE_Z>_<bx>_<by>.json    { "Z/X/Y": [seq_count, newest_day, pano_count] }
  changes/<YYYY-MM-DD>.jsonl       {"t":"Z/X/Y","old":[...]|null,"new":[...]|null}

TUNING (env vars)
  SWEEP_TARGET_Z   default 11   finer = more detail, longer pass
  SWEEP_BASE_Z     default 7    coarse enumeration level
  SWEEP_BUDGET     default 45000 max tile fetches per run
  SWEEP_MAX_SECONDS default 1500 wall-clock cap per run (seconds)
  SWEEP_WORKERS    default 24   concurrent fetches
"""
import os, sys, json, time, glob, datetime, threading, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import mapbox_vector_tile
except ImportError:
    sys.exit("Missing dependency. Run: pip install mapbox-vector-tile")

TOKEN       = os.environ.get("MAPILLARY_TOKEN", "").strip()
BASE_Z      = int(os.environ.get("SWEEP_BASE_Z", "7"))
TARGET_Z    = int(os.environ.get("SWEEP_TARGET_Z", "11"))
BUDGET      = int(os.environ.get("SWEEP_BUDGET", "45000"))
MAX_SECONDS = int(os.environ.get("SWEEP_MAX_SECONDS", "1500"))
WORKERS     = int(os.environ.get("SWEEP_WORKERS", "24"))
CHUNK       = int(os.environ.get("SWEEP_CHUNK", "48"))
OUTDIR      = os.environ.get("SWEEP_OUT", "archive")
URL = "https://tiles.mapillary.com/maps/vtp/mly1_public/2/{z}/{x}/{y}?access_token={t}"

_lock = threading.Lock()
fetched = 0
_stop = False
_auth_failed = False


class AuthError(Exception):
    pass


class BudgetReached(Exception):
    pass


def today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def fetch_tile(z, x, y):
    """Return raw .pbf bytes or None (empty). Raises AuthError / BudgetReached to halt."""
    global fetched, _stop, _auth_failed
    with _lock:
        if _auth_failed:
            raise AuthError()
        if _stop or fetched >= BUDGET:
            _stop = True
            raise BudgetReached()
        fetched += 1
    try:
        with urllib.request.urlopen(URL.format(z=z, x=x, y=y, t=TOKEN), timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            with _lock:
                _auth_failed = True
            raise AuthError("Mapillary rejected the token (HTTP {}). Check the MAPILLARY_TOKEN secret.".format(e.code))
        if e.code == 429:        # daily rate limit hit -> stop cleanly, resume next run
            with _lock:
                _stop = True
            raise BudgetReached()
        return None              # 204/404/etc -> empty
    except (AuthError, BudgetReached):
        raise
    except Exception:
        return None              # transient network error -> treat as empty


def decode(raw):
    if not raw:
        return None
    try:
        return mapbox_vector_tile.decode(raw)
    except Exception:
        return None


def has_coverage(raw):
    d = decode(raw)
    return bool(d and d.get("sequence") and d["sequence"].get("features"))


def target_stats(raw):
    d = decode(raw)
    feats = (d.get("sequence") or {}).get("features", []) if d else []
    if not feats:
        return None
    newest = panos = 0
    for f in feats:
        p = f.get("properties", {}) or {}
        ca = p.get("captured_at") or 0
        if ca > newest:
            newest = ca
        if p.get("is_pano"):
            panos += 1
    return [len(feats), int(newest // 86400000), panos]


def descend(z, x, y, out):
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


def process_base(bx, by):
    out = {}
    descend(BASE_Z, bx, by, out)
    return bx, by, out


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
    for k, v in cur.items():
        if k not in prev:
            yield {"t": k, "old": None, "new": v}
        elif prev[k] != v:
            yield {"t": k, "old": prev[k], "new": v}
    for k, v in prev.items():
        if k not in cur:
            yield {"t": k, "old": v, "new": None}


def persist(bx, by, out, changes):
    sp = state_path(bx, by)
    prev = json.load(open(sp)) if os.path.exists(sp) else {}
    changes.extend(diff(prev, out))
    if out:
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        json.dump(out, open(sp, "w"), separators=(",", ":"))
    elif os.path.exists(sp):
        os.remove(sp)


def probe():
    """Validate the token before the big loop (Berlin z14 tile). Aborts on auth error."""
    raw = fetch_tile(14, 8802, 5382)
    print("auth probe ok ({})".format("coverage seen" if has_coverage(raw) else "probe tile empty, fine"))


def run():
    n = 1 << BASE_Z
    total = n * n
    s = load_cursor()
    i = s["cursor"]
    date = today()
    start = time.time()
    changes, done = [], 0

    probe()  # raises AuthError -> exits below

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        while i < total:
            if fetched >= BUDGET or (time.time() - start) > MAX_SECONDS:
                break
            chunk = [(j % n, j // n) for j in range(i, min(i + CHUNK, total))]
            futs = {ex.submit(process_base, bx, by): (bx, by) for bx, by in chunk}
            results, partial = [], False
            try:
                for f in as_completed(futs):
                    results.append(f.result())
            except BudgetReached:
                partial = True
                for f in futs:
                    if f.done() and not f.exception():
                        results.append(f.result())
            for bx, by, out in results:
                persist(bx, by, out, changes)
            i = min(i + CHUNK, total)
            s["cursor"] = i
            save_cursor(s)
            done += len(results)
            if partial:
                break

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

    print("date={} base_done={} fetched={} secs={:.0f} changes={} cursor={}/{} pass={} wrapped={}".format(
        date, done, fetched, time.time() - start, len(changes), i, total, s["pass"], wrapped))


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("Set MAPILLARY_TOKEN (your MLY|... client token) in the environment.")
    try:
        run()
    except AuthError as e:
        sys.exit("AUTH ERROR: {}".format(e))
