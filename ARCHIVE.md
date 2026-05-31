# Coverage archive (the sweep)

This is the **archive plane** — a slowly-built, dated record of *where* Mapillary
coverage exists, how recent it is, and whether it's 360. It exists only to power
the "how coverage changed over time" view. It never stores images.

Clicking a feature on the map always opens imagery from the **live** Mapillary
plane, never from this archive, so click-to-view can't go stale or conflict with
a snapshot.

## Setup

1. Put `sweep.py` at the repo root and `.github/workflows/sweep.yml` under
   `.github/workflows/`.
2. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**, name `MAPILLARY_TOKEN`, value your `MLY|...` client token.
3. The workflow runs daily and commits snapshots into `archive/`. You can also
   trigger it manually from the **Actions** tab to test.

## How it works

Each run walks the globe a little further, enumerating coarse tiles (`BASE_Z`)
and descending to the fine zoom (`TARGET_Z`) only where coverage actually exists
— empty ocean and wilderness are skipped after one cheap probe. A cursor file
remembers progress, so runs resume where the last left off and none exceeds
Mapillary's 50,000 tiles/day limit. When the cursor wraps, a new pass begins.

## Output (`archive/`)

```
cursor.json                      {cursor, pass, pass_started}
state/<BASE_Z>_<bx>_<by>.json    latest coverage under that coarse tile:
                                   { "Z/X/Y": [seq_count, newest_day, pano_count] }
changes/<YYYY-MM-DD>.jsonl       one row per detected change that day:
                                   {"t":"Z/X/Y","old":[...]|null,"new":[...]|null}
```

`newest_day` is days since the Unix epoch (compact; multiply by 86400000 for ms).
In a change row, `old:null` means coverage newly appeared, `new:null` means it
was removed. The `changes/` logs are the raw material for the time-over-time view.

## Tuning (env vars — no code edits)

| var | default | effect |
|-----|---------|--------|
| `SWEEP_TARGET_Z` | `11` | finer = more detail, longer full pass |
| `SWEEP_BASE_Z`   | `7`  | coarse enumeration level |
| `SWEEP_BUDGET`   | `45000` | max tile fetches per run (keep < 50000) |

Rough full-pass duration at 45k fetches/day:

- `TARGET_Z=10` (town-level): about a week.
- `TARGET_Z=11` (district-level, default): about two to three weeks.
- `TARGET_Z=12+` (true street-level): a month or more globally.

True street-level detail across the *entire* globe is not reachable in two weeks
under the 50k/day cap — that ceiling, not the method, is the limit. For a faster
or finer result you can raise the daily budget toward 50k, request a higher quota
from Mapillary, or scope the sweep to a region.

## Caveats

- **Scheduled workflows are disabled after 60 days of repo inactivity** (GitHub
  emails you first). Any commit/run re-arms it.
- **Completeness vs. cost**: skipping relies on coverage showing up at `BASE_Z`.
  Extremely sparse, isolated imagery that doesn't register at the coarse zoom can
  be missed. Lower `BASE_Z` for safety, raise it to spend less on enumeration.
- The token here is read-only; storing it as an Actions secret is cleaner than
  inlining it, even though the same token is also public in the site's client code.
