"""What is actually sitting in the open worklists, item by item.

The worry recorded in the backlog was paraphrase: two lines meaning one job with no
words in common, which lexical dedup cannot merge. Reading every open item on this
machine, that is real but it is not the biggest thing in there. Two other kinds of
item should never have been on the list at all:

  the loop's own job   "Render and score this attempt to check the panel move"
                       Every answer is rendered and scored the moment it is sent,
                       and the schema says so, so this is an item that completes
                       itself and can never be closed by a round.

  a material gap       "Install or identify the design's typeface"
                       The needs list owns this, with what it is worth and how to
                       hand it over. On the list it reads as ordinary work.

Both can be read off the line, the way a blocked line can. Paraphrase cannot, and
the remaining count is the honest size of that problem.

    python scripts/worklist_items.py      # writes scripts/worklist-items.json
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("spot_on", ROOT / "spot-on.py")
so = importlib.util.module_from_spec(spec)
sys.modules["spot_on"] = so
spec.loader.exec_module(so)


def main():
    runs = {}
    for path in sorted((ROOT / "runs").glob("*/attempts/*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        runs.setdefault(path.parent.parent.name, []).append(rec)

    # Classified from the RAW lines, not from worklist()'s output: the filters under
    # test run inside it, so reading its result would show every count as zero and
    # look like proof.
    rows, seen = [], set()
    for run, history in sorted(runs.items()):
        history.sort(key=lambda r: r.get("n", 0))
        closed = {so._trim_item(d).lower()
                  for rec in history for d in (rec.get("done") or [])}
        for rec in history:
            for raw in (rec.get("next") or []):
                text = so._trim_item(raw)
                key = (run, text.lower())
                if not text or key in seen or text.lower() in closed:
                    continue
                seen.add(key)
                rows.append({"run": run, "text": text,
                             "loop": bool(so._is_the_loops_job(text)),
                             "gap": bool(so._is_material_gap(text))})

    loop = [r for r in rows if r["loop"]]
    gap = [r for r in rows if r["gap"] and not r["loop"]]
    work = [r for r in rows if not r["loop"] and not r["gap"]]
    out = {"runs": len(set(r["run"] for r in rows)), "items": len(rows),
           "loops_own_job": len(loop), "material_gaps": len(gap),
           "real_work": len(work), "rows": rows}
    path = ROOT / "scripts" / "worklist-items.json"
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")

    for tag, group in (("the loop's own job", loop), ("a material gap", gap),
                       ("real work", work)):
        print("--- {} ({})".format(tag, len(group)))
        for r in group:
            print("   [{}] {}".format(r["run"], r["text"]))
        print()
    print("{} open items across {} runs: {} the loop's own job, {} material gaps, "
          "{} real work".format(out["items"], out["runs"], out["loops_own_job"],
                                out["material_gaps"], out["real_work"]))
    print("wrote", path)


if __name__ == "__main__":
    main()
