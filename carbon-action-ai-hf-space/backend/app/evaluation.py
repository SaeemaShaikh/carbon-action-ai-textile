"""Back-test: AI anomaly detection vs a plain CO2 threshold alarm.

Two views are reported for every machine:
  in_sample : scored on the same 30-day history the models were trained on
  held_out  : models trained on one history, scored on a *different* 30 days
              (new random seed, new randomised fault schedule, weaker faults).
              This is the honest number.

CLI:  python -m app.evaluation      (writes data/model_report.json)
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .analytics import MachineModel
from .simulator import generate_history, random_fault_schedule

ROOT = Path(__file__).resolve().parents[1]
REPORT_VERSION = 2

FAULT_TO_DRIVER = {
    "boiler_excess_air": "excess_air", "boiler_fouling": "fouling", "boiler_steam_leak": "steam_loss",
    "boiler_economizer": "feedwater", "stenter_damper_open": "exhaust_damper",
    "stenter_filter_clog": "airflow", "stenter_overdrying": "overdrying",
    "stenter_idle_burners": "idle_firing", "stenter_low_padder": "inlet_moisture",
}
TRAIN_END = datetime(2026, 10, 1, 13, 0)
TEST_END = datetime(2026, 10, 31, 13, 0)


def events(g: pd.DataFrame):
    """Contiguous single-fault episodes -> list of (kind, start_idx, end_idx).
    Idle-burner faults are only observable while the stenter is idle, so an
    episode that never overlaps an idle period is skipped."""
    lbl = g["fault_label"]
    out, cur, start = [], None, None
    for i, v in enumerate(lbl):
        if v != cur:
            if cur not in (None, "normal"):
                out.append((cur, start, i))
            cur, start = v, i
    if cur not in (None, "normal"):
        out.append((cur, start, len(lbl)))
    keep = []
    for kind, s, e in out:
        if "," in kind:
            continue
        if kind == "stenter_idle_burners" and not (g["status"].iloc[s:e] == "idle").any():
            continue
        keep.append((kind, s, e))
    return keep


def _alarm_episodes(flags: pd.Series) -> int:
    f = flags.astype(bool).values
    return int(np.sum(f[1:] & ~f[:-1]) + (1 if len(f) and f[0] else 0))


def evaluate(model: MachineModel, g: pd.DataFrame, threshold: float) -> dict:
    g = g.reset_index(drop=True)
    sc = model.score(g)
    # the live engine raises an alert when 2 of the last 3 readings are anomalous
    ai = sc["anomaly"].astype(int).rolling(3, min_periods=1).sum() >= 2
    thr = g["co2_kg_h"] > threshold
    ev = events(g)
    det = thr_det = rc = 0
    d_ai, d_thr, missed = [], [], []
    for kind, s, e in ev:
        hit = ai.iloc[s:e]
        if hit.any():
            det += 1
            first = int(hit.values.argmax())
            d_ai.append(first * 5)
            dr = model.drivers(g.iloc[max(s + first - 2, s): s + first + 4])
            if dr and dr[0]["driver"] == FAULT_TO_DRIVER[kind]:
                rc += 1
        else:
            missed.append(kind)
        th = thr.iloc[s:e]
        if th.any():
            thr_det += 1
            d_thr.append(int(th.values.argmax()) * 5)
    # "normal" = no fault label and not inside the 2 h after a fault (fault effects linger briefly)
    lab = g["fault_label"] != "normal"
    recent = lab.astype(int).rolling(24, min_periods=1).max().astype(bool)
    normal = ~recent
    days = len(g) * 5 / 1440
    med = lambda x: None if not x else float(pd.Series(x).median())  # noqa: E731
    return {
        "faults": len(ev),
        "ai": {"detected": det, "median_delay_min": med(d_ai),
               "false_alarm_pct": round(float(ai[normal].mean()) * 100, 2),
               "false_alarms_per_day": round(_alarm_episodes(ai & normal) / days, 2),
               "root_cause_correct": rc, "missed": missed},
        "threshold": {"detected": thr_det, "median_delay_min": med(d_thr),
                      "false_alarm_pct": round(float(thr[normal].mean()) * 100, 2),
                      "false_alarms_per_day": round(_alarm_episodes(thr & normal) / days, 2)},
        "model": model.metrics,
    }


def _source_hash(cfg: dict) -> str:
    h = hashlib.sha256()
    for name in ("analytics.py", "simulator.py", "evaluation.py"):
        h.update((ROOT / "app" / name).read_bytes())
    h.update(json.dumps(cfg.get("factories", []), sort_keys=True).encode())
    h.update(str(REPORT_VERSION).encode())
    return h.hexdigest()[:16]


def factory_report(fac: dict, seed: int = 7) -> dict:
    """Train on one 30-day history, score in-sample and on an independent 30 days."""
    _, train_rows = generate_history(fac, TRAIN_END, days=30, seed=seed)
    test_seed = seed + 1000
    _, test_rows = generate_history(fac, TEST_END, days=30, seed=test_seed,
                                    schedule=random_fault_schedule(test_seed, days=30))
    train, test = pd.DataFrame(train_rows), pd.DataFrame(test_rows)
    rep = {}
    for m in fac["machines"]:
        g_tr = train[train.machine_id == m["id"]].reset_index(drop=True)
        g_te = test[test.machine_id == m["id"]].reset_index(drop=True)
        model = MachineModel(fac, m).fit(g_tr)
        rep[m["id"]] = {"in_sample": evaluate(model, g_tr, m["co2_threshold_kg_h"]),
                        "held_out": evaluate(model, g_te, m["co2_threshold_kg_h"])}
    return rep


def load_or_build(cfg: dict, path: Path, fid: str | None = None) -> dict:
    """Cached report, rebuilt automatically when the model code or config changes."""
    key = _source_hash(cfg)
    cache = {}
    if path.exists():
        try:
            cache = json.loads(path.read_text())
        except ValueError:
            cache = {}
    if cache.get("_key") != key:
        cache = {"_key": key, "_built": datetime.now().isoformat(timespec="seconds")}
    changed = False
    for i, fac in enumerate(cfg["factories"]):
        if (fid is None or fac["id"] == fid) and fac["id"] not in cache:
            cache[fac["id"]] = _clean(factory_report(fac, seed=7 + i))
            changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2))
    return cache


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.generic):
        return o.item()
    return o


def main():
    cfg = json.loads((ROOT / "config/factories.json").read_text())
    data_dir = Path(os.getenv("CARBON_DATA_DIR", ROOT / "data"))
    rep = load_or_build(cfg, data_dir / "model_report.json")
    for fid, machines in rep.items():
        if fid.startswith("_"):
            continue
        for mid, r in machines.items():
            for view in ("in_sample", "held_out"):
                v = r[view]
                print(f"{fid}/{mid} {view:9s} faults={v['faults']:2d}  AI {v['ai']['detected']:2d} "
                      f"(delay {v['ai']['median_delay_min']} min, root cause {v['ai']['root_cause_correct']}, "
                      f"FA/day {v['ai']['false_alarms_per_day']}, missed {v['ai']['missed']})  "
                      f"threshold {v['threshold']['detected']:2d} (delay {v['threshold']['median_delay_min']} min, "
                      f"FA/day {v['threshold']['false_alarms_per_day']})")


if __name__ == "__main__":
    main()
