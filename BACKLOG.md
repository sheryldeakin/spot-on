# Backlog

Deferred work and deliberate limits. An entry carries a size and a trigger: what
would make it worth doing. An entry with no trigger is one to question rather than
schedule. Finishing an item deletes its line in the commit that does the work.

## Deferred

- **Lexical dedup cannot catch paraphrases.** Size: medium. Trigger: a worklist
  filling with restatements again. "items 14 and 15" and "the 10-item and 4-item row
  pitch" are one job with no words in common. The schema now asks rounds to reuse an
  earlier wording, which is unmeasured.
- **The findings tie-break stays off, and one trial did not settle it.** Size:
  medium. Trigger: enough appetite to run several seeds per arm. Two arms on
  hud-concept attempt 1, four rounds, three candidates, 24 minutes
  (`scripts/ab_trial.py`, output in `scripts/ab-tb.json`). The mechanism fires
  reliably when it can, 2 of 2 chances, and costs almost nothing in fidelity, 0.1
  and 0.2. The arm with it on ended 1.5 lower, 72.0 against 73.5, and that number
  cannot be attributed to it: the arms had already diverged by 0.7 in round one,
  where the mechanism could not act at all, and one run per arm says nothing about
  model noise. What a real answer needs is several seeds per arm, which is hours of
  model time, so the honest position is that it is cheap, it works as designed, and
  nobody knows whether it helps.

- **Whether narrowing `blocked` worked is unmeasured.** Size: small. Trigger: the
  next run where a command is refused or a library will not load. Its examples used
  to be the typeface and the photographs, which the needs list now owns, so a round
  reasonably filed those as ordinary work and left the field empty. It now asks only
  about what stopped that round. Nobody has run a round against the new wording.

## Deliberate limits

- **A dispute needs two rounds to agree before it is repeated to anyone.** One round
  saying a fault is not there is an opinion, and models are agreeable enough that
  asking the question invites a yes. Two independent rounds is the gate; it has not
  been tuned against real disputes because there are none yet.
- **A disputed fault is still measured and still reported.** Only the pressing stops.
  The rounds can be wrong together, and the person has the last word.

- **An in-set icon at 24px with a thick stroke can be called absent.** Measured: at
  32px a set drawing scores at least 0.80 against itself 98.7% to 100% of the time,
  but across the 24px arms that floor falls to 68.4%. The verdict is kept at 24px
  anyway, because the cost of the error is low: being told to draw inline when the
  set did have it loses nothing measurable, while a substitute icon that was not the
  design's was measured as worth nothing at all.
- **A name is never printed for a plain shape.** "The glyph here is circle" is true,
  useless, and crowds out the line that matters. `ICON_PLAIN` holds the list.
- **`assets/icons/templates.npz` is committed, not built on demand.** It needs a
  browser and about half a minute. Rebuild with `scripts/build_icon_templates.py`
  when the icon set or `ICON_NORM` changes. `glyph_identity` returns no opinion when
  the file is missing, so a clone without it degrades rather than breaks.
- **Re-measuring the stability mask is not implemented.** It governs which pixels are
  excluded from every score of its run, and there is no running page here to test a
  change against.
- **"Given up" does not expire.** Unchanged on purpose: pressing was never the binding
  constraint.
- **hud-concept is converged at 73.9** and is a test subject, not a target. About half
  that design is artwork; the remaining headroom is the globe and the photographic
  lower half.
