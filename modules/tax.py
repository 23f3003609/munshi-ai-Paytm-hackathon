"""Tax Kavach - separates real sales from personal money and tracks the GST threshold.

Classification of every incoming payment (in priority order):
  1. Refunded by the owner                       -> refunded (not income)
  2. Owner already labelled this payer          -> owner's label
  3. Suraksha flagged it high risk               -> suspicious
  4. Note has personal words (rent, papa ...)    -> personal
       (if it was paid to the shop QR, confidence is low, so the owner is asked to confirm)
  5. Paid to the shop QR (P2M)                   -> sale
  6. Same family name as the owner               -> personal
  7. Same person, round amount, 3+ months        -> personal (family support / rent pattern)
  8. Regular QR customer paying a normal bill    -> sale
  9. Small direct transfer                       -> sale (low confidence)
 10. Large direct transfer                       -> unsure, ask owner

Live payments are classified the moment they arrive (classify_one); the full picture is
recalculated nightly and in the background after each live payment (run).
"""
import json
import re
from datetime import date, datetime, timedelta

import pandas as pd

import db

PERSONAL_WORDS = r"\b(rent|papa|mummy|mumma|maa|ghar|home|family|fees|kitty|rakhi|loan|salary|self|gift|bhai|didi)\b"


def decide(*, mode, note, name, amount, risk_level, owner_action, owner_label, surname,
           round_months, payer_p2m_median):
    """One payment -> (category, confidence, reason). Shared by nightly and live classification."""
    name = name.lower() if isinstance(name, str) else ""        # new payers may have no name
    note = note if isinstance(note, str) else ""
    note_l = note.lower()
    if owner_action == "refunded":
        return "refunded", 1.0, "Refunded to the sender"
    if owner_label:
        return owner_label, 1.0, "Confirmed by you"
    if risk_level == "hold":
        return "suspicious", 0.9, "Flagged by Suraksha as high risk"
    if re.search(PERSONAL_WORDS, note_l):
        if mode == "P2M":
            return "personal", 0.6, f"Paid to your shop QR, but the note says '{note}'"
        return "personal", 0.9, f"Note says '{note}'"
    if mode == "P2M":
        return "sale", 0.95, "Paid to your shop QR"
    if surname and name.split()[-1:] == [surname]:
        return "personal", 0.85, "Same family name as you"
    if round_months >= 3:
        return "personal", 0.85, "Same person sends a round amount every month"
    if payer_p2m_median and amount <= 3 * payer_p2m_median:
        return "sale", 0.8, "Regular customer paying a normal bill by number"
    if amount < 2000:
        return "sale", 0.6, "Small direct transfer, probably a sale"
    return "personal", 0.5, "Large direct transfer, not sure"


def _surname():
    return (db.get_setting("owner_name") or "Ramesh Gupta").lower().split()[-1]


def classify(ref_date: date):
    surname = _surname()
    pay = pd.DataFrame(db.query_all(
        "SELECT p.id, p.ts, p.payer_vpa, p.amount, p.mode, p.note, p.risk_level, p.category, p.category_source, p.owner_action, "
        "y.name FROM payments p LEFT JOIN payers y ON y.vpa = p.payer_vpa WHERE p.ts < ?",
        ((ref_date + timedelta(days=1)).isoformat(),)))
    pay["month"] = pay["ts"].str[:7]
    owner_labels = {r["payer_vpa"]: r["category"] for r in db.query_all(
        "SELECT DISTINCT payer_vpa, category FROM payments WHERE category_source='owner' "
        "AND COALESCE(owner_action,'')!='refunded'")}

    p2m_bills = pay[pay["mode"] == "P2M"].groupby("payer_vpa")["amount"].median()
    p2p = pay[pay["mode"] == "P2P"]
    round_months = (p2p[(p2p["amount"] % 500 == 0) & (p2p["amount"] >= 2000)]
                    .groupby("payer_vpa")["month"].nunique())

    out = []
    for r in pay.itertuples():
        cat, conf, why = decide(mode=r.mode, note=r.note, name=r.name, amount=r.amount, risk_level=r.risk_level,
                                owner_action=r.owner_action, owner_label=owner_labels.get(r.payer_vpa),
                                surname=surname, round_months=int(round_months.get(r.payer_vpa, 0)),
                                payer_p2m_median=p2m_bills.get(r.payer_vpa))
        src = "owner" if (r.payer_vpa in owner_labels or r.owner_action == "refunded") else "rule"
        out.append((cat, conf, src, r.id, why))
    with db.get_conn() as c:
        c.executemany("UPDATE payments SET category=?, category_confidence=?, category_source=? WHERE id=?",
                      [o[:4] for o in out])
    pay["category"] = [o[0] for o in out]
    pay["conf"] = [o[1] for o in out]
    pay["why"] = [o[4] for o in out]
    return pay


def classify_one(payment_id: int):
    """Classify a single live payment immediately, using that payer's history."""
    p = db.query_one("SELECT p.*, y.name FROM payments p LEFT JOIN payers y ON y.vpa=p.payer_vpa WHERE p.id=?",
                     (payment_id,))
    label = db.query_one("SELECT category FROM payments WHERE payer_vpa=? AND category_source='owner' "
                         "AND COALESCE(owner_action,'')!='refunded' LIMIT 1", (p["payer_vpa"],))
    months = db.query_one("SELECT COUNT(DISTINCT substr(ts,1,7)) n FROM payments WHERE payer_vpa=? AND mode='P2P' "
                          "AND amount>=2000 AND CAST(amount AS INTEGER) % 500 = 0", (p["payer_vpa"],))["n"]
    bills = [r["amount"] for r in db.query_all("SELECT amount FROM payments WHERE payer_vpa=? AND mode='P2M' AND id!=?",
                                                (p["payer_vpa"], payment_id))]
    median = sorted(bills)[len(bills) // 2] if bills else None
    cat, conf, why = decide(mode=p["mode"], note=p["note"], name=p["name"], amount=p["amount"],
                            risk_level=p["risk_level"], owner_action=p["owner_action"],
                            owner_label=label["category"] if label else None, surname=_surname(),
                            round_months=months, payer_p2m_median=median)
    db.execute("UPDATE payments SET category=?, category_confidence=?, category_source=? WHERE id=?",
               (cat, conf, "owner" if label else "rule", payment_id))
    return {"category": cat, "confidence": conf, "reason": why}


def run(ref_date: date):
    limit = db.get_setting("gst_limit")
    fy_start = date.fromisoformat(db.get_setting("fy_start"))
    pay = classify(ref_date)
    fy = pay[pay["ts"] >= fy_start.isoformat()]
    raw = float(fy["amount"].sum())
    sales = float(fy.loc[fy["category"] == "sale", "amount"].sum())
    personal = float(fy.loc[fy["category"] == "personal", "amount"].sum())
    suspicious = float(fy.loc[fy["category"] == "suspicious", "amount"].sum())
    days = (ref_date - fy_start).days + 1
    fy_end = date(fy_start.year + 1, 3, 31)
    per_day = sales / days
    projected = per_day * ((fy_end - fy_start).days + 1)
    cross_date = fy_start + timedelta(days=int(limit / per_day)) if per_day else None
    pct = sales / limit * 100

    # personal money sources, for the owner and a notice reply
    pers = fy[fy["category"] == "personal"].assign(name=lambda d: d["name"].fillna(d["payer_vpa"])).groupby(["payer_vpa", "name"]).agg(
        total=("amount", "sum"), n=("amount", "size"),
        why=("why", lambda s: s.value_counts().index[0])).reset_index().sort_values("total", ascending=False)
    # questions: lowest-confidence payers (owner answers once per person)
    fy = fy.assign(name=fy["name"].fillna(fy["payer_vpa"]))
    unsure = fy[fy["conf"] <= 0.6].groupby(["payer_vpa", "name"]).agg(
        total=("amount", "sum"), n=("amount", "size"), guess=("category", "first"),
        why=("why", "first")).reset_index()
    unsure = unsure.sort_values("total", ascending=False).head(5)

    result = {
        "ref_date": ref_date.isoformat(), "fy_start": fy_start.isoformat(), "limit": limit,
        "updated_at": datetime.now().replace(microsecond=0).isoformat(),
        "raw_upi_total": round(raw), "business_sales": round(sales), "personal": round(personal),
        "suspicious": round(suspicious), "pct_of_limit": round(pct, 1),
        "projected_year_end": round(projected),
        "cross_date": cross_date.isoformat() if cross_date and cross_date <= fy_end else None,
        "raw_looks_over_limit": raw >= limit,
        "personal_sources": [{"payer": r.name or r.payer_vpa, "vpa": r.payer_vpa, "reason": r.why,
                              "total": round(r.total), "count": int(r.n)}
                             for r in pers.itertuples()],
        "questions": [{"vpa": r.payer_vpa, "payer": r.name or r.payer_vpa, "total": round(r.total),
                       "count": int(r.n), "guess": r.guess, "reason": r.why} for r in unsure.itertuples()],
    }
    alerts = []
    level = next((lv for lv in (95, 85, 70) if pct >= lv), None)
    if level:
        when = f" At this pace you cross the limit around {result['cross_date']}." if result["cross_date"] else ""
        extra = (f" Your raw UPI total (Rs {raw:,.0f}) already looks over the limit, but Rs {personal:,.0f} of it "
                 f"is personal money, which Munshi has separated.") if raw >= limit else ""
        alerts.append(("tax", "high" if level >= 85 else "warn",
                       f"You are at {pct:.0f}% of the GST registration limit",
                       f"Real shop sales this year: Rs {sales:,.0f} of Rs {limit:,.0f}.{when}{extra}",
                       json.dumps({k: result[k] for k in ("business_sales", "raw_upi_total", "personal", "pct_of_limit", "cross_date")})))
    return result, alerts


def label_payer(vpa: str, category: str):
    if category not in ("sale", "personal"):
        raise ValueError("category must be sale or personal")
    with db.get_conn() as c:
        c.execute("UPDATE payments SET category=?, category_confidence=1.0, category_source='owner' WHERE payer_vpa=?",
                  (category, vpa))
