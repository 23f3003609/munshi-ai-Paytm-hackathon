"""Galla Check - finds money that should have arrived but didn't (QR swaps, staff leakage).

How it decides:
  1. Expected sales per shift per day = median of the same weekday + shift over the 8 weeks
     before the recent window, adjusted for that day's context (salary week, festival, rain).
  2. A shift is flagged when actual < 70% of expected on at least 5 of the last 7 days
     while the other shifts stay normal (>= 85%) -> points to a person, not a slow market.
  3. Leak start = earliest day from which most days stay below 75%.
"""
import json
from datetime import date, timedelta

import pandas as pd

import db

RECENT_DAYS = 21
BASELINE_WEEKS = 8
LOW, OK = 0.70, 0.85


def _festival_days():
    fests = [date.fromisoformat(r["date"]) for r in db.query_all("SELECT date FROM day_context WHERE festival IS NOT NULL")]
    return {f - timedelta(days=k) for f in fests for k in range(0, 3)}


def _factor(row, pre_fest):
    f = 1.10 if row["salary_week"] else 1.0
    f *= 1.35 if row["date"] in pre_fest else 1.0
    f *= 0.80 if row["is_rain"] else 1.0
    return f


def run(ref_date: date):
    shifts = db.get_setting("shifts")
    pay = pd.DataFrame(db.query_all(
        "SELECT ts, amount FROM payments WHERE mode='P2M' AND COALESCE(risk_level,'safe')!='hold' AND ts < ?",
        ((ref_date + timedelta(days=1)).isoformat(),)))
    pay["ts"] = pd.to_datetime(pay["ts"])
    pay["date"] = pay["ts"].dt.date
    pay["hour"] = pay["ts"].dt.hour

    def shift_of(h):
        for s in shifts:
            if s["start"] <= h < s["end"]:
                return s["name"]
        return None
    pay["shift"] = pay["hour"].map(shift_of)

    ctx = pd.DataFrame(db.query_all("SELECT * FROM day_context"))
    ctx["date"] = pd.to_datetime(ctx["date"]).dt.date
    pre_fest = _festival_days()
    ctx["factor"] = ctx.apply(lambda r: _factor(r, pre_fest), axis=1)
    ctx = ctx.set_index("date")

    daily = pay.groupby(["date", "shift"]).agg(amount=("amount", "sum"), count=("amount", "size")).reset_index()
    recent_start = ref_date - timedelta(days=RECENT_DAYS - 1)
    base_start = recent_start - timedelta(weeks=BASELINE_WEEKS)

    result = {"ref_date": ref_date.isoformat(), "shifts": {}, "flagged": None}
    for s in shifts:
        name = s["name"]
        d = daily[daily["shift"] == name].set_index("date")
        rows = []
        for i in range((ref_date - base_start).days + 1):
            day = base_start + timedelta(days=i)
            if day not in ctx.index:
                continue
            amt = float(d["amount"].get(day, 0.0))
            cnt = int(d["count"].get(day, 0))
            rows.append({"date": day, "weekday": day.weekday(), "amount": amt, "count": cnt,
                         "factor": float(ctx.loc[day, "factor"])})
        df = pd.DataFrame(rows)
        df["norm"] = df["amount"] / df["factor"]
        df["norm_count"] = df["count"] / df["factor"]
        base = df[df["date"] < recent_start]
        med = base.groupby("weekday")[["norm", "norm_count"]].median()
        rec = df[df["date"] >= recent_start].copy()
        rec["expected"] = rec.apply(lambda r: med.loc[r["weekday"], "norm"] * r["factor"], axis=1)
        rec["expected_count"] = rec.apply(lambda r: med.loc[r["weekday"], "norm_count"] * r["factor"], axis=1)
        rec["ratio"] = rec["amount"] / rec["expected"]
        last7 = rec.tail(7)
        result["shifts"][name] = {
            "staff": s.get("staff"), "hours": f"{s['start']}:00-{s['end']}:00",
            "low_days_last7": int((last7["ratio"] < LOW).sum()),
            "median_ratio_last7": round(float(last7["ratio"].median()), 2),
            "daily": [{"date": r["date"].isoformat(), "actual": round(r["amount"]), "expected": round(r["expected"]),
                       "ratio": round(r["ratio"], 2)} for _, r in rec.iterrows()],
            "_rec": rec,
        }

    # decide: one shift low, others normal
    for name, info in result["shifts"].items():
        others = [v["median_ratio_last7"] for k, v in result["shifts"].items() if k != name]
        if info["low_days_last7"] >= 5 and all(o >= OK for o in others):
            rec = info["_rec"].reset_index(drop=True)
            start_idx = len(rec) - 1
            for i in range(len(rec)):
                tail = rec.iloc[i:]
                if (tail["ratio"] < 0.75).mean() >= 0.8 and rec.loc[i, "ratio"] < 0.6:
                    start_idx = i
                    break
            leak = rec.iloc[start_idx:]
            lost = float((leak["expected"] - leak["amount"]).clip(lower=0).sum())
            count_drop = 1 - leak["count"].sum() / leak["expected_count"].sum()
            bill_now = leak["amount"].sum() / max(1, leak["count"].sum())
            bill_before = info["_rec"].iloc[:start_idx]["amount"].sum() / max(1, info["_rec"].iloc[:start_idx]["count"].sum()) if start_idx else bill_now
            result["flagged"] = {
                "shift": name, "staff": info["staff"], "hours": info["hours"],
                "since": leak["date"].iloc[0].isoformat(), "days": int(len(leak)),
                "drop_pct": round((1 - leak["amount"].sum() / leak["expected"].sum()) * 100),
                "estimated_loss": round(lost),
                "count_drop_pct": round(count_drop * 100),
                "bill_change_pct": round((bill_now / bill_before - 1) * 100) if bill_before else 0,
                "other_shifts_normal": True,
                "start_hour": int(info["hours"].split(":")[0]), "end_hour": int(info["hours"].split("-")[1].split(":")[0]),
            }
    for info in result["shifts"].values():
        info.pop("_rec", None)

    alerts = []
    f = result["flagged"]
    if f:
        alerts.append(("galla", "high",
                       f"{f['shift']} shift sales {f['drop_pct']}% below normal since {f['since']}",
                       f"About Rs {f['estimated_loss']:,} missing over {f['days']} days. The number of QR payments fell "
                       f"{f['count_drop_pct']}% while bill size stayed normal, and other shifts are fine. "
                       f"Check that every QR in the shop is yours.", json.dumps(f)))
    return result, alerts


def verify_qr(qr_text: str):
    """Checks a scanned QR (upi://pay?pa=...&pn=...) points to the owner's registered UPI IDs."""
    from urllib.parse import parse_qs, urlparse
    mine = [u.lower() for u in db.get_setting("upi_ids")]
    try:
        u = urlparse(qr_text.strip())
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
    except Exception:
        params = {}
    if u.scheme.lower() != "upi" or "pa" not in params:
        return {"valid_upi": False, "safe": False, "message": "This is not a UPI payment QR."}
    pa = params["pa"].lower()
    safe = pa in mine
    return {"valid_upi": True, "safe": safe, "pays_to": pa, "name_on_qr": params.get("pn"),
            "message": "This QR pays into your account." if safe else
            f"DANGER: this QR pays {pa}, which is NOT your account. Remove it now."}
