"""Nightly check: runs Tax Kavach -> Galla Check -> Wapsi, saves insights and alerts,
and measures accuracy against the hidden planted events (for the demo / final deck)."""
from datetime import date, datetime

import db
from modules import galla, tax, wapsi


def reference_date():
    rng = db.get_setting("data_range") or {}
    return date.fromisoformat(rng.get("end", date.today().isoformat()))


def run():
    ref = reference_date()
    tax_res, tax_alerts = tax.run(max(ref, date.today()))   # first: labels payments; includes today's live payments
    galla_res, galla_alerts = galla.run(ref)
    wapsi_res, wapsi_alerts = wapsi.run(ref, galla_res.get("flagged"))

    now = datetime.now().replace(microsecond=0).isoformat()
    with db.get_conn() as c:
        c.execute("DELETE FROM alerts WHERE module IN ('galla','tax','wapsi')")
        c.executemany("INSERT INTO alerts(ts,module,severity,title,message,data) VALUES (?,?,?,?,?,?)",
                      [(now, *a) for a in tax_alerts + galla_alerts + wapsi_alerts])
    db.set_setting("insight_tax", tax_res)
    db.set_setting("insight_galla", galla_res)
    db.set_setting("insight_wapsi", wapsi_res)
    db.set_setting("last_nightly", now)
    acc = accuracy(galla_res, wapsi_res)
    db.set_setting("last_accuracy", acc)
    return {"ran_at": now, "reference_date": ref.isoformat(),
            "alerts_created": len(tax_alerts) + len(galla_alerts) + len(wapsi_alerts),
            "accuracy": acc}


def accuracy(galla_res, wapsi_res):
    """Compare what the modules found with the hidden truth planted by the generator."""
    plant = db.get_setting("planted_events") or {}
    out = {}
    # Tax: classification accuracy on non-fraud payments
    row = db.query_one("""SELECT COUNT(*) n,
        SUM(CASE WHEN category = truth THEN 1 ELSE 0 END) ok,
        SUM(CASE WHEN truth='personal' THEN 1 ELSE 0 END) pers,
        SUM(CASE WHEN truth='personal' AND category='personal' THEN 1 ELSE 0 END) pers_ok
        FROM payments WHERE truth IN ('sale','personal')""")
    out["tax"] = {"classification_accuracy_pct": round(100 * row["ok"] / row["n"], 2),
                  "personal_transfers_caught": f"{row['pers_ok']}/{row['pers']}"}
    # Galla
    f, leak = galla_res.get("flagged"), plant.get("galla_leak", {})
    out["galla"] = {"leak_found": bool(f), "planted_start": leak.get("start"),
                    "detected_start": f and f["since"], "planted_loss": leak.get("amount_diverted"),
                    "estimated_loss": f and f["estimated_loss"]}
    # Wapsi
    planted = {db.query_one("SELECT customer_id FROM customers WHERE payer_vpa=?", (c["vpa"],))["customer_id"]
               for c in plant.get("wapsi_churned", [])}
    found = {x["customer_id"] for x in wapsi_res["lost"]}
    out["wapsi"] = {"planted": len(planted), "found": len(found), "correct": len(planted & found),
                    "false_alarms": len(found - planted),
                    "explained_by_qr_leak": len(wapsi_res.get("leak_hidden", []))}
    # Suraksha
    s = db.query_one("""SELECT SUM(CASE WHEN truth='fraud' AND risk_level='hold' THEN 1 ELSE 0 END) caught,
        SUM(CASE WHEN truth='fraud' THEN 1 ELSE 0 END) fraud,
        SUM(CASE WHEN truth!='fraud' AND risk_level!='safe' THEN 1 ELSE 0 END) false_alarms,
        SUM(CASE WHEN truth!='fraud' THEN 1 ELSE 0 END) legit FROM payments WHERE is_live=0""")
    out["suraksha"] = {"fraud_caught": f"{s['caught']}/{s['fraud']}", "false_alarms": f"{s['false_alarms']}/{s['legit']}"}
    return out
