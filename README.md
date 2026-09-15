# Spot On

**Pixel-perfect pages, measured.**

Stop playing spot the difference with your AI.

You hand a coding model a design and ask for the page. It builds something close. You say the spacing is off and the button is the wrong colour. It says "fixed". You put the two side by side, squint, and write the next correction. Twelve rounds later it is still not quite right, and neither of you can say how close it is or whether the last change helped at all.

Spot On takes a screenshot of the page, scores it against the design out of 100, draws where the two disagree, and writes down what to fix first in sentences a model can act on. Every attempt is kept, so "closer" becomes a number that either went up or did not.

![Spot On](docs/screenshot.jpg)

## What it does

- Screenshots a running page (`http://localhost:5173/pricing`), or pasted HTML, SVG or canvas code, with headless Chrome at the design's exact page size and display scale.
- Scores the result out of 100, split into five parts that each fail for a different reason, so the report can say which kind of mistake was made.
- Draws a difference map: bright red where the page misses the design, black where it matches.
- Notices when content is shifted rather than wrong. One missing label near the top pushes the whole page down, and a plain pixel comparison then reports everything below it as broken. Spot On reports the shift and where it starts instead.
- Keeps every attempt with its score and a note on what changed.
- Writes a feedback packet to paste back to whatever wrote the code, or runs the loop itself.

Overlay extensions such as [PerfectPixel](https://www.welldonecode.com/perfectpixel/) lay a design over a live page so a person can line the two up by eye. Spot On is built for the other half of the job: telling a model, in numbers and sentences, how far off it is and what to change.

## Two ways to run the loop

**On a real project.** The code lives in a repo, so the loop runs in the coding session that edits it. After each change the session scores the page from the command line, reads the difference map, and fixes the first problem in the report. If a change lowers the score, it is undone. Each call is recorded in the same run history the page shows.

```
python spot-on.py score design.png http://localhost:5173/pricing --scale 2 --run pricing --note "fixed card padding"
```

**On a snippet.** For pasted HTML, an SVG or canvas code with no repo behind it, the page can run the loop by itself: render a first attempt, then press **Run 3 rounds**. Each round shows headless Claude Code the design, its best render so far and the difference map, hands it the report, and scores what comes back. It builds on the best attempt, not the latest, so a round that made things worse is discarded instead of compounded. It defaults to Sonnet; set `SPOT_ON_MODEL` to change that.

## The score

| Part | Weight | What it measures | What a low number usually means on a page |
|---|---|---|---|
| structure | 40% | SSIM over 7px windows, on the drawn content | border radius, borders, shadows, font size or line height |
| shape | 25% | overlap of the drawn area with the design's, as intersection over union | width, height, padding, gap or alignment |
| colour | 20% | distance between the two palettes, in both directions | a background, text or button colour is wrong or missing |
| detail | 15% | correlation of edge density over a 9px window | missing text, icons or borders, or the wrong font weight |
| coverage | caps the total | share of the design with something drawn within 6px of it | an element that was never built |

The four weighted parts make a subtotal, and coverage scales it: `match = subtotal x (0.6 + 0.4 x coverage)`. A page that leaves out a fifth of the design can reach at most 92% of what it would otherwise score.

Each of these choices fixed a case where the score disagreed with what the eye sees:

- **Structure and detail are measured on the drawn content**, not the whole page. Averaged over empty space, leaving an element out scored better than drawing it slightly wrong.
- **Colour compares palettes, not pixels.** Per-pixel colour over the drawn area was really measuring position, so a three pixel offset was punished three times.
- **Detail is blurred before it is compared.** Without that, one pixel of antialiasing difference read as badly as missing detail, and a near-identical copy scored in the fifties.
- **Coverage caps the score.** Even with the three fixes above, a design with one element removed could still edge out a close copy of the whole thing.

### Calibration

A design with a circle and a square, and seven attempts at it, each rendered by Chrome. Regenerate with `python scripts/calibrate.py`.

<!-- calibration:start -->
```
case    match  structure  shape  colour  detail  coverage  what it is
exact    97.7       95.6   99.1    99.8    98.1     100.0  identical to the design
close    88.8       85.4   94.6    99.7    73.6     100.0  3px offset and a slight hue shift
half     80.7       88.5   84.1    83.8    84.9      85.0  the square left out entirely
hue      84.6       95.3   99.1    35.0    98.2     100.0  right geometry, wrong colour
shift    51.2       65.3   46.7    99.8     0.0      71.8  right colours, 40px to the right
wrong    32.8       62.8   42.7    24.0     0.0      52.0  one wrong shape in the wrong place
blank    14.5       60.3    0.0     0.1     0.0       0.0  nothing drawn
```
<!-- calibration:end -->

Do not chase 100. Text rendering and antialiasing leave a floor a few points below it, and the report says so when that is all that remains.

## A worked example

`docs/demo/design.html` is a pricing section standing in for a design export. `docs/demo/first-attempt.html` is a plausible first pass: same content, but the small PRICING label is missing, the fonts and button styles are different, and the highlighted plan is not filled. The automatic loop then ran from there. Regenerate with `python scripts/demo.py`; model output varies, so a rerun gives different numbers.

<!-- demo:start -->
```
attempt  match  structure  shape  colour  detail  coverage  source   what changed
      1   19.9       28.8   13.1    51.6     1.1      46.9  manual   hand-written first pass
      2   63.9       45.1   71.3    98.6    60.5      96.9  claude   Removed the outer white padded box (source of the top-of-page shift), moved the page background to #F7F8F6, added the "PRICING" eyebrow label, made the Team card a solid dark-green (#183830) panel with a gold "Most popular" badge and gold filled button, switched Starter/Business buttons to white/outlined instead of green-filled, and recolored headings/body text to the near-black-green and muted teal-gray tones from the design's dominant palette.
      3   71.5       50.7   77.0    98.6    86.9      97.4  claude   Shrunk the H1 (28px/700/-0.3px letter-spacing) to close the width overshoot that was driving the top-of-page RMSE, and cut feature-list line-height from 1.9 to 1.7 to remove the ~8px downward drift affecting everything below the header; switched to a system-UI font stack since Arial was the suspect for the text/detail mismatch.
      4   51.2       36.9   67.3    98.6    11.5      91.2  claude   Trimmed the 3-line description blocks' line-height from 1.7 to 1.6 (removes the ~4px cumulative vertical drift the report flagged, which lands right at the description block in the layout) and added explicit line-heights to every text element (labels, headings, prices, buttons, badge) so font-metric defaults stop varying between renders, per the font/line-height guidance in the report.
      5   73.6       55.4   77.0    98.6    88.1      97.4  claude   Reduced only the 3-line description blocks' line-height from 1.7 to 1.6 (a ~3.9px cut across three lines, matching the reported ~4px drift starting at that point), and left every other element's line-height untouched, the prior attempt's regression came from also forcing explicit line-heights on labels/headings/prices/buttons/badge, not from this specific change.
```
<!-- demo:end -->

Three things in that run are the reason the tool exists:

- **The first report named the cause, not the symptom.** It did not say everything is wrong. It said content sits about 38px higher than the design from roughly 50px down, and that something above that point is missing. That something was the label.
- **The font was the wall.** Colours were right from round 2 on, yet structure stayed low, and the report said to check the typeface before moving boxes. Round 3 switched off Arial to a stack that includes the design's Segoe UI, and detail went from 60.5 to 86.9. In an earlier run without that hint (`docs/demo-run-before-typeface-hint.txt`), the model kept adjusting weights and spacing and stalled at 58.1.
- **A confident fix made it worse, and the number said so.** Round 4 set explicit line heights on every text element "so font-metric defaults stop varying". It scored 51.2, down from 71.5. The loop discarded it, told the model what had failed, and round 5 kept only the part that helped: 73.6.

## Running it

```
pip install -r requirements.txt
python spot-on.py              # http://127.0.0.1:7265
python spot-on.py 7266         # another port
```

Needs Python 3.10 or newer, and Chrome or Edge. Set `SPOT_ON_CHROME` if the browser is somewhere unusual. The server binds to 127.0.0.1 only and nothing is uploaded; it only fetches the page you point it at.

**Capture scale.** A screenshot from a 150% Windows display or a Retina Mac is larger than the page it shows. A 2880px-wide design of a 1440px layout was captured at 2x. Pick the matching scale when starting a run, or pass `--scale`, so the page is rendered at its real width. The wrong scale triggers a different responsive breakpoint, and every score after that measures the wrong layout.

**Command line.**

```
python spot-on.py score <design.png> <url-or-file> [--kind url|html|svg|canvas]
                        [--scale 1|1.25|1.5|2] [--run NAME] [--note TEXT] [--json]
```

It prints the report, the change from the previous attempt, the best score so far, and the paths of the design, the screenshot and the difference map.

## Limits

- **Fonts.** A model cannot reliably read a typeface off a screenshot, and a page in the wrong font stalls: colours and layout match while structure stays low. The report names that pattern and says to check the font first, but if the design's font is known, say so up front. If it is not installed at all, text will never line up exactly and the ceiling drops with how much text the page has.
- **Moving content.** Animations, carousels, live dates and cookie banners change between screenshots. Freeze or hide them first.
- **States.** A design showing a hover, an open menu or a filled form is compared against the page's default state unless the page is put in that state.
- **Viewport only.** The screenshot is exactly the design's size. A design of one section needs a URL or component that renders that section at that size.
- **It can be gamed.** Putting the design image itself on the page as a background would score near 100. Nothing detects that; the tool assumes the goal is a page that looks like the design because it is built like it.

## Tests

```
python -m unittest discover -s tests -v
```

Scoring, guards and geometry run anywhere. The server and screenshot tests need Chrome or Edge and are skipped without one. `tests/test_docs.py` fails if the calibration or demo tables in this README drift from the files the scripts generate.

## Layout

One file, `spot-on.py`: the scorer, the screenshot step, the HTTP server and the page. Runs are written to `runs/<name>/`: the design, a `run.json`, and one `.code`, `.png`, `-diff.png` and `.json` per attempt. Deleting a run folder deletes the run.

## License

MIT
