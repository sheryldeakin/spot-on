# Spot On

**Pixel-perfect pages, measured.**

Stop playing spot the difference with your AI.

You hand a coding model a design and ask for the page. It builds something close. You say the spacing is off and the button is the wrong colour. It says "fixed". You put the two side by side, squint, and write the next correction. Twelve rounds later it is still not quite right, and neither of you can say how close it is or whether the last change helped at all.

Spot On takes a screenshot of the page, scores it against the design out of 100, draws where the two disagree, and writes down what to fix first in sentences a model can act on. Every attempt is kept, so "closer" becomes a number that either went up or did not.

![Spot On](docs/screenshot.jpg)

## What it does

- Takes the design as an image (a Figma export, a screenshot) **or as a link**: paste a page and Spot On captures it through the same renderer the attempts use, so both sit on the browser's virtual clock and an animation lands in the same place in each. That makes "does my rebuild still match production" a one-line check.
- Screenshots a running page (`http://localhost:5173/pricing`), or pasted HTML, SVG or canvas code, with headless Chrome at the design's exact page size and display scale.
- Scores the result out of 100, split into five parts that each fail for a different reason, so the report can say which kind of mistake was made.
- Draws a difference map: bright red where the page misses the design, black where it matches.
- Notices when content is shifted rather than wrong. One missing label near the top pushes the whole page down, and a plain pixel comparison then reports everything below it as broken. Spot On reports the shift and where it starts instead.
- Names the elements that miss, in CSS pixels: which text is 9% wider, which boxes are 4px shorter, what sits 7px to the right, what is missing entirely. Page-wide numbers stop being useful once a page is close, and a model given only them starts making sweeping changes that break what already matched.
- Says when the font is wrong, and only when it is: text lines that sit in the right place are lined up and compared letter by letter, since the same string in Arial and in Segoe UI is nearly the same width.
- Keeps every attempt with its score and a note on what changed.
- Writes a feedback packet to paste back to whatever wrote the code, or runs the loop itself.

Overlay extensions such as [PerfectPixel](https://www.welldonecode.com/perfectpixel/) lay a design over a live page so a person can line the two up by eye. Spot On is built for the other half of the job: telling a model, in numbers and sentences, how far off it is and what to change.

## Two ways to run the loop

**On a real project.** The code lives in a repo, so the loop runs in the coding session that edits it. After each change the session scores the page from the command line, reads the difference map, and fixes the first problem in the report. If a change lowers the score, it is undone. Each call is recorded in the same run history the page shows.

```
python spot-on.py score design.png http://localhost:5173/pricing --scale 2 --run pricing --note "fixed card padding"
```

The design can be a link instead of a file, captured at whatever page size you ask for:

```
python spot-on.py score https://yoursite.com/pricing http://localhost:5173/pricing --width 1440 --height 900
```

Use that on pages you have the right to match: your own production or staging site, a page your team owns, a design system's own docs.

**On a snippet.** For pasted HTML, an SVG or canvas code with no repo behind it, the page can run the loop by itself: render a first attempt, then press **Run 3 rounds**. Each round shows the AI the design, its best render so far and the difference map, hands it the report, and scores what comes back. It builds on the best attempt, not the latest, so a round that made things worse is discarded instead of compounded. Each round asks for **three rewrites at once and keeps the highest scoring one**, which evens out the luck of a single draw; the others stay in the history marked "not kept". Change it in the page (1 try each, best of 2, 3 or 5), with `--candidates` on the demo script, or with `SPOT_ON_CANDIDATES`. They run in parallel, so a round takes about as long as one rewrite and costs as much as that many.

While the page is far off, a round fixes everything the report names. Once it is close, a round may change at most three things, each on a named element, because sweeping edits ("set a line height on every text element") were what repeatedly undid good rounds. Every round is also told which earlier changes lowered the score, so it stops re-trying them.

## Which AI runs the loop

Whatever you already have. Spot On looks for these in order and uses the first one it finds:

| Agent | Needs | Sees the images |
|---|---|---|
| Chrome built-in (Gemini Nano) | Chrome 148+ on desktop, one model download | yes, in the page |
| Claude Code | `claude` on PATH, signed in | yes, it opens them itself |
| Codex CLI | `codex` on PATH | only if that CLI can open local files |
| Gemini CLI | `agy` on PATH | only if that CLI can open local files |
| Anthropic API | `ANTHROPIC_API_KEY` | yes, sent with the request |
| OpenAI API | `OPENAI_API_KEY` | yes, sent with the request |
| Ollama | running locally, a vision model pulled | yes, sent with the request |

**Nothing installed and no account? Chrome can do it.** Recent Chrome desktop ships a small model (Gemini Nano) behind the `LanguageModel` API. When the browser has it, Spot On offers it in the list automatically and runs the round inside the page: no key, no account, no cost, and nothing leaves the machine. The first use downloads the model, which needs free disk space and either a GPU with more than 4GB of VRAM or 16GB of RAM. It is a small model, so expect it to make coarser changes than the hosted ones, and to struggle on a long page. It is the free way in, not the best result. For a larger local model, Ollama with a vision model is the next step up.

Pick a different one in the page, or set `SPOT_ON_AGENT` (`claude`, `codex`, `gemini`, `anthropic-api`, `openai-api`, `ollama`). `SPOT_ON_MODEL` picks the model; the defaults are Sonnet for Claude Code, `claude-sonnet-5`, `gpt-4o` and `llava`, and model names change, so set it if a request is rejected. The API calls are plain HTTP, so no provider SDK has to be installed.

An agent that cannot open local files still gets the whole written report, which is the part that says what to fix; it just cannot look at the difference map. **If none of them is available, nothing breaks except the automatic loop**: scoring, the difference map, the history and the feedback packet all work, and the packet is written to be pasted into any chat window.

Keys and accounts stay on your machine. The page never talks to any provider itself, and never asks anyone to sign in; the local server makes the call, using the account signed in there, and that account's usage is what gets spent.

## The score

| Part | Weight | What it measures | What a low number usually means on a page |
|---|---|---|---|
| structure | 40% | SSIM over 7px windows, on the drawn content | border radius, borders, shadows, font size or line height |
| shape | 25% | overlap of the drawn area with the design's, as intersection over union | width, height, padding, gap or alignment |
| colour | 20% | distance between the two palettes, in both directions, and between the pages behind them; whichever is worse | a background, text or button colour is wrong or missing |
| detail | 15% | correlation of edge density over a 9px window | missing text, icons or borders, or the wrong font weight |
| coverage | caps the total | share of the design with something drawn within 6px of it | an element that was never built |

Four of those work on the **drawn content**, which means whatever has local contrast: an edge, and the region its edges enclose. A full-bleed background gradient has almost none however strong it is, so it is not content, and a gradient that is wrong is reported by colour instead.

The four weighted parts make a subtotal, and coverage scales it: `match = subtotal x (0.6 + 0.4 x coverage)`. A page that leaves out a fifth of the design can reach at most 92% of what it would otherwise score.

**The number is for comparing attempts with each other, not for reading as a percentage of visual similarity.** It says whether this attempt is closer than the last one and which part got worse. What a real page can reach depends on how much text it carries, whether the design's font is installed, and how much of it is photography or artwork the code cannot reproduce. There is no passing mark, and setting one is how a loop ends up buying rounds after it has stopped climbing.

Each of these choices fixed a case where the score disagreed with what the eye sees:

- **Structure and detail are measured on the drawn content**, not the whole page. Averaged over empty space, leaving an element out scored better than drawing it slightly wrong.
- **Colour compares palettes, not pixels.** Per-pixel colour over the drawn area was really measuring position, so a three pixel offset was punished three times.
- **Detail is blurred before it is compared.** Without that, one pixel of antialiasing difference read as badly as missing detail, and a near-identical copy scored in the fifties.
- **Coverage caps the score.** Even with the three fixes above, a design with one element removed could still edge out a close copy of the whole thing.
- **Content is found by local contrast, not by distance from one page colour.** Measuring distance from a single ground colour failed both ways on real pages: a background gradient a little too saturated crossed the threshold everywhere and was measured as content, which pinned shape near 10 for an entire run and sent the model to fix box geometry that was already right; and a white card on an off-white page fell under the threshold, so leaving out the largest element on the page cost a tenth of a point. The page-like calibration cases below hold each of those directions down.
- **The page behind the content is compared separately.** Once the background was out of the mask it was out of the palette too, and a clearly over-saturated gradient scored 99.2. Colour now takes the worse of the content palette and the page behind it, so the background is charged once, to the part that means colour.

### Calibration

Two designs. The first is a circle and a square on flat white, with seven attempts at it. The second is closer to a real page, a card and some text on a full-bleed gradient, and each of its five cases changes one thing: the gradient gets stronger, the gradient goes away, or the card is left out. Both are rendered by Chrome. Regenerate with `python scripts/calibrate.py`.

Flat shapes on flat white never exercise the background, and the background is where the measure decides what counts as content, which is why the second design exists. Its cases pull in opposite directions on purpose: `deeper` and `strong` change nothing a reader would call content, so they have to stay cheap, while `nocard` removes the largest element on the page, so it has to stay expensive. Any change to how content is found has to answer both at once.

<!-- calibration:start -->
```
case    match  structure  shape  colour  detail  coverage  what it is
exact    97.7       95.9   99.0   100.0    97.7     100.0  identical to the design
close    89.2       86.2   94.5    99.9    73.9     100.0  3px offset and a slight hue shift
half     80.4       89.1   83.0    84.7    84.1      83.9  the square left out entirely
hue      85.1       95.6   99.0    37.2    97.7     100.0  right geometry, wrong colour
shift    52.2       66.9   47.6   100.0     0.0      72.4  right colours, 40px to the right
wrong    33.8       64.8   42.9    26.0     0.0      51.9  one wrong shape in the wrong place
blank    15.0       62.4    0.0     0.1     0.0       0.0  nothing drawn

a page-like design: a full-bleed gradient with a card and text on it

case    match  structure  shape  colour  detail  coverage  what it is
same    100.0      100.0  100.0   100.0   100.0     100.0  identical to the design
deeper   95.8       99.4  100.0    80.3    99.9     100.0  same content, gradient a little stronger
strong   91.7       98.2   99.8    62.3    99.7     100.0  same content, gradient clearly stronger
flatbg   98.5       99.9  100.0    92.9   100.0     100.0  same content, no gradient at all
nocard   58.2       97.3   21.6    93.7    99.7      36.6  right gradient, the card left out
```
<!-- calibration:end -->

Do not chase 100, and do not set a target at all. Those numbers come from designs built to be reproducible exactly; a real page carries text in a font that may not be installed, icons, photographs and artwork, and a close rebuild of one lands far lower than a close copy of a circle. Stop when the climb flattens, not when a number is reached.

## A worked example

`docs/demo/design.html` is a pricing section standing in for a design export. `docs/demo/first-attempt.html` is a plausible first pass: same content, but the small PRICING label is missing, the fonts and button styles are different, and the highlighted plan is not filled. The automatic loop then ran from there. Regenerate with `python scripts/demo.py`; model output varies, so a rerun gives different numbers.

<!-- demo:start -->
```
attempt  match  structure  shape  colour  detail  coverage  round tried     source   what changed
      1   48.3       51.7   60.0    79.3    13.4      75.5  -               manual   hand-written first pass
      2   66.4       59.2   75.9    99.3    27.0      99.6  66.4/64.3/61.8  claude   Fixed the biggest miss: the middle "Team" card is dark green (#183830) with a gold "Most popular" badge and gold CTA button, not a white card with a green outline, matching the reference exactly. Also switched the outer two buttons from filled green to white/outlined (as in the design), changed "$9/mo" to "$9 / month", increased header spacing and card padding/line-height to push the card row and buttons down to match the design's vertical position, and added the missing "PRICING" eyebrow label.
      6   71.1       58.6   88.2    99.3    39.1      99.5  71.1/67.5/67.3  claude   Raised top padding 36→43px to correct the 7px global downward shift; compacted the Team card's padding/margins/line-height to cut ~50-60px of excess height; narrowed Starter/Business card side padding (26→16px) and tightened their pre-button spacing/line-height so the "Choose" buttons widen toward the reported 178px and move up/left toward their design position.
      8   74.7       61.4   90.9    99.4    51.3      99.5  74.7/71.0/66.5  claude   Cut Starter/Business "Choose" button margin-top 14→5px to lift those buttons the reported 9px; removed Team card's bottom padding (20→0px) to cut its reported 20px excess height without moving any content already positioned correctly.
     11   69.4       56.7   85.5    99.6    40.3      97.9  69.4/67.1/64.9  claude   Set the title to a serif font-family only (isolated, addressing the total title mismatch); cut Starter/Business price-row margin-top 16→4px to lift that line ~13px toward its design position; cut Team's button margin-top 14→6px to shed most of its 11px excess card height and pull its button up toward its design position.
```
<!-- demo:end -->

What the run shows:

- **The first report named the cause, not the symptom.** It did not say everything is wrong. It said content sits about 38px higher than the design from roughly 50px down, and that something above that point is missing. That something was the small PRICING label.
- **Taking the best of three is not a formality.** The `round tried` column is what all three rewrites of that round scored. The third round drew 74.7, 71.0 and 66.5 from one prompt, more than eight points apart, and the loop kept the top one. No round in this run drew closer than 3.8 points apart.
- **A confident fix made it worse, and the number caught it.** The last round decided the title was set in a serif face and changed it, on its own, to isolate the effect. Its best rewrite scored 69.4, below the best at 74.7, so the whole round was discarded, and attempt 8 reached the best score of the run.
- **Later rounds work element by element**, because that is what the report gives them: which text is a few percent too wide, which card is too tall, what sits a few pixels off.

**The numbers move between runs.** Rendering and scoring do not: the same code scores the same, and three renders of one page differed by zero pixels. The model does, and the spread inside a single round is the size of it. Each round is a fresh sample and the loop is a greedy climb, so the big rebuild in the first round sets a ceiling that later rounds only nudge. Taking the best of three rewrites per round is the lever against that, at three times the cost, and the spread column is what it is buying.

## Running it

```
pip install -r requirements.txt
python spot-on.py              # http://127.0.0.1:7265
python spot-on.py 7266         # another port
```

Needs Python 3.10 or newer (numpy, pillow and scipy), and Chrome or Edge. Set `SPOT_ON_CHROME` if the browser is somewhere unusual. The server binds to 127.0.0.1 only and nothing is uploaded; it only fetches the page you point it at.

**Capture scale.** A screenshot from a 150% Windows display or a Retina Mac is larger than the page it shows. A 2880px-wide design of a 1440px layout was captured at 2x. Pick the matching scale when starting a run, or pass `--scale`, so the page is rendered at its real width. The wrong scale triggers a different responsive breakpoint, and every score after that measures the wrong layout.

**Command line.**

```
python spot-on.py score <design.png> <url-or-file> [--kind url|html|svg|canvas]
                        [--scale 1|1.25|1.5|2] [--run NAME] [--note TEXT] [--json]
```

It prints the report, the change from the previous attempt, the best score so far, and the paths of the design, the screenshot and the difference map.

## Limits

- **A rewrite can quietly put the render on the network.** Asked to match a typeface, a model will reach for a webfont and add an `@import` from a font host. The page then renders differently depending on whether that request succeeds, so a score can move without the code changing. If runs need to be reproducible offline, say so in the extra instruction, or install the font locally and name it.
- **Fonts.** A model cannot reliably read a typeface off a screenshot. The report says the font is wrong only when text that sits in the right place still has the wrong letter shapes, which is the honest evidence for it, but if the design's font is known, say so up front and skip the guessing. If the font is not installed at all, text will never line up exactly and the ceiling drops with how much text the page has.
- **Moving content is excluded, and the exclusion is measured.** Chrome renders on a virtual clock, so two captures of the same page at the same settle time land at the same point in an animation: a real app with a drifting hero and a 3D avatar scored 99.3 against itself with nothing excluded. What does move (random content, video, live data) is found on the first attempt of a running page, by screenshotting it three times and comparing the last two, and is then excluded from every score and painted slate blue in the difference map. On that app the moving area was 0.65% of the page and the ceiling 100.0. The first capture is thrown away on purpose: a cold page is still loading fonts and lazy chunks, and measuring that would mask out real content for the whole run.
- **A page that moves everywhere cannot be scored.** If more than 60% of it changes between captures (a video background, a full-bleed animation), Spot On excludes nothing, says so, and tells you the number is mostly measuring motion. Excluding that much would leave nothing to compare and every attempt would come back a meaningless 100.
- **A design screenshot taken by hand is not on that clock.** If the design is a screenshot of the same app, capture it through Spot On (or with the same settle time), or the animation will sit at a different point in the two images and the difference is real but unfixable. Capturing one design at 6000ms and scoring it against attempts at 4000ms cost 19 points on an otherwise identical page.
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
