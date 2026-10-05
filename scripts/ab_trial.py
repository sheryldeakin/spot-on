"""Two arms of the loop on one seed, differing only in the thing under test.

Four trials of one mechanism were run before this existed and three could not have
shown anything: the subject's candidates were too far apart for the mechanism to
fire, or the seed carried none of the faults it existed to close, or a run that
errored still printed a table that read like a result. Each was rebuilt by hand. This
is the harness those should have used, and it enforces the three rules that would
have caught them:

  - it refuses to start unless the seed can show the effect
  - it reports whether the mechanism actually fired, not only the scores
  - a round that errors aborts the whole thing, so a trial that did not run
    cannot print a summary

    python scripts/ab_trial.py --source hud-concept --attempt 104 \\
        --name tb --env SPOT_ON_REPAIR --rounds 3 --candidates 3

Both arms are seeded from the same attempt's code against the same design, so the
only difference is the environment variable, set for arm B and unset for arm A.
"""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def tool(env=None):
    """Import the tool fresh, so a module-level setting is read under this env."""
    saved = dict(os.environ)
    try:
        if env:
            os.environ.update(env)
        spec = importlib.util.spec_from_file_location("spot_on_arm", ROOT / "spot-on.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(saved)


def seed(so, source, attempt, arm_slug):
    """A fresh run with the same design and the same starting code."""
    src = ROOT / "runs" / source
    dst = ROOT / "runs" / arm_slug
    shutil.rmtree(dst, ignore_errors=True)
    (dst / "attempts").mkdir(parents=True)
    shutil.copy(src / "reference.png", dst / "reference.png")
    for extra in ("captured-design.png", "icons.py"):
        if (src / extra).exists():
            shutil.copy(src / extra, dst / extra)
    run = json.loads((src / "run.json").read_text(encoding="utf-8"))
    run["slug"] = arm_slug
    run["name"] = arm_slug
    for drop in ("best_match", "best_attempt", "asks", "supplied"):
        run.pop(drop, None)
    (dst / "run.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    code = (src / "attempts" / "{:03d}.code".format(attempt)).read_text(encoding="utf-8")
    return so.record_attempt(arm_slug, code, source="seed",
                             changes="seeded from {} attempt {}".format(source, attempt))


def can_show_the_effect(so, source, attempt):
    """The precondition, stated and checked rather than assumed.

    For the findings tie-break the question is whether the source run has ever
    produced a round where two candidates sat within tolerance and differed in what
    they had closed. A subject that has never done that cannot show the mechanism
    doing anything, which is how two earlier trials were wasted.
    """
    by_round = {}
    for path in (ROOT / "runs" / source / "attempts").glob("*.json"):
        a = json.loads(path.read_text(encoding="utf-8"))
        a["n"] = int(path.stem)
        if a.get("candidate_of") is not None:
            by_round.setdefault(a["candidate_of"], []).append(a)
    chances = 0
    for cands in by_round.values():
        if len(cands) < 2:
            continue
        top = max(so.rank_of(c) for c in cands)
        band = [c for c in cands if so.rank_of(c) >= top - so.REPAIR_TOLERANCE]
        if len(band) >= 2 and len(set(round(so._penalty_of(c), 4) for c in band)) > 1:
            chances += 1
    return chances


def run_arm(name, source, attempt, env, rounds, candidates, log):
    """One arm, in its own process, so a module-level setting is really applied."""
    script = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('so', r'{tool}')\n"
        "so = importlib.util.module_from_spec(spec); sys.modules['so'] = so\n"
        "spec.loader.exec_module(so)\n"
        "out = []\n"
        "for i in range({rounds}):\n"
        "    got = so.run_iteration('{slug}', candidates={cands})\n"
        "    out.append({{'n': got['n'], 'match': got['match'],\n"
        "                 'selection': got.get('selection'),\n"
        "                 'base_selection': got.get('base_selection')}})\n"
        "    print('round', i + 1, 'attempt', got['n'], 'match', got['match'], flush=True)\n"
        "print('RESULT ' + json.dumps(out))\n"
    ).format(tool=ROOT / "spot-on.py", rounds=rounds, slug=name, cands=candidates)

    child = dict(os.environ)
    child.update(env)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, cwd=str(ROOT), env=child)
    log.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        # No summary from a trial that did not run.
        raise SystemExit("arm {} failed, see {}\n{}".format(
            name, log, proc.stderr[-900:]))
    line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT ")]
    if not line:
        raise SystemExit("arm {} produced no result line, see {}".format(name, log))
    return json.loads(line[-1][len("RESULT "):])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--attempt", type=int, required=True)
    ap.add_argument("--name", required=True, help="prefix for the two arm runs")
    ap.add_argument("--env", required=True, help="the variable arm B sets")
    ap.add_argument("--value", default="1")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--candidates", type=int, default=3)
    ap.add_argument("--force", action="store_true",
                    help="run even if the seed cannot show the effect")
    args = ap.parse_args()

    so = tool()
    chances = can_show_the_effect(so, args.source, args.attempt)
    print("the source run has produced {} rounds where the mechanism could act"
          .format(chances))
    if not chances and not args.force:
        raise SystemExit(
            "this seed has never produced a round the mechanism could act on, so the "
            "trial cannot observe it. Pick another subject, or pass --force and say "
            "in the write-up that the precondition was not met.")

    arms = [("{}-off".format(args.name), {args.env: ""}),
            ("{}-on".format(args.name), {args.env: args.value})]
    started = time.time()
    results = {}
    for arm_slug, env in arms:
        print("\n=== {}  ({}={!r})".format(arm_slug, args.env, env[args.env]), flush=True)
        first = seed(tool(env), args.source, args.attempt, arm_slug)
        print("seeded at {}".format(first["match"]), flush=True)
        log = ROOT / "scripts" / "ab-{}.log".format(arm_slug)
        results[arm_slug] = {"seed": first["match"],
                             "rounds": run_arm(arm_slug, args.source, args.attempt,
                                               env, args.rounds, args.candidates, log)}

    out = {"source": args.source, "attempt": args.attempt, "variable": args.env,
           "rounds": args.rounds, "candidates": args.candidates,
           "source_opportunities": chances,
           "minutes": round((time.time() - started) / 60.0, 1), "arms": results}

    print("\n{:<16} {:>7} {:>7} {:>7}  {}".format(
        "arm", "seed", "final", "gain", "mechanism"))
    for arm_slug, got in results.items():
        last = got["rounds"][-1]["match"] if got["rounds"] else got["seed"]
        fired = sum(1 for r in got["rounds"]
                    if (r.get("selection") or {}).get("acted")
                    or (r.get("base_selection") or {}).get("acted"))
        could = sum(1 for r in got["rounds"]
                    if (r.get("selection") or {}).get("could_have")
                    or (r.get("base_selection") or {}).get("could_have"))
        got["fired"] = fired
        got["could_have"] = could
        print("{:<16} {:>7.1f} {:>7.1f} {:>+7.1f}  fired {} of {} chances".format(
            arm_slug, got["seed"], last, last - got["seed"], fired, could))

    on = results["{}-on".format(args.name)]
    if on["fired"] == 0:
        print("\nThe mechanism never fired in the arm that had it on, so any "
              "difference above is not about it.")

    path = ROOT / "scripts" / "ab-{}.json".format(args.name)
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("\nwrote", path)


if __name__ == "__main__":
    main()
