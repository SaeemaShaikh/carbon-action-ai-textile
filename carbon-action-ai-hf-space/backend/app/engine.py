"""Live engine: runs each factory's simulator, scores every new reading,
raises alerts, tracks actions and learns from verified outcomes."""
from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import safety
from .analytics import ACTIONS, DRIVERS, MachineModel, forecast, measure_saving, rank_actions
from .datasource import CsvReplay
from .db import Store
from .simulator import FAULTS, SENSOR_FAULTS, TICK_MIN, generate_history

DATA_DIR = Path(os.getenv("CARBON_DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
LEARN_FILE = DATA_DIR / "learning.json"
REPLAY_SECONDS = float(os.getenv("REPLAY_SECONDS", "2"))  # real seconds per reading when replaying an upload
PRE_TICKS = 6  # readings just before the change that form the M&V baseline
AUTO_FAULTS = os.getenv("AUTO_FAULTS", "1") == "1"
FAULT_EVERY_H = float(os.getenv("FAULT_EVERY_HOURS", "8"))  # average gap between new faults per plant
# faults operators tend to correct by themselves clear after a few hours; the rest last until fixed
TRANSIENT_FAULTS = {"boiler_excess_air", "stenter_damper_open", "stenter_overdrying"}
SENSOR_FAULT_SHARE = 0.1  # share of automatic faults that hit an instrument instead of the machine
# The simulated crew reports work done at the planned time; sometimes the job did not really work.
SIM_CREW_MISS = float(os.getenv("SIM_CREW_MISS", "0.05"))
CONFIRM_TICKS = 6  # readings after "done" before checking that the sensors show the change
ACTIVE = ("Assigned", "Measuring")
# evidence that the work really happened: column, direction it should move, smallest real move
ACTION_CHECK = {
    "tune_o2": ("flue_o2_pct", -1, 0.15), "soot_blow": ("exhaust_dev_c", -1, 3.0), "repair_leaks": ("steam_ratio", -1, 0.01),
    "restore_condensate": ("feedwater_temp_c", 1, 1.0), "humidity_control": ("exhaust_damper_pct", -1, 2.0),
    "clean_filters": ("airflow_pct", 1, 1.0), "reduce_temperature": ("outlet_moisture_pct", 1, 0.2),
    "idle_shutoff": ("chamber_temp_c", -1, 5.0), "raise_padder": ("inlet_moisture_pct", -1, 0.5),
}
DEFAULT_LEARN = {"calibration": 1.0, "n": 0, "accuracy": [], "ratios": [], "confidence": 0.7}


def learn_entry(learning: dict, key: str, action: str) -> dict:
    """What has been learned for one action on one machine (key = "factory/machine").
    With no verified result yet, borrow the average of other machines as a weaker
    prior; with nothing at all, fall back to the default."""
    own = learning.get(key, {}).get(action)
    if own and own["n"] > 0:
        return {**DEFAULT_LEARN, **own, "source": "this machine"}
    others = [v[action] for k, v in learning.items() if k != key and action in v and v[action]["n"] > 0]
    if others:
        cal = float(np.mean([o["calibration"] for o in others]))
        conf = max(0.6, float(np.mean([o["confidence"] for o in others])) - 0.1)
        ratios = [r for o in others for r in o.get("ratios", [])][-10:]
        return {**DEFAULT_LEARN, "calibration": round(cal, 3), "confidence": round(conf, 2),
                "ratios": ratios, "source": "similar machines"}
    return {**DEFAULT_LEARN, "source": "model only"}


def saving_range(saving_kg_h: float, entry: dict) -> list[float]:
    """Likely range of the real saving from the spread of past real/predicted
    ratios (±25 % until there are at least 3 verified results)."""
    r = entry.get("ratios") or []
    rel = float(np.std(r) / max(entry["calibration"], 1e-6)) if len(r) >= 3 else 0.25
    rel = min(max(rel, 0.08), 0.6)
    return [round(saving_kg_h * (1 - rel), 1), round(saving_kg_h * (1 + rel), 1)]


class FactoryRuntime:
    def __init__(self, fac: dict, now: datetime, seed: int, store: Store | None = None,
                 replay: CsvReplay | None = None):
        self.fac = fac
        self.fid = fac["id"]
        self._memo: dict = {}
        self.store = store
        self.alerts: list[dict] = []
        self.actions: list[dict] = []
        self.open_alert: dict[str, dict] = {}
        self.sensor_alert: dict[str, dict] = {}
        self._sensor_clear: dict[str, int] = {}
        self.crew_miss = SIM_CREW_MISS
        self.restored = False
        self.upload_warnings: list[str] = []
        self._saved_rows: list[dict] = []
        # the live plant develops faults on its own (the AI is never told which)
        self._frng = random.Random(seed * 101)
        if replay is not None:
            self.sim, self.df, self.now = replay, replay.history.reset_index(drop=True), replay.start
        elif not self._restore(now):
            self.sim, rows = generate_history(fac, now, days=30, seed=seed)
            self.now = self.sim.now
            self.df = pd.DataFrame(rows)
            if store:
                store.save_rows(self.fid, rows)
        self.models: dict[str, MachineModel] = {}
        for m in fac["machines"]:
            g = self.df[self.df.machine_id == m["id"]].reset_index(drop=True)
            self.models[m["id"]] = MachineModel(fac, m).fit(g)
        if not self.restored:
            self._seed_alert_history()
            self.next_fault_at = self.now + timedelta(minutes=self._frng.uniform(15, 45))
        if store:
            self.persist()

    def _restore(self, wall: datetime) -> bool:
        """Continue from the saved plant if there is one, filling any gap while the app was off."""
        st = self.store.load_state(self.fid) if self.store else None
        if not st:
            return False
        if st["now"] > wall + timedelta(minutes=TICK_MIN) or wall - st["now"] > timedelta(days=45):
            # fast-forwarded past the real clock, or off for too long: this history no longer fits today
            print(f"{self.fid}: saved plant does not fit the current date; starting fresh.")
            self.store.clear_factory(self.fid)
            return False
        self.sim, self.now = st["sim"], st["now"]
        self._frng = st["frng"]
        self.next_fault_at = st["next_fault_at"]
        rows = self.store.load_rows(self.fid, since=wall - timedelta(days=45))
        gap = []
        while self.now + timedelta(minutes=TICK_MIN) <= wall:
            gap.extend(self.sim.step(self.now))
            self.now += timedelta(minutes=TICK_MIN)
        if gap:
            print(f"{self.fid}: filled {len(gap) // len(self.fac['machines']) * TICK_MIN} minutes while the app was off.")
            self.store.save_rows(self.fid, gap)
        self.df = pd.DataFrame(rows + [{**r, "timestamp": r["timestamp"].isoformat()} for r in gap])
        self.df["timestamp"] = pd.to_datetime(self.df["timestamp"])
        self.alerts = self.store.load_records("alert", self.fid)
        self.actions = self.store.load_records("action", self.fid)
        self.open_alert = {a["machine_id"]: a for a in self.alerts
                           if not a["resolved"] and a["kind"] == "anomaly"}
        self.sensor_alert = {a["machine_id"]: a for a in self.alerts
                             if not a["resolved"] and a["kind"] == "sensor"}
        self.restored = True
        return True

    def persist(self):
        """Save what changed. Replayed uploads are not stored."""
        if not self.store or self.sim.kind != "simulator":
            return
        if self._saved_rows:
            self.store.save_rows(self.fid, self._saved_rows)
            self._saved_rows = []
        self.store.save_records("alert", self.fid, self.alerts)
        self.store.save_records("action", self.fid, self.actions)
        self.store.save_state(self.fid, {"sim": self.sim, "now": self.now, "frng": self._frng,
                                         "next_fault_at": self.next_fault_at})
        self.store.commit()

    # ------------------------------------------------------------------
    def memo(self, key, fn):
        """Per-tick cache: results only change when the plant ticks or a decision is made."""
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    def lkey(self, mid: str) -> str:
        return f"{self.fid}/{mid}"

    def machine(self, mid: str) -> dict | None:
        return next((m for m in self.fac["machines"] if m["id"] == mid), None)

    def machine_df(self, mid: str, last: int | None = None) -> pd.DataFrame:
        g = self.df[self.df.machine_id == mid]
        return (g.tail(last) if last else g).reset_index(drop=True)

    def _seed_alert_history(self):
        """Show a few past (resolved) alerts so the panel isn't empty at start."""
        for m in self.fac["machines"]:
            g = self.machine_df(m["id"], last=288)
            sc = self.models[m["id"]].score(g)
            on = sc["anomaly"].values
            i = 0
            while i < len(on):
                if on[i]:
                    j = i
                    while j < len(on) and on[j]:
                        j += 1
                    if j - i >= 3:
                        win = g.iloc[i:i + 6]
                        d = self.models[m["id"]].drivers(win)
                        self.alerts.append(self._make_alert(m, g.iloc[i], sc.iloc[i], d, resolved=True,
                                                            resolved_at=g.iloc[min(j, len(g) - 1)]["timestamp"]))
                    i = j
                i += 1

    def _make_alert(self, m, row, sc, drivers, resolved=False, resolved_at=None, kind="anomaly"):
        top = drivers[0] if drivers else None
        excess = float(sc["excess_pct"])
        sev = "High" if excess >= 10 else "Medium" if excess >= 5 else "Low"
        if kind == "threshold":
            sev = "High"
        title = {
            "boiler": "Boiler is using more fuel than it should", "stenter": "Stenter is using more energy than it should"}[m["type"]]
        if kind == "threshold":
            title = f"{m['name']} above CO2 threshold"
        msg = (f"Releasing {row['co2_kg_h']:.0f} kg of CO2 per hour, {excess:.0f}% more than expected "
               f"({sc['expected_co2']:.0f} kg/h) for the fabric being made.")
        if top and top["contribution_kg_h"] > 0.5:
            msg += (f" Most likely cause: {top['label'].lower()}. {top['evidence_label']} is {top['current']:,.1f} "
                    f"{top['unit']}; healthy is {top['healthy']:,.1f} {top['unit']}.")
        return {"id": uuid.uuid4().hex[:8], "machine_id": m["id"], "machine": m["name"],
                "machine_type": m["type"], "kind": kind, "severity": sev, "title": title,
                "message": msg, "time": pd.Timestamp(row["timestamp"]).isoformat(),
                "excess_pct": round(excess, 1), "co2_kg_h": round(float(row["co2_kg_h"]), 1),
                "expected_kg_h": round(float(sc["expected_co2"]), 1),
                "drivers": drivers[:4], "resolved": resolved,
                "resolved_at": None if resolved_at is None else pd.Timestamp(resolved_at).isoformat(),
                "acknowledged": resolved}

    # ------------------------------------------------------------------
    def _maybe_start_fault(self):
        """Random faults at a realistic rate, at most one active per machine."""
        if not AUTO_FAULTS or self.sim.kind != "simulator" or self.now < self.next_fault_at:
            return
        self.next_fault_at = self.now + timedelta(hours=self._frng.expovariate(1 / FAULT_EVERY_H) + 0.5)
        busy = {FAULTS[k]["machine"] for k in self.sim.active_faults(self.now)}
        busy |= {self.machine(a["machine_id"])["type"] for a in self.actions if a["status"] in ACTIVE}
        if self._frng.random() < SENSOR_FAULT_SHARE:
            if not self.sim.sensor_faults or all(f["end"] and f["end"] <= self.now
                                                 for f in self.sim.sensor_faults.values()):
                self.sim.sensor_faults.clear()
                self.sim.add_sensor_fault(self._frng.choice(list(SENSOR_FAULTS)), self.now, self._frng.uniform(6, 12))
            return
        kinds = [k for k, f in FAULTS.items() if f["machine"] not in busy]
        if not kinds:
            return
        kind = self._frng.choice(kinds)
        hours = self._frng.uniform(3, 10) if kind in TRANSIENT_FAULTS else None
        self.sim.add_fault(kind, self.now, hours, round(self._frng.uniform(0.6, 1.0), 2),
                           ramp_min=int(self._frng.uniform(20, 90)))

    def tick(self, learning: dict) -> list[dict]:
        if self.sim.kind == "csv" and self.sim.finished:
            return []
        self._memo.clear()
        self._maybe_start_fault()
        rows = self.sim.step(self.now)
        if not rows:  # a gap in uploaded data: nothing to score at this time
            self.now += timedelta(minutes=TICK_MIN)
            return []
        self._saved_rows.extend(rows)
        self.df = pd.concat([self.df, pd.DataFrame(rows)], ignore_index=True)
        cutoff = self.now - timedelta(days=45)
        if len(self.df) > 30000:
            self.df = self.df[pd.to_datetime(self.df.timestamp) > cutoff].reset_index(drop=True)
        new_alerts = []
        for m in self.fac["machines"]:
            mid = m["id"]
            g = self.machine_df(mid, last=12)
            sc = self.models[mid].score(g)
            last = sc.iloc[-1]
            anomalous = bool(sc["anomaly"].iloc[-3:].sum() >= 2)
            row = g.iloc[-1]
            issues = self.models[mid].sensor_health(self.machine_df(mid, last=20))
            new_alerts.extend(self._update_sensor_alert(m, issues, row))
            cur = self.open_alert.get(mid)
            if cur is not None and any(i["problem"] in ("disagree", "disagrees with fan") for i in issues):
                # the numbers themselves are in doubt: don't keep telling people the machine is at fault
                cur.update({"resolved": True, "resolved_at": pd.Timestamp(row["timestamp"]).isoformat(),
                            "message": cur["message"] + " Withdrawn: the readings behind this alert look unreliable."})
                self.open_alert.pop(mid, None)
                cur = None
            if anomalous and cur is None and issues:
                pass  # no new process alert while the instruments are in doubt
            elif anomalous and cur is None:
                d = self.models[mid].drivers(g.tail(6))
                a = self._make_alert(m, row, last, d)
                self.alerts.append(a)
                self.open_alert[mid] = a
                new_alerts.append(a)
            elif cur is not None:
                if not bool(sc["anomaly"].iloc[-4:].any()):
                    cur["resolved"] = True
                    cur["resolved_at"] = pd.Timestamp(row["timestamp"]).isoformat()
                    self.open_alert.pop(mid, None)
                else:
                    cur["drivers"] = self.models[mid].drivers(g.tail(6))[:4]
                    fresh = self._make_alert(m, row, last, cur["drivers"])
                    cur["title"] = fresh["title"]
                    cur["message"], cur["severity"] = fresh["message"], max(
                        cur["severity"], fresh["severity"], key=["Low", "Medium", "High"].index)
                    cur["excess_pct"] = round(float(last["excess_pct"]), 1)
                    recent = g.tail(3)
                    cur["co2_kg_h"] = round(float(recent["co2_kg_h"].mean()), 1)
                    cur["expected_kg_h"] = round(float(sc["expected_co2"].tail(3).mean()), 1)
        self._update_verifications(learning)
        self._credit_savings()
        self.now += timedelta(minutes=TICK_MIN)
        return new_alerts

    def _update_sensor_alert(self, m: dict, issues: list[dict], row) -> list[dict]:
        mid = m["id"]
        cur = self.sensor_alert.get(mid)
        if issues:
            self._sensor_clear[mid] = 0
            msg = " ".join(i["detail"] for i in issues[:2])
            if cur is None:
                a = {"id": uuid.uuid4().hex[:8], "machine_id": mid, "machine": m["name"], "machine_type": m["type"],
                     "kind": "sensor", "severity": "Medium", "title": f"Check instrument: {issues[0]['label']}",
                     "message": msg, "time": pd.Timestamp(row["timestamp"]).isoformat(), "excess_pct": 0, "co2_kg_h": 0,
                     "expected_kg_h": 0, "drivers": [], "resolved": False, "resolved_at": None, "acknowledged": False}
                self.alerts.append(a)
                self.sensor_alert[mid] = a
                return [a]
            cur["message"], cur["title"] = msg, f"Check instrument: {issues[0]['label']}"
        elif cur is not None:  # needs half an hour of clean readings before it is closed
            self._sensor_clear[mid] = self._sensor_clear.get(mid, 0) + 1
            if self._sensor_clear[mid] >= 6:
                cur["resolved"], cur["resolved_at"] = True, pd.Timestamp(row["timestamp"]).isoformat()
                self.sensor_alert.pop(mid, None)
        return []

    def sensor_issues(self, mid: str) -> list[dict]:
        return self.memo(("sensors", mid), lambda: self.models[mid].sensor_health(self.machine_df(mid, last=20)))

    # ------------------------------------------------------------------
    def recommendations(self, learning: dict, mid: str | None = None) -> list[dict]:
        if mid is None:
            return self.memo("recs", lambda: self._recommendations(learning))
        return self._recommendations(learning, mid)

    def _recommendations(self, learning: dict, mid: str | None = None) -> list[dict]:
        sims = []
        for m in self.fac["machines"]:
            if mid and m["id"] != mid:
                continue
            g = self._recent(m["id"], "")
            g_long = self.machine_df(m["id"], last=36)
            for aid, a in ACTIONS.items():
                if a["machine"] != m["type"] or self.in_progress(m["id"], aid):
                    continue
                check = self._safety(m["id"], aid)
                if check["status"] == "blocked":
                    continue  # never recommend what breaks a safety or quality limit
                win = g_long if aid == "idle_shutoff" else g
                L = learn_entry(learning, self.lkey(m["id"]), aid)
                s = self.models[m["id"]].simulate(win, aid, calib=L["calibration"])
                s["machine_id"] = m["id"]
                s["machine_name"] = m["name"]
                s["confidence"] = L["confidence"]
                s["learned_from"] = L["source"]
                s["safety"] = check
                s["saving"]["range_kg_h"] = saving_range(s["saving"]["co2_kg_h"], L)
                sims.append(s)
        return rank_actions(sims)

    def _recent(self, mid: str, action: str) -> pd.DataFrame:
        """Readings an action is simulated on. During a changeover the stenter runs no fabric, so
        use its latest running readings: otherwise a real fault would look like it has no saving."""
        if action == "idle_shutoff":
            return self.machine_df(mid, last=36)
        g = self.machine_df(mid, last=3)
        if (g["status"] == "idle").all():
            long = self.machine_df(mid, last=36)
            running = long[long["status"] == "running"]
            if len(running):
                return running.tail(3).reset_index(drop=True)
        return g

    def _safety(self, mid: str, action: str, overrides: dict | None = None) -> dict:
        g = self.machine_df(mid, last=3)
        row = g.select_dtypes("number").mean().to_dict()
        row["status"] = g["status"].iloc[-1]
        check = safety.check(self.machine(mid), action, row, overrides)
        issues = self.sensor_issues(mid)
        if issues:  # advice built on doubtful readings must say so
            check["notes"].append(f"Instrument problem on this machine ({issues[0]['label']}): confirm the readings first.")
            check["status"] = "caution" if check["status"] == "ok" else check["status"]
        return check

    def simulate(self, mid: str, action: str, learning: dict, overrides=None) -> dict:
        g = self._recent(mid, action)
        L = learn_entry(learning, self.lkey(mid), action)
        s = self.models[mid].simulate(g, action, calib=L["calibration"] if action != "custom" else 1.0,
                                      overrides=overrides)
        s["confidence"], s["learned_from"] = L["confidence"], L["source"]
        s["safety"] = self._safety(mid, action, overrides)
        s["can_act"] = self.sim.can_act
        if action != "custom":
            s["saving"]["range_kg_h"] = saving_range(s["saving"]["co2_kg_h"], L)
        a = ACTIONS.get(action)
        if a:
            s.update({k: a[k] for k in ("title", "detail", "effort", "downtime_min", "capex", "time", "implement_min", "measure_h")})
        s["machine_id"] = mid
        return s

    def in_progress(self, mid: str, action: str) -> bool:
        return any(a["machine_id"] == mid and a["action"] == action and a["status"] in ACTIVE
                   for a in self.actions)

    def apply_action(self, mid: str, action: str, learning: dict, user: str, assignee: str = "",
                     note: str = "") -> dict:
        """Approval raises a work order. The machine only changes when the crew has done
        the job, and the sensors then have to confirm that it really changed."""
        sim = self.simulate(mid, action, learning)
        a = ACTIONS[action]
        rec = {"id": uuid.uuid4().hex[:8], "machine_id": mid, "action": action,
               "title": a["title"], "decided_by": user,
               "decided_at": self.now.isoformat(), "status": "Assigned",
               "assignee": assignee or "Maintenance crew", "note": note, "safety_notes": sim["safety"]["notes"],
               "implement_due": (self.now + timedelta(minutes=a["implement_min"])).isoformat(),
               "implemented_at": None, "done_by": None, "measure_until": None, "measure_h": a["measure_h"],
               "credit_days": a["credit_days"], "credited_h": 0.0, "credit_ended": None, "confirmed": None,
               "predicted_kg_h": sim["saving"]["co2_kg_h"],
               "predicted_raw_kg_h": round(sim["saving"]["co2_kg_h"] / max(sim["calibration"], 1e-6), 1),
               "predicted_pct": sim["saving"]["co2_pct"],
               "predicted_inr_day": sim["saving"]["inr_per_day"],
               "actual_kg_h": None, "accuracy_pct": None,
               "predicted_range_kg_h": sim["saving"].get("range_kg_h"), "pre": []}
        self.actions.append(rec)
        self._memo.clear()
        return rec

    def reject_action(self, mid: str, action: str, user: str, reason: str = "") -> dict:
        rec = {"id": uuid.uuid4().hex[:8], "machine_id": mid, "action": action,
               "title": ACTIONS[action]["title"], "decided_by": user, "decided_at": self.now.isoformat(),
               "status": "Rejected", "predicted_kg_h": None, "actual_kg_h": None, "accuracy_pct": None,
               "reason": reason, "pre": []}
        self.actions.append(rec)
        self._memo.clear()
        return rec

    def get_action(self, action_id: str) -> dict | None:
        return next((a for a in self.actions if a["id"] == action_id), None)

    def mark_done(self, rec: dict, by: str, took_effect: bool = True):
        """The crew reports the job finished: baseline = readings just before now."""
        mid = rec["machine_id"]
        pre = self.machine_df(mid, last=36 if rec["action"] == "idle_shutoff" else PRE_TICKS)
        rec["pre"] = pre.to_dict("records")
        if took_effect:
            self.sim.apply_action(rec["action"], self.now)
        rec.update({"status": "Measuring", "implemented_at": self.now.isoformat(), "done_by": by,
                    "measure_until": (self.now + timedelta(hours=rec["measure_h"])).isoformat()})
        rec.pop("overdue", None)
        self._memo.clear()

    def cancel(self, rec: dict, by: str):
        rec.update({"status": "Cancelled", "reason": f"Cancelled by {by}"})
        self._memo.clear()

    def _confirm_change(self, rec: dict, post: pd.DataFrame) -> bool | None:
        """Do the sensors show the work happened? None = cannot tell yet."""
        col, sign, min_move = ACTION_CHECK[rec["action"]]
        model = self.models[rec["machine_id"]]
        pre, after = model.prepare(pd.DataFrame(rec["pre"])), model.prepare(post)
        if rec["action"] == "idle_shutoff":
            pre, after = pre[pre["running"] == 0], after[after["running"] == 0]
        elif model.mtype == "stenter":
            pre, after = pre[pre["running"] == 1], after[after["running"] == 1]
        if len(after) < 3:
            return None
        if pre.empty:
            return True  # nothing to compare with: leave the verdict to the measured saving
        move = sign * (float(after[col].mean()) - float(pre[col].mean()))
        return move >= max(min_move, 0.5 * float(np.nan_to_num(pre[col].std())))

    def _update_verifications(self, learning: dict):
        for rec in self.actions:
            if rec["status"] == "Assigned" and self.now >= datetime.fromisoformat(rec["implement_due"]):
                if self.sim.kind == "simulator":  # stand-in for a real crew pressing "Mark done"
                    self.mark_done(rec, "Crew (simulated)", took_effect=self._frng.random() >= self.crew_miss)
                else:
                    rec["overdue"] = True
                continue
            if rec["status"] != "Measuring":
                continue
            post = self.df[(self.df.machine_id == rec["machine_id"]) &
                           (self.df.timestamp > pd.Timestamp(rec["implemented_at"]))].reset_index(drop=True)
            if rec["confirmed"] is None and len(post) >= CONFIRM_TICKS:
                rec["confirmed"] = self._confirm_change(rec, post)
                if rec["confirmed"] is False:
                    col = ACTION_CHECK[rec["action"]][0].replace("_", " ")
                    rec.update({"status": "Not confirmed", "verified_at": self.now.isoformat(),
                                "reason": f"Reported done, but {col} did not change. Check the work on site."})
                    self._memo.clear()
                    continue
            if self.now < datetime.fromisoformat(rec["measure_until"]):
                continue
            pre = pd.DataFrame(rec["pre"])
            if rec["action"] == "idle_shutoff" and (post["status"] == "idle").sum() < 3 and \
                    self.now < datetime.fromisoformat(rec["measure_until"]) + timedelta(hours=22):
                rec["waiting_for"] = "a changeover (idle period) to measure"
                continue  # an idle-time action can only be measured while the stenter is idle
            rec.pop("waiting_for", None)
            actual = measure_saving(self.models[rec["machine_id"]], pre, post, rec["action"])
            pred = rec["predicted_kg_h"]
            acc = max(0.0, 1 - abs(actual - pred) / max(abs(pred), 1e-6)) * 100
            rec.update({"status": "Verified", "actual_kg_h": round(actual, 1), "accuracy_pct": round(acc, 1),
                        "verified_at": self.now.isoformat(),
                        # the measuring period itself counts, from the moment the work was done
                        "credited_h": (self.now - datetime.fromisoformat(rec["implemented_at"])).total_seconds() / 3600})
            if rec["predicted_raw_kg_h"] < 1.0:  # nothing to learn from a ~zero prediction
                rec["accuracy_pct"] = None
                continue
            # learn: calibration = EWMA of actual / raw model prediction
            prior = learn_entry(learning, self.lkey(rec["machine_id"]), rec["action"])
            L = learning.setdefault(self.lkey(rec["machine_id"]), {}).setdefault(rec["action"], {
                **DEFAULT_LEARN, "calibration": prior["calibration"], "accuracy": [], "ratios": []})
            L.setdefault("ratios", [])
            ratio = actual / max(rec["predicted_raw_kg_h"], 1e-6)
            ratio = float(np.clip(ratio, 0.3, 1.8))
            L["ratios"] = (L["ratios"] + [round(ratio, 3)])[-10:]
            alpha = 0.5 if L["n"] == 0 else 0.35
            L["calibration"] = round((1 - alpha) * L["calibration"] + alpha * ratio, 3)
            L["n"] += 1
            L["accuracy"] = (L["accuracy"] + [round(acc, 1)])[-10:]
            L["confidence"] = round(min(0.97, 0.6 + 0.05 * L["n"] + 0.002 * np.mean(L["accuracy"])), 2)
            rec["calibration_after"] = L["calibration"]
            self._memo.clear()
            save_learning(learning)

    # ------------------------------------------------------------------
    def plant_df(self) -> pd.DataFrame:
        return self.df

    def forecast(self, learning: dict) -> dict:
        return self.memo("forecast", lambda: self._forecast(learning))

    def _forecast(self, learning: dict) -> dict:
        recs = self.recommendations(learning)
        top_saving = sum(r["saving"]["co2_t_day"] for r in recs[:3])
        excess = 0.0
        for mid, a in self.open_alert.items():
            if a.get("kind") == "anomaly":
                excess += max(0.0, a["co2_kg_h"] - a["expected_kg_h"]) * 24 / 1000
        return forecast(self.df, self.now, self.fac["daily_cap_t_co2"], self.fac["rolling_30d_cap_t_co2"],
                        saving_t_day=top_saving, excess_t_day=excess)

    def learning_view(self, learning: dict) -> dict:
        """What was learned on this factory's machines: {"BLR-01": {action: entry}}."""
        return {m["id"]: learning.get(self.lkey(m["id"]), {}) for m in self.fac["machines"]}

    def _credit_savings(self):
        """Count a verified saving only while it plausibly still applies: from when the work
        was done, for the action's credit period, and not after the same problem returns."""
        for a in self.actions:
            if a["status"] != "Verified" or (a.get("actual_kg_h") or 0) <= 0 or a.get("credit_ended"):
                continue
            hours = (self.now - datetime.fromisoformat(a["implemented_at"])).total_seconds() / 3600
            alert = self.open_alert.get(a["machine_id"])
            if hours >= a.get("credit_days", 7) * 24:
                a["credit_ended"] = f"credit period of {a.get('credit_days', 7)} days over"
            elif alert and alert.get("drivers") and alert["drivers"][0]["action"] == a["action"]:
                a["credit_ended"] = "the same problem came back"
            else:
                a["credited_h"] = a.get("credited_h", 0.0) + TICK_MIN / 60

    def savings(self) -> dict:
        """Verified savings counted so far (see _credit_savings), and what is still being saved."""
        co2_t = inr = now_kg_h = 0.0
        for a in self.actions:
            if a["status"] != "Verified" or (a.get("actual_kg_h") or 0) <= 0:
                continue
            hours = a.get("credited_h", 0.0)
            co2_t += a["actual_kg_h"] * hours / 1000
            ratio = a["actual_kg_h"] / max(a["predicted_kg_h"] or 0, 1e-6)
            inr += (a.get("predicted_inr_day") or 0) * min(ratio, 2.0) * hours / 24
            if not a.get("credit_ended"):
                now_kg_h += a["actual_kg_h"]
        return {"co2_avoided_t": round(co2_t, 2), "inr_saved": round(inr),
                "co2_t_per_day_now": round(now_kg_h * 24 / 1000, 2)}

    def check_caps(self, learning: dict) -> list[dict]:
        """Plant-level early warnings from the no-action forecast."""
        fc = self.forecast(learning)
        out = []
        t = fc["today"]
        r = fc["rolling"]
        cands = []
        if t["cross_time"]:
            cands.append(("daily_cap", "High", "Daily CO2 cap will be crossed",
                          f"At the current rate the plant reaches {t['projected_t']} t today vs cap {t['cap_t']} t "
                          f"— crossing around {t['cross_time']} if nothing changes."))
        elif t["pct_of_cap"] >= (95 if any(a["kind"] == "daily_near" and not a["resolved"]
                                           for a in self.alerts) else 98):
            cands.append(("daily_near", "Medium", "Emission level close to daily cap",
                          f"Projected {t['projected_t']} t today = {t['pct_of_cap']}% of the {t['cap_t']} t cap."))
        if r["days_to_cross"] is not None and r["days_to_cross"] <= 14:
            cands.append(("rolling_cap", "High" if r["days_to_cross"] <= 7 else "Medium",
                          "30-day CO2 budget at risk",
                          f"With no action the rolling 30-day total crosses {r['cap_t']} t on {r['cross_date']} "
                          f"(in {r['days_to_cross']} days)."
                          + (" Recommended actions avoid the breach." if not r["cross_date_with_actions"] else "")))
        open_kinds = {a["kind"] for a in self.alerts if a["machine_id"] == "PLANT" and not a["resolved"]}
        active = {c[0] for c in cands}
        for a in self.alerts:
            if a["machine_id"] == "PLANT" and not a["resolved"] and a["kind"] not in active:
                a["resolved"] = True
                a["resolved_at"] = self.now.isoformat()
        for kind, sev, title, msg in cands:
            if kind in open_kinds:
                continue
            a = {"id": uuid.uuid4().hex[:8], "machine_id": "PLANT", "machine": "Whole plant",
                 "machine_type": "plant", "kind": kind, "severity": sev, "title": title, "message": msg,
                 "time": self.now.isoformat(), "excess_pct": 0, "co2_kg_h": 0, "expected_kg_h": 0,
                 "drivers": [], "resolved": False, "resolved_at": None, "acknowledged": False}
            self.alerts.append(a)
            out.append(a)
        self._fc_cache = fc
        return out


def load_learning() -> dict:
    if not LEARN_FILE.exists():
        return {}
    try:
        data = json.loads(LEARN_FILE.read_text())
    except ValueError:
        return {}
    if any(isinstance(v, dict) and "calibration" in v for v in data.values()):
        # old format (one entry per action shared by every machine): keep a copy, start per machine
        LEARN_FILE.replace(LEARN_FILE.with_name("learning_legacy.json"))
        return {}
    return data


def save_learning(learning: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LEARN_FILE.write_text(json.dumps(learning, indent=2))


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.RLock()
        start = datetime.now().replace(second=0, microsecond=0)
        start = start.replace(minute=(start.minute // TICK_MIN) * TICK_MIN)
        self.store = Store(DATA_DIR / "carbon.db")
        self.factories = {f["id"]: FactoryRuntime(f, start, seed=7 + i, store=self.store)
                          for i, f in enumerate(cfg["factories"])}
        self.shadow: dict[str, FactoryRuntime] = {}  # simulated plants kept running behind an upload
        self._last_replay = 0.0
        self.learning = load_learning()
        self.paused = False
        # None = real time (one 5-min reading every 5 real minutes, clock = wall clock);
        # a number = demo fast-forward, real seconds per 5-min reading
        self.speed: float | None = None

    @property
    def data_time(self) -> datetime:
        """Time the plant data has reached (each 5-min reading is reported when its interval ends)."""
        return min(fr.now for fr in self._runtimes() if fr.sim.kind == "simulator")

    def _runtimes(self) -> list[FactoryRuntime]:
        return list(self.factories.values()) + list(self.shadow.values())

    def due(self) -> bool:
        return datetime.now() >= self.data_time + timedelta(minutes=TICK_MIN)

    def ahead_min(self) -> int:
        """After a fast-forward, how far the plant data is ahead of the real clock."""
        return max(0, int((self.data_time - datetime.now()).total_seconds() // 60))

    def tick(self):
        with self.lock:
            if self.paused:
                return []
            self.ticks = getattr(self, "ticks", 0) + 1
            return self._tick_some(self._runtimes())

    def _tick_some(self, runtimes: list[FactoryRuntime]) -> list[dict]:
        out = []
        for fr in runtimes:
            out.extend(fr.tick(self.learning))
            if self.ticks % 6 == 1:
                out.extend(fr.check_caps(self.learning))
            fr.persist()
        return out

    def step_due(self):
        """Real-time clock: simulated plants tick when their 5 minutes are over;
        uploaded data replays at its own faster pace."""
        with self.lock:
            if self.paused:
                return
            sims = [fr for fr in self._runtimes() if fr.sim.kind == "simulator"]
            due = [fr for fr in sims if datetime.now() >= fr.now + timedelta(minutes=TICK_MIN)]
            replays = []
            if time.monotonic() - self._last_replay >= REPLAY_SECONDS:
                replays = [fr for fr in self._runtimes() if fr.sim.kind == "csv"]
                self._last_replay = time.monotonic()
            if due or replays:
                self.ticks = getattr(self, "ticks", 0) + 1
                self._tick_some(due + replays)

    def start_replay(self, fid: str, runtime: FactoryRuntime):
        with self.lock:
            cur = self.factories[fid]
            if cur.sim.kind == "simulator":
                self.shadow[fid] = cur
            self.factories[fid] = runtime

    def stop_replay(self, fid: str):
        with self.lock:
            if fid in self.shadow:
                self.factories[fid] = self.shadow.pop(fid)

    def inject(self, fid: str, kind: str, severity: float = 1.0):
        with self.lock:
            fr = self.factories[fid]
            if kind in SENSOR_FAULTS:
                fr.sim.add_sensor_fault(kind, fr.now)
                return
            fr.sim.add_fault(kind, fr.now, None, severity, ramp_min=15)
            if kind == "stenter_idle_burners":  # only visible during a changeover: start one now
                fr.sim._batch_left = 0
                fr.sim._idle_left = 18  # a 90-min changeover, long enough to detect, act and verify

    def clear_faults(self, fid: str):
        with self.lock:
            fr = self.factories[fid]
            for k in FAULTS:
                fr.sim.clear_fault(k, fr.now)
            fr.sim.sensor_faults.clear()


__all__ = ["Engine", "ACTIONS", "DRIVERS", "FAULTS"]
