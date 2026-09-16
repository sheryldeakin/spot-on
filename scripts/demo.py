"""Regenerate docs/demo-run.txt: a pricing section iterated toward its design.

    python scripts/demo.py            # three rounds
    python scripts/demo.py --rounds 5
    python scripts/demo.py --rebuild  # rewrite the table from the recorded run, no model calls

docs/demo/design.html stands in for a design export and is screenshot to make
the design image. docs/demo/first-attempt.html is a plausible first pass. Each
round asks headless Claude Code for a better version, scores it, and appends a
line. The run lands in runs/pricing-demo, so the page shows it too.

The README quotes docs/demo-run.txt verbatim and tests/test_docs.py fails if the
two drift apart. Model output varies, so a rerun gives different numbers; the
README is updated from the new file with scripts/sync_readme.py, never by hand.
"""

import argparse
import importlib.util
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("spot_on", ROOT / "spot-on.py")
so = importlib.util.module_from_spec(spec)
spec.loader.exec_module(so)

RUN = "pricing demo"
W, H = 760, 500
HEADER = ("attempt  match  structure  shape  colour  detail  coverage  round tried     "
          "source   what changed")


def round_spread(rec, everything):
    """What the other rewrites in this round scored, best first.

    Only the winner of each round is listed in the table, so without this the
    README has no evidence for what taking the best of three actually buys. Each
    attempt records the attempt its round built on, which is enough to find its
    siblings again from the run on disk.
    """
    base = rec.get("candidate_of")
    if base is None:
        return "-"
    sibs = sorted((a["match"] for a in everything if a.get("candidate_of") == base),
                  reverse=True)
    return "/".join("{:.1f}".format(s) for s in sibs)


def format_line(rec, everything=()):
    c = rec["report"]["components"]
    note = " ".join((rec.get("changes") or "hand-written first pass").split())
    # The model's notes are quoted in the README, which uses no em or en dashes.
    note = note.replace(" \u2014 ", ", ").replace("\u2014", ", ").replace("\u2013", "-")
    return ("{:>7}  {:>5.1f}  {:>9.1f}  {:>5.1f}  {:>6.1f}  {:>6.1f}  {:>8.1f}  {:<14}  "
            "{:<7}  {}").format(
        rec["n"], rec["match"], c["structure"], c["shape"], c["colour"], c["detail"],
        c["coverage"], round_spread(rec, everything), rec["source"], note)


def write(lines):
    out = ROOT / "docs" / "demo-run.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def show(line):
    # The console may not be UTF-8; the file always is.
    print(line.encode("ascii", "replace").decode(), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--candidates", type=int, default=3,
                    help="rewrites per round; the best scoring one is kept (default 3)")
    ap.add_argument("--rebuild", action="store_true",
                    help="rewrite docs/demo-run.txt from runs/pricing-demo without calling a model")
    a = ap.parse_args()

    if a.rebuild:
        everything = so._attempts(so._slugify(RUN))
        best = {}
        for rec in everything:
            key = rec.get("candidate_of", 0)
            if key not in best or rec["match"] > best[key]["match"]:
                best[key] = rec
        kept = sorted(best.values(), key=lambda r: r["n"])
        write([HEADER] + [format_line(rec, everything) for rec in kept])
        return

    demo = ROOT / "docs" / "demo"
    tmp = Path(tempfile.mkdtemp(prefix="spot-on-demo-"))
    try:
        design_png = tmp / "design.png"
        so.render_code((demo / "design.html").read_text(encoding="utf-8"), "html", W, H, design_png)
        shutil.rmtree(so._run_dir(RUN), ignore_errors=True)
        so.create_run(RUN, "html", reference_path=design_png)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    lines = [HEADER]
    show(HEADER)

    def add(rec):
        lines.append(format_line(rec, so._attempts(so._slugify(RUN))))
        show(lines[-1])

    add(so.record_attempt(RUN, (demo / "first-attempt.html").read_text(encoding="utf-8")))
    for _ in range(a.rounds):
        try:
            add(so.run_iteration(so._slugify(RUN), candidates=a.candidates))
        except Exception as e:
            lines.append("stopped: {}".format(e))
            show(lines[-1])
            break
    write(lines)


if __name__ == "__main__":
    main()
