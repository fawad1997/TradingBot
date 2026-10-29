#!/usr/bin/env python3
"""Database backup: a compressed pg_dump of the whole Quantara database into backups/.

Nothing in the database is ever deleted, and every dump holds ALL records from the first day, so older dumps are
only fallbacks: the newest `--keep-daily` dumps are kept plus the first dump of every month, forever. Each new dump
is checked with pg_restore --list before older ones are pruned.

    python tools/backup_db.py
    python tools/backup_db.py --keep-daily 30

Restore (into an EMPTY database): pg_restore -d <database> backups/quantara_<stamp>.dump
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BACKUP_DIR = ROOT / "backups"
PREFIX = "quantara_"


def find_tool(name: str) -> Optional[str]:
    """PostgreSQL client tools are usually not on PATH on Windows; use the newest installed version."""
    found = shutil.which(name)
    if found:
        return found
    installs = [p for p in Path(r"C:\Program Files\PostgreSQL").glob(f"*/bin/{name}.exe")]
    installs.sort(key=lambda p: int(p.parts[-3]) if p.parts[-3].isdigit() else 0)
    return str(installs[-1]) if installs else None


def dump(database_url: str, out_dir: Path, now: datetime) -> Path:
    pg_dump, pg_restore = find_tool("pg_dump"), find_tool("pg_restore")
    if not pg_dump or not pg_restore:
        raise SystemExit("pg_dump / pg_restore not found - install the PostgreSQL client tools or add them to PATH")
    u = urlsplit(database_url)
    env = {**os.environ, "PGPASSWORD": unquote(u.password or "")}      # never on the command line
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{PREFIX}{now:%Y-%m-%d_%H%M}.dump"
    partial = target.with_suffix(".partial")
    cmd = [pg_dump, "--format=custom", "--compress=6", "--no-owner", "-h", u.hostname or "localhost",
           "-p", str(u.port or 5432), "-U", unquote(u.username or ""), "-f", str(partial), u.path.lstrip("/")]
    subprocess.run(cmd, env=env, check=True, capture_output=True, text=True, timeout=3600)
    listed = subprocess.run([pg_restore, "--list", str(partial)], check=True, capture_output=True, text=True,
                            timeout=600).stdout
    if "TABLE DATA" not in listed:
        raise SystemExit(f"backup {partial} lists no table data - not keeping it")
    partial.replace(target)
    return target


def prune(out_dir: Path, keep_daily: int) -> List[Path]:
    """Keep the newest `keep_daily` dumps and the first dump of each month; return what was removed."""
    dumps = sorted(out_dir.glob(f"{PREFIX}*.dump"))           # names sort by time
    keep = set(dumps[-keep_daily:]) if keep_daily > 0 else set()
    first_of_month = {}
    for p in dumps:
        first_of_month.setdefault(p.name[len(PREFIX):len(PREFIX) + 7], p)
    keep |= set(first_of_month.values())
    removed = [p for p in dumps if p not in keep]
    for p in removed:
        p.unlink()
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-daily", type=int, default=14, help="newest dumps to keep besides monthly ones (14)")
    args = ap.parse_args()
    from app.config import load_secrets
    url = load_secrets().database_url
    if not url:
        raise SystemExit("DATABASE_URL is not set in .env")
    path = dump(url, BACKUP_DIR, datetime.now(timezone.utc))
    print(f"backup written: {path} ({path.stat().st_size / 1e6:.1f} MB)")
    for p in prune(BACKUP_DIR, args.keep_daily):
        print(f"removed older backup: {p.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
