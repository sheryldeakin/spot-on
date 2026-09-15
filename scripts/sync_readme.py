"""Copy the generated tables into README.md between their markers.

    python scripts/sync_readme.py

Run after scripts/calibrate.py or scripts/demo.py. The README's numbers are
never typed by hand; tests/test_docs.py checks they match these files.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCKS = {
    "calibration": ROOT / "docs" / "calibration.txt",
    "demo": ROOT / "docs" / "demo-run.txt",
}


def block(name):
    body = BLOCKS[name].read_text(encoding="utf-8").rstrip("\n")
    return "<!-- {0}:start -->\n```\n{1}\n```\n<!-- {0}:end -->".format(name, body)


def main():
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    for name in BLOCKS:
        pattern = re.compile(r"<!-- {0}:start -->.*?<!-- {0}:end -->".format(name), re.S)
        if not pattern.search(text):
            raise SystemExit("README has no {} markers".format(name))
        text = pattern.sub(lambda _: block(name), text)
    readme.write_text(text, encoding="utf-8")
    print("README tables synced")


if __name__ == "__main__":
    main()
