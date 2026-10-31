"""Wapsi - finds regular customers who quietly stopped coming.

  Regular  = paid on 5+ different days in the 90 days before their last visit
  Lost     = days since last visit > max(14, 4 x their usual gap between visits)
  Ranking  = monthly value (average bill x visits per month)
The owner sees only a customer number, never the name or UPI ID.

Works with Galla Check: if a "lost" customer usually shops during a shift that Galla flagged
for a QR leak, and they vanished after the leak began, they are probably still visiting but
paying into the swapped QR. They are reported separately and get no win-back offer.
"""
import json
from datetime import date, timedelta

import pandas as pd

import db

MIN_VISITS, MIN_DAYS, GAP_MULT = 5, 14, 4


def run(ref_date: date, galla_flag=None):
    pay = pd.DataFrame(db.query_all(
        "SELECT p.payer_vpa, p.ts, p.amount FROM payments p JOIN customers c ON c.payer_vpa = p.payer_vpa "
        "WHERE COALESCE(p.category,'sale')='sale' AND p.ts < ?", ((ref_date + timedelta(days=1)).isoformat(),)))
    pay["date"] = pd.to_datetime(pay["ts"]).dt.normalize()
    cust = {c["payer_vpa"]: c for c in db.query_all("SELECT customer_id, payer_vpa, opted_in FROM customers")}
    ref = pd.Timestamp(ref_date)

    pay["hour"] = pd.to_datetime(pay["ts"]).dt.hour
    leak_since = pd.Timestamp(galla_flag["since"]) if galla_flag else None
    stats, lost, leak_hidden = [], [], []
    for vpa, g in pay.groupby("payer_vpa"):
        days = sorted(g["date"].unique())
        last = days[-1]
        window = [d for d in days if d >= last - pd.Timedelta(days=90)]
        gaps = pd.Series(days).diff().dt.days.dropna()
        usual_gap = float(gaps.median()) if len(gaps) else None
        avg_bill = float(g["amount"].mean())
        span_months = max(1.0, (last - days[0]).days / 30)
        monthly_value = avg_bill * len(days) / span_months
        since = (ref - last).days
        stats.append((len(days), float(g["amount"].sum()), str(days[0].date()), str(last.date()), usual_gap, vpa))
        if len(window) >= MIN_VISITS and usual_gap and since > max(MIN_DAYS, GAP_MULT * usual_gap):
            c = cust[vpa]
            usual_hour = int(g["hour"].median())
            item = {"customer_id": c["customer_id"], "last_visit": str(last.date()), "days_since": int(since),
                    "usual_gap_days": round(usual_gap, 1), "visits": len(days), "avg_bill": round(avg_bill),
                    "monthly_value": round(monthly_value), "opted_in": bool(c["opted_in"]), "usual_hour": usual_hour}
            in_leak_shift = galla_flag and galla_flag["start_hour"] <= usual_hour < galla_flag["end_hour"]
            next_expected = last + pd.Timedelta(days=2 * usual_gap)
            if in_leak_shift and next_expected >= leak_since:
                item["note"] = f"Usually shops in the {galla_flag['shift'].lower()} shift; probably still visiting but paying into the swapped QR"
                leak_hidden.append(item)
            else:
                lost.append(item)
    with db.get_conn() as c:
        c.executemany("UPDATE customers SET visits=?, total_spent=?, first_visit=?, last_visit=?, avg_gap_days=? "
                      "WHERE payer_vpa=?", stats)
    lost.sort(key=lambda x: -x["monthly_value"])
    leak_hidden.sort(key=lambda x: -x["monthly_value"])
    regulars = sum(1 for s in stats if s[0] >= MIN_VISITS)
    result = {"ref_date": ref_date.isoformat(), "regulars": regulars, "lost": lost, "leak_hidden": leak_hidden,
              "monthly_value_at_risk": sum(x["monthly_value"] for x in lost)}
    alerts = []
    if lost:
        alerts.append(("wapsi", "warn", f"{len(lost)} regular customers have stopped coming",
                       f"They used to spend about Rs {result['monthly_value_at_risk']:,} a month. "
                       f"{sum(x['opted_in'] for x in lost)} of them accept offers. Send a win-back offer?",
                       json.dumps({"customer_ids": [x["customer_id"] for x in lost]})))
    if leak_hidden:
        alerts.append(("galla", "high", f"{len(leak_hidden)} regulars' payments vanished during the {galla_flag['shift'].lower()} shift",
                       f"These customers usually shop in the {galla_flag['shift'].lower()} shift and their payments stopped "
                       f"after {galla_flag['since']}. They are likely still coming but paying into a different QR.",
                       json.dumps({"customer_ids": [x["customer_id"] for x in leak_hidden]})))
    return result, alerts
