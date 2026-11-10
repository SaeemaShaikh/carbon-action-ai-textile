"""Carbon Action AI - FastAPI server.

Run:  python run.py      (then open http://localhost:8000)
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from . import llm  # noqa: E402
from . import report, robustness  # noqa: E402
from .analytics import ACTIONS, DRIVERS  # noqa: E402
from .auth import DUMMY_HASH, LoginLimiter, session_secret, verify_password  # noqa: E402
from .datasource import MAX_BYTES, CsvError, CsvReplay, parse_csv, template_columns  # noqa: E402
from .engine import DATA_DIR, Engine, FactoryRuntime, REPLAY_SECONDS  # noqa: E402
from .evaluation import load_or_build  # noqa: E402
from .simulator import FAULTS, SENSOR_FAULTS  # noqa: E402

CFG = json.loads((ROOT / "config/factories.json").read_text(encoding="utf-8"))
FRONTEND = ROOT.parent / "frontend"
# CLOCK=realtime (default): the plant clock follows the wall clock, one 5-min reading every 5 minutes.
# CLOCK=fast: demo fast-forward, TICK_SECONDS real seconds per 5-min reading.
CLOCK = os.getenv("CLOCK", "realtime")
TICK_SECONDS = float(os.getenv("TICK_SECONDS", "2"))
DEMO_MODE = os.getenv("DEMO_MODE", "1") == "1"
ENGINE: Engine | None = None
REPORT_CACHE = DATA_DIR / "model_report.json"
REPORT: dict = {"status": "building", "data": None, "error": None}
ROLES = {"viewer": 0, "operator": 1, "admin": 2}

USERS = {u["username"]: u for u in CFG["users"]}
LIMITER = LoginLimiter()


async def ticker():
    loop = asyncio.get_running_loop()
    while True:
        realtime = ENGINE.speed is None
        await asyncio.sleep(1 if realtime else ENGINE.speed)
        try:
            # real time: each reading is produced once its 5 minutes are over
            await loop.run_in_executor(None, ENGINE.step_due if realtime else ENGINE.tick)
        except Exception as e:  # keep the app alive
            print("tick error:", repr(e))


def clock_info(fr=None) -> dict:
    if fr is not None and fr.sim.kind == "csv":
        return {"mode": "replay", "seconds_per_reading": REPLAY_SECONDS, "ahead_min": 0,
                "finished": fr.sim.finished}
    return {"mode": "realtime" if ENGINE.speed is None else "fast",
            "seconds_per_reading": 300 if ENGINE.speed is None else ENGINE.speed,
            "ahead_min": ENGINE.ahead_min() if ENGINE.speed is None else 0}


def _build_report():
    """Held-out back-test runs once per code/config version, in the background."""
    try:
        REPORT["data"] = load_or_build(CFG, REPORT_CACHE)
        REPORT["status"] = "ready"
    except Exception as e:  # report is optional: never take the app down
        REPORT["status"], REPORT["error"] = "error", repr(e)
        print("model report failed:", repr(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ENGINE
    print("Building plant history and training models (about 30 s)...", flush=True)
    ENGINE = Engine(CFG)
    if CLOCK == "fast":
        ENGINE.speed = TICK_SECONDS
    if os.getenv("SKIP_REPORT") != "1":
        threading.Thread(target=_build_report, daemon=True).start()
    print(f"Ready. Open http://localhost:{os.getenv('PORT', '8000')}  (login: manager / carbon@123)", flush=True)
    task = asyncio.create_task(ticker())
    yield
    task.cancel()
    with ENGINE.lock:
        ENGINE.store.close()


app = FastAPI(title="Carbon Action AI", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=session_secret(DATA_DIR),
                   same_site="lax", max_age=12 * 3600)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def clean(o):
    """Make numpy / pandas values JSON-safe."""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.floating,)):
        v = float(o)
        return None if np.isnan(v) else v
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    if isinstance(o, float) and np.isnan(o):
        return None
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return o


def current_user(request: Request) -> dict:
    u = request.session.get("user")
    if not u or u not in USERS:
        raise HTTPException(401, "Not logged in")
    return USERS[u]


def require(role: str):
    def dep(user: dict = Depends(current_user)) -> dict:
        if ROLES.get(user.get("role", "viewer"), 0) < ROLES[role]:
            raise HTTPException(403, f"This needs the {role} role")
        return user
    return dep


def demo_admin(user: dict = Depends(require("admin"))) -> dict:
    if not DEMO_MODE:
        raise HTTPException(404, "Demo controls are disabled (DEMO_MODE=0)")
    return user


def factory(fid: str, user: dict = Depends(current_user)):
    if fid not in user["factories"] or fid not in ENGINE.factories:
        raise HTTPException(403, "No access to this factory")
    return ENGINE.factories[fid]


def check_machine(fr, mid: str) -> dict:
    m = fr.machine(mid)
    if m is None:
        raise HTTPException(404, f"Unknown machine '{mid}'")
    return m


def check_action(fr, mid: str, action: str, allow_custom: bool = False):
    m = check_machine(fr, mid)
    if action == "custom" and allow_custom:
        return m
    if action not in ACTIONS:
        raise HTTPException(400, "Unknown action")
    if ACTIONS[action]["machine"] != m["type"]:
        raise HTTPException(400, f"'{action}' does not apply to a {m['type']}")
    return m


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


@app.post("/api/login")
def login(body: Login, request: Request):
    name = body.username.strip().lower()
    keys = ("u:" + name, "ip:" + (request.client.host if request.client else "?"))
    wait = LIMITER.retry_after(*keys)
    if wait:
        raise HTTPException(429, f"Too many failed attempts. Try again in {wait} s.",
                            headers={"Retry-After": str(wait)})
    u = USERS.get(name)
    ok = verify_password(body.password, u["password_hash"] if u else DUMMY_HASH)
    if not u or not ok:
        LIMITER.fail(*keys)
        raise HTTPException(401, "Wrong username or password")
    LIMITER.success(*keys)
    request.session["user"] = u["username"]
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    facs = [{k: f[k] for k in ("id", "name", "location", "industry")} |
            {"machines": [{k: m[k] for k in ("id", "name", "type", "fuel", "fuel_unit")} for m in f["machines"]]}
            for f in CFG["factories"] if f["id"] in user["factories"]]
    role = user.get("role", "viewer")
    return {"username": user["username"], "name": user["name"], "role": role, "factories": facs,
            "can_decide": ROLES[role] >= ROLES["operator"], "demo": DEMO_MODE and role == "admin",
            "clock": clock_info(), "llm": llm.status()}


# ---------------------------------------------------------------------------
# data views
# ---------------------------------------------------------------------------
def _kpis(fr) -> dict:
    df = fr.df
    ts = df["timestamp"]
    now = pd.Timestamp(fr.now)
    wk = df[ts > now - pd.Timedelta(days=7)]
    prev = df[(ts <= now - pd.Timedelta(days=7)) & (ts > now - pd.Timedelta(days=14))]
    h = 5 / 60

    def agg(d):
        b = d[d.machine_type == "boiler"]
        s = d[d.machine_type == "stenter"]
        return {
            "energy_kwh": float(d["electricity_kw"].sum() * h),
            "boiler_fuel": float(b["fuel_rate"].sum() * h),
            "stenter_gas": float(s["fuel_rate"].sum() * h),
            "steam_kg": float(b["steam_generated_kg_h"].sum() * h),
            "co2_t": float(d["co2_kg_h"].sum() * h / 1000),
            "production_kg": float(s["production_kg_h"].sum() * h),
        }

    a, p = agg(wk), agg(prev)
    out = {}
    for k in a:
        out[k] = {"value": round(a[k], 1), "change_pct": round((a[k] / p[k] - 1) * 100, 1) if p[k] else None}
    pi = p["co2_t"] / max(p["production_kg"], 1)
    out["intensity"] = {"value": round(a["co2_t"] * 1000 / max(a["production_kg"], 1), 3),
                        "change_pct": round(((a["co2_t"] / max(a["production_kg"], 1)) / pi - 1) * 100, 1)
                        if pi else None}
    return out


def _latest(fr) -> dict:
    def build():
        out = {}
        for m in fr.fac["machines"]:
            g = fr.machine_df(m["id"], last=6)
            sc = fr.models[m["id"]].score(g).iloc[-1]
            row = g.iloc[-1].to_dict()
            row["expected_co2_kg_h"] = round(float(sc["expected_co2"]), 1)
            row["excess_pct"] = round(float(sc["excess_pct"]), 1)
            row["anomaly"] = bool(m["id"] in fr.open_alert)
            row["threshold_kg_h"] = m["co2_threshold_kg_h"]
            row["sensor_issues"] = fr.sensor_issues(m["id"])
            row["name"] = m["name"]
            out[m["id"]] = row
        return out
    return fr.memo("latest", build)


@app.get("/api/{fid}/overview")
def overview(fr=Depends(factory)):
    with ENGINE.lock:
        def build():
            df = fr.df
            now = pd.Timestamp(fr.now)
            last = df[df["timestamp"] > now - pd.Timedelta(days=7)].copy()
            last["hour"] = last["timestamp"].dt.floor("h")
            hourly = last.groupby("hour").agg(co2=("co2_kg_h", "sum"), power=("electricity_kw", "sum")).reset_index()
            hourly[["co2", "power"]] = hourly[["co2", "power"]] / 12
            bl = last[last.machine_type == "boiler"].groupby("hour")["fuel_rate"].mean()
            st = last[last.machine_type == "stenter"].groupby("hour")["fuel_rate"].mean()
            hourly["boiler_fuel"] = hourly["hour"].map(bl)
            hourly["stenter_gas"] = hourly["hour"].map(st)
            return {"kpis": _kpis(fr), "trend": hourly.rename(columns={"hour": "t"}).round({"co2": 1, "power": 1, "boiler_fuel": 1, "stenter_gas": 1}).to_dict("records")}
        base = fr.memo("overview", build)
        alerts = sorted(fr.alerts, key=lambda a: a["time"], reverse=True)[:12]
        return clean({
            "factory": {k: fr.fac[k] for k in ("id", "name", "location", "industry", "daily_cap_t_co2",
                                               "rolling_30d_cap_t_co2")},
            "now": fr.now.isoformat(), **base, "latest": _latest(fr),
            "alerts": alerts, "open_alerts": [a for a in fr.alerts if not a["resolved"]],
            "forecast_today": getattr(fr, "_fc_cache", {}).get("today"),
            "savings": fr.savings(),
        })


@app.get("/api/{fid}/live")
def live(since: str | None = Query(None, max_length=40), fr=Depends(factory)):
    with ENGINE.lock:
        g = fr.df
        if since:
            try:
                t = pd.Timestamp(since)
            except ValueError:
                raise HTTPException(400, "Bad 'since' timestamp")
            if t.tzinfo is not None:
                t = t.tz_localize(None)
            g = g[g["timestamp"] > t]
        g = g.tail(40)
        alerts = fr.alerts[-20:]  # client de-duplicates by id
        return clean({"now": fr.now.isoformat(), "rows": g.to_dict("records"), "new_alerts": alerts,
                      "latest": _latest(fr), "paused": ENGINE.paused, "clock": clock_info(fr),
                      "open_alerts": [a for a in fr.alerts if not a["resolved"]]})


def _source(fr) -> dict:
    return {"kind": fr.sim.kind, "name": getattr(fr.sim, "name", None)}


@app.get("/api/{fid}/readings")
def readings(limit: int = Query(40, ge=5, le=200), fr=Depends(factory)):
    """Newest readings first, each scored against what it should be."""
    def build():
        out = []
        n = limit // len(fr.fac["machines"]) + 1
        for m in fr.fac["machines"]:
            g = fr.machine_df(m["id"], last=n + 6)
            sc = fr.models[m["id"]].score(g)
            flagged = sc["anomaly"].astype(int).rolling(3, min_periods=1).sum() >= 2
            for i in range(max(len(g) - n, 0), len(g)):
                r = g.iloc[i]
                row = {"time": r["timestamp"], "machine_id": m["id"], "machine": m["name"], "type": m["type"],
                       "status": r["status"], "fuel": r["fuel_rate"], "fuel_unit": m["fuel_unit"] + "/h",
                       "power_kw": r["electricity_kw"], "co2": r["co2_kg_h"], "expected": sc["expected_co2"].iloc[i],
                       "excess_pct": sc["excess_pct"].iloc[i], "alarm": bool(flagged.iloc[i])}
                if m["type"] == "boiler":
                    row.update(signal_a=r["flue_o2_pct"], signal_b=r["exhaust_gas_temp_c"], steam=r["steam_generated_kg_h"])
                else:
                    row.update(signal_a=r["exhaust_damper_pct"], signal_b=r["chamber_temp_c"], moisture=r["outlet_moisture_pct"])
                out.append(row)
        return sorted(out, key=lambda x: (x["time"], x["machine_id"]), reverse=True)[:limit]
    with ENGINE.lock:
        rows = fr.memo(("readings", limit), build)
        return clean({"rows": rows, "now": fr.now, "clock": clock_info(fr), "source": _source(fr),
                      "issues": {m["id"]: fr.sensor_issues(m["id"]) for m in fr.fac["machines"]}})


@app.get("/api/{fid}/incident/{alert_id}")
def incident(alert_id: str = PathParam(max_length=16), fr=Depends(factory)):
    """One problem from evidence to verified saving."""
    with ENGINE.lock:
        alert = next((a for a in fr.alerts if a["id"] == alert_id), None)
        if alert is None:
            raise HTTPException(404, "This incident is no longer available")
        m = fr.machine(alert["machine_id"])
        if m is None or alert["kind"] != "anomaly":
            raise HTTPException(400, "Only machine anomalies have an incident page")
        mid = m["id"]
        related = [a for a in fr.actions if a["machine_id"] == mid and a["decided_at"] >= alert["time"]]
        action = related[-1] if related else None
        proposal, top = None, alert["drivers"][0]["action"] if alert["drivers"] else None
        if top and not alert["resolved"] and (action is None or action["status"] in ("Rejected", "Cancelled", "Not confirmed")):
            proposal = fr.simulate(mid, top, ENGINE.learning)
        g = fr.machine_df(mid, last=72)
        sc = fr.models[mid].score(g)
        return clean({"alert": alert, "machine": {k: m[k] for k in ("id", "name", "type", "fuel_unit")},
                      "action": None if action is None else {k: v for k, v in action.items() if k != "pre"},
                      "proposal": proposal, "now": fr.now, "can_act": fr.sim.can_act,
                      "series": {"t": g["timestamp"].tolist(), "actual": g["co2_kg_h"].tolist(),
                                 "expected": sc["expected_co2"].round(1).tolist()}})


@app.get("/api/{fid}/machine/{mid}")
def machine(mid: str, hours: int = Query(24, ge=1, le=168), fr=Depends(factory)):
    with ENGINE.lock:
        m = check_machine(fr, mid)
        g = fr.machine_df(mid, last=hours * 12)
        sc = fr.models[mid].score(g)
        g = g.assign(expected_co2=sc["expected_co2"].round(1).values, anomaly=sc["anomaly"].values)
        drivers = fr.memo(("drivers", mid), lambda: fr.models[mid].drivers(fr.machine_df(mid, last=6)))
        return clean({"machine": m, "rows": g.to_dict("records"), "drivers": drivers,
                      "baselines": fr.models[mid].baselines, "metrics": fr.models[mid].metrics,
                      "open_alert": fr.open_alert.get(mid)})


@app.get("/api/{fid}/diagnosis")
def diagnosis(fr=Depends(factory)):
    with ENGINE.lock:
        def build():
            out = []
            for m in fr.fac["machines"]:
                g = fr.machine_df(m["id"], last=6)
                sc = fr.models[m["id"]].score(g)
                out.append({"machine": m, "anomaly": m["id"] in fr.open_alert,
                            "excess_pct": round(float(sc["excess_pct"].tail(3).mean()), 1),
                            "co2_kg_h": round(float(g["co2_kg_h"].tail(3).mean()), 1),
                            "expected_kg_h": round(float(sc["expected_co2"].tail(3).mean()), 1),
                            "drivers": fr.memo(("drivers", m["id"]), lambda: fr.models[m["id"]].drivers(g)),
                            "alert": fr.open_alert.get(m["id"])})
            return out
        return clean({"machines": fr.memo("diagnosis", build), "now": fr.now.isoformat()})


@app.get("/api/{fid}/recommendations")
def recommendations(fr=Depends(factory)):
    with ENGINE.lock:
        return clean({"items": fr.recommendations(ENGINE.learning), "learning": fr.learning_view(ENGINE.learning)})


class SimReq(BaseModel):
    machine_id: str = Field(max_length=32)
    action: str = Field(max_length=32)
    overrides: dict[str, float] | None = None


@app.post("/api/{fid}/simulate")
def simulate(body: SimReq, fr=Depends(factory)):
    with ENGINE.lock:
        check_action(fr, body.machine_id, body.action, allow_custom=True)
        return clean(fr.simulate(body.machine_id, body.action, ENGINE.learning, body.overrides))


class Decide(BaseModel):
    machine_id: str = Field(max_length=32)
    action: str = Field(max_length=32)
    decision: Literal["apply", "reject"]
    reason: str | None = Field("", max_length=300)
    assignee: str | None = Field("", max_length=80)
    note: str | None = Field("", max_length=300)


@app.post("/api/{fid}/actions")
def decide(body: Decide, fr=Depends(factory), user: dict = Depends(require("operator"))):
    with ENGINE.lock:
        check_action(fr, body.machine_id, body.action)
        if body.decision == "apply":
            if not fr.sim.can_act:
                raise HTTPException(409, "This is replayed history: a decision cannot change it.")
            if fr.in_progress(body.machine_id, body.action):
                raise HTTPException(409, "This action is already being carried out or measured on this machine")
            sim = fr.simulate(body.machine_id, body.action, ENGINE.learning)
            if sim["safety"]["status"] == "blocked":
                raise HTTPException(409, "Blocked by a safety limit: " + sim["safety"]["notes"][0])
            if sim["saving"]["co2_kg_h"] < 1:
                raise HTTPException(400, "No measurable saving expected from this action right now")
            rec = fr.apply_action(body.machine_id, body.action, ENGINE.learning, user["name"],
                                  (body.assignee or "").strip(), (body.note or "").strip())
        else:
            rec = fr.reject_action(body.machine_id, body.action, user["name"], body.reason or "")
        fr.persist()
        return clean({k: v for k, v in rec.items() if k != "pre"})


def _work_order(fr, action_id: str) -> dict:
    rec = fr.get_action(action_id)
    if rec is None:
        raise HTTPException(404, "Unknown work order")
    return rec


@app.post("/api/{fid}/actions/{action_id}/done")
def work_done(action_id: str, fr=Depends(factory), user: dict = Depends(require("operator"))):
    """The crew reports the job finished. Measurement starts, and the sensors must confirm the change."""
    with ENGINE.lock:
        rec = _work_order(fr, action_id)
        if rec["status"] != "Assigned":
            raise HTTPException(409, f"This work order is already {rec['status']}")
        fr.mark_done(rec, user["name"])
        fr.persist()
        return clean({k: v for k, v in rec.items() if k != "pre"})


@app.post("/api/{fid}/actions/{action_id}/cancel")
def work_cancel(action_id: str, fr=Depends(factory), user: dict = Depends(require("operator"))):
    with ENGINE.lock:
        rec = _work_order(fr, action_id)
        if rec["status"] != "Assigned":
            raise HTTPException(409, f"This work order is already {rec['status']}")
        fr.cancel(rec, user["name"])
        fr.persist()
        return clean({k: v for k, v in rec.items() if k != "pre"})


def _actions_summary(fr) -> dict:
    verified = [a for a in fr.actions if a["status"] == "Verified"]
    acc = [a["accuracy_pct"] for a in verified if a["accuracy_pct"] is not None]
    return {"verified": len(verified),
            "mean_accuracy_pct": round(float(np.mean(acc)), 1) if acc else None,
            "co2_saved_kg_h": round(sum(max(a["actual_kg_h"] or 0, 0) for a in verified), 1),
            "accuracy_trend": acc, **fr.savings()}


@app.get("/api/{fid}/actions")
def actions(fr=Depends(factory)):
    with ENGINE.lock:
        items = [{k: v for k, v in a.items() if k != "pre"} for a in reversed(fr.actions)]
        return clean({"items": items, "learning": fr.learning_view(ENGINE.learning),
                      "summary": _actions_summary(fr)})


@app.get("/api/{fid}/actions.csv")
def actions_csv(fr=Depends(factory)):
    cols = ["decided_at", "machine_id", "action", "title", "decided_by", "assignee", "status", "reason", "implement_due",
            "implemented_at", "done_by", "confirmed", "credited_h", "credit_ended", "predicted_kg_h", "actual_kg_h", "accuracy_pct", "predicted_inr_day", "verified_at", "calibration_after"]
    with ENGINE.lock:
        df = pd.DataFrame([{c: a.get(c) for c in cols} for a in fr.actions], columns=cols)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={fr.fid}_actions.csv"})


@app.get("/api/{fid}/forecast")
def get_forecast(fr=Depends(factory)):
    with ENGINE.lock:
        return clean(fr.forecast(ENGINE.learning))


@app.get("/api/{fid}/model-report")
def model_report(fr=Depends(factory)):
    if REPORT["status"] != "ready":
        return JSONResponse({"status": REPORT["status"], "error": REPORT["error"]}, status_code=202)
    return {"status": "ready", "built": REPORT["data"].get("_built"), "machines": REPORT["data"].get(fr.fid, {})}


@app.get("/api/evidence")
def evidence(user: dict = Depends(current_user)):
    """Stress-test results built by `python -m app.robustness`; null until it has been run."""
    return {"report": robustness.load(CFG)}


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=500)


@app.get("/api/nugen/status")
def nugen_status(user: dict = Depends(current_user)):
    return llm.connection()


@app.post("/api/{fid}/assistant")
def assistant(body: Ask, fr=Depends(factory)):
    with ENGINE.lock:
        recs = fr.recommendations(ENGINE.learning)[:3]
        fc = fr.forecast(ENGINE.learning)
        ctx = {"factory": fr.fac["name"], "time": fr.now.isoformat(),
               "data_source": "simulated plant" if fr.sim.kind == "simulator" else "uploaded plant data",
               "latest": {k: {kk: v[kk] for kk in ("co2_kg_h", "expected_co2_kg_h", "excess_pct", "fuel_rate",
                                                   "status") if kk in v} for k, v in _latest(fr).items()},
               "open_alerts": [a for a in fr.alerts if not a["resolved"]][:3],
               "recommendations": [{k: r[k] for k in ("title", "machine_name", "saving", "priority",
                                                      "confidence", "detail", "safety")} for r in recs],
               "forecast": {"today": fc["today"], "rolling": fc["rolling"]},
               "verified_savings": fr.savings()}
    return clean(llm.ask(clean(ctx), body.question))


@app.get("/api/{fid}/explain")
def explain(machine_id: str = Query(max_length=32), action: str = Query(max_length=32), fr=Depends(factory)):
    with ENGINE.lock:
        m = check_action(fr, machine_id, action)
        rec = fr.simulate(machine_id, action, ENGINE.learning)
        rec["machine_name"] = m["name"]
        alert = fr.open_alert.get(machine_id)
    return clean(llm.explain_recommendation(clean(rec), clean(alert)))


@app.get("/api/{fid}/report.pdf")
def report_pdf(days: int = Query(30, ge=1, le=45), fr=Depends(factory)):
    with ENGINE.lock:
        data = report.build(fr, days)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={fr.fid}_report.pdf"})


# ---------------------------------------------------------------------------
# data source: simulator or an uploaded CSV of real readings
# ---------------------------------------------------------------------------
@app.get("/api/{fid}/data/status")
def data_status(fr=Depends(factory)):
    with ENGINE.lock:
        machines = []
        for m in fr.fac["machines"]:
            g = fr.machine_df(m["id"])
            recent = g.tail(288)
            machines.append({"id": m["id"], "name": m["name"], "readings": len(g),
                             "first": g["timestamp"].iloc[0], "last": g["timestamp"].iloc[-1],
                             "missing_pct_24h": round(float(recent["fuel_rate"].isna().mean()) * 100, 1),
                             "issues": fr.sensor_issues(m["id"])})
        src = {"kind": fr.sim.kind, "can_act": fr.sim.can_act, "stored": fr.store is not None and fr.sim.kind == "simulator"}
        if fr.sim.kind == "csv":
            src.update({"name": fr.sim.name, "played": fr.sim.played, "total": fr.sim.total_steps,
                        "finished": fr.sim.finished, "warnings": fr.upload_warnings})
        return clean({"source": src, "machines": machines, "now": fr.now, "clock": clock_info(fr),
                      "credit_note": "Savings are counted per action for a limited period; see the report."})


@app.get("/api/{fid}/data/template.csv")
def data_template(fr=Depends(factory)):
    """Column layout for uploads: one example row per machine, from the current data."""
    frames = []
    for m in fr.fac["machines"]:
        cols = template_columns(m["type"])
        frames.append(fr.machine_df(m["id"], last=2)[[c for c in cols if c in fr.df.columns]])
    buf = io.StringIO()
    pd.concat(frames).sort_values("timestamp").to_csv(buf, index=False)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fr.fid}_template.csv"})


@app.post("/api/{fid}/data/upload")
async def data_upload(request: Request, name: str = Query("upload.csv", max_length=120), fr=Depends(factory),
                      _=Depends(require("admin"))):
    """Raw CSV in the body. Trains on the first 70 % and replays the rest as if live."""
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "The upload is empty")
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "File is too large")
    fac = fr.fac

    def build():
        df, warnings = parse_csv(fac, raw)
        runtime = FactoryRuntime(fac, ENGINE.data_time, seed=1, replay=CsvReplay(df, name))
        runtime.upload_warnings = warnings
        return runtime
    try:
        runtime = await run_in_threadpool(build)
    except CsvError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # data the models can't use: say so instead of a bare 500
        print("upload failed:", repr(e))
        raise HTTPException(400, f"This data could not be used for training: {type(e).__name__}. "
                                 "Check the column values against the template.")
    ENGINE.start_replay(fac["id"], runtime)
    return {"ok": True, "warnings": runtime.upload_warnings, "train_rows": len(runtime.df),
            "replay_readings": runtime.sim.total_steps, "start": runtime.now.isoformat()}


@app.post("/api/{fid}/data/stop")
def data_stop(fr=Depends(factory), _=Depends(require("admin"))):
    ENGINE.stop_replay(fr.fid)
    return {"ok": True}


@app.get("/api/{fid}/export.csv")
def export(fr=Depends(factory)):
    with ENGINE.lock:
        buf = io.StringIO()
        fr.df.drop(columns=["fault_label"]).to_csv(buf, index=False)
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={fr.fid}_data.csv"})


# ---------------------------------------------------------------------------
# demo controls (for recording the video) - admin only, off with DEMO_MODE=0
# ---------------------------------------------------------------------------
@app.get("/api/catalog")
def catalog(user: dict = Depends(current_user)):
    return {"faults": FAULTS if DEMO_MODE else {}, "sensor_faults": SENSOR_FAULTS if DEMO_MODE else {}, "actions": ACTIONS,
            "drivers": {k: {kk: vv["label"] for kk, vv in v.items()} for k, v in DRIVERS.items()}}


class Inject(BaseModel):
    fault: str = Field(max_length=40)
    severity: float = Field(1.0, ge=0.2, le=1.5)


@app.post("/api/{fid}/demo/inject")
def inject(body: Inject, fr=Depends(factory), _=Depends(demo_admin)):
    if fr.sim.kind != "simulator":
        raise HTTPException(409, "Faults can only be injected into the simulated plant")
    if body.fault not in FAULTS and body.fault not in SENSOR_FAULTS:
        raise HTTPException(400, "Unknown fault")
    ENGINE.inject(fr.fid, body.fault, body.severity)
    return {"ok": True, "fault": FAULTS.get(body.fault) or SENSOR_FAULTS[body.fault]}


@app.post("/api/{fid}/demo/clear")
def clear(fr=Depends(factory), _=Depends(demo_admin)):
    if fr.sim.kind != "simulator":
        raise HTTPException(409, "This factory is replaying uploaded data")
    ENGINE.clear_faults(fr.fid)
    return {"ok": True}


@app.post("/api/demo/pause")
def pause(_=Depends(demo_admin)):
    with ENGINE.lock:
        ENGINE.paused = not ENGINE.paused
    return {"paused": ENGINE.paused}


@app.post("/api/demo/speed")
def speed(seconds: float = Query(ge=0, le=10.0), _=Depends(demo_admin)):
    """0 = back to real time (the clock then waits for the wall clock to catch up);
    otherwise fast-forward at `seconds` real seconds per 5-min reading."""
    if 0 < seconds < 0.3:
        raise HTTPException(422, "Use 0 for real time, or 0.3-10 seconds per reading")
    with ENGINE.lock:
        ENGINE.speed = None if seconds == 0 else seconds
    return clock_info()


# ---------------------------------------------------------------------------
# frontend
# ---------------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_err(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


app.mount("/static", StaticFiles(directory=FRONTEND / "static"), name="static")


def _static_version() -> str:
    h = hashlib.sha256()
    for name in ("styles.css", "app.js"):
        h.update((FRONTEND / "static" / name).read_bytes())
    return h.hexdigest()[:10]


@app.get("/")
def index():
    v = _static_version()
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    html = html.replace("/static/styles.css", f"/static/styles.css?v={v}").replace("/static/app.js", f"/static/app.js?v={v}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
