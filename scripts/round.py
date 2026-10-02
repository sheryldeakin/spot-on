"""Run one round against a run, and prove it happened.

    python scripts/round.py <run-slug> [--candidates N] [--note "..."]

Exists because of a specific failure rather than for convenience. A round was
dispatched in a way that died on startup, the wrapper around it exited 0, and the
result was reported as a round that had run. Nothing was wrong with the tool: the
mistake was reading an exit code as evidence that work happened. It went unnoticed
until the newest attempt file turned out to be two hours old.

So this records which attempts exist before it starts and refuses to exit 0 unless a
new one is there afterwards. "Did it run" stops being a judgement call.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:7265"


def attempts(slug):
    d = ROOT / "runs" / slug / "attempts"
    return set(p.name for p in d.glob("*.json")) if d.is_dir() else set()


def post(path, body, timeout):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("--candidates", type=int, default=2)
    ap.add_argument("--note", default="")
    ap.add_argument("--timeout", type=int, default=3000)
    args = ap.parse_args()

    before = attempts(args.slug)
    if not before:
        print("no attempts yet for {}: render a first one before iterating".format(args.slug))
        return 2
    print("{} attempts before".format(len(before)), flush=True)

    start = time.time()
    try:
        got = post("/iterate", {"run": args.slug, "candidates": args.candidates,
                                "instructions": args.note}, args.timeout)
    except urllib.error.URLError as e:
        print("the server did not answer ({}). Is spot-on.py running?".format(e))
        return 1
    took = time.time() - start

    after = attempts(args.slug)
    new = sorted(after - before)
    if not new:
        # The whole point of this script.
        print("FAILED: the call returned after {:.0f}s and no new attempt exists. "
              "Nothing ran.".format(took))
        return 1

    print("ran {:.0f}s, {} new attempt{}: {}".format(
        took, len(new), "" if len(new) == 1 else "s", ", ".join(new)))
    for field in ("n", "match", "changes", "disputed", "blocked", "next", "icons_used"):
        if field in got:
            print("  {}: {}".format(field, got[field]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
