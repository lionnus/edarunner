"""The metric hook of the energy of one power task."""

import csv
import json
from pathlib import Path


def energy_nj(path):
    """The WHOLE power in W of `reports/power.csv` times `window_ns` of `path`, the task's phases.json."""
    path = Path(path)
    window_ns = json.loads(path.read_text())["window_ns"]
    with open(path.parent / "reports" / "power.csv", newline="") as fh:
        power_w = next((float(r["total_w"]) for r in csv.DictReader(fh) if r["phase"] == "WHOLE"), None)
    if power_w is None:
        raise ValueError("power.csv has no WHOLE row")
    return power_w * window_ns
