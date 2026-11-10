"""Safety and quality limits: the AI never recommends an action that breaks them.

status: ok | caution (allowed, with notes the crew must follow) | blocked (not allowed now)
Limits can be overridden per machine with a "safety" block in config/factories.json.
These defaults are engineering rules of thumb for the prototype, to be replaced
by the plant's own operating limits.
"""
from __future__ import annotations

import math

DEFAULTS = {
    "boiler": {"o2_margin_pct": 0.3, "min_stack_c_coal": 160.0, "min_stack_c_gas": 120.0},
    "stenter": {"moisture_margin_pct": 0.7, "moisture_target_pct": 7.0, "max_exhaust_humidity": 0.10,
                "max_padder_bar": 3.6, "min_damper_pct": 40.0},
}

ALWAYS = {
    "tune_o2": "Lower air in small steps and watch stack CO and smoke: this unit has no CO sensor.",
    "soot_blow": "Soot-blow under the normal permit, with load steady.",
    "repair_leaks": "Isolate and depressurise the line first; work under a permit.",
    "restore_condensate": "Test condensate for dye or chemical contamination before returning it.",
    "humidity_control": "Close the damper gradually and check the fabric still dries to target.",
    "clean_filters": "Stop burners and let the chamber cool first; needs a 30-minute stop.",
    "reduce_temperature": "Lower temperature in steps and check outlet moisture on the next batch.",
    "idle_shutoff": "Allow warm-up time before the next batch starts.",
    "raise_padder": "Raise pressure in steps and check the fabric for marks.",
}


def _num(row: dict, key: str) -> float | None:
    v = row.get(key)
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def check(machine: dict, action: str, row: dict, overrides: dict | None = None) -> dict:
    """row = average of the latest readings for this machine."""
    lim = {**DEFAULTS[machine["type"]], **machine.get("safety", {})}
    o = overrides or {}
    blocked, notes = [], []
    if action in ALWAYS:
        notes.append(ALWAYS[action])

    if machine["type"] == "boiler":
        o2_min = machine["o2_optimal_pct"] + lim["o2_margin_pct"]
        o2 = _num(row, "flue_o2_pct")
        if action == "tune_o2" and o2 is not None and o2 <= o2_min:
            blocked.append(f"Flue O2 is already {o2:.1f}%, at the safe minimum of {o2_min:.1f}%. "
                           "Less air risks carbon monoxide and smoke.")
        if action == "custom":
            if "flue_o2_pct" in o and o["flue_o2_pct"] < machine["o2_optimal_pct"]:
                blocked.append(f"Flue O2 below {machine['o2_optimal_pct']:.1f}% risks carbon monoxide and smoke.")
            min_stack = lim["min_stack_c_coal"] if machine["fuel"] == "coal" else lim["min_stack_c_gas"]
            if "exhaust_gas_temp_c" in o and o["exhaust_gas_temp_c"] < min_stack:
                notes.append(f"Stack temperature below {min_stack:.0f} °C risks acid corrosion in the flue.")
    else:
        running = row.get("status") != "idle"
        out = _num(row, "outlet_moisture_pct")
        floor = lim["moisture_target_pct"] - lim["moisture_margin_pct"]
        if action == "reduce_temperature" and running and out is not None and out >= floor:
            blocked.append(f"Outlet moisture is {out:.1f}%, already close to the {lim['moisture_target_pct']:.0f}% "
                           "target. Lower temperature risks under-dried fabric.")
        hum = _num(row, "exhaust_humidity_kg_kg")
        if action == "humidity_control" and running and hum is not None and hum >= lim["max_exhaust_humidity"]:
            blocked.append(f"Exhaust humidity is {hum:.3f} kg/kg, at the {lim['max_exhaust_humidity']:.2f} limit. "
                           "Closing the damper further risks poor drying.")
        pad = _num(row, "padder_pressure_bar")
        if action == "raise_padder" and pad is not None and pad >= lim["max_padder_bar"]:
            blocked.append(f"Padder pressure is {pad:.1f} bar, at the {lim['max_padder_bar']:.1f} bar limit. "
                           "More pressure risks marking the fabric.")
        if action == "custom":
            if "exhaust_damper_pct" in o and o["exhaust_damper_pct"] < lim["min_damper_pct"]:
                blocked.append(f"Exhaust damper below {lim['min_damper_pct']:.0f}% risks poor drying and fume build-up.")
            cur_t = _num(row, "chamber_temp_c")
            if "chamber_temp_c" in o and cur_t is not None and running and o["chamber_temp_c"] < cur_t - 10:
                notes.append("This much lower temperature may leave fabric under-dried; check outlet moisture.")

    status = "blocked" if blocked else "caution" if notes else "ok"
    return {"status": status, "notes": blocked + notes}
