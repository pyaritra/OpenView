#!/usr/bin/env python3
"""
Aggregate the coverage archive into:
  stats/summary.json   headline numbers (also usable for a dynamic README badge)
  stats/coverage.png   a chart embedded on the repo homepage (README)

Run after sweep.py. Safe to run when the archive is empty (writes placeholders).
"""
import os, json, glob, datetime

ARCH = os.environ.get("SWEEP_OUT", "archive")
OUT = "stats"


def load_state():
    total = panos = seqs = 0
    years = {}
    for fp in glob.glob(os.path.join(ARCH, "state", "*.json")):
        try:
            d = json.load(open(fp))
        except Exception:
            continue
        for v in d.values():
            count, newest_day, pano = v[0], v[1], v[2]
            total += 1
            seqs += count
            if pano > 0:
                panos += 1
            yr = datetime.datetime.fromtimestamp(newest_day * 86400, datetime.timezone.utc).year
            years[yr] = years.get(yr, 0) + 1
    return total, seqs, panos, years


def load_changes(days=30):
    series = {}  # date -> [added, removed, updated]
    for fp in sorted(glob.glob(os.path.join(ARCH, "changes", "*.jsonl")))[-days:]:
        date = os.path.basename(fp)[:-6]
        a = r = u = 0
        for line in open(fp):
            try:
                c = json.loads(line)
            except Exception:
                continue
            if c.get("old") is None:
                a += 1
            elif c.get("new") is None:
                r += 1
            else:
                u += 1
        series[date] = [a, r, u]
    return series


def main():
    os.makedirs(OUT, exist_ok=True)
    total, seqs, panos, years = load_state()
    series = load_changes()
    summary = {
        "tiles_with_coverage": total,
        "sequences": seqs,
        "tiles_with_360": panos,
        "by_year": {str(k): years[k] for k in sorted(years)},
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    json.dump(summary, open(os.path.join(OUT, "summary.json"), "w"), indent=2)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; wrote summary.json only")
        return

    BG, INK, DIM, BLUE, AMBER, LINE = "#0e131c", "#e8eef6", "#8a97a8", "#39c2ff", "#ffb238", "#1f2937"
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    fig.patch.set_facecolor(BG)
    for ax in (ax1, ax2):
        ax.set_facecolor(BG)
        for s in ax.spines.values():
            s.set_color(LINE)
        ax.tick_params(colors=DIM, labelsize=8)
        ax.title.set_color(INK)

    if years:
        ys = sorted(years)
        ax1.bar([str(y) for y in ys], [years[y] for y in ys], color=BLUE)
    ax1.set_title("Tiles by newest capture year")
    ax1.tick_params(axis="x", rotation=45)

    if series:
        dates = list(series)
        added = [series[d][0] for d in dates]
        removed = [-series[d][1] for d in dates]
        ax2.bar(dates, added, color=BLUE, label="added")
        ax2.bar(dates, removed, color=AMBER, label="removed")
        leg = ax2.legend(facecolor=BG, edgecolor=LINE)
        for t in leg.get_texts():
            t.set_color(INK)
        ax2.tick_params(axis="x", rotation=45)
    ax2.set_title("Coverage changes per day")

    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "coverage.png"), dpi=120, facecolor=fig.get_facecolor())
    print("wrote stats/coverage.png and stats/summary.json:", summary)


if __name__ == "__main__":
    main()
