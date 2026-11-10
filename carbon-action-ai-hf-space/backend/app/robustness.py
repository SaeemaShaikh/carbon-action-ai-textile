"""Stress tests beyond the single held-out month. Slow (several minutes), so run by hand:

    python -m app.robustness      (writes reports/robustness.json, which the app displays)

Part 1: one trained model per plant, scored over several unseen months per scenario:
        baseline, noisy meters, weak faults, overlapping faults, instrument faults.
Part 2: is the "actual saving" right? The simulator can run the same plant twice from the
        same moment, with and without the fix, so the true saving is known exactly.
"""
from __future__ import annotations

import copy
import json
import random
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .analytics import ACTIONS, DRIVERS, SENSORS, MachineModel, measure_saving
from .evaluation import FAULT_TO_DRIVER, ROOT, TEST_END, TRAIN_END, _clean, _source_hash, evaluate
from .simulator import FAULTS, SENSOR_FAULTS, TICK_MIN, generate_history, random_fault_schedule

REPORT = ROOT / "reports" / "robustness.json"
PRE_TICKS = 6


def _spread(values: list[float]) -> dict:
    v = [x for x in values if x is not None]
    return {"mean": round(float(np.mean(v)), 2), "min": round(float(np.min(v)), 2),
            "max": round(float(np.max(v)), 2)} if v else {"mean": None, "min": None, "max": None}


def _noisy(df: pd.DataFrame, seed: int, pct: float = 1.5) -> pd.DataFrame:
    """Meters with extra random error of about pct % on every reading."""
    rng = np.random.default_rng(seed)
    out = df.copy()
    cols = {c for t in SENSORS.values() for c in t} | {"fabric_processed_kg_h", "dry_fabric_kg_h"}
    for c in sorted(cols & set(out.columns)):  # fixed order: same noise on every run
        if c in ("fuel_rate", "electricity_kw"):
            continue
        out[c] = out[c] * (1 + rng.normal(0, pct / 100, len(out)))
    fuel = 1 + rng.normal(0, pct / 100, len(out))
    power = 1 + rng.normal(0, pct / 100, len(out))
    out["fuel_rate"] *= fuel
    out["electricity_kw"] *= power
    out["co2_direct_kg_h"] *= fuel
    out["co2_indirect_kg_h"] *= power
    out["co2_kg_h"] = out["co2_direct_kg_h"] + out["co2_indirect_kg_h"]
    return out


def overlap_schedule(seed: int, days: int = 30) -> list[tuple]:
    """Two faults on the same machine at once."""
    g = random.Random(seed)
    groups = {"boiler": [k for k, f in FAULTS.items() if f["machine"] == "boiler"],
              "stenter": [k for k, f in FAULTS.items() if f["machine"] == "stenter" and k != "stenter_idle_burners"]}
    out, day = [], 1.0
    while day < days - 2:
        for mt, kinds in groups.items():
            a, b = g.sample(kinds, 2)
            hour = g.randrange(6, 16)
            out.append((round(day, 2), hour, a, 12.0, round(g.uniform(0.7, 1.0), 2)))
            out.append((round(day, 2), hour + 3, b, 8.0, round(g.uniform(0.7, 1.0), 2)))
        day += 3.0
    return out


def evaluate_overlaps(model: MachineModel, g: pd.DataFrame) -> dict:
    g = g.reset_index(drop=True)
    ai = model.score(g)["anomaly"].astype(int).rolling(3, min_periods=1).sum() >= 2
    lbl = g["fault_label"]
    n = det = any_cause = both = i = 0
    while i < len(lbl):
        if "," not in lbl[i]:
            i += 1
            continue
        j = i
        while j < len(lbl) and lbl[j] == lbl[i]:
            j += 1
        want = {FAULT_TO_DRIVER[k] for k in lbl[i].split(",")}
        n += 1
        if bool(ai.iloc[max(i - 24, 0):j].any()):  # an alert already raised by the first fault also counts
            det += 1
            top = [d["driver"] for d in model.drivers(g.iloc[i + (j - i) // 2:i + (j - i) // 2 + 6])[:2]]
            any_cause += int(top[0] in want)
            both += int(want <= set(top))
        i = j
    return {"episodes": n, "detected": det, "top_cause_is_one_of_them": any_cause, "both_in_top_two": both}


def _fuse(reports: list[dict]) -> dict:
    """Combine evaluate() outputs (one per machine and test month, grouped by month)."""
    by_month: dict[int, list[dict]] = {}
    for month, r in reports:
        by_month.setdefault(month, []).append(r)
    det, rc, thr, delay, thr_delay, fa, thr_fa = [], [], [], [], [], [], []
    total_f = total_d = total_rc = total_t = 0
    missed: dict[str, int] = {}
    for rs in by_month.values():
        f = sum(r["faults"] for r in rs)
        d = sum(r["ai"]["detected"] for r in rs)
        c = sum(r["ai"]["root_cause_correct"] for r in rs)
        t = sum(r["threshold"]["detected"] for r in rs)
        total_f, total_d, total_rc, total_t = total_f + f, total_d + d, total_rc + c, total_t + t
        if f:
            det.append(d / f * 100)
            thr.append(t / f * 100)
        if d:
            rc.append(c / d * 100)
        delay += [r["ai"]["median_delay_min"] for r in rs]
        thr_delay += [r["threshold"]["median_delay_min"] for r in rs]
        fa += [r["ai"]["false_alarms_per_day"] for r in rs]
        thr_fa += [r["threshold"]["false_alarms_per_day"] for r in rs]
        for r in rs:
            for k in r["ai"]["missed"]:
                missed[k] = missed.get(k, 0) + 1
    return {"months": len(by_month), "faults": total_f, "detected": total_d, "root_cause_correct": total_rc,
            "threshold_detected": total_t, "detected_pct": _spread(det), "root_cause_pct": _spread(rc),
            "threshold_detected_pct": _spread(thr), "delay_min": _spread(delay), "threshold_delay_min": _spread(thr_delay),
            "false_alarms_per_machine_day": _spread(fa), "threshold_false_alarms_per_machine_day": _spread(thr_fa),
            "missed": missed}


def _sensor_test(fac: dict, models: dict, seed: int) -> list[dict]:
    out = []
    for kind, f in SENSOR_FAULTS.items():
        m = next(x for x in fac["machines"] if x["type"] == f["machine"])
        model = models[m["id"]]
        _, rows = generate_history(fac, TEST_END, 10, seed, schedule=[(5.0, 10, kind, 24.0, 1.0)])
        df = pd.DataFrame(rows)
        g = df[df.machine_id == m["id"]].reset_index(drop=True)
        start = int(g.index[g["timestamp"] >= g["timestamp"].iloc[0].normalize() + pd.Timedelta(days=5, hours=10)][0])
        flag = model.sensor_flag(model.sensor_scan(g))
        hit = flag.iloc[start:start + 288]
        ai = model.score(g)["anomaly"].astype(int).rolling(3, min_periods=1).sum() >= 2
        first = int(hit.values.argmax()) if hit.any() else None
        # a process alert raised before the instrument was flagged would blame the machine wrongly
        before = bool(ai.iloc[start:start + (first if first is not None else 288)].any())
        out.append({"kind": kind, "label": f["label"], "detected": bool(hit.any()),
                    "delay_min": None if first is None else first * TICK_MIN, "process_alert_first": before})
    return out


def _sensor_false_alarms(model: MachineModel, g: pd.DataFrame) -> float:
    f = model.sensor_flag(model.sensor_scan(g.reset_index(drop=True))).astype(bool).values
    return float(np.sum(f[1:] & ~f[:-1])) / (len(f) * TICK_MIN / 1440)


def stress(fac: dict, seed: int, months: int) -> dict:
    _, rows = generate_history(fac, TRAIN_END, 30, seed)
    train = pd.DataFrame(rows)
    models = {m["id"]: MachineModel(fac, m).fit(train[train.machine_id == m["id"]].reset_index(drop=True))
              for m in fac["machines"]}
    res: dict[str, list] = {"baseline": [], "noisy_meters": [], "weak_faults": []}
    overlaps, sensor_fa = [], []
    for k in range(months):
        ts = seed + 2000 + 37 * k
        _, rows = generate_history(fac, TEST_END, 30, ts, schedule=random_fault_schedule(ts, 30))
        base = pd.DataFrame(rows)
        noisy = _noisy(base, ts)
        _, rows = generate_history(fac, TEST_END, 30, ts + 1, schedule=random_fault_schedule(ts + 1, 30, min_sev=0.3, max_sev=0.6))
        weak = pd.DataFrame(rows)
        _, rows = generate_history(fac, TEST_END, 30, ts + 2, schedule=overlap_schedule(ts + 2))
        both = pd.DataFrame(rows)
        for m in fac["machines"]:
            mid, thr, model = m["id"], m["co2_threshold_kg_h"], models[m["id"]]
            for name, frame in (("baseline", base), ("noisy_meters", noisy), ("weak_faults", weak)):
                g = frame[frame.machine_id == mid].reset_index(drop=True)
                res[name].append((k, evaluate(model, g, thr)))
            overlaps.append(evaluate_overlaps(model, both[both.machine_id == mid]))
            sensor_fa.append(_sensor_false_alarms(model, base[base.machine_id == mid]))
        print(f"  {fac['id']}: month {k + 1}/{months} done", flush=True)
    return {"models": models, "res": res, "overlaps": overlaps, "sensor_false_alarms_per_machine_day": sensor_fa,
            "sensors": _sensor_test(fac, models, seed + 2500)}


def verification_test(fac: dict, models: dict, seed: int) -> list[dict]:
    """True saving = same plant, same moment, same random numbers, with vs without the fix."""
    base, _ = generate_history(fac, TEST_END, 30, seed, schedule=[])
    out = []
    for fault, driver in FAULT_TO_DRIVER.items():
        mtype = FAULTS[fault]["machine"]
        action = DRIVERS[mtype][driver]["action"]
        m = next(x for x in fac["machines"] if x["type"] == mtype)
        model = models[m["id"]]
        for sev in (0.7, 1.0):
            sim = copy.deepcopy(base)
            t = sim.now
            sim.add_fault(fault, t, None, sev, ramp_min=30)
            if fault == "stenter_idle_burners":
                sim._batch_left, sim._idle_left = 0, 18
            rows = []
            for _ in range(36):  # three hours for the fault to establish
                rows += [r for r in sim.step(t) if r["machine_id"] == m["id"]]
                t += pd.Timedelta(minutes=TICK_MIN).to_pytimedelta()
            pre = pd.DataFrame(rows).tail(36 if action == "idle_shutoff" else PRE_TICKS).reset_index(drop=True)
            window = pre if action == "idle_shutoff" else pre.tail(3)
            predicted = model.simulate(window, action)["saving"]["co2_kg_h"]
            with_fix, without = copy.deepcopy(sim), copy.deepcopy(sim)
            state = with_fix.rng.getstate()
            with_fix.apply_action(action, t)
            with_fix.rng.setstate(state)  # both runs see the same production and noise
            a_rows, b_rows = [], []
            tt = t
            hours = max(ACTIONS[action]["measure_h"], 8 if action == "idle_shutoff" else 0)
            for _ in range(int(hours * 60 / TICK_MIN)):
                a_rows += [r for r in with_fix.step(tt) if r["machine_id"] == m["id"]]
                b_rows += [r for r in without.step(tt) if r["machine_id"] == m["id"]]
                tt += pd.Timedelta(minutes=TICK_MIN).to_pytimedelta()
            post = pd.DataFrame(a_rows)
            if action == "idle_shutoff" and (post["status"] == "idle").sum() < 3:
                continue
            true_saving = float(pd.DataFrame(b_rows)["co2_kg_h"].mean() - post["co2_kg_h"].mean())
            measured = measure_saving(model, pre, post, action)
            out.append({"factory": fac["id"], "machine": m["id"], "fault": fault, "action": action, "severity": sev,
                        "true_kg_h": round(true_saving, 1), "measured_kg_h": round(float(measured), 1),
                        "predicted_kg_h": round(float(predicted), 1)})
    return out


def _error_stats(cases: list[dict], key: str) -> dict:
    use = [c for c in cases if abs(c["true_kg_h"]) >= 1]
    err = [abs(c[key] - c["true_kg_h"]) / abs(c["true_kg_h"]) * 100 for c in use]
    bias = [(c[key] - c["true_kg_h"]) / abs(c["true_kg_h"]) * 100 for c in use]
    return {"cases": len(use), "median_error_pct": round(float(np.median(err)), 1),
            "mean_bias_pct": round(float(np.mean(bias)), 1),
            "within_20pct": sum(e <= 20 for e in err), "within_50pct": sum(e <= 50 for e in err)}


def run(cfg: dict, months: int = 5) -> dict:
    all_res: dict[str, list] = {"baseline": [], "noisy_meters": [], "weak_faults": []}
    overlaps, sensors, sensor_fa, cases = [], [], [], []
    for i, fac in enumerate(cfg["factories"]):
        print(f"{fac['id']}: stress test", flush=True)
        s = stress(fac, 7 + i, months)
        offset = 100 * i
        for name, items in s["res"].items():
            all_res[name] += [(month + offset, r) for month, r in items]
        overlaps += s["overlaps"]
        sensors += [{**x, "factory": fac["id"]} for x in s["sensors"]]
        sensor_fa += s["sensor_false_alarms_per_machine_day"]
        print(f"{fac['id']}: verification test", flush=True)
        for k in range(2):
            cases += verification_test(fac, s["models"], 7 + i + 3000 + k)
    return _clean({
        "built": datetime.now().isoformat(timespec="seconds"), "code_key": _source_hash(cfg), "months_per_plant": months,
        "scenarios": {k: _fuse(v) for k, v in all_res.items()},
        "overlapping": {k: sum(o[k] for o in overlaps) for k in overlaps[0]},
        "instruments": {"cases": sensors, "detected": sum(x["detected"] for x in sensors), "total": len(sensors),
                        "process_alert_first": sum(x["process_alert_first"] for x in sensors),
                        "false_alarms_per_machine_day": _spread(sensor_fa)},
        "verification": {"measured": _error_stats(cases, "measured_kg_h"), "predicted": _error_stats(cases, "predicted_kg_h"),
                         "cases": cases},
    })


def main():
    cfg = json.loads((ROOT / "config/factories.json").read_text(encoding="utf-8"))
    months = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    rep = run(cfg, months)
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(json.dumps({k: rep[k] for k in ("scenarios", "overlapping")}, indent=1))
    print({k: v for k, v in rep["instruments"].items() if k != "cases"})
    print(rep["verification"]["measured"], rep["verification"]["predicted"])


def load(cfg: dict) -> dict | None:
    if not REPORT.exists():
        return None
    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    rep["current"] = rep.get("code_key") == _source_hash(cfg)
    return rep


if __name__ == "__main__":
    main()
