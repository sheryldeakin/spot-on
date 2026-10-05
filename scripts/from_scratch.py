"""Does a run started from nothing get everything, with nobody helping it?

Everything added on 2026-10-04 and 05 was checked against runs that already existed:
a stored report, a folder with history, a design the tool had already captured. That
is the weaker test. One of these features had already failed the stronger one without
anybody noticing: the colour finding computed correctly and reached no round at all
until SCORER_VERSION moved, because a prompt reads the STORED report rather than
recomputing it.

So this starts from nothing. A design the tool has never seen, rendered here. A new
run. Attempts that are wrong in the specific ways the new findings exist to catch.
The round prompts are built by run_iteration itself, not by this script, so what is
checked is the thing a model would actually be handed.

Four things are looked for, and they arrive at two different times, which is the
point of running past the first round:

  round 1   the needs list, a glyph verdict, a colour line
  later     the dispute question, which waits for a fault to have been pressed
            GIVE_UP_AFTER rounds, and then the dispute itself being heard

Nothing here bumps a version, rescores an attempt or edits a stored report.

    python scripts/from_scratch.py          # writes scripts/from-scratch.json
"""
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("spot_on", ROOT / "spot-on.py")
so = importlib.util.module_from_spec(spec)
sys.modules["spot_on"] = so
spec.loader.exec_module(so)

W, H = 560, 360

# A mark no icon set has, because that is the point: the glyph verdict is only worth
# printing when it can say the set does not have this one. Two nested arcs on a bar.
MARK = (
    '<svg viewBox="0 0 48 48" width="40" height="40">'
    '<path d="M8 34 Q24 6 40 34" fill="none" stroke="{ink}" stroke-width="5"/>'
    '<path d="M15 34 Q24 18 33 34" fill="none" stroke="{ink}" stroke-width="5"/>'
    '<rect x="6" y="36" width="36" height="5" fill="{ink}"/>'
    '</svg>'
)

ART = (
    '<div style="height:150px;border-radius:10px;'
    'background:linear-gradient(135deg,#312E81 0%,#9333EA 45%,#F97316 100%)"></div>'
)
FLAT = '<div style="height:150px;border-radius:10px;background:#6B7280"></div>'


def page(mark, art, heading_ink, body="", body_pad=14, card_pad=18):
    """One page. The parts that vary are the mark in the well, the colour of the
    heading, the body line, the artwork block and two spacings."""
    return (
        '<div style="width:{W}px;height:{H}px;background:#E2E8F0;font-family:Arial">'
        '<div style="margin:22px;background:#FFFFFF;border-radius:12px;padding:{card}px">'
        '<div style="display:flex;align-items:center;gap:14px">'
        '<div style="width:48px;height:48px;border-radius:10px;border:2px solid #CBD5E1;'
        'display:flex;align-items:center;justify-content:center">{mark}</div>'
        '<div style="color:{heading_ink};font-size:23px;font-weight:700">Workspace billing</div>'
        '</div>'
        '<div style="color:#475569;font-size:14px;margin-top:{pad}px;min-height:18px">'
        '{body}</div>'
        '<div style="margin-top:12px">{art}</div>'
        '</div></div>'
    ).format(W=W, H=H, mark=mark, art=art, heading_ink=heading_ink,
             body=body, pad=body_pad, card=card_pad)


BODY = "Review the plan and the seats your team is paying for this month."
DESIGN = page(MARK.format(ink="#0F766E"), ART, "#B45309", BODY)

# One thing closes per step, so every round is a real improvement and the climb that
# stuck_problems reads goes on growing. The two faults under test are not in this
# list: the well stays empty and the heading stays the wrong hue for every step, so
# they survive every round and are what accumulates.
DIM = ART.replace("#312E81", "#4C4A96").replace("#9333EA", "#A063C0").replace(
    "#F97316", "#D98A4A")
NEAR = ART.replace("#312E81", "#3A3690").replace("#F97316", "#EF8020")

STEPS = [
    dict(art=FLAT, body="", pad=34, card=34),
    dict(art=FLAT, body="", pad=34, card=26),
    dict(art=FLAT, body=BODY, pad=34, card=26),
    dict(art=FLAT, body=BODY, pad=24, card=18),
    dict(art=DIM, body=BODY, pad=24, card=18),
    dict(art=NEAR, body=BODY, pad=24, card=18),
    dict(art=ART, body=BODY, pad=18, card=18),
    dict(art=ART, body=BODY, pad=14, card=18),
]


def attempt(step):
    """Wrong in exactly the ways the new findings exist to name: an empty well where
    the design draws a mark, and a heading at about the same darkness in a different
    hue. Everything else closes as the steps go on, so nothing else can be what a
    surviving finding is about."""
    s = STEPS[min(step, len(STEPS) - 1)]
    return page("", s["art"], "#1D4ED8", s["body"], s["pad"], s["card"])

PHRASES = {
    "needs section": "known to need something that is not here",
    "glyph verdict": "nothing in the set is this shape",
    "colour line": "is the wrong colour: the design draws it",
    "dispute question": "if the report is wrong about it, put it in",
    "dispute heard": "said the report is wrong about them",
}


def flat(text):
    """One space between words, so a phrase cannot miss on a line break.

    The first version searched the prompt as written. The prompt wraps between
    "put it in" and the field name, so the dispute phrase could never match and
    nine rounds were reported as never asking the question. The mechanism was
    fine; the instrument was not.
    """
    return " ".join((text or "").lower().split())


def stale_phrases():
    """The phrases above that are no longer anywhere in the tool's source.

    The control the first version lacked. A phrase that never turns up has two
    possible causes needing opposite work: the feature really is missing, or the
    phrase here has gone stale and the probe is lying about the tool. Searching
    the source separates them, so a never-seen phrase can no longer be read as a
    finding without the script saying which kind it is.
    """
    src = flat((ROOT / "spot-on.py").read_text(encoding="utf-8"))
    return [name for name, phrase in PHRASES.items() if flat(phrase) not in src]


def seen(prompt):
    said = flat(prompt)
    return {k: flat(v) in said for k, v in PHRASES.items()}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="from-scratch-"))
    saved_runs, saved_agent = so.RUNS_DIR, so.run_agent
    so.RUNS_DIR = tmp / "runs"
    try:
        so.render_code(DESIGN, "html", W, H, tmp / "design.png").save(tmp / "design.png")
        run = so.create_run("from scratch", "html", reference_path=tmp / "design.png")
        slug = run["slug"]

        first = so.record_attempt(slug, attempt(0))
        report = first["report"]
        els = report.get("elements") or {}

        prompts, disputing, step = [], [False], [0]

        def fake(agent, prompt, cwd, images, schema=True):
            if prompt.startswith(so.NEEDS_PROMPT[:40]):
                # The one-off "what does this design need" call is not a round and
                # must not advance the step, or a round is skipped silently.
                return json.dumps({"result": "A card with an icon well and a heading."})
            prompts.append(prompt)
            step[0] += 1
            said = {"code": attempt(step[0]),
                    "changes": "closed one more of the differences"}
            if disputing[0]:
                # What a round actually writes: the report's own sentence, with its
                # coordinates, then what the round says it can see instead.
                said["disputed"] = [
                    "The element at x 36, y 39 is the right size in the right place but "
                    "empty: there is no icon at that position in the design, the box is "
                    "the whole of what is drawn there."]
            return json.dumps({"structured_output": said})

        so.run_agent = fake

        rounds = []
        for i in range(9):
            so.run_iteration(slug, candidates=1)
            prompt = next(p for p in reversed(prompts)
                          if not p.startswith(so.NEEDS_PROMPT[:40]))
            got = seen(prompt)
            hist = so._attempts(slug)
            base, _ = so.iteration_base(hist)
            carried = so.stuck_problems(hist, base)
            rounds.append({"round": i + 1, "seen": got,
                           "match": round(_best(slug), 1),
                           "base": base["n"], "attempts": len(hist),
                           "stuck": [[x["key"], x["rounds"]] for x in carried]})
            print("round {}: {}".format(
                i + 1, "  ".join("{} {}".format("+" if v else "-", k)
                                 for k, v in got.items())), flush=True)
            # Start disputing as soon as the round is asked the question, which is
            # how the mechanism is meant to be reached.
            if got["dispute question"]:
                disputing[0] = True

        out = {
            "scorer_version": so.SCORER_VERSION,
            "first_match": round(report["match"], 1),
            "first_problems": report.get("problems") or [],
            "hollow": len(els.get("hollow") or []),
            "colour_findings": len(els.get("colour") or []),
            "needs": [n["title"] for n in so.needs_from_you(report)],
            "rounds": rounds,
            "first_seen": {k: next((r["round"] for r in rounds if r["seen"][k]), None)
                           for k in PHRASES},
            "disputes_recorded": so.run_disputes(slug),
        }
        path = ROOT / "scripts" / "from-scratch.json"
        path.write_text(json.dumps(out, indent=1), encoding="utf-8")

        print()
        print("first attempt scored {:.1f} on scorer {}, needs: {}".format(
            report["match"], so.SCORER_VERSION, ", ".join(out["needs"]) or "(none)"))
        print()
        stale = stale_phrases()
        for k in PHRASES:
            r = out["first_seen"][k]
            print("   {:<18} {}".format(
                k, "round {}".format(r) if r else
                "NEVER, and this probe's phrase is stale" if k in stale else "NEVER"))
        print()
        missing = [k for k in PHRASES if out["first_seen"][k] is None]
        if stale:
            print("STOP. {} of these phrases are not in spot-on.py at all: {}. That is"
                  .format(len(stale), ", ".join(stale)))
            print("this script being out of date, not the tool losing a feature. Fix the")
            print("phrase against the source before reading anything else here.")
        elif missing:
            print("{} never reached a round, and every phrase is still in the source, so"
                  .format(", ".join(missing)))
            print("this is the tool and not the probe.")
        print("wrote", path)
    finally:
        so.RUNS_DIR, so.run_agent = saved_runs, saved_agent
        shutil.rmtree(tmp, ignore_errors=True)


def _best(slug):
    return so._load_run(slug).get("best_match", -1)


if __name__ == "__main__":
    main()
