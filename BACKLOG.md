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

- **Two of six `blocked` entries still name the material gaps.** Size: small.
  Trigger: another run that produces them. Eight real rounds under the new wording
  used the field six times: four correctly ("Write to attempt.html was denied",
  "Could not render or score the page this round") and twice with the old mistake
  ("Globe and cityscape artwork and the design's typeface are not available as
  assets"), which the needs list owns. From zero correct uses before, so the
  narrowing worked; it did not fully land.

- **One panel at the wrong alpha is still only "the palette is off".** Size: small.
  Trigger: a run where a single translucent panel is the fault. The other four of the
  five unnamed properties are closed; this one is left because the reason is narrow.
  `_fill_findings` wants two panels wrong the same way, deliberately, since one panel
  off is that panel and four off is the fill rule behind them. But the survey page has
  one card and it is not an element at all, being white on near-white, so nothing is
  measured and the page-wide palette line is all that fires: 12.3 points, and no
  sentence saying where. Border and shadow were closed by measuring off the page
  instead of off an element, and the obvious version of the same move was tried here
  and does NOT work. Read on a 16px grid, cells whose difference is large and flat
  find the wrong alpha cleanly, 191 cells at a uniform -6.4 levels with nothing from
  a border, a shadow, a button fill or a weight change. They also find a changed
  padding: 77 cells at a uniform +13.6, because content moving down 16px leaves a
  band that is uniformly brighter. The two are identical to the measure, both
  perfectly consistent across the region, so there is no threshold that separates
  them, and content moving is the commonest rebuild fault of all. What would separate
  them is differencing AFTER putting the two images back in register, band by band,
  using the per-band offsets the report already computes, so that a shift explains
  itself away and only a real shade change is left. That is the work, and it is why
  this is still here rather than shipped. Corner radius is the fifth of the five and
  stays unnamed on purpose: 0.5 points, which is not worth a line.

## Deliberate limits

- **The letter-case reading cannot tell capitals from different words.** It measures
  where the ink sits between the cap line and the baseline, and setting other words
  there moves it the same way: measured, "ocean nurse" reads 1.02 and "summer our"
  1.19, both above CONTINUE at 1.10, because a word with no ascenders fills its band
  evenly too. Comparing design against attempt cancels most of it, since the same
  words read the same on both sides, and the glyph-group count has to match within
  one. What is left is a rebuild that used the wrong words, so the sentence names
  both readings rather than asserting the one it cannot prove. It also says nothing
  below a band of 10 rows, about 13px of type, where the reading is coarse: a real
  case change at 9px was measured and is deliberately not reported.

- **A dispute needs two rounds to agree before it is repeated to anyone.** One round
  saying a fault is not there is an opinion, and models are agreeable enough that
  asking the question invites a yes. Two independent rounds is the gate; it has not
  been tuned against real disputes because there are none yet.
- **A disputed fault is still measured and still reported.** Only the pressing stops.
  The rounds can be wrong together, and the person has the last word.
- **A dispute is matched to a fault by what the sentence is about, then by where it
  is, and ambiguity refuses.** A round quotes the report's sentence, so the kind is
  read off the headline the report already gives it and the 50px band only chooses
  between faults of that same kind. Two faults of one kind in one band identify
  neither, and a set fault (the empty containers, the panels at the wrong alpha) has
  no band at all, so it is matched on kind alone and a dispute about one box retires
  the whole set. Hearing nothing costs the rounds that go on being spent on a fault;
  hearing the wrong thing retires a fault nobody questioned and reports that to the
  person, which is worse. Found by `scripts/from_scratch.py`, which caught a dispute
  about an empty icon well being recorded against the heading's font weight because
  both sat at y 39.

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
