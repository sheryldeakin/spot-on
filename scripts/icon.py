"""Print an icon from the bundled set, ready to paste into the page.

A rebuild that is told "the design puts an icon in each of these wells" still has to
invent seven glyphs, and inventing them one at a time is how a page ends up with seven
icons from seven different hands. This hands over one consistent set instead.

    python scripts/icon.py house calendar settings     # the markup for those three
    python scripts/icon.py --find weather              # names matching a word
    python scripts/icon.py --list                      # every name, one per line

Nothing is fetched: the markup is inlined into the page, so a run never depends on the
network. Set the size with width and height, the colour with stroke, and the line weight
with stroke-width to match the design.
"""
import argparse
import json
import sys
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "assets" / "icons" / "lucide.json"


def load():
    if not INDEX.exists():
        raise SystemExit("no icon set at {}".format(INDEX))
    return json.loads(INDEX.read_text(encoding="utf-8"))


def find(doc, word):
    word = word.lower()
    hits = [n for n in doc["icons"] if word in n]
    for name, tags in doc.get("tags", {}).items():
        if name not in hits and any(word in str(t).lower() for t in tags):
            hits.append(name)
    return sorted(hits)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="icon names to print")
    ap.add_argument("--find", metavar="WORD", help="search names and tags")
    ap.add_argument("--list", action="store_true", help="print every name")
    ap.add_argument("--size", type=int, default=24, help="width and height in px")
    ap.add_argument("--stroke", default="currentColor", help="stroke colour")
    ap.add_argument("--stroke-width", default="2", dest="sw", help="line weight")
    args = ap.parse_args(argv)
    doc = load()

    if args.list:
        print("\n".join(sorted(doc["icons"])))
        return 0
    if args.find:
        hits = find(doc, args.find)
        print("\n".join(hits) if hits else "nothing matching {!r}".format(args.find))
        return 0 if hits else 1
    if not args.names:
        ap.print_help()
        return 2

    missing = [n for n in args.names if n not in doc["icons"]]
    for name in missing:
        near = find(doc, name)[:8]
        print("no icon called {!r}{}".format(
            name, ". Close: " + ", ".join(near) if near else ""), file=sys.stderr)
    for name in args.names:
        if name not in doc["icons"]:
            continue
        svg = doc["wrapper"].format(body=doc["icons"][name])
        svg = (svg.replace('width="24"', 'width="{}"'.format(args.size))
                  .replace('height="24"', 'height="{}"'.format(args.size))
                  .replace('stroke="currentColor"', 'stroke="{}"'.format(args.stroke))
                  .replace('stroke-width="2"', 'stroke-width="{}"'.format(args.sw)))
        print("<!-- {} -->".format(name))
        print(svg)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
