# Match Lab

A local tool for the case where a model is asked to reproduce an exact reference image and keeps getting it almost right.

Ask a language model to redraw a picture in SVG and it will hand you something plausible. Ask it to fix what is wrong and it has nothing to go on, because it cannot see how far off it was. Match Lab renders the attempt, scores it against the reference, shows where the miss is, and writes the feedback the model needs for the next try. It can run that loop by itself.

![Match Lab](docs/screenshot.jpg)

## What it does

- Renders SVG, canvas 2D or HTML through headless Chrome at exactly the reference size, so the score reflects the code and nothing else.
- Scores the result out of 100, broken into four parts that each fail for a different reason.
- Draws a difference map: bright red where the attempt missed, black where it matched.
- Keeps every attempt with its score, so you can see whether a change actually helped.
- Writes a feedback packet in plain sentences, ready to paste back to whatever wrote the code.
- Optionally runs the loop itself: each round shows the model the reference, its own render and the difference map, hands it the report, and scores whatever comes back.

## The score

One number to chase, four to explain it. They are deliberately independent, so the report can say which kind of mistake was made rather than just that the attempt is worse.

| Part | Weight | What it measures | What a low score means |
|---|---|---|---|
| structure | 40% | SSIM over 7px windows, measured on the drawn content | Edges and gradients are in the wrong places |
| shape | 25% | Overlap of the drawn area with the reference's, as intersection over union | The silhouette is wrong, or something is missing |
| colour | 20% | Distance between the two palettes, in both directions | A reference colour is missing, or one was invented |
| detail | 15% | Correlation of edge density over a 9px window | The attempt is smoother or busier than the reference |

Three decisions behind those numbers, each of which fixed a case where the score disagreed with what the eye says:

- **Structure and detail are measured on the drawn content**, not the whole canvas. Averaged over an empty page, an attempt that simply leaves an element out scores better than one that draws it slightly wrong.
- **Colour compares palettes, not pixels.** Per-pixel colour distance over the drawn area is really a measure of position, which structure and shape already cover, so a three pixel offset was being punished three times.
- **Detail is blurred before it is correlated.** Without that, one pixel of antialiasing difference reads as badly as missing the detail entirely, and a near-perfect copy scores in the fifties.

Calibration on a reference with two shapes, for a sense of the range:

```
exact   96.8    structure 94.1  shape 98.6  colour 99.8  detail 97.3
close   88.0    3px offset and a slight hue shift
half    85.6    one of the two shapes missing entirely
hue     83.8    perfect geometry, wrong colour     -> colour 35.2
shift   57.7    right colours, 40px displacement   -> shape 46.8
wrong   40.5    one wrong shape in the wrong place
blank   24.1    nothing drawn
```

Do not chase 100. Antialiasing and sub-pixel placement leave a floor in the high nineties, and the report says so when you reach it.

## Running it

```
python match-lab.py            # http://127.0.0.1:7265
python match-lab.py 7266       # another port
```

Needs Python with `numpy` and `pillow`, and Chrome or Edge installed. Set `MATCH_LAB_CHROME` if the browser is somewhere unusual. Everything stays on loopback; nothing is uploaded.

There is a command line mode for scripting and for use as a check in a test suite:

```
python match-lab.py score reference.png attempt.svg svg
```

It prints the same report the page shows.

## Using it

1. Drop a reference image in and name the run.
2. Paste the model's code, or take the starter, and render it.
3. Read the score. The numbered list under it is ordered by what is worth fixing first.
4. Copy the feedback packet back to the model, or press **Run 3 rounds** and let it iterate on its own.

Every attempt is kept. Open one to put its code back in the editor; the best one is marked.

The automatic loop shells out to Claude Code in headless mode and defaults to Sonnet, which is enough for this and does not spend the premium quota on a loop that may run many rounds. Override with `MATCH_LAB_MODEL`. A round takes one to four minutes, most of it the model looking at the three images.

A worked example, starting from a deliberately bad first attempt (a grey circle standing in for a landscape):

```
attempt 1  31.0  manual
attempt 2  81.6  replaced the placeholder with the actual scene
attempt 3  85.9  raised the horizon band to match the green area share
attempt 4  88.6  nudged the sun left and up, fixing the crescent offset seen in the diff
```

## Layout

Single file, standard library plus numpy and pillow. Runs are written to `runs/<name>/`: the reference, a `run.json`, and one `.code`, `.png`, `-diff.png` and `.json` per attempt. Deleting a run folder deletes the run.

## Licence

MIT.
