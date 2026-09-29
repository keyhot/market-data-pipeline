"""Shared results-JSON writer for the report scripts (KI-097).

`archive_report.py`, `cross_asset_report.py`, `order_flow_report.py` and
`funding_report.py` used to print a verdict and persist nothing — E12 stage
B came to exist only as vault prose once its worktree (and gitignored
caches) were gone. `write_result` gives each of them the one place P1
(`cost_label_report.py`) and E13 (`cross_section_report.py`) already had:
a JSON file in `scripts/results/` that outlives the worktree, with the git
hash and a timestamp added so the file also says what code produced it.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _default(obj: Any):
    """`json.dumps(default=...)` hook for the numpy scalars every one of
    these reports produces (auc()/np.percentile/np.mean all return them) —
    plain `json.dumps` raises TypeError on np.float64/np.int64/np.bool_
    after the expensive walk-forward has already run."""
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def _git_hash() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def write_result(name: str, *, inputs: dict, parameters: dict, statistic: dict,
                 ci: Any, verdict: str, extra: dict | None = None,
                 results_dir: Path | None = None) -> Path:
    """Persist one experiment's verdict as JSON at `<results_dir>/<name>.json`.

    `results_dir` defaults to the module-level `RESULTS_DIR` (read at call
    time, not bind time, so tests can `monkeypatch.setattr` it) rather than
    `scripts/results/` directly, so a test never writes into the real
    results directory.
    """
    payload = {
        "experiment": name,
        "git_hash": _git_hash(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "inputs": inputs,
        "parameters": parameters,
        "statistic": statistic,
        "ci": ci,
        "verdict": verdict,
    }
    if extra:
        payload["extra"] = extra
    out_dir = results_dir if results_dir is not None else RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, default=_default) + "\n")
    return path
