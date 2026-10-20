#!/usr/bin/env python3
"""Add strategies that exist in the shipped config but are missing from yours - and change nothing else.

Why this exists: keeping your own `config/` across package upgrades protects the settings you have tuned, but it
also means a NEW strategy shipped in a later package never reaches your running bot, because a strategy only
exists if it has an entry in `config/strategies.json`. The code arrives; the entry does not. The strategy is then
invisible everywhere - no diagnostics row, no scoreboard row - which looks identical to a strategy that is broken.

What this does, and only this:
  * adds any strategy id present in the reference file and absent from yours, appended in reference order
  * leaves every strategy you already have EXACTLY as it is - status, enabled, params, risk_multiplier, stats
  * writes a timestamped backup of your file first
  * prints a diff summary and exits non-zero if nothing changed, so it is safe to re-run

It never removes, reorders, re-enables or re-parameterises anything. Run with --dry-run to see the plan first.

    python tools/merge_strategies.py --mine config/strategies.json --reference /path/to/new/config/strategies.json
    python tools/merge_strategies.py --dry-run          # uses the default paths above
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import shutil
import sys


def load(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        sys.exit(f"not found: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("strategies"), list):
        sys.exit(f"{path} does not look like a strategies file (expected a 'strategies' list)")
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mine", default="config/strategies.json", help="the file your bot actually reads")
    ap.add_argument("--reference", default="config/strategies.json.new",
                    help="the shipped file from the new package")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    a = ap.parse_args()

    mine_path, ref_path = pathlib.Path(a.mine), pathlib.Path(a.reference)
    if mine_path.resolve() == ref_path.resolve():
        sys.exit("--mine and --reference are the same file; point --reference at the new package's copy")

    mine, ref = load(mine_path), load(ref_path)
    have = {s.get("id") for s in mine["strategies"]}
    missing = [s for s in ref["strategies"] if s.get("id") not in have]

    print(f"yours     : {mine_path}  ({len(mine['strategies'])} strategies)")
    print(f"reference : {ref_path}  ({len(ref['strategies'])} strategies)")
    print()
    for s in mine["strategies"]:
        print(f"  keep  {s.get('id'):<22} {s.get('status'):<12} enabled={s.get('enabled', True)}  (untouched)")
    if not missing:
        print("\nnothing to add - your file already has every strategy in the reference.")
        return 1
    for s in missing:
        print(f"  ADD   {s.get('id'):<22} {s.get('status'):<12} class={s.get('class_name')} "
              f"risk_multiplier={s.get('risk_multiplier', 1.0)}")

    if a.dry_run:
        print(f"\n--dry-run: nothing written. {len(missing)} strategy(ies) would be added.")
        return 0

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = mine_path.with_suffix(f".json.backup-{stamp}")
    shutil.copy2(mine_path, backup)

    merged = dict(mine)
    merged["strategies"] = list(mine["strategies"]) + missing
    tmp = mine_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))          # never leave a half-written config behind
    tmp.replace(mine_path)

    print(f"\nbackup  : {backup}")
    print(f"written : {mine_path}  ({len(merged['strategies'])} strategies)")
    print("\nRestart the bot for it to take effect, then check the Analysis page - every strategy should now have a"
          "\nscoreboard row, including ones showing 'no signals yet'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
