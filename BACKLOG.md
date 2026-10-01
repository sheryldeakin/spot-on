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
  Trigger: a subject with headroom. One trial cleared both findings, held them for
  0.3 of fidelity, and stopped climbing in the same run. One page, one run.
- **Instrument both selection paths** before trialling that again. Size: small.
  Trigger: doing the above. The flips counter watches `choose_attempt` only, and the
  mechanism acts through `iteration_base`.
- **Turn "declined N times" into "verify this instruction".** Size: medium. Trigger:
  any report item surviving many rounds again. A finding was false for twenty rounds
  while three features were built to make the loop obey it; the loop was right from
  the first refusal. The count exists and nothing reads it as evidence.

## Deliberate limits

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
