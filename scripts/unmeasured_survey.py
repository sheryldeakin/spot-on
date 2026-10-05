"""What a person notices that the report has no line for.

The text-colour gap was found by eye, not by the tool, which raises the question of
what else is like that. This is the same question asked deliberately: build pairs of
pages that differ in exactly one visible property, score each pair, and see whether
any sentence in the report names the thing that changed.

A property that changes the score but is never named is the shape of the problem:
the loop feels it, nobody tells it what it is, and it goes round again.

    python scripts/unmeasured_survey.py      # writes scripts/unmeasured-survey.json
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("spot_on", ROOT / "spot-on.py")
so = importlib.util.module_from_spec(spec)
sys.modules["spot_on"] = so
spec.loader.exec_module(so)

W, H = 520, 300


def page(card="#FFFFFF", radius=10, border="none", shadow="none", text="#1F2937",
         align="left", label="Continue", weight="400", button="#2563EB",
         pad=18, opacity="1", letter="normal"):
    return (
        '<div style="width:{W}px;height:{H}px;background:#EEF2F7;padding:0;'
        'font-family:Arial">'
        '<div style="margin:36px;background:{card};border-radius:{radius}px;'
        'border:{border};box-shadow:{shadow};padding:{pad}px;opacity:{opacity}">'
        '<div style="color:{text};font-size:22px;font-weight:{weight};text-align:{align};'
        'letter-spacing:{letter}">Account settings</div>'
        '<div style="color:{text};font-size:14px;text-align:{align};margin-top:10px">'
        'Manage how this workspace behaves for everyone on the team.</div>'
        '<div style="margin-top:16px;text-align:{align}">'
        '<span style="background:{button};color:#FFFFFF;padding:9px 18px;'
        'border-radius:6px;font-size:14px">{label}</span></div>'
        '</div></div>'
    ).format(W=W, H=H, card=card, radius=radius, border=border, shadow=shadow,
             text=text, align=align, label=label, weight=weight, button=button,
             pad=pad, opacity=opacity, letter=letter)


# Each case: a name, what the attempt changes, and the words a report would have to
# contain for that change to count as named.
CASES = [
    ("text colour", dict(text="#2563EB"), ["wrong colour", "draws it"]),
    ("button fill colour", dict(button="#DC2626"), ["colour", "button", "fill"]),
    ("card fill colour", dict(card="#FEF3C7"), ["colour", "fill"]),
    ("corner radius", dict(radius=0), ["radius", "corner", "rounded", "square"]),
    ("border added", dict(border="2px solid #94A3B8"), ["border", "outline", "stroke"]),
    ("drop shadow added", dict(shadow="0 10px 24px rgba(0,0,0,0.35)"),
     ["shadow", "elevation"]),
    ("text alignment", dict(align="center"), ["align", "centre", "center"]),
    ("letter case", dict(label="CONTINUE"), ["case", "capital", "uppercase", "caps"]),
    ("letter spacing", dict(letter="2px"), ["spacing", "tracking", "letter"]),
    ("padding inside the card", dict(pad=34), ["padding", "inset", "margin", "gap"]),
    ("panel opacity", dict(opacity="0.55"), ["opacity", "alpha", "shade", "translucen"]),
    ("font weight", dict(weight="800"), ["weight", "bold", "heavier"]),
]


def main():
    tmp = Path(tempfile.mkdtemp(prefix="unmeasured-"))
    base_img = so.render_code(page(), "html", W, H, tmp / "base.png")
    rows = []
    for name, change, words in CASES:
        shot = so.render_code(page(**change), "html", W, H, tmp / "x.png")
        report, _, _ = so.score_images(base_img, shot)
        said = " ".join(report.get("problems") or []).lower()
        named = [w for w in words if w in said]
        rows.append({"property": name, "match": round(report["match"], 1),
                     "named": bool(named), "matched_words": named,
                     "problems": len(report.get("problems") or []),
                     "first": (report.get("problems") or ["(none)"])[0][:110]})
        print("{:<26} match {:>5.1f}  named: {}".format(
            name, report["match"], "yes" if named else "NO"), flush=True)

    unnamed = [r for r in rows if not r["named"]]
    out = {"cases": len(rows), "unnamed": len(unnamed), "rows": rows}
    path = ROOT / "scripts" / "unmeasured-survey.json"
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")

    print()
    print("{} of {} visible changes have no sentence naming them:".format(
        len(unnamed), len(rows)))
    for r in sorted(unnamed, key=lambda x: x["match"]):
        print("   {:<26} score fell to {:>5.1f}, report says: {}".format(
            r["property"], r["match"], r["first"]))
    print()
    print("wrote", path)


if __name__ == "__main__":
    main()
