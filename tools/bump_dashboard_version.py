"""Give every dashboard page a new ?v= on its stylesheet and scripts.

Run after changing anything in app/dashboard/static that a page loads (style.css or a .js file):

    .venv\\Scripts\\python.exe tools\\bump_dashboard_version.py

A browser keeps a copy of each file it has fetched. A new ?v= is a new address, so the next page load has to
fetch the new file instead of reusing the old one. Without it, on 20 Oct 2026, a browser combined the new
scripts with its saved old stylesheet, and the new header buttons appeared as bare grey browser buttons.
tests/unit/test_dashboard_explainer.py fails if the pages disagree on the version.
"""
import re
import sys
from datetime import datetime
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"
ASSET = re.compile(r'(/static/[\w.-]+\.(?:css|js))(?:\?v=[^"]*)?"')


def bump(version: str) -> None:
    for page in sorted(STATIC.glob("*.html")):
        html = page.read_text(encoding="utf-8")
        new = ASSET.sub(rf'\1?v={version}"', html)
        if new != html:
            page.write_text(new, encoding="utf-8", newline="")
        print(f"{page.name}: {len(ASSET.findall(new))} files now at ?v={version}")


VERSION = re.compile(r"[\w.]+")

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d.%H%M")
    # 23 Oct 2026: "--help" was taken for a version and written into every page as ?v=--help
    if arg in ("-h", "--help") or not VERSION.fullmatch(arg):
        print(__doc__.strip() + "\n\nOptional argument: a version made of letters, digits, '_' and '.' "
              "(default: the current date and time).")
        raise SystemExit(0 if arg in ("-h", "--help") else 2)
    bump(arg)
