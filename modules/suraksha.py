"""Suraksha - real-time payment risk scoring (freeze shield).

Score 0-100 from explainable weighted factors:
  < 40  safe    -> normal Soundbox announcement
  40-70 verify  -> "please verify" voice alert
  > 70  hold    -> "do not hand over cash" voice alert
"""
import json
from datetime import datetime, timedelta

import numpy as np

import db

SAFE_MAX, VERIFY_MAX = 40, 70
_stats_cache = {}


def shop_stats(refresh=False):
    """Typical bill size of this shop (last 60 days of sales)."""
    if _stats_cache and not refresh:
        return _stats_cache
    last = db.query_one("SELECT MAX(ts) m FROM payments")["m"]
    since = (datetime.fromisoformat(last) - timedelta(days=60)).isoformat() if last else "2000-01-01"
    rows = db.query_all("SELECT amount FROM payments WHERE mode='P2M' AND ts>=? "
                        "AND COALESCE(truth,'sale')='sale'", (since,))
    amounts = np.array([r["amount"] for r in rows]) if rows else np.array([150.0])
    _stats_cache.update(median=float(np.median(amounts)), p95=float(np.percentile(amounts, 95)),
                        p99=float(np.percentile(amounts, 99)))
    return _stats_cache


def score_payment(amount, hour, payer, is_first_time, stats=None):
    """Returns (score, level, reasons[]) - reasons sorted by importance."""
    stats = stats or shop_stats()
    factors = []                                      # (points, reason)
    if payer.get("is_reported"):
        factors.append((45, "This payer is on the reported fraud list"))
    if payer.get("pass_through"):
        factors.append((20, "Money in this payer's account moves out within minutes"))
    if (payer.get("network_shops_24h") or 0) >= 10:
        factors.append((20, f"Payer paid {payer['network_shops_24h']} different shops in the last 24 hours"))
    if amount > stats["p99"]:
        factors.append((20, f"Rupees {amount:,.0f} is far above your usual bill of about rupees {stats['median']:,.0f}"))
    elif amount > stats["p95"]:
        factors.append((10, f"Rupees {amount:,.0f} is higher than your usual bill"))
    if is_first_time:
        factors.append((10, "First payment from this customer"))
    age = payer.get("account_age_days")
    if age is not None and age < 30:
        factors.append((10, f"Payer's account is only {age} days old"))
    if hour < 8 or hour >= 21:
        factors.append((5, "Unusual hour for your shop"))

    factors.sort(key=lambda f: -f[0])
    score = min(100, sum(p for p, _ in factors))
    if payer.get("trusted") and not payer.get("is_reported"):
        score = max(0, score - 40)                    # owner vouched for this customer earlier
        factors.append((0, "Owner marked this customer as genuine before"))
    level = "safe" if score < SAFE_MAX else ("verify" if score <= VERIFY_MAX else "hold")
    return score, level, [r for _, r in factors]


def speak_text(amount, level, reasons):
    """What the Soundbox says out loud."""
    if level == "hold":
        return (f"Warning! Rupees {amount:,.0f} payment is high risk. {reasons[0]}. "
                f"Do not hand over cash or goods yet.")
    if level == "verify":
        return f"Please verify. Rupees {amount:,.0f} received. {reasons[0]}."
    return f"Rupees {amount:,.0f} received on Paytm."


def backfill():
    """Score every historical payment in time order (so 'first time' is correct)."""
    stats = shop_stats(refresh=True)
    payers = {p["vpa"]: p for p in db.query_all("SELECT * FROM payers")}
    seen, updates, holds = set(), [], []
    for p in db.query_all("SELECT id, ts, payer_vpa, amount FROM payments ORDER BY ts"):
        first = p["payer_vpa"] not in seen
        seen.add(p["payer_vpa"])
        ts = datetime.fromisoformat(p["ts"])
        s, lvl, reasons = score_payment(p["amount"], ts.hour,
                                        payers.get(p["payer_vpa"], {}), first, stats)
        updates.append((s, lvl, json.dumps(reasons), p["id"]))
        if lvl == "hold":
            holds.append((p["ts"], "suraksha", "high", f"High-risk payment of Rs {p['amount']:,.0f}",
                          reasons[0], json.dumps({"payment_id": p["id"], "score": s, "reasons": reasons})))
    with db.get_conn() as c:
        c.executemany("UPDATE payments SET risk_score=?, risk_level=?, risk_reasons=? WHERE id=?", updates)
        c.executemany("INSERT INTO alerts(ts,module,severity,title,message,data) VALUES (?,?,?,?,?,?)", holds)
    counts = db.query_all("SELECT risk_level, COUNT(*) n FROM payments GROUP BY risk_level")
    return {r["risk_level"]: r["n"] for r in counts}
