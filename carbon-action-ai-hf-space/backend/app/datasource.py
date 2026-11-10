"""Where plant readings come from.

The engine only needs an object with: kind, can_act, step(t) -> rows, apply_action(),
clear_fault(), active_faults(). Two sources exist today:
  simulator.PlantSimulator : the physics-based plant (actions change the machine)
  CsvReplay                : an uploaded CSV of real readings, replayed reading by reading.
                             History cannot be changed, so decisions are switched off.
A live meter feed would be a third source with the same methods.
"""
from __future__ import annotations

import io
from datetime import timedelta

import numpy as np
import pandas as pd

from .simulator import TICK_MIN

MIN_DAYS = 7
TRAIN_SHARE = 0.7
MAX_BYTES = 80 * 1024 * 1024

# columns the models need
REQUIRED = {
    "boiler": ["steam_generated_kg_h", "feedwater_temp_c", "flue_o2_pct", "exhaust_gas_temp_c",
               "fabric_processed_kg_h", "fuel_rate", "electricity_kw"],
    "stenter": ["dry_fabric_kg_h", "inlet_moisture_pct", "outlet_moisture_pct", "chamber_temp_c",
                "exhaust_damper_pct", "airflow_pct", "fabric_speed_m_min", "fuel_rate", "electricity_kw"],
}
# optional columns and what happens without them (NaN = shown as "—", the check using it is skipped)
OPTIONAL = {
    "boiler": ["steam_pressure_bar", "condensate_return_pct", "fd_fan_speed_pct", "id_fan_speed_pct",
               "boiler_efficiency_pct"],
    "stenter": ["gsm_g_m2", "exhaust_humidity_kg_kg", "filter_dp_kpa", "padder_pressure_bar", "exhaust_temp_c",
                "temperature_setpoint_c", "water_evaporated_kg_h", "first_pass_yield_pct", "fabric_type", "batch_id"],
}


class CsvError(ValueError):
    """A problem with the uploaded file, worded for the person who uploaded it."""


def template_columns(mtype: str) -> list[str]:
    return ["timestamp", "machine_id"] + REQUIRED[mtype] + OPTIONAL[mtype]


def parse_csv(fac: dict, raw: bytes) -> tuple[pd.DataFrame, list[str]]:
    """Validate an upload and return (rows on a regular 5-minute grid, warnings)."""
    if len(raw) > MAX_BYTES:
        raise CsvError(f"File is too large (limit {MAX_BYTES // 1024 // 1024} MB).")
    try:
        df = pd.read_csv(io.BytesIO(raw), low_memory=False)
    except Exception as e:
        raise CsvError(f"Could not read this as a CSV file: {e}")
    if "timestamp" not in df or "machine_id" not in df:
        raise CsvError("The file needs 'timestamp' and 'machine_id' columns.")
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    if ts.isna().mean() > 0.01:
        raise CsvError("More than 1% of the timestamps could not be read. Use a format like 2026-10-31 14:05.")
    df = df.assign(timestamp=ts).dropna(subset=["timestamp"])
    if getattr(df["timestamp"].dt, "tz", None) is not None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(None)

    warnings: list[str] = []
    frames = []
    for m in fac["machines"]:
        if not (df["machine_id"].astype(str) == m["id"]).any():
            raise CsvError(f"No rows for machine '{m['id']}' ({m['name']}). "
                           f"This factory needs: {', '.join(x['id'] for x in fac['machines'])}.")
    for m in fac["machines"]:
        g = df[df["machine_id"].astype(str) == m["id"]]
        missing = [c for c in REQUIRED[m["type"]] if c not in g]
        if missing:
            raise CsvError(f"Machine '{m['id']}' is missing required columns: {', '.join(missing)}.")
        frames.append(_clean_machine(fac, m, g, warnings))
    start = max(f["timestamp"].min() for f in frames)
    end = min(f["timestamp"].max() for f in frames)
    days = (end - start).total_seconds() / 86400
    if days < MIN_DAYS:
        raise CsvError(f"The machines overlap for only {days:.1f} days; at least {MIN_DAYS} days are needed to train.")
    out = pd.concat([f[(f["timestamp"] >= start) & (f["timestamp"] <= end)] for f in frames], ignore_index=True)
    return out.sort_values(["timestamp", "machine_id"]).reset_index(drop=True), warnings


def _clean_machine(fac: dict, m: dict, g: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
    mtype = m["type"]
    numeric = [c for c in REQUIRED[mtype] + OPTIONAL[mtype] if c in g and c not in ("fabric_type", "batch_id")]
    g = g.copy()
    for c in numeric:
        g[c] = pd.to_numeric(g[c], errors="coerce")
    g = g.set_index("timestamp").sort_index()
    g = g[~g.index.duplicated(keep="last")]
    step = g.index.to_series().diff().median()
    # regular 5-minute grid: average finer data, carry coarser data for up to 30 minutes
    grid = g[numeric].resample(f"{TICK_MIN}min").mean()
    if step > pd.Timedelta(minutes=TICK_MIN):
        warnings.append(f"{m['id']}: readings arrive every {step} and are held to fill the 5-minute grid, "
                        "so detection cannot be faster than that.")
    grid = grid.ffill(limit=6)
    gaps = float(grid["fuel_rate"].isna().mean())
    if gaps > 0.02:
        warnings.append(f"{m['id']}: {gaps * 100:.0f}% of the period has no fuel reading.")
    grid = grid.reset_index()
    for c in REQUIRED[mtype] + OPTIONAL[mtype]:
        if c not in grid and c not in ("fabric_type", "batch_id"):
            grid[c] = np.nan
    absent = [c for c in OPTIONAL[mtype] if c not in g.columns]
    if absent:
        warnings.append(f"{m['id']}: optional columns not provided: {', '.join(absent)}.")

    grid["machine_id"], grid["machine_type"], grid["factory_id"] = m["id"], mtype, fac["id"]
    grid["fuel_unit"] = m["fuel_unit"] + "/h"
    grid["fault_label"] = "unknown"  # real data: nobody knows the true faults
    # emissions always come from the formula, using this site's factors
    factor = (m["carbon_pct"] / 100 * 44 / 12 * 0.98) if m["fuel"] == "coal" else m["co2_kg_per_unit"]
    grid["co2_direct_kg_h"] = (grid["fuel_rate"] * factor).round(1)
    grid["co2_indirect_kg_h"] = (grid["electricity_kw"] * fac["grid_factor_kg_per_kwh"]).round(1)
    grid["co2_kg_h"] = (grid["co2_direct_kg_h"] + grid["co2_indirect_kg_h"]).round(1)
    if mtype == "stenter":
        grid["status"] = np.where(grid["fabric_speed_m_min"].fillna(0) > 1, "running", "idle")
        grid["gsm_g_m2"] = grid["gsm_g_m2"].fillna(grid["gsm_g_m2"].median()).fillna(150.0)
        for c in ("exhaust_humidity_kg_kg", "filter_dp_kpa"):
            if grid[c].isna().all():
                grid[c] = 0.0  # no such sensor: neutral for the health model
        grid["production_kg_h"] = grid["dry_fabric_kg_h"]
    else:
        grid["status"] = "running"
        grid["production_kg_h"] = grid["fabric_processed_kg_h"]
    return grid


class CsvReplay:
    """Plays uploaded readings forward, one 5-minute step per tick."""
    kind = "csv"
    can_act = False

    def __init__(self, df: pd.DataFrame, name: str):
        cut = df["timestamp"].min() + (df["timestamp"].max() - df["timestamp"].min()) * TRAIN_SHARE
        self.name = name
        self.history = df[df["timestamp"] < cut].copy()
        self.future = df[df["timestamp"] >= cut]
        self.start = pd.Timestamp(cut).floor(f"{TICK_MIN}min").to_pydatetime()
        self.end = self.future["timestamp"].max().to_pydatetime()
        self.now = self.start
        self._rows = {t: g.to_dict("records") for t, g in self.future.groupby("timestamp")}
        self.finished = False
        self.total_steps = len(self._rows)
        self.played = 0

    def step(self, t) -> list[dict]:
        rows = self._rows.pop(pd.Timestamp(t), [])
        if rows:
            self.played += 1
        if t + timedelta(minutes=TICK_MIN) > self.end:
            self.finished = True
        return rows

    # nothing can change history, and there are no injected faults
    def apply_action(self, *_a, **_k):
        return "not applied"

    def clear_fault(self, *_a, **_k):
        pass

    def active_faults(self, *_a, **_k) -> list[str]:
        return []
