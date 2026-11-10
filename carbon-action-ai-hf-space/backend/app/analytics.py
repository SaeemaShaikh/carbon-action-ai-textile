"""The AI layer of Carbon Action AI.

Per machine we learn a data-driven "digital twin":
  fuel model   : fuel rate      ~ operating conditions   (gradient boosting, monotonic)
  power model  : electricity kW ~ operating conditions
  context model: CO2 kg/h       ~ production context only  (what CO2 *should* be)

Emissions are then *calculated* from predicted fuel and power with explicit
emission factors (accounting by formula, ML for estimation).

1. Anomaly detection : robust residual of the context model + IsolationForest
                       on health indicators.
2. Root-cause drivers: counterfactual attribution - reset one driver group to its
                       healthy value, see how much predicted CO2 falls.
3. Simulation        : apply an action's change to recent data, predict outcome.
4. Recommendation    : rank actions by calibrated CO2 saving, cost, effort, risk.
5. Verify and learn  : adjusted-baseline measurement after an action; per-action
                       calibration factor updates future predictions.
6. Forecast          : no-action projection of daily and rolling-30-day caps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor, IsolationForest
from sklearn.linear_model import LinearRegression

# --------------------------------------------------------------------------
# Feature definitions
# --------------------------------------------------------------------------
FEATURES = {
    "boiler": ["steam_generated_kg_h", "feedwater_temp_c", "flue_o2_pct",
               "exhaust_gas_temp_c", "fabric_processed_kg_h"],
    "stenter": ["running", "dry_fabric_kg_h", "inlet_moisture_pct", "outlet_moisture_pct",
                "chamber_temp_c", "exhaust_damper_pct", "airflow_pct"],
}
MONOTONIC = {
    "boiler": [1, -1, 1, 1, 0],
    "stenter": [1, 1, 1, -1, 1, 1, 0],
}
CONTEXT = {
    "boiler": ["fabric_processed_kg_h"],
    "stenter": ["running", "dry_fabric_kg_h", "gsm_g_m2"],
}
HEALTH = {
    "boiler": ["flue_o2_pct", "exhaust_dev_c", "feedwater_temp_c", "steam_ratio"],
    "stenter": ["exhaust_humidity_kg_kg", "airflow_pct", "filter_dp_kpa",
                "outlet_moisture_pct", "inlet_moisture_pct", "idle_temp"],
}

# Driver groups: which sensor readings move together for one physical cause
DRIVERS = {
    "boiler": {
        "excess_air": {"label": "Too much air in the boiler", "evidence_label": "Oxygen in chimney gas", "features": ["flue_o2_pct"],
                       "evidence": "flue_o2_pct", "unit": "%", "action": "tune_o2"},
        "fouling": {"label": "Soot on the boiler tubes (hot chimney)", "evidence_label": "Chimney temperature", "features": ["exhaust_gas_temp_c"],
                    "evidence": "exhaust_gas_temp_c", "unit": "°C", "action": "soot_blow"},
        "steam_loss": {"label": "Steam being lost (leaks or faulty traps)", "evidence_label": "Steam produced", "features": ["steam_generated_kg_h"],
                       "evidence": "steam_generated_kg_h", "unit": "kg/h", "action": "repair_leaks"},
        "feedwater": {"label": "Water entering the boiler is too cold", "evidence_label": "Boiler feed water temperature", "features": ["feedwater_temp_c"],
                      "evidence": "feedwater_temp_c", "unit": "°C", "action": "restore_condensate"},
    },
    "stenter": {
        "exhaust_damper": {"label": "Stenter exhaust damper too open", "evidence_label": "Damper opening", "features": ["exhaust_damper_pct"],
                           "evidence": "exhaust_damper_pct", "unit": "%", "action": "humidity_control"},
        "airflow": {"label": "Lint filters blocking the airflow", "evidence_label": "Airflow", "features": ["airflow_pct", "chamber_temp_c"],
                    "evidence": "airflow_pct", "unit": "%", "action": "clean_filters"},
        "overdrying": {"label": "Fabric dried more than needed", "evidence_label": "Moisture left in the fabric",
                       "features": ["outlet_moisture_pct", "chamber_temp_c"],
                       "evidence": "outlet_moisture_pct", "unit": "%", "action": "reduce_temperature"},
        "inlet_moisture": {"label": "Fabric entering the stenter too wet", "evidence_label": "Moisture in the incoming fabric",
                           "features": ["inlet_moisture_pct", "chamber_temp_c"],
                           "evidence": "inlet_moisture_pct", "unit": "%", "action": "raise_padder"},
        "idle_firing": {"label": "Burners running with no fabric", "evidence_label": "Stenter temperature while idle", "features": ["chamber_temp_c", "exhaust_damper_pct"],
                        "evidence": "chamber_temp_c", "unit": "°C", "action": "idle_shutoff", "idle_only": True},
    },
}

# implement_min: time for the crew to carry the action out (the machine only changes after it)
# measure_h: how long the system then measures before judging the real saving
# credit_days: how long a verified saving is counted (settings drift back and deposits
# rebuild, so no saving lasts forever); stated assumptions, to be replaced by plant experience
ACTIONS = {
    "tune_o2": {"machine": "boiler", "title": "Reduce the extra air going into the boiler",
                "detail": "The boiler is burning with more air than it needs, and that extra air carries heat out of the chimney. Adjust the air setting so less fuel is wasted.",
                "effort": 1, "downtime_min": 0, "capex": "None", "time": "30 min", "implement_min": 30, "measure_h": 2, "credit_days": 3},
    "soot_blow": {"machine": "boiler", "title": "Clean the soot from the boiler tubes",
                  "detail": "Soot on the tubes stops heat reaching the water, so more heat escapes through the chimney. Cleaning them brings the chimney temperature back down.",
                  "effort": 2, "downtime_min": 0, "capex": "None", "time": "1 h", "implement_min": 60, "measure_h": 4, "credit_days": 14},
    "repair_leaks": {"machine": "boiler", "title": "Fix steam leaks and faulty steam traps",
                     "detail": "Steam is being lost before it does useful work, so the boiler burns extra fuel to replace it. Find and repair the leaks.",
                     "effort": 3, "downtime_min": 0, "capex": "Low", "time": "4 h", "implement_min": 240, "measure_h": 6, "credit_days": 90},
    "restore_condensate": {"machine": "boiler", "title": "Send more hot water back to the boiler",
                           "detail": "Cold water takes more fuel to heat. Reusing the hot water returning from the steam lines cuts that fuel.",
                           "effort": 3, "downtime_min": 0, "capex": "Low", "time": "1 day", "implement_min": 1440, "measure_h": 12, "credit_days": 90},
    "humidity_control": {"machine": "stenter", "title": "Close the stenter exhaust damper a little",
                         "detail": "Hot air is leaving the stenter before it has finished drying the fabric. Keeping it in longer uses less gas.",
                         "effort": 1, "downtime_min": 0, "capex": "None", "time": "15 min", "implement_min": 15, "measure_h": 2, "credit_days": 3},
    "clean_filters": {"machine": "stenter", "title": "Clean the stenter lint filters",
                      "detail": "Blocked filters reduce airflow, so the machine runs hotter to dry the same fabric. Clean filters let it run cooler.",
                      "effort": 2, "downtime_min": 30, "capex": "None", "time": "30 min", "implement_min": 30, "measure_h": 4, "credit_days": 7},
    "reduce_temperature": {"machine": "stenter", "title": "Lower the stenter temperature a little",
                           "detail": "The fabric is being dried more than it needs. Drying only to the 7% moisture target saves gas.",
                           "effort": 1, "downtime_min": 0, "capex": "None", "time": "10 min", "implement_min": 10, "measure_h": 2, "credit_days": 1},
    "idle_shutoff": {"machine": "stenter", "title": "Turn the burners down when no fabric is running",
                     "detail": "Keeping the stenter fully hot between batches burns gas for nothing. Drop it to standby until the next batch.",
                     "effort": 1, "downtime_min": 0, "capex": "None", "time": "10 min", "implement_min": 10, "measure_h": 2, "credit_days": 30},
    "raise_padder": {"machine": "stenter", "title": "Squeeze more water out before drying",
                     "detail": "Squeezing water out with rollers costs far less than evaporating it with heat. Raise the roller pressure a little.",
                     "effort": 1, "downtime_min": 0, "capex": "None", "time": "10 min", "implement_min": 10, "measure_h": 2, "credit_days": 3},
}


# Sensor health: physical range of each reported value, and the minimum move
# that counts as "the sensor is alive". Frozen = identical for FROZEN_N running readings.
SENSORS = {
    "boiler": {"flue_o2_pct": ("Flue O2", 0.5, 18), "exhaust_gas_temp_c": ("Stack temperature", 60, 450),
               "feedwater_temp_c": ("Feedwater temperature", 5, 130), "steam_generated_kg_h": ("Steam flow", 0, None),
               "fuel_rate": ("Fuel meter", 0, None), "electricity_kw": ("Power meter", 0, None)},
    "stenter": {"chamber_temp_c": ("Chamber temperature", 30, 260), "exhaust_damper_pct": ("Exhaust damper", 0, 100),
                "airflow_pct": ("Airflow", 0, 110), "inlet_moisture_pct": ("Inlet moisture", 0, 150),
                "outlet_moisture_pct": ("Outlet moisture", 0, 100), "fuel_rate": ("Gas meter", 0, None),
                "electricity_kw": ("Power meter", 0, None)},
}
FROZEN_N = 12
GAP_N = 12  # readings over which metered fuel and the twin are compared
FAN_N = 6  # readings over which FD fan speed and flue O2 are compared

# twin-channel alarm: smoothed robust z of driver-attributed excess, and its size vs expected CO2
TWIN_Z = 4.0
TWIN_PCT = 3.0
DRIVER_RANK = "above"
CONTEXT_TRIM_ROUNDS = 1
EXCESS_SHIFT = 1.5  # alert floors (in % above expected), re-centred for the unbiased context model


def add_derived(df: pd.DataFrame, mtype: str, rated: float | None = None,
                exh_fit=None, steam_fit=None) -> pd.DataFrame:
    df = df.copy()
    if mtype == "boiler":
        df["load"] = df["steam_generated_kg_h"] / rated
        if exh_fit is not None:
            df["exhaust_dev_c"] = df["exhaust_gas_temp_c"] - exh_fit.predict(df[["load"]])
        if steam_fit is not None:
            df["steam_ratio"] = df["steam_generated_kg_h"] / steam_fit.predict(df[["fabric_processed_kg_h"]])
    else:
        df["running"] = (df["status"] == "running").astype(float)
        df["idle_temp"] = np.where(df["running"] == 0, df["chamber_temp_c"], 100.0)
    return df


def robust_fit(x: pd.DataFrame, y: pd.Series, trim: float = 0.2) -> LinearRegression:
    """Fit, drop the worst positive residuals (fault periods), refit."""
    m = LinearRegression().fit(x, y)
    for _ in range(2):
        r = y - m.predict(x)
        keep = r <= r.quantile(1 - trim)
        m = LinearRegression().fit(x[keep], y[keep])
    return m


# --------------------------------------------------------------------------
@dataclass
class MachineModel:
    factory: dict
    cfg: dict
    mtype: str = ""
    baselines: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)

    def __post_init__(self):
        self.mtype = self.cfg["type"]
        f = self.cfg
        if f["fuel"] == "coal":
            self.fuel_factor = f["carbon_pct"] / 100 * 44 / 12 * 0.98
            self.fuel_price = self.factory["fuel_price"]["coal_inr_per_kg"]
        else:
            self.fuel_factor = f["co2_kg_per_unit"]
            self.fuel_price = self.factory["fuel_price"]["gas_inr_per_nm3"]
        self.grid = self.factory["grid_factor_kg_per_kwh"]
        self.power_price = self.factory["fuel_price"]["power_inr_per_kwh"]

    # ------------------------------------------------------------------
    def fit(self, hist: pd.DataFrame):
        mt = self.mtype
        df = hist.copy()
        if mt == "boiler":
            rated = self.cfg["rated_steam_kg_h"]
            df["load"] = df["steam_generated_kg_h"] / rated
            self.exh_fit = robust_fit(df[["load"]], df["exhaust_gas_temp_c"])
            self.steam_fit = robust_fit(df[["fabric_processed_kg_h"]], df["steam_generated_kg_h"])
            df = add_derived(df, mt, rated, self.exh_fit, self.steam_fit)
        else:
            df = add_derived(df, mt)
            run = df[df.running == 1]
            self.temp_fit = robust_fit(run[["inlet_moisture_pct", "airflow_pct", "fabric_speed_m_min"]],
                                       run["chamber_temp_c"], trim=0.15)
        X = df[FEATURES[mt]]
        n = len(df)
        cut = int(n * 0.85)  # chronological split for honest metrics
        params = dict(max_iter=150, learning_rate=0.1, max_leaf_nodes=31,
                      l2_regularization=0.5, random_state=0)
        fuel_eval = HistGradientBoostingRegressor(monotonic_cst=MONOTONIC[mt], **params)
        fuel_eval.fit(X.iloc[:cut], df["fuel_rate"].iloc[:cut])
        pred = fuel_eval.predict(X.iloc[cut:])
        act = df["fuel_rate"].iloc[cut:]
        mae = float(np.mean(np.abs(pred - act)))
        mape = float(np.mean(np.abs(pred - act) / np.maximum(act, 1e-3))) * 100
        naive = float(np.mean(np.abs(act - df["fuel_rate"].iloc[:cut].mean())))
        r2 = 1 - float(np.sum((pred - act) ** 2) / np.sum((act - act.mean()) ** 2))
        # how far metered fuel and the twin normally drift apart over GAP_N readings, on unseen data
        pa = pd.Series(pred, index=act.index)
        gap = ((act.rolling(GAP_N).sum() - pa.rolling(GAP_N).sum()) / pa.rolling(GAP_N).sum() * 100).dropna().abs()
        self.gap_cut = max(float(gap.quantile(0.995)) * 1.5, 3.0) if len(gap) else 5.0
        self.metrics = {"fuel_mae": round(mae, 2), "fuel_mape_pct": round(mape, 2), "fuel_r2": round(r2, 3),
                        "naive_mae": round(naive, 2), "train_rows": cut, "test_rows": n - cut}

        self.fuel_model = HistGradientBoostingRegressor(monotonic_cst=MONOTONIC[mt], **params).fit(X, df["fuel_rate"])
        self.power_model = HistGradientBoostingRegressor(**params).fit(X, df["electricity_kw"])

        # context model: what CO2 should be for this production context
        C = df[CONTEXT[mt]] if mt == "boiler" else df[CONTEXT[mt]].fillna(0)
        # robust: refit without the worst positive residuals so fault periods in
        # history don't teach the model that excess CO2 is "expected"
        ctx = dict(max_iter=120, learning_rate=0.1, random_state=0)
        self.context_model = HistGradientBoostingRegressor(**ctx).fit(C, df["co2_kg_h"])
        for _ in range(CONTEXT_TRIM_ROUNDS):
            r = df["co2_kg_h"] - self.context_model.predict(C)
            keep = (r <= r.quantile(0.85)).values
            self.context_model = HistGradientBoostingRegressor(**ctx).fit(C[keep], df["co2_kg_h"][keep])
        resid = df["co2_kg_h"] - self.context_model.predict(C)
        self.res_med = float(resid.median())
        self.res_mad = float(1.4826 * (resid - self.res_med).abs().median()) or 1.0
        self.co2_mean = float(df["co2_kg_h"].mean())

        self.idle_frac = float((df["running"] == 0).mean()) if mt == "stenter" else 0.0
        # O2 probe cross-check: the FD fan physically follows combustion air
        self.fan_fit = None
        if mt == "boiler" and df["fd_fan_speed_pct"].notna().mean() > 0.9:
            self.fan_fit = LinearRegression().fit(self._fan_x(df), df["fd_fan_speed_pct"])
            r = (df["fd_fan_speed_pct"] - self.fan_fit.predict(self._fan_x(df))).rolling(FAN_N).mean().abs()
            self.fan_cut = max(float(r.quantile(0.999)) * 2, 1.5)
        # healthy baselines (robust: typical good operation)
        if mt == "boiler":
            self.baselines = {
                "flue_o2_pct": float(df["flue_o2_pct"].quantile(0.4)),
                "feedwater_temp_c": float(df["feedwater_temp_c"].quantile(0.6)),
                "o2_target": self.cfg["o2_optimal_pct"] + 0.3,
                "exhaust_dev_c": 0.0,
            }
        else:
            run = df[df.running == 1]
            self.baselines = {
                "exhaust_damper_pct": float(run["exhaust_damper_pct"].median()),
                "airflow_pct": float(run["airflow_pct"].quantile(0.6)),
                "outlet_moisture_pct": float(run["outlet_moisture_pct"].median()),
                "inlet_moisture_pct": float(run["inlet_moisture_pct"].median()),
                "idle_temp": 100.0, "idle_damper": 35.0,
                "damper_target": 46.0, "moisture_target": 6.8,
            }

        # twin channel: CO2 each driver adds over healthy operation, with its own
        # normal level and noise. Contributions are one-sided (often exactly 0) and
        # the top ~20 % of history holds fault periods, so use the median and the
        # upper spread q75 - q50 (scaled to a normal sigma) as a robust scale.
        D = self.driver_deltas(df, prepared=True)
        self.drv_med, self.drv_mad = {}, {}
        for k in D:
            q50, q75 = float(D[k].quantile(0.5)), float(D[k].quantile(0.75))
            self.drv_med[k] = q50
            self.drv_mad[k] = max((q75 - q50) / 0.674, 0.004 * self.co2_mean)

        # isolation forest on health indicators
        H = df[HEALTH[mt]].astype(float)
        self.h_mean, self.h_std = H.mean(), H.std().replace(0, 1)
        self.iso = IsolationForest(n_estimators=100, contamination=0.08, random_state=0)
        self.iso.fit(((H - self.h_mean) / self.h_std).values)
        self.iso_cut = float(np.quantile(self.iso.score_samples(((H - self.h_mean) / self.h_std).values), 0.06))
        return self

    # ------------------------------------------------------------------
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        # a dropped signal must not stop the models: hold the last good value (sensor health reports it)
        cols = [c for c in set(FEATURES[self.mtype]) | {"fabric_speed_m_min"} | set(HEALTH[self.mtype])
                if c in df.columns]
        if df[cols].isna().any().any():
            df = df.copy()
            df[cols] = df[cols].ffill().bfill()
        if self.mtype == "boiler":
            return add_derived(df, "boiler", self.cfg["rated_steam_kg_h"], self.exh_fit, self.steam_fit)
        return add_derived(df, "stenter")

    def predict(self, df: pd.DataFrame) -> dict:
        X = df[FEATURES[self.mtype]]
        fuel = np.maximum(self.fuel_model.predict(X), 0)
        power = np.maximum(self.power_model.predict(X), 0)
        co2 = fuel * self.fuel_factor + power * self.grid
        return {"fuel": fuel, "power": power, "co2": co2}

    def predict_many(self, frames: list[pd.DataFrame]) -> list[dict]:
        """predict() for several frames in one model call each (a boosting
        predict call has ~20 ms fixed cost, so batching keeps scoring fast)."""
        p = self.predict(pd.concat([f[FEATURES[self.mtype]] for f in frames], ignore_index=True))
        out, i = [], 0
        for f in frames:
            n = len(f)
            out.append({k: v[i:i + n] for k, v in p.items()})
            i += n
        return out

    # ------------------------------------------------------------------
    # 1. anomaly detection
    # ------------------------------------------------------------------
    def score(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self.prepare(df)
        C = df[CONTEXT[self.mtype]].fillna(0)
        expected = self.context_model.predict(C)
        z = (df["co2_kg_h"] - expected - self.res_med) / self.res_mad
        H = ((df[HEALTH[self.mtype]].astype(float) - self.h_mean) / self.h_std).fillna(0).values
        iso = self.iso.score_samples(H)
        out = pd.DataFrame({"expected_co2": expected, "z": z.values, "iso": iso}, index=df.index)
        out["excess_pct"] = (df["co2_kg_h"].values - expected) / np.maximum(expected, 1) * 100
        out["z_smooth"] = out["z"].ewm(span=4).mean()
        D = self.driver_deltas(df, prepared=True)
        med, mad = pd.Series(self.drv_med), pd.Series(self.drv_mad)
        Z = ((D - med) / mad).ewm(span=4).mean()
        top = Z.idxmax(axis=1)
        above = (D - med).ewm(span=4).mean()
        out["twin_z"] = Z.max(axis=1).values
        out["twin_driver"] = top.values
        out["twin_excess_kg_h"] = D.clip(lower=0).sum(axis=1).values
        twin_pct = above.to_numpy()[np.arange(len(D)), D.columns.get_indexer(top)] / np.maximum(expected, 1) * 100
        out["anomaly"] = ((out["z_smooth"] > 2.3) & (out["excess_pct"] > 4 + EXCESS_SHIFT)) | \
                         ((out["iso"] < self.iso_cut) & (out["z_smooth"] > 1.2) & (out["excess_pct"] > 3 + EXCESS_SHIFT)) | \
                         ((out["twin_z"] > TWIN_Z) & (twin_pct > TWIN_PCT) & (out["excess_pct"] > 2 + EXCESS_SHIFT))
        return out

    # ------------------------------------------------------------------
    # sensor health: is the instrument wrong, rather than the machine?
    # ------------------------------------------------------------------
    def _fan_x(self, d: pd.DataFrame) -> pd.DataFrame:
        load = d["steam_generated_kg_h"] / self.cfg["rated_steam_kg_h"]
        return pd.DataFrame({"load": load, "o2": d["flue_o2_pct"], "load_o2": load * d["flue_o2_pct"]}).ffill().bfill()

    def sensor_scan(self, df: pd.DataFrame) -> pd.DataFrame:
        """One row per reading; columns "<sensor>|frozen", "|missing", "|range" and "gap_pct"."""
        out = {}
        running = df["status"] != "idle" if self.mtype == "stenter" else pd.Series(True, index=df.index)
        for col, (_, lo, hi) in SENSORS[self.mtype].items():
            s = df[col].astype(float)
            out[col + "|missing"] = s.isna().rolling(3, min_periods=3).sum() == 3
            bad = (s < lo) if hi is None else ((s < lo) | (s > hi))
            out[col + "|range"] = bad.rolling(3, min_periods=3).sum() == 3
            same = (s.diff() == 0) & running & running.shift(1, fill_value=False)
            out[col + "|frozen"] = same.rolling(FROZEN_N - 1, min_periods=FROZEN_N - 1).sum() == FROZEN_N - 1
        scan = pd.DataFrame(out, index=df.index)
        pred = pd.Series(self.predict(self.prepare(df))["fuel"], index=df.index)
        act = df["fuel_rate"].astype(float)
        scan["gap_pct"] = (act.rolling(GAP_N).sum() - pred.rolling(GAP_N).sum()) / \
            pred.rolling(GAP_N).sum().clip(lower=1e-6) * 100
        if getattr(self, "fan_fit", None) is not None and df["fd_fan_speed_pct"].notna().any():
            scan["fan_gap"] = (df["fd_fan_speed_pct"] - self.fan_fit.predict(self._fan_x(df))).rolling(FAN_N).mean()
        else:
            scan["fan_gap"] = np.nan
        return scan

    def sensor_flag(self, scan: pd.DataFrame) -> pd.Series:
        """True where any instrument problem is present."""
        checks = scan.drop(columns=["gap_pct", "fan_gap"]).any(axis=1)
        fan = scan["fan_gap"].abs() > self.fan_cut if getattr(self, "fan_fit", None) is not None else False
        return checks | (scan["gap_pct"].abs() > self.gap_cut) | fan

    def sensor_health(self, df: pd.DataFrame) -> list[dict]:
        """Instrument problems on the latest reading of df (needs about the last 15 readings)."""
        last = self.sensor_scan(df).iloc[-1]
        out = []
        for col, (label, _, _) in SENSORS[self.mtype].items():
            if last[col + "|missing"]:
                out.append({"sensor": col, "label": label, "problem": "no signal",
                            "detail": f"{label} has sent no data for at least 15 minutes."})
            elif last[col + "|frozen"]:
                out.append({"sensor": col, "label": label, "problem": "frozen",
                            "detail": f"{label} has not changed for an hour of running: the signal looks stuck."})
            elif last[col + "|range"]:
                out.append({"sensor": col, "label": label, "problem": "out of range",
                            "detail": f"{label} is outside its physical range."})
        fan = last["fan_gap"]
        if not out and getattr(self, "fan_fit", None) is not None and pd.notna(fan) and abs(fan) > self.fan_cut:
            out.append({"sensor": "flue_o2_pct", "label": "Flue O2", "problem": "disagrees with fan",
                        "detail": f"Flue O2 reads {df['flue_o2_pct'].iloc[-1]:.1f}%, but the FD fan is "
                                  f"{abs(fan):.1f} points {'below' if fan < 0 else 'above'} what that needs. "
                                  "The O2 probe may be drifting: check it before adjusting air."})
        gap = last["gap_pct"]
        if not out and pd.notna(gap) and abs(gap) > self.gap_cut:
            what = "more" if gap > 0 else "less"
            out.append({"sensor": "fuel_vs_process", "label": "Fuel vs process sensors", "problem": "disagree",
                        "detail": f"Metered fuel is {abs(gap):.1f}% {what} than the process sensors can explain "
                                  f"(normal: under {self.gap_cut:.1f}%). Check the fuel meter and the O2 and temperature sensors."})
        return out

    # ------------------------------------------------------------------
    # 2. root-cause drivers (counterfactual attribution)
    # ------------------------------------------------------------------
    def healthy_value(self, df: pd.DataFrame, feat: str, driver: str) -> pd.Series:
        b = self.baselines
        if self.mtype == "boiler":
            if feat == "flue_o2_pct":
                return np.minimum(df[feat], b["flue_o2_pct"])
            if feat == "exhaust_gas_temp_c":
                return np.minimum(df[feat], self.exh_fit.predict(df[["load"]]))
            if feat == "steam_generated_kg_h":
                return np.minimum(df[feat], self.steam_fit.predict(df[["fabric_processed_kg_h"]]))
            if feat == "feedwater_temp_c":
                return np.maximum(df[feat], b["feedwater_temp_c"])
        else:
            run = df["running"] == 1
            if driver == "idle_firing":
                if feat == "chamber_temp_c":
                    return np.where(run, df[feat], np.minimum(df[feat], b["idle_temp"]))
                return np.where(run, df[feat], np.minimum(df[feat], b["idle_damper"]))
            if feat == "exhaust_damper_pct":
                return np.where(run, np.minimum(df[feat], b["exhaust_damper_pct"]), df[feat])
            if feat == "airflow_pct":
                return np.where(run, np.maximum(df[feat], b["airflow_pct"]), df[feat])
            if feat == "outlet_moisture_pct":
                return np.where(run, np.maximum(df[feat], b["outlet_moisture_pct"]), df[feat])
            if feat == "inlet_moisture_pct":
                return np.where(run, np.minimum(df[feat], b["inlet_moisture_pct"]), df[feat])
        return df[feat]

    def display_healthy(self, df: pd.DataFrame, ev: str, key: str) -> float:
        b = self.baselines
        if self.mtype == "boiler":
            if ev == "exhaust_gas_temp_c":
                return float(np.mean(self.exh_fit.predict(df[["load"]])))
            if ev == "steam_generated_kg_h":
                return float(np.mean(self.steam_fit.predict(df[["fabric_processed_kg_h"]])))
            return float(b[ev])
        if key == "idle_firing":
            return b["idle_temp"]
        return float(b.get(ev, df[ev].mean()))

    def _retemp(self, x: pd.DataFrame) -> pd.Series:
        """Chamber temperature an operator would need for the given conditions."""
        run = x["running"] == 1
        need = self.temp_fit.predict(x[["inlet_moisture_pct", "airflow_pct", "fabric_speed_m_min"]])
        # over-drying is captured separately; keep any extra above the need
        extra = x["chamber_temp_c"] - need
        return np.where(run, need + np.clip(extra, -5, None), x["chamber_temp_c"])

    def driver_deltas(self, df: pd.DataFrame, prepared: bool = False) -> pd.DataFrame:
        """Per-row CO2 (kg/h) each driver adds: twin prediction now minus the
        prediction with that driver reset to healthy."""
        df = df if prepared else self.prepare(df)
        keys = list(DRIVERS[self.mtype])
        preds = self.predict_many([df] + [self._healthy_for(df, k) for k in keys])
        base = preds[0]["co2"]
        return pd.DataFrame({k: base - p["co2"] for k, p in zip(keys, preds[1:])}, index=df.index)

    def _healthy_for(self, df: pd.DataFrame, key: str) -> pd.DataFrame:
        d = DRIVERS[self.mtype][key]
        x = df.copy()
        for feat in d["features"]:
            if feat == "chamber_temp_c" and key in ("airflow", "inlet_moisture"):
                continue
            x[feat] = self.healthy_value(x, feat, key)
        if key in ("airflow", "inlet_moisture"):
            old_need = self.temp_fit.predict(df[["inlet_moisture_pct", "airflow_pct", "fabric_speed_m_min"]])
            new_need = self.temp_fit.predict(x[["inlet_moisture_pct", "airflow_pct", "fabric_speed_m_min"]])
            x["chamber_temp_c"] = np.where(x["running"] == 1,
                                           x["chamber_temp_c"] - np.maximum(old_need - new_need, 0),
                                           x["chamber_temp_c"])
        if key == "overdrying":
            gap = np.maximum(x["outlet_moisture_pct"] - df["outlet_moisture_pct"], 0)
            x["chamber_temp_c"] = np.where(x["running"] == 1, df["chamber_temp_c"] - 9 * gap,
                                           x["chamber_temp_c"])
        return x

    def drivers(self, df: pd.DataFrame) -> list[dict]:
        df = self.prepare(df)
        deltas = self.driver_deltas(df, prepared=True).mean()
        out = []
        for key, d in DRIVERS[self.mtype].items():
            if d.get("idle_only") and (df["running"] == 1).all():
                continue
            contrib = deltas[key]
            ev = d["evidence"]
            now_v = float(df[ev].mean())
            healthy = self.display_healthy(df, ev, key)
            above = float(contrib) - self.drv_med[key]  # beyond what this driver adds in normal running
            out.append({"driver": key, "label": d["label"], "contribution_kg_h": round(float(contrib), 1),
                        "above_normal_kg_h": round(above, 1),
                        "significance": round(above / self.drv_mad[key], 1),
                        "evidence": ev, "evidence_label": d["evidence_label"], "current": round(now_v, 2), "healthy": round(healthy, 2),
                        "unit": d["unit"], "action": d["action"]})
        tot = sum(max(o["contribution_kg_h"], 0) for o in out) or 1
        for o in out:
            o["share_pct"] = round(max(o["contribution_kg_h"], 0) / tot * 100, 1)
        key = {"raw": lambda o: -o["contribution_kg_h"], "above": lambda o: -o["above_normal_kg_h"],
               "z": lambda o: -o["significance"]}[DRIVER_RANK]
        return sorted(out, key=key)

    # ------------------------------------------------------------------
    # 3. simulation of an action (what-if)
    # ------------------------------------------------------------------
    def transform(self, df: pd.DataFrame, action: str, overrides: dict | None = None) -> pd.DataFrame:
        x = df.copy()
        b = self.baselines
        o = overrides or {}
        if self.mtype == "boiler":
            if action == "tune_o2":
                x["flue_o2_pct"] = np.minimum(x["flue_o2_pct"], o.get("flue_o2_pct", b["o2_target"]))
            elif action == "soot_blow":
                clean = self.exh_fit.predict(x[["load"]]) - o.get("extra_c", 3)
                x["exhaust_gas_temp_c"] = np.minimum(x["exhaust_gas_temp_c"], clean)
            elif action == "repair_leaks":
                x["steam_generated_kg_h"] = np.minimum(
                    x["steam_generated_kg_h"], self.steam_fit.predict(x[["fabric_processed_kg_h"]]))
            elif action == "restore_condensate":
                x["feedwater_temp_c"] = np.maximum(x["feedwater_temp_c"], o.get("feedwater_temp_c", 85.0))
            elif action == "custom":
                for k in ("flue_o2_pct", "exhaust_gas_temp_c", "feedwater_temp_c"):
                    if k in o:
                        x[k] = o[k]
        else:
            run = x["running"] == 1
            if action == "humidity_control":
                x["exhaust_damper_pct"] = np.where(run, np.minimum(x["exhaust_damper_pct"],
                                                                   o.get("exhaust_damper_pct", b["damper_target"])),
                                                   x["exhaust_damper_pct"])
            elif action == "clean_filters":
                new = x.copy()
                new["airflow_pct"] = np.where(run, np.maximum(x["airflow_pct"], 92.0), x["airflow_pct"])
                old_need = self.temp_fit.predict(x[["inlet_moisture_pct", "airflow_pct", "fabric_speed_m_min"]])
                new_need = self.temp_fit.predict(new[["inlet_moisture_pct", "airflow_pct", "fabric_speed_m_min"]])
                new["chamber_temp_c"] = np.where(run, x["chamber_temp_c"] - np.maximum(old_need - new_need, 0),
                                                 x["chamber_temp_c"])
                x = new
            elif action == "reduce_temperature":
                target = o.get("outlet_moisture_pct", b["moisture_target"])
                gap = np.maximum(target - x["outlet_moisture_pct"], 0)
                x["outlet_moisture_pct"] = np.where(run, np.maximum(x["outlet_moisture_pct"], target),
                                                    x["outlet_moisture_pct"])
                x["chamber_temp_c"] = np.where(run, x["chamber_temp_c"] - 9 * gap, x["chamber_temp_c"])
            elif action == "idle_shutoff":
                x["chamber_temp_c"] = np.where(run, x["chamber_temp_c"], np.minimum(x["chamber_temp_c"], 90))
                x["exhaust_damper_pct"] = np.where(run, x["exhaust_damper_pct"],
                                                   np.minimum(x["exhaust_damper_pct"], 35))
            elif action == "raise_padder":
                target = o.get("inlet_moisture_pct", 60.0)
                new = x.copy()
                new["inlet_moisture_pct"] = np.where(run, np.minimum(x["inlet_moisture_pct"], target),
                                                     x["inlet_moisture_pct"])
                old_need = self.temp_fit.predict(x[["inlet_moisture_pct", "airflow_pct", "fabric_speed_m_min"]])
                new_need = self.temp_fit.predict(new[["inlet_moisture_pct", "airflow_pct", "fabric_speed_m_min"]])
                new["chamber_temp_c"] = np.where(run, x["chamber_temp_c"] - np.maximum(old_need - new_need, 0),
                                                 x["chamber_temp_c"])
                x = new
            elif action == "custom":
                for k in ("exhaust_damper_pct", "chamber_temp_c", "airflow_pct"):
                    if k in o:
                        x[k] = np.where(run, o[k], x[k])
        return x

    def simulate(self, df: pd.DataFrame, action: str, calib: float = 1.0,
                 overrides: dict | None = None) -> dict:
        df = self.prepare(df)
        part, scale = df, 1.0
        if action == "idle_shutoff":  # only idle time changes: saving per idle hour x normal idle share
            idle = df[df["running"] == 0]
            if len(idle):
                part, scale = idle, self.idle_frac
        cur, c0, new = self.predict_many([df, part, self.transform(part, action, overrides)])
        d_co2 = float(c0["co2"].mean() - new["co2"].mean()) * calib * scale
        d_fuel = float(c0["fuel"].mean() - new["fuel"].mean()) * calib * scale
        d_power = float(c0["power"].mean() - new["power"].mean()) * calib * scale
        a = ACTIONS.get(action, {"downtime_min": 0})
        prod = float(df["production_kg_h"].mean())
        prod_loss_day = prod * a.get("downtime_min", 0) / 60
        cur_co2 = float(cur["co2"].mean())
        return {
            "action": action,
            "current": {"co2_kg_h": round(cur_co2, 1), "fuel_rate": round(float(cur["fuel"].mean()), 2),
                        "power_kw": round(float(cur["power"].mean()), 1),
                        "production_kg_h": round(prod, 1),
                        "co2_intensity_kg_per_kg": round(cur_co2 / max(prod, 1), 3)},
            "simulated": {"co2_kg_h": round(cur_co2 - d_co2, 1),
                          "fuel_rate": round(float(cur["fuel"].mean()) - d_fuel, 2),
                          "power_kw": round(float(cur["power"].mean()) - d_power, 1),
                          "production_kg_h": round(prod, 1),
                          "co2_intensity_kg_per_kg": round((cur_co2 - d_co2) / max(prod, 1), 3)},
            "saving": {"co2_kg_h": round(d_co2, 1), "co2_t_day": round(d_co2 * 24 / 1000, 2),
                       "co2_pct": round(d_co2 / max(cur_co2, 1) * 100, 1),
                       "fuel_pct": round(d_fuel / max(float(cur["fuel"].mean()), 1e-6) * 100, 1),
                       "fuel_per_day": round(d_fuel * 24, 1),
                       "inr_per_day": round((d_fuel * self.fuel_price + d_power * self.power_price) * 24),
                       "production_loss_kg": round(prod_loss_day, 0)},
            "calibration": round(calib, 3),
        }


# --------------------------------------------------------------------------
# 4. recommendation ranking
# --------------------------------------------------------------------------
def rank_actions(sims: list[dict]) -> list[dict]:
    """sims carry their own learned "confidence" (per machine and action)."""
    good = [s for s in sims if s["saving"]["co2_kg_h"] > 0.5]
    if not good:
        return []
    max_t = max(s["saving"]["co2_t_day"] for s in good) or 1
    max_inr = max(s["saving"]["inr_per_day"] for s in good) or 1
    for s in good:
        a = ACTIONS[s["action"]]
        conf = s.get("confidence", 0.7)
        score = (0.5 * s["saving"]["co2_t_day"] / max_t + 0.2 * s["saving"]["inr_per_day"] / max_inr
                 + 0.15 * conf - 0.08 * (a["effort"] - 1) / 2
                 - 0.07 * min(1, s["saving"]["production_loss_kg"] / 500))
        s["score"] = round(score, 3)
        s["confidence"] = round(conf, 2)
        t_day, pct = s["saving"]["co2_t_day"], s["saving"]["co2_pct"]
        s["impact"] = "High" if pct >= 5 else "Medium" if pct >= 2 else "Low"
        s["priority"] = ("High" if (t_day >= 0.5 or pct >= 5) and a["effort"] <= 2 else
                         "Medium" if t_day >= 0.2 or pct >= 2 else "Low")
        s.update({k: a[k] for k in ("title", "detail", "effort", "downtime_min", "capex", "time", "machine")})
    rank = {"High": 0, "Medium": 1, "Low": 2}
    return sorted(good, key=lambda s: (rank[s["priority"]], -s["score"]))


# --------------------------------------------------------------------------
# 5. verify & learn
# --------------------------------------------------------------------------
def measure_saving(model: MachineModel, pre: pd.DataFrame, post: pd.DataFrame, action: str) -> float:
    """Adjusted-baseline M&V: what would post-period CO2 have been with the
    pre-action driver settings, minus what was actually measured."""
    pre = model.prepare(pre)
    post = model.prepare(post)
    drivers = [d for d in DRIVERS[model.mtype].values() if d["action"] == action]
    feats = {f for d in drivers for f in d["features"]}
    if action == "clean_filters":
        feats |= {"airflow_pct", "chamber_temp_c"}
    if action == "raise_padder":
        feats |= {"inlet_moisture_pct", "chamber_temp_c"}
    cf = post.copy()
    if model.mtype == "stenter":
        run_pre = pre[pre.running == 1]
        idle_pre = pre[pre.running == 0]
        for f in feats:
            if action == "idle_shutoff":
                if len(idle_pre):
                    cf[f] = np.where(cf.running == 0, idle_pre[f].mean(), cf[f])
            elif f in ("exhaust_damper_pct", "airflow_pct", "outlet_moisture_pct", "inlet_moisture_pct",
                       "chamber_temp_c") and len(run_pre):
                if f == "chamber_temp_c" and action not in ("reduce_temperature", "clean_filters", "raise_padder"):
                    continue
                shift = run_pre[f].mean() - post[post.running == 1][f].mean() if (post.running == 1).any() else 0
                cf[f] = np.where(cf.running == 1, cf[f] + shift, cf[f])
    else:
        for f in feats:
            if f == "exhaust_gas_temp_c":
                cf[f] = cf[f] + (pre["exhaust_dev_c"].mean() - post["exhaust_dev_c"].mean())
            elif f == "steam_generated_kg_h":
                cf[f] = cf[f] * (pre["steam_ratio"].mean() / post["steam_ratio"].mean())
            else:
                cf[f] = cf[f] + (pre[f].mean() - post[f].mean())
    baseline = model.predict(cf)["co2"].mean()
    return float(baseline - post["co2_kg_h"].mean())


# --------------------------------------------------------------------------
# 6. forecast if no action is taken
# --------------------------------------------------------------------------
def forecast(plant: pd.DataFrame, now, daily_cap: float, roll_cap: float,
             saving_t_day: float = 0.0, excess_t_day: float = 0.0, horizon_days: int = 21) -> dict:
    """plant: rows of all machines with timestamp & co2_kg_h at 5-min ticks."""
    p = plant[["timestamp", "co2_kg_h"]].copy()
    p["t"] = p["co2_kg_h"] * 5 / 60 / 1000  # tonnes per tick
    p["date"] = pd.to_datetime(p["timestamp"]).dt.normalize()
    daily = p.groupby("date")["t"].sum()
    today = pd.Timestamp(now).normalize()
    complete = daily[daily.index < today]

    # --- today
    today_so_far = float(daily.get(today, 0.0))
    last2h = p[pd.to_datetime(p["timestamp"]) > pd.Timestamp(now) - pd.Timedelta(hours=2)]
    rate_t_h = float(last2h["co2_kg_h"].sum() / max(last2h["timestamp"].nunique(), 1)) / 1000
    hours_left = 24 - (pd.Timestamp(now) - today).total_seconds() / 3600
    proj_today = today_so_far + rate_t_h * hours_left
    cross_today = None
    if today_so_far >= daily_cap:
        cross_today = "already exceeded"
    elif proj_today > daily_cap and rate_t_h > 0:
        hrs = (daily_cap - today_so_far) / rate_t_h
        cross_today = (pd.Timestamp(now) + pd.Timedelta(hours=hrs)).strftime("%H:%M")

    # --- rolling 30 days: day-of-week profile x linear trend on recent weekdays
    recent = complete.iloc[-28:]
    dow = recent.groupby(recent.index.day_of_week).mean()
    wk = complete.iloc[-14:]
    wk = wk[wk.index.day_of_week != 6]
    slope = 0.0
    if len(wk) >= 5:
        xs = np.arange(len(wk))
        slope = float(np.polyfit(xs, wk.values, 1)[0]) / (len(complete.iloc[-14:]) / max(len(wk), 1))
    base_level = recent.mean()
    hist_days = list(complete.index)
    vals = list(complete.values)
    proj_none, proj_act = [], []
    series_none = dict(zip(hist_days, vals))
    series_act = dict(zip(hist_days, vals))
    # today (projected end of day)
    series_none[today] = proj_today
    series_act[today] = proj_today
    for i in range(1, horizon_days + 1):
        d = today + pd.Timedelta(days=i)
        v = float(dow.get(d.day_of_week, base_level)) + slope * i
        series_none[d] = v + excess_t_day  # unresolved excess keeps running
        series_act[d] = max(0.0, v + excess_t_day - saving_t_day)

    def rolling(series):
        s = pd.Series(series).sort_index()
        return s.rolling(30, min_periods=30).sum()

    r_none, r_act = rolling(series_none), rolling(series_act)
    future = r_none[r_none.index >= today]
    cross = future[future > roll_cap]
    cross_date = None if cross.empty else cross.index[0]
    fa = r_act[r_act.index >= today]
    cross_act = fa[fa > roll_cap]
    cur_roll = float(r_none.get(today, np.nan))
    return {
        "today": {"so_far_t": round(today_so_far, 2), "projected_t": round(proj_today, 2), "cap_t": daily_cap,
                  "run_rate_t_h": round(rate_t_h, 3), "cross_time": cross_today,
                  "pct_of_cap": round(proj_today / daily_cap * 100, 1)},
        "rolling": {"cap_t": roll_cap, "current_t": round(cur_roll, 1) if not math.isnan(cur_roll) else None,
                    "cross_date": None if cross_date is None else cross_date.strftime("%Y-%m-%d"),
                    "days_to_cross": None if cross_date is None else int((cross_date - today).days),
                    "cross_date_with_actions": None if cross_act.empty else cross_act.index[0].strftime("%Y-%m-%d"),
                    "trend_t_per_day": round(slope, 3), "excess_t_day": round(excess_t_day, 2),
                    "saving_t_day": round(saving_t_day, 2)},
        "series": [{"date": d.strftime("%Y-%m-%d"), "daily_t": round(float(series_none[d]), 2),
                    "rolling_none": None if math.isnan(r_none[d]) or d < today - pd.Timedelta(days=1)
                    else round(float(r_none[d]), 1),
                    "rolling_actions": None if math.isnan(r_act[d]) else round(float(r_act[d]), 1),
                    "projected": bool(d >= today)} for d in sorted(series_none) if d >= today - pd.Timedelta(days=14)],
    }
