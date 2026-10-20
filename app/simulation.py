"""Simulation harness: the full trading core on a replayed (synthetic or CSV)
market, with a paper broker standing in for MT5. Used by the test-suite and by
`python -m app.main run --simulate` (see the pipeline + dashboard work without MT5).

Results on synthetic data say NOTHING about real profitability.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from app.broker.paper import PaperBroker
from app.broker.sim import SimMarket, synthetic_bars
from app.config import CONFIG_DIR, ConfigManager, Secrets
from app.core.bridge import Bridge
from app.core.engine import TradingCore
from app.core.types import Bar
from app.storage.repo import MemoryRepo, Repository


def sim_config_dir(env: str = "DEMO", overrides: Optional[dict] = None, src: Path = CONFIG_DIR) -> Path:
    d = Path(tempfile.mkdtemp(prefix="ata_cfg_"))
    shutil.copy(src / "strategies.json", d / "strategies.json")
    cfg = json.loads((src / "config.json").read_text(encoding="utf-8"))
    cfg["environment"] = env
    cfg["health"]["stale_tick_sec"] = 3600          # simulated ticks arrive every 75 simulated seconds
    cfg["broker"]["reconnect_max_attempts"] = 1
    cfg["broker"]["broker_utc_offset_hours"] = 0
    cfg["broker"]["symbol_candidates"] = ["XAUUSD"]  # simulator has one synthetic market
    cfg["hydration"]["ema_warmup_multiple"] = 1.0    # keeps simulated history short (tests only)
    cfg["risk"]["max_concurrent_positions"] = 1       # keep single-symbol chaos tests deterministic
    for path, value in (overrides or {}).items():
        cur = cfg
        parts = path.split(".")
        for p in parts[:-1]:
            cur = cur[p]
        cur[parts[-1]] = value
    (d / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return d


def make_sim_core(env: str = "DEMO", bars: Optional[List[Bar]] = None, n_bars: int = 9000, seed: int = 11,
                  balance: float = 100.0, repo: Optional[Repository] = None, overrides: Optional[dict] = None,
                  warmup_bars: int = 7000, broker: Optional[PaperBroker] = None, cfg_dir: Optional[Path] = None,
                  bridge: Optional[Bridge] = None) -> Tuple[TradingCore, SimMarket, PaperBroker, Path]:
    """Build a core positioned `warmup_bars` into the data (enough history to hydrate)."""
    if broker is None:
        bars = bars or synthetic_bars(n_bars, seed=seed)
        sim = SimMarket(bars, base_seconds=300, account_mode=env if env in ("DEMO", "REAL") else "DEMO",
                        balance=balance)
        sim.seek(warmup_bars)
        broker = PaperBroker(sim, starting_balance=balance, slippage_points=2.0,
                             account_mode="DEMO" if env in ("DEMO", "DRY_RUN", "SHADOW") else env)
    sim = broker.data
    cfg_dir = cfg_dir or sim_config_dir(env, overrides)
    cfgm = ConfigManager(cfg_dir / "config.json", cfg_dir / "strategies.json")
    repo = repo if repo is not None else MemoryRepo()
    core = TradingCore(cfgm, broker, repo, Secrets(), bridge=bridge, exec_broker=broker,
                       journal_path=cfg_dir / "journal.jsonl", wall=lambda: sim.now)
    return core, sim, broker, cfg_dir


def run_sim(core: TradingCore, sim: SimMarket, ticks: int) -> int:
    n = 0
    for _ in range(ticks):
        if not sim.step():
            break
        core.step()
        n += 1
    return n
