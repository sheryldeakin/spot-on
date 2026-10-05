# Backlog

Deferred work and deliberate limits. An entry carries a size and a trigger: what
would make it worth doing. An entry with no trigger is one to question rather than
schedule. Finishing an item deletes its line in the commit that does the work.

## Deferred

- **A glyph family, named as a family.** Size: small. Trigger: a design where naming
  would help and the variants defeat it. The matcher can tell map-pin from heart and
  cannot tell `map-pin-plus-inside` from `map-pin-x-inside`: on one real well those
  scored 0.94 and 0.93. The margin gate now stays silent there, which is right and
  wastes a true observation. Saying "a map pin, variant unclear" would keep it.
- **`blocked` goes unused.** Size: small. Trigger: a second run where something
  genuinely cannot be done. On first real use the rounds filed "Install or identify
  the design's typeface" under work-to-do and left `blocked` empty, so the one
  impossible thing was not marked impossible. Either the description needs to be
  plainer or the distinction is not one the model makes.
- **Lexical dedup cannot catch paraphrases.** Size: medium. Trigger: a worklist
  filling with restatements again. "items 14 and 15" and "the 10-item and 4-item row
  pitch" are one job with no words in common. The schema now asks rounds to reuse an
  earlier wording, which is unmeasured.
- **The findings tie-break is off by default** (`SPOT_ON_REPAIR`). Size: medium.
  Trigger: ready to run. Both selection paths now record what they did
  (`selection` and `base_selection` on each attempt), so a trial can ask the data
  whether the mechanism fired. A survey of what is already on disk
  (`scripts/tiebreak_survey.py`) says it is not rare: across 136 rounds with more
  than one candidate it could act on 44 and would change the pick on 22. Subjects
  with both headroom and ties are `bl-with` (74.2, changes 2 of 3 chances) and
  `cmarix` (79.6); the 96-point runs tie constantly but have nowhere to climb.
  What is still unknown is whether the changed pick ends the run higher, which
  needs two arms on one seed.

## Guards that exist because a written rule did not hold

- **Stray control characters are a test, not a warning.** `tests/test_docs.py` scans
  every tracked text file. A shell heredoc collapses a doubled backslash, so a patch
  written that way puts a real backspace where the source said `\b`; it parses, it
  runs, and the regex quietly stops matching. Three times in one session, in a repo
  whose CLAUDE.md warns about it. The silent class is exactly `\a \b \f \v \0`;
  the rest either survive or fail loudly.
- **A round is run through `scripts/round.py`, which proves it happened.** It records
  the attempts before, and exits non-zero unless a new one exists after. A round was
  once dispatched in a way that died on startup, the wrapper exited 0, and it was
  reported as having run; the newest attempt file was two hours old.

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
