"""Physics-based plant simulator for a textile dyeing & printing unit.

Each machine is driven by simple, well-known energy balances so that emissions
respond to real operating causes:

Boiler  : fuel = steam x (h_steam - h_feedwater) / (efficiency x calorific value)
          efficiency falls with flue O2 (excess air) and stack temperature (fouling).
Stenter : gas  = (water evaporated x latent heat + fabric heating
                  + exhaust-air heat loss + radiation) / (burner efficiency x CV)
          exhaust-air loss rises with damper opening; water evaporated follows
          inlet moisture (padder) and outlet moisture (over-drying).

Faults are injected as changes to the physical state, never to the emission
numbers directly, so the ML layer has to discover them from sensor data.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

TICK_MIN = 5  # simulated minutes per record
H_STEAM_KJ = 2778.0  # saturated steam ~10 bar(a)

# --------------------------------------------------------------------------
# Fault catalogue: what goes wrong physically, and how it shows up
# --------------------------------------------------------------------------
FAULTS = {
    "boiler_excess_air": {
        "machine": "boiler", "label": "Excess combustion air",
        "desc": "FD fan damper drifted open; flue O2 well above optimum.",
    },
    "boiler_fouling": {
        "machine": "boiler", "label": "Tube fouling / soot build-up",
        "desc": "Fire-side deposits raise stack temperature and waste heat.",
    },
    "boiler_steam_leak": {
        "machine": "boiler", "label": "Steam leak / failed traps",
        "desc": "Steam generated rises with no rise in production.",
    },
    "boiler_economizer": {
        "machine": "boiler", "label": "Low feedwater temperature",
        "desc": "Economizer bypass / poor condensate return; colder feedwater.",
    },
    "stenter_damper_open": {
        "machine": "stenter", "label": "Exhaust damper too open",
        "desc": "Excess hot air exhausted; exhaust humidity very low.",
    },
    "stenter_filter_clog": {
        "machine": "stenter", "label": "Lint filter clogged",
        "desc": "Airflow falls; operators raise temperature to compensate.",
    },
    "stenter_overdrying": {
        "machine": "stenter", "label": "Over-drying",
        "desc": "Temperature set too high; outlet moisture far below target.",
    },
    "stenter_idle_burners": {
        "machine": "stenter", "label": "Burners firing while idle",
        "desc": "Chamber kept at full temperature with no fabric running.",
    },
    "stenter_low_padder": {
        "machine": "stenter", "label": "Low padder squeeze",
        "desc": "Fabric enters wetter, so more water must be evaporated.",
    },
}


# Faults in the instrument, not the machine: only the reported values go wrong.
SENSOR_FAULTS = {
    "sensor_o2_drift": {"machine": "boiler", "label": "Flue O2 sensor drifting",
                        "desc": "O2 probe reads about 2.5 % too high; the boiler itself is fine."},
    "sensor_fuel_meter": {"machine": "boiler", "label": "Fuel meter over-reading",
                          "desc": "Fuel meter reads about 12 % high, so reported CO2 is too high."},
    "sensor_steam_frozen": {"machine": "boiler", "label": "Steam meter frozen",
                            "desc": "Steam flow signal stuck at one value."},
    "sensor_chamber_dead": {"machine": "stenter", "label": "Chamber temperature signal lost",
                            "desc": "Chamber temperature sends no data."},
    "sensor_airflow_frozen": {"machine": "stenter", "label": "Airflow sensor frozen",
                              "desc": "Airflow signal stuck at one value."},
}


@dataclass
class ActiveFault:
    kind: str
    start: datetime
    end: datetime | None  # None = until fixed by an action
    severity: float = 1.0
    ramp_min: int = 60

    def level(self, t: datetime) -> float:
        if t < self.start or (self.end is not None and t >= self.end):
            return 0.0
        ramp = min(1.0, (t - self.start).total_seconds() / 60 / max(self.ramp_min, 1))
        return ramp * self.severity


@dataclass
class BoilerState:
    o2_trim: float = 0.0  # operator O2 offset from optimum (actions can lower)
    fouling_c: float = 4.0  # stack temperature rise from deposits
    fouling_rate_c_day: float = 0.7
    feedwater_c: float = 82.0
    condensate_pct: float = 58.0
    leak_kg_h: float = 0.0


@dataclass
class StenterState:
    damper_base: float = 55.0
    filter_dp: float = 0.18  # kPa
    filter_rate_day: float = 0.012
    padder_bar: float = 3.0
    temp_bias: float = 0.0
    idle_shutoff: bool = False  # True after "auto burner shut-off" action
    humidity_control: bool = False


@dataclass
class MachineSim:
    cfg: dict
    rng: random.Random
    faults: list = field(default_factory=list)

    def fault_level(self, kind: str, t: datetime) -> float:
        return max((f.level(t) for f in self.faults if f.kind == kind), default=0.0)


class PlantSimulator:
    """Simulates one factory (boiler + stenter) tick by tick."""
    kind = "simulator"
    can_act = True  # actions change the (simulated) machine

    def __init__(self, factory: dict, seed: int = 7):
        self.factory = factory
        self.rng = random.Random(seed)
        self.machines = {}
        for m in factory["machines"]:
            sim = MachineSim(cfg=m, rng=random.Random(seed * 31 + len(self.machines)))
            sim.state = BoilerState() if m["type"] == "boiler" else StenterState()
            self.machines[m["id"]] = sim
        # stenter batch schedule state
        self._batch_left = 0
        self._idle_left = 0
        self._gsm = 150
        self._fabric = "cotton"
        self._batch_no = 0
        self._dye_load = 0.85
        self._o2_noise = 0.0
        self._t_noise = 0.0
        self.sensor_faults: dict[str, dict] = {}  # kind -> {"start", "end", "held"}

    # ------------------------------------------------------------------
    def add_sensor_fault(self, kind: str, start: datetime, hours: float | None = None):
        end = None if hours is None else start + timedelta(hours=hours)
        self.sensor_faults[kind] = {"start": start, "end": end, "held": None}

    def _sensor_faults_on(self, t: datetime, row: dict):
        for kind, f in self.sensor_faults.items():
            if SENSOR_FAULTS[kind]["machine"] != row["machine_type"] or t < f["start"] or \
                    (f["end"] is not None and t >= f["end"]):
                continue
            if kind == "sensor_o2_drift":
                row["flue_o2_pct"] = round(row["flue_o2_pct"] + 2.5, 2)
            elif kind == "sensor_fuel_meter":
                row["fuel_rate"] = round(row["fuel_rate"] * 1.12, 2)
                row["co2_direct_kg_h"] = round(row["co2_direct_kg_h"] * 1.12, 1)
                row["co2_kg_h"] = round(row["co2_direct_kg_h"] + row["co2_indirect_kg_h"], 1)
            elif kind == "sensor_steam_frozen":
                f["held"] = row["steam_generated_kg_h"] if f["held"] is None else f["held"]
                row["steam_generated_kg_h"] = f["held"]
            elif kind == "sensor_chamber_dead":
                row["chamber_temp_c"] = float("nan")
            elif kind == "sensor_airflow_frozen":
                f["held"] = row["airflow_pct"] if f["held"] is None else f["held"]
                row["airflow_pct"] = f["held"]

    def machine_of_type(self, mtype: str) -> MachineSim:
        return next(s for s in self.machines.values() if s.cfg["type"] == mtype)

    def add_fault(self, kind: str, start: datetime, hours: float | None,
                  severity: float = 1.0, ramp_min: int = 45):
        sim = self.machine_of_type(FAULTS[kind]["machine"])
        end = None if hours is None else start + timedelta(hours=hours)
        sim.faults.append(ActiveFault(kind, start, end, severity, ramp_min))

    def clear_fault(self, kind: str, at: datetime):
        sim = self.machine_of_type(FAULTS[kind]["machine"])
        for f in sim.faults:
            if f.kind == kind and (f.end is None or f.end > at):
                f.end = at

    def active_faults(self, t: datetime) -> list[str]:
        out = []
        for sim in self.machines.values():
            for f in sim.faults:
                if f.level(t) > 0.05:
                    out.append(f.kind)
        return sorted(set(out))

    # ------------------------------------------------------------------
    def _shift_factor(self, t: datetime) -> float:
        h = t.hour + t.minute / 60
        f = 1.0 if 6 <= h < 22 else 0.82
        if t.weekday() == 6:
            f *= 0.55
        return f

    def step(self, t: datetime) -> list[dict]:
        """Produce one record per machine for time t."""
        g = self.rng
        # dyeing load drives process steam demand (slow random walk around shift level)
        target = self._shift_factor(t) * 0.92
        self._dye_load += 0.15 * (target - self._dye_load) + g.gauss(0, 0.025)
        self._dye_load = min(1.05, max(0.25, self._dye_load))
        rows = []
        stn = self.machine_of_type("stenter")
        blr = self.machine_of_type("boiler")
        rows.append(self._step_stenter(stn, t))
        rows.append(self._step_boiler(blr, t))
        for row in rows:
            self._sensor_faults_on(t, row)
        return rows

    # ------------------------------------------------------------------
    def _step_boiler(self, sim: MachineSim, t: datetime) -> dict:
        c, s, g = sim.cfg, sim.state, sim.rng
        dt_day = TICK_MIN / 1440
        s.fouling_c += s.fouling_rate_c_day * dt_day

        nominal_fabric = self.machine_of_type("stenter").cfg["width_m"] * \
            self.machine_of_type("stenter").cfg["nominal_speed_m_min"] * 60 * 0.15
        fabric_processed = max(0.0, nominal_fabric * self._dye_load * (1 + g.gauss(0, 0.03)))
        if not hasattr(self, "t0"):
            self.t0 = t
        growth = 1 + 0.0015 * (t - self.t0).total_seconds() / 86400  # order book creeping up
        fabric_processed *= growth
        steam_process = 300 + 3.6 * fabric_processed + g.gauss(0, 40)

        leak = s.leak_kg_h + 320 * sim.fault_level("boiler_steam_leak", t)
        steam = steam_process + leak
        load = steam / c["rated_steam_kg_h"]

        self._o2_noise = 0.85 * self._o2_noise + g.gauss(0, 0.18)
        o2 = c["o2_optimal_pct"] + 0.6 + s.o2_trim + self._o2_noise \
            + 3.8 * sim.fault_level("boiler_excess_air", t)
        o2 = min(14.0, max(c["o2_optimal_pct"] - 0.5, o2))

        fouling = s.fouling_c + 60 * sim.fault_level("boiler_fouling", t)
        t_exh = c["exhaust_base_c"] + 28 * (load - 0.6) + fouling + g.gauss(0, 2.0)

        cond = s.condensate_pct - 18 * sim.fault_level("boiler_economizer", t) \
            - 6 * sim.fault_level("boiler_steam_leak", t) + g.gauss(0, 1.2)
        t_fw = s.feedwater_c - 0.45 * (s.condensate_pct - cond) \
            - 22 * sim.fault_level("boiler_economizer", t) + g.gauss(0, 0.8)

        ea = o2 / (21 - o2)
        ea0 = c["o2_optimal_pct"] / (21 - c["o2_optimal_pct"])
        t_amb = 30 + 4 * math.sin((t.hour - 9) / 24 * 2 * math.pi)
        coef = 0.13 / ((c["exhaust_base_c"] - 30) * (1 + ea0)) if c["fuel"] == "coal" \
            else 0.10 / ((c["exhaust_base_c"] - 30) * (1 + ea0))
        flue_loss = coef * (t_exh - t_amb) * (1 + ea)
        other_loss = (1 - c["base_efficiency"]) - coef * (c["exhaust_base_c"] - 30) * (1 + ea0)
        eff = max(0.55, 1 - flue_loss - other_loss + g.gauss(0, 0.003))

        heat_mj = steam * (H_STEAM_KJ - 4.19 * t_fw) / 1000
        fuel = heat_mj / eff / c["cv_mj_per_unit"]
        if c["fuel"] == "coal":
            co2 = fuel * c["carbon_pct"] / 100 * 44 / 12 * 0.98
        else:
            co2 = fuel * c["co2_kg_per_unit"]

        fd = min(100, 38 + 42 * load * (1 + ea) / (1 + ea0) + g.gauss(0, 1.2))
        idf = min(100, fd * 0.9 + 4 + g.gauss(0, 1.0))
        elec = 18 + 0.0009 * fd ** 3 / 10 + 0.0008 * idf ** 3 / 10 + 0.004 * steam
        pressure = 8.6 - 0.9 * max(0, load - 0.85) - 0.35 * sim.fault_level("boiler_steam_leak", t) \
            + g.gauss(0, 0.06)
        grid = self.factory["grid_factor_kg_per_kwh"]

        return {
            "timestamp": t, "factory_id": self.factory["id"], "machine_id": c["id"],
            "machine_type": "boiler", "status": "running",
            "fabric_processed_kg_h": round(fabric_processed, 1),
            "steam_generated_kg_h": round(steam, 1),
            "steam_pressure_bar": round(pressure, 2),
            "feedwater_temp_c": round(t_fw, 1),
            "condensate_return_pct": round(cond, 1),
            "flue_o2_pct": round(o2, 2),
            "exhaust_gas_temp_c": round(t_exh, 1),
            "fd_fan_speed_pct": round(fd, 1),
            "id_fan_speed_pct": round(idf, 1),
            "boiler_efficiency_pct": round(eff * 100, 2),
            "fuel_rate": round(fuel, 2),
            "fuel_unit": c["fuel_unit"] + "/h",
            "electricity_kw": round(elec, 1),
            "co2_direct_kg_h": round(co2, 1),
            "co2_indirect_kg_h": round(elec * grid, 1),
            "co2_kg_h": round(co2 + elec * grid, 1),
            "production_kg_h": round(fabric_processed, 1),
            "fault_label": ",".join(k for k in FAULTS if FAULTS[k]["machine"] == "boiler"
                                    and sim.fault_level(k, t) > 0.3) or "normal",
        }

    # ------------------------------------------------------------------
    def _next_batch(self, g: random.Random, t: datetime):
        self._batch_no += 1
        self._fabric, self._gsm = g.choice([("cotton", 140), ("cotton", 165), ("cotton_poly_blend", 150),
                                            ("polyester", 120), ("cotton", 190), ("viscose", 130),
                                            ("cotton_poly_blend", 210)])
        hours = g.uniform(2.0, 5.0) * (0.6 if t.weekday() == 6 else 1.0)
        self._batch_left = int(hours * 60 / TICK_MIN)

    def _step_stenter(self, sim: MachineSim, t: datetime) -> dict:
        c, s, g = sim.cfg, sim.state, sim.rng
        dt_day = TICK_MIN / 1440
        s.filter_dp += s.filter_rate_day * dt_day
        if t.weekday() == 0 and t.hour == 7 and t.minute < TICK_MIN and s.filter_dp > 0.22:
            s.filter_dp = 0.18  # weekly routine filter cleaning

        # batch / changeover schedule
        if self._batch_left <= 0 and self._idle_left <= 0:
            idle_h = g.uniform(0.3, 0.9) * (3.0 if t.weekday() == 6 else 1.0)
            self._idle_left = int(idle_h * 60 / TICK_MIN)
            self._next_batch(g, t)
        if self._idle_left > 0:
            self._idle_left -= 1
            running = False
        else:
            self._batch_left -= 1
            running = True

        f_idle = sim.fault_level("stenter_idle_burners", t)
        f_damper = sim.fault_level("stenter_damper_open", t)
        f_clog = sim.fault_level("stenter_filter_clog", t)
        f_over = sim.fault_level("stenter_overdrying", t)
        f_pad = sim.fault_level("stenter_low_padder", t)

        gsm = self._gsm
        speed = c["nominal_speed_m_min"] * (150 / gsm) ** 0.55 * (1 + g.gauss(0, 0.015)) if running else 0.0
        dry = speed * 60 * c["width_m"] * gsm / 1000  # kg dry fabric / h
        padder = s.padder_bar - 1.1 * f_pad + g.gauss(0, 0.03)
        m_in = 0.62 + 0.15 * (3.0 - padder) + g.gauss(0, 0.012)  # kg water / kg dry
        target_out = 0.07

        filter_dp = s.filter_dp + 0.55 * f_clog + g.gauss(0, 0.005)
        airflow = max(45.0, 92 - 60 * max(0.0, filter_dp - 0.18) + g.gauss(0, 0.8))

        # operator chooses temperature to hit target moisture given airflow & speed
        k = 2.42
        dwell = (c["nominal_speed_m_min"] / max(speed, 1)) ** 0.6 if running else 1.0
        needed_i = math.log(m_in / target_out) / k
        t_set = 55 + 105 * needed_i / (airflow / 100 * dwell) if running else 165
        t_set = min(205, max(130, t_set)) + s.temp_bias + 18 * f_over
        self._t_noise = 0.8 * self._t_noise + g.gauss(0, 0.9)
        if running:
            t_ch = t_set + self._t_noise
        else:
            idle_target = 100 if not f_idle else t_set
            if s.idle_shutoff:
                idle_target = 90
            t_ch = idle_target + self._t_noise
        intensity = airflow / 100 * (t_ch - 55) / 105 * dwell
        m_out = m_in * math.exp(-k * intensity) if running else 0.0
        water = dry * max(0.0, m_in - m_out)

        damper = s.damper_base + 30 * f_damper + g.gauss(0, 1.0)
        if not running:
            damper = 35 + (s.damper_base - 35) * f_idle + 30 * f_damper * f_idle
        t_amb = 30 + 4 * math.sin((t.hour - 9) / 24 * 2 * math.pi)
        air = 5700 * (damper / 55) * (c["width_m"] / 1.8)  # kg dry air/h exhausted
        t_exh = t_ch - 45 + 0.9 * max(0.0, 92 - airflow) + g.gauss(0, 1.5)
        x_exh = 0.015 + water / max(air, 1)

        # poor convection (clogged filters) means more burner heat leaves unused
        transfer_penalty = 1 + 0.005 * max(0.0, 90 - airflow)
        heat = transfer_penalty * (water * 2.6 + dry * 1.4 * (t_ch - 30) / 1000
                + air * 1.01 * (t_exh - t_amb) / 1000 + 120 * (t_ch / 160))
        if not running and not f_idle:
            heat *= 0.55 if not s.idle_shutoff else 0.4
        gas = heat / (c["burner_efficiency"] * c["cv_mj_per_unit"])
        co2_direct = gas * c["co2_kg_per_unit"]

        recirc = 48 * (airflow / 100) ** 3 * (1 + 0.6 * max(0, filter_dp - 0.18))
        exfan = 14 * (damper / 55)
        drive = 9 * (speed / c["nominal_speed_m_min"]) if running else 1.0
        elec = recirc + exfan + drive + 4
        grid = self.factory["grid_factor_kg_per_kwh"]

        yield_pct = 97.0
        if running:
            if m_out > target_out + 0.025:
                yield_pct -= 4.0 * min(1.0, (m_out - target_out) / 0.05)
            if m_out < target_out - 0.03:
                yield_pct -= 1.5
        good = dry * (1 + m_out) * yield_pct / 100 if running else 0.0

        return {
            "timestamp": t, "factory_id": self.factory["id"], "machine_id": c["id"],
            "machine_type": "stenter", "status": "running" if running else "idle",
            "batch_id": f"{c['id']}-{self._batch_no:04d}", "fabric_type": self._fabric,
            "gsm_g_m2": gsm, "fabric_speed_m_min": round(speed, 1),
            "dry_fabric_kg_h": round(dry, 1),
            "temperature_setpoint_c": round(t_set if running else t_ch, 1),
            "chamber_temp_c": round(t_ch, 1), "exhaust_temp_c": round(t_exh, 1),
            "exhaust_humidity_kg_kg": round(x_exh, 4),
            "exhaust_damper_pct": round(damper, 1), "airflow_pct": round(airflow, 1),
            "filter_dp_kpa": round(filter_dp, 3), "padder_pressure_bar": round(padder, 2),
            "inlet_moisture_pct": round(m_in * 100, 1),
            "outlet_moisture_pct": round(m_out * 100, 2),
            "outlet_target_pct": 7.0,
            "water_evaporated_kg_h": round(water, 1),
            "fuel_rate": round(gas, 2), "fuel_unit": "Nm3/h",
            "electricity_kw": round(elec, 1),
            "co2_direct_kg_h": round(co2_direct, 1),
            "co2_indirect_kg_h": round(elec * grid, 1),
            "co2_kg_h": round(co2_direct + elec * grid, 1),
            "production_kg_h": round(good, 1), "first_pass_yield_pct": round(yield_pct, 1),
            "fault_label": ",".join(k for k in FAULTS if FAULTS[k]["machine"] == "stenter"
                                    and sim.fault_level(k, t) > 0.3) or "normal",
        }

    # ------------------------------------------------------------------
    # Actions: change the physical state (true effect, unknown to the AI)
    # ------------------------------------------------------------------
    def apply_action(self, action_id: str, t: datetime, params: dict | None = None) -> str:
        params = params or {}
        blr = self.machine_of_type("boiler")
        stn = self.machine_of_type("stenter")
        b, s = blr.state, stn.state
        g = self.rng
        if action_id == "tune_o2":
            self.clear_fault("boiler_excess_air", t)
            b.o2_trim = -0.6 + g.uniform(0.0, 0.5)  # real trim lands slightly off target
        elif action_id == "soot_blow":
            self.clear_fault("boiler_fouling", t)
            b.fouling_c = max(0.0, b.fouling_c * g.uniform(0.15, 0.35))
        elif action_id == "repair_leaks":
            self.clear_fault("boiler_steam_leak", t)
            b.leak_kg_h = 0.0
        elif action_id == "restore_condensate":
            self.clear_fault("boiler_economizer", t)
            b.condensate_pct = 58 + g.uniform(3, 7)
            b.feedwater_c = 82 + g.uniform(1, 3)
        elif action_id == "humidity_control":
            self.clear_fault("stenter_damper_open", t)
            s.damper_base = 46 + g.uniform(0, 4)
            s.humidity_control = True
        elif action_id == "clean_filters":
            self.clear_fault("stenter_filter_clog", t)
            s.filter_dp = 0.17
        elif action_id == "reduce_temperature":
            self.clear_fault("stenter_overdrying", t)
            s.temp_bias = -g.uniform(1, 4)
        elif action_id == "idle_shutoff":
            self.clear_fault("stenter_idle_burners", t)
            s.idle_shutoff = True
        elif action_id == "raise_padder":
            self.clear_fault("stenter_low_padder", t)
            s.padder_bar = 3.0 + g.uniform(0.2, 0.4)
        else:
            return "unknown action"
        return "applied"


# --------------------------------------------------------------------------
# History generation with a realistic mix of faults
# --------------------------------------------------------------------------
HISTORY_FAULTS = [
    # (day offset, hour, kind, duration h, severity)
    (1.0, 10, "boiler_excess_air", 9, 1.0),
    (2.5, 14, "stenter_damper_open", 11, 1.0),
    (4.0, 2, "stenter_idle_burners", 6, 1.0),
    (5.2, 9, "boiler_steam_leak", 20, 0.9),
    (6.4, 16, "stenter_overdrying", 7, 1.0),
    (8.0, 11, "stenter_filter_clog", 14, 1.0),
    (9.6, 3, "boiler_economizer", 10, 1.0),
    (11.0, 13, "boiler_fouling", 16, 1.0),
    (12.3, 8, "stenter_low_padder", 9, 1.0),
    (13.7, 19, "boiler_excess_air", 7, 0.8),
    (15.1, 12, "stenter_damper_open", 13, 0.9),
    (16.4, 22, "stenter_idle_burners", 5, 1.0),
    (17.8, 7, "boiler_steam_leak", 12, 1.0),
    (19.2, 15, "stenter_filter_clog", 10, 0.9),
    (20.5, 10, "boiler_fouling", 14, 0.9),
    (21.9, 6, "stenter_overdrying", 8, 0.9),
    (23.2, 17, "boiler_economizer", 9, 0.9),
    (24.6, 9, "stenter_low_padder", 11, 1.0),
    (25.9, 13, "boiler_excess_air", 10, 1.0),
    (27.0, 20, "stenter_damper_open", 9, 1.0),
    (28.2, 11, "stenter_filter_clog", 8, 1.0),
]


def random_fault_schedule(seed: int, days: int = 30, min_sev: float = 0.6, max_sev: float = 1.0) -> list[tuple]:
    """A fresh, randomised fault schedule (kinds, timing, duration, severity)
    for held-out testing: nothing about it is shared with HISTORY_FAULTS."""
    g = random.Random(seed)
    kinds = list(FAULTS)
    out, day = [], g.uniform(0.6, 1.2)
    while day < days - 1.0:
        out.append((round(day, 2), g.randrange(24), g.choice(kinds), round(g.uniform(4, 16), 1),
                    round(g.uniform(min_sev, max_sev), 2)))
        day += g.uniform(1.1, 1.8)
    return out


def generate_history(factory: dict, end: datetime, days: int = 30, seed: int = 7,
                     schedule: list[tuple] | None = None):
    """Return (simulator positioned at `end`, list of records)."""
    sim = PlantSimulator(factory, seed=seed)
    start = end - timedelta(days=days)
    start = start.replace(minute=(start.minute // TICK_MIN) * TICK_MIN, second=0, microsecond=0)
    shift = 0.0 if factory["id"].startswith("surat") else 0.45
    if schedule is not None:
        shift = 0.0
    for day, hour, kind, dur, sev in (HISTORY_FAULTS if schedule is None else schedule):
        when = (start + timedelta(days=int(day + shift))).replace(hour=hour) + \
            timedelta(minutes=int(((day + shift) % 1) * 60))
        if kind in SENSOR_FAULTS:
            sim.add_sensor_fault(kind, when, dur)
        else:
            sim.add_fault(kind, when, dur, sev)
    rows = []
    t = start
    while t < end:
        rows.extend(sim.step(t))
        t += timedelta(minutes=TICK_MIN)
    sim.now = t
    return sim, rows
