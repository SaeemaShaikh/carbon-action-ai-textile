"""AI advisor text: Nugen-aligned model when configured, local templates otherwise.

Set in backend/.env:
  NUGEN_API_KEY=...            (from platform.nugen.in)
  NUGEN_MODEL_ID=...           (your aligned + deployed model id)
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

import requests

NUGEN_URL = os.getenv("NUGEN_URL", "https://api.nugen.in/api/v3/inference/chat/completions")

SYSTEM = (
    "You are Carbon Action AI, an energy and emissions advisor for textile dyeing and printing "
    "plants (boilers and stenters). Answer using ONLY the plant data given in the context. "
    "Be concrete: name the machine, the number, and the next step. Never invent readings. "
    "Copy numbers exactly as they appear in the context: do not calculate new numbers or differences. "
    "All money is in Indian rupees: write it with the ₹ sign, never $. "
    "A recommended action is allowed unless its safety status is blocked. If asked whether an action "
    "is safe, say it can be done with the listed safety notes, and repeat those notes. "
    "Use the key facts for limits and dates: the daily limit and the 30-day limit are different. "
    "Say 'most likely' for causes and 'expected' for savings. If asked whether the data is real, "
    "answer from the data source fact: simulated data is not real plant data. "
    "Keep answers under 120 words. Use plain language a plant manager understands."
)


_lock = threading.Lock()
_usage = {"calls": 0, "ok": 0, "failed": 0, "tokens": 0, "last_ok_at": None, "last_latency_s": None,
          "last_confidence": None, "last_error": None}
_check: dict = {"at": 0.0, "result": None}


def status() -> dict:
    return {"provider": "nugen" if os.getenv("NUGEN_API_KEY") and os.getenv("NUGEN_MODEL_ID") else "local",
            "model": os.getenv("NUGEN_MODEL_ID") or "rule-based templates"}


def connection() -> dict:
    """Is the configured model really deployed? Asks Nugen at most once a minute and uses no inference."""
    st = status()
    if st["provider"] != "nugen":
        return {**st, "reachable": False, "deployment": None, "usage": dict(_usage)}
    with _lock:
        fresh = _check["result"] is not None and time.time() - _check["at"] < 60
    if not fresh:
        result = {"reachable": False, "deployment": None, "base_model": None, "model_name": None}
        try:
            r = requests.get(NUGEN_URL.split("/inference")[0] + "/models/aligned", timeout=10,
                             headers={"Authorization": f"Bearer {os.getenv('NUGEN_API_KEY')}"})
            r.raise_for_status()
            result["reachable"] = True
            for m in r.json().get("domain_aligned_models", []):
                if m.get("model_id") == st["model"]:
                    result.update(deployment=m.get("deployment_status"), base_model=m.get("base_model_id"),
                                  model_name=m.get("model_name"))
        except (requests.RequestException, ValueError) as e:
            print("Nugen status check failed:", type(e).__name__)
        with _lock:
            _check.update(at=time.time(), result=result)
    with _lock:
        return {**st, **_check["result"], "usage": dict(_usage)}


def _call_nugen(messages: list[dict], max_tokens: int = 300) -> dict | None:
    """Returns {"text", "confidence"}, {"error"}, or None when Nugen is not configured."""
    key, model = os.getenv("NUGEN_API_KEY"), os.getenv("NUGEN_MODEL_ID")
    if not key or not model:
        return None
    start, error = time.time(), None
    try:
        r = requests.post(NUGEN_URL, timeout=25, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json", "accept": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens,
                  "temperature": 0.2, "stream": False})
        r.raise_for_status()
        data = r.json()
        ch = data["choices"][0]
        text = (ch.get("message") or {}).get("content") or ch.get("text")
        if not text:
            raise ValueError("empty answer")
        with _lock:
            _usage.update(calls=_usage["calls"] + 1, ok=_usage["ok"] + 1, last_error=None,
                          tokens=_usage["tokens"] + ((data.get("usage") or {}).get("total_tokens") or 0),
                          last_ok_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                          last_latency_s=round(time.time() - start, 1), last_confidence=data.get("confidence_score"))
        return {"text": text, "confidence": data.get("confidence_score")}
    except requests.HTTPError as e:  # details stay in the server log, not in the browser
        print("Nugen call failed:", type(e).__name__)
        code = e.response.status_code if e.response is not None else 0
        error = "Nugen rejected the API key or model id" if code in (401, 403, 404) else f"Nugen returned HTTP {code}"
    except requests.RequestException as e:
        print("Nugen call failed:", type(e).__name__)
        error = "Nugen is unreachable"
    except (ValueError, KeyError, IndexError, TypeError) as e:
        print("Nugen response not understood:", type(e).__name__)
        error = "unexpected response from Nugen"
    with _lock:
        _usage.update(calls=_usage["calls"] + 1, failed=_usage["failed"] + 1, last_error=error)
    return {"error": error}


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[float]:
    return {float(n.replace(",", "")) for n in _NUM.findall(text) if n.replace(",", "").rstrip(".")}


def unverified_numbers(answer: str, source: str) -> list[str]:
    """Numbers in an answer that are not in the plant data the model was given, so a person
    should check them. Rounded matches count; small whole numbers such as "3 steps" are ignored."""
    known = _numbers(source)
    out = []
    for raw in _NUM.findall(answer):
        v = float(raw.replace(",", ""))
        if v.is_integer() and v <= 10:
            continue
        if not any(abs(v - k) <= 0.051 or abs(v - k) <= abs(k) * 0.005 for k in known):
            out.append(raw)
    return list(dict.fromkeys(out))


def plain_facts(ctx: dict) -> list[str]:
    """The facts small models most often misread, as sentences instead of JSON keys."""
    out = []
    if ctx.get("data_source"):
        out.append("Data source: simulated plant. These readings come from a simulator, NOT from real factory meters."
                   if "simulated" in ctx["data_source"] else f"Data source: {ctx['data_source']}.")
    fc = ctx.get("forecast") or {}
    t, r = fc.get("today") or {}, fc.get("rolling") or {}
    if t:
        out.append(f"Daily limit: {t.get('cap_t')} t of CO2. Projected total for today: {t.get('projected_t')} t "
                   f"({t.get('pct_of_cap')}% of the daily limit).")
    if r:
        cross = (f"If nothing changes, the 30-day limit is crossed on {r['cross_date']}, in {r.get('days_to_cross')} days."
                 if r.get("cross_date") else "No crossing of the 30-day limit is forecast in the next 3 weeks.")
        out.append(f"30-day limit: {r.get('cap_t')} t of CO2. Current 30-day total: {r.get('current_t')} t. {cross}")
    sv = ctx.get("verified_savings") or {}
    if sv:
        out.append(f"Verified savings so far: {sv.get('co2_avoided_t')} t of CO2 and ₹{sv.get('inr_saved')}.")
    for rec in ctx.get("recommendations") or []:
        notes = (rec.get("safety") or {}).get("notes") or []
        if notes:
            out.append(f"Safety for \"{rec.get('title')}\" ({(rec.get('safety') or {}).get('status')}): {' '.join(notes)}")
    return out


def ask(context: dict, question: str) -> dict:
    facts = "\n".join("- " + f for f in plain_facts(context))
    data = json.dumps(context, default=str)[:6000]
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": (f"Key facts:\n{facts}\n\n" if facts else "")
             + f"Plant context (JSON):\n{data}\n\nQuestion: {question}"}]
    out = _call_nugen(msgs)
    if out and out.get("text"):
        answer = out["text"].strip()
        return {"answer": answer, "source": "nugen", "model": os.getenv("NUGEN_MODEL_ID"),
                "confidence": out.get("confidence"),
                "unverified_numbers": unverified_numbers(answer, f"{facts} {data} {question}")}
    return {"answer": local_answer(context, question), "source": "local", "error": out.get("error") if out else None}


def explain_recommendation(rec: dict, alert: dict | None) -> dict:
    ctx = {"recommendation": {k: rec.get(k) for k in ("title", "detail", "machine_name", "saving", "current",
                                                        "simulated", "priority", "confidence", "safety")},
           "recommendations": [{"title": rec.get("title"), "safety": rec.get("safety")}],
           "alert": None if not alert else {k: alert.get(k) for k in ("title", "message", "drivers")}}
    q = "Explain in 3 short sentences why this action is recommended, what it will change, and how to do it safely."
    return ask(ctx, q)


# ---------------------------------------------------------------------------
def local_answer(ctx: dict, q: str) -> str:
    ql = q.lower()
    if "recommendation" in ctx and ctx["recommendation"]:
        r = ctx["recommendation"]
        s = r.get("saving") or {}
        why = ""
        if ctx.get("alert") and ctx["alert"].get("drivers"):
            d = ctx["alert"]["drivers"][0]
            why = (f"The most likely cause is {d['label'].lower()}: {d['evidence_label'].lower()} is "
                   f"{d['current']:,.1f} {d['unit']}, and healthy is {d['healthy']:,.1f} {d['unit']}. ")
        return (f"{why}{r['title']} on the {r.get('machine_name', 'machine')} is expected to cut about "
                f"{s.get('co2_kg_h', 0)} kg CO2/h ({s.get('co2_pct', 0)}%), roughly {s.get('co2_t_day', 0)} t/day "
                f"and ₹{s.get('inr_per_day', 0):,} per day in fuel and power. {r.get('detail', '')} "
                f"After applying it, the system will measure the real effect for one hour and update its confidence.")
    fc = ctx.get("forecast", {})
    recs = ctx.get("recommendations", [])
    alerts = ctx.get("open_alerts", [])
    sv = ctx.get("verified_savings") or {}
    if any(w in ql for w in ("saved", "saving", "achieve", "so far", "result")) and sv:
        if not sv.get("co2_avoided_t"):
            return ("No verified savings yet. Approve a recommendation in the Action Simulator; the system "
                    "measures its real effect for one hour and then counts it here.")
        return (f"Verified actions have avoided {sv['co2_avoided_t']} t CO2 and saved about ₹{sv['inr_saved']:,} so far, "
                f"and keep cutting {sv['co2_t_per_day_now']} t CO2 per day. These are measured against an "
                f"adjusted baseline, not just predicted.")
    if any(w in ql for w in ("cross", "limit", "cap", "forecast", "when", "budget")) and fc:
        r = fc.get("rolling", {})
        t = fc.get("today", {})
        if r.get("cross_date"):
            extra = (" Applying the top recommendations moves it to " + r["cross_date_with_actions"] + "."
                     if r.get("cross_date_with_actions") else " Applying the top recommendations avoids the breach.")
            return (f"If nothing changes, the rolling 30-day total reaches the {r['cap_t']} t cap on "
                    f"{r['cross_date']} ({r['days_to_cross']} days).{extra} Today is projected at "
                    f"{t.get('projected_t')} t against a {t.get('cap_t')} t daily cap.")
        return (f"No breach is forecast in the next 3 weeks. Rolling 30-day total is {r.get('current_t')} t of "
                f"{r.get('cap_t')} t; today is projected at {t.get('projected_t')} t ({t.get('pct_of_cap')}% of cap).")
    if alerts and any(w in ql for w in ("why", "cause", "alert", "problem", "wrong", "anomal")):
        a = alerts[0]
        d = a["drivers"][0] if a.get("drivers") else None
        txt = f"{a['title']} on {a['machine']}: {a['message']}"
        if d:
            txt += f" The top driver adds about {d['contribution_kg_h']} kg CO2/h."
        return txt
    if recs:
        r = recs[0]
        return (f"Top priority now: {r['title']} on the {r['machine_name']} — saves about "
                f"{r['saving']['co2_kg_h']} kg CO2/h ({r['saving']['co2_t_day']} t/day, ₹{r['saving']['inr_per_day']:,}/day) "
                f"with {int(r['confidence'] * 100)}% confidence. {r['detail']}")
    return "Everything is running within expected limits right now. I'll alert you as soon as something drifts."
