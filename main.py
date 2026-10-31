"""Munshi AI - FastAPI backend (step 1 skeleton)."""
import sys
from contextlib import asynccontextmanager
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))  # always find db.py and modules/ next to this file

from dotenv import load_dotenv

load_dotenv(BASE / ".env")

import secrets  # noqa: E402

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import db  # noqa: E402
import generator  # noqa: E402
from modules import galla, llm, nightly, suraksha, tax, voice  # noqa: E402

import asyncio  # noqa: E402
import re  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from typing import Optional  # noqa: E402

CHANNELS = {"staff", "owner", "customer", "admin"}


# ---------- WebSocket hub: pushes live events to each role's screen ----------
class Hub:
    def __init__(self):
        self.channels = {c: set() for c in CHANNELS}

    async def connect(self, channel, ws):
        await ws.accept()
        self.channels[channel].add(ws)

    def disconnect(self, channel, ws):
        self.channels[channel].discard(ws)

    async def send(self, channel, message: dict):
        for ws in list(self.channels[channel]):
            try:
                await ws.send_json(message)
            except Exception:
                self.disconnect(channel, ws)


hub = Hub()


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    # On a fresh server (e.g. free cloud hosting that wipes files on restart), build the demo data automatically
    if db.query_one("SELECT COUNT(*) n FROM payments")["n"] == 0:
        print("Empty database: generating demo data and running the nightly check...")
        await asyncio.to_thread(generator.generate)
        await asyncio.to_thread(nightly.run)
        print("Demo data ready.")
    yield


app = FastAPI(title="Munshi AI", lifespan=lifespan)


@app.middleware("http")
async def no_cache_for_screens(request, call_next):
    """Always serve the latest screens (avoids Chrome showing an old copy after an update)."""
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


# ---------- basic routes ----------
@app.get("/")
def home():
    if (BASE / "static" / "login.html").exists():
        return RedirectResponse("/static/login.html")
    return {"status": "Munshi AI server is running"}


@app.get("/api/health")
def health():
    return {"status": "ok", "tables": db.table_counts()}


class LoginIn(BaseModel):
    role: str
    pin: str


@app.post("/api/login")
def login(body: LoginIn):
    user = db.query_one("SELECT role, name FROM users WHERE role=? AND pin=?", (body.role, body.pin))
    if not user:
        raise HTTPException(status_code=401, detail="Wrong role or PIN")
    token = secrets.token_urlsafe(18)
    db.execute("INSERT INTO sessions(token, role) VALUES (?,?)", (token, user["role"]))
    return {**user, "token": token}


def role_of(token):
    row = db.query_one("SELECT role FROM sessions WHERE token=?", (token or "",))
    return row["role"] if row else None


def require(*roles):
    """Route guard: only these roles (and admin, who runs the demo) may call the route."""
    def check(x_token: Optional[str] = Header(None)):
        role = role_of(x_token)
        if role is None:
            raise HTTPException(401, "Please log in")
        if role != "admin" and role not in roles:
            raise HTTPException(403, f"The {role} role cannot do this")
        return role
    return Depends(check)


@app.get("/api/me")
def me(x_token: Optional[str] = Header(None)):
    role = role_of(x_token)
    if not role:
        raise HTTPException(401, "Please log in")
    return {"role": role}


@app.post("/api/logout")
def logout(x_token: Optional[str] = Header(None)):
    db.execute("DELETE FROM sessions WHERE token=?", (x_token or "",))
    return {"ok": True}


@app.get("/api/settings", dependencies=[require("owner", "staff")])
def get_settings():
    return {r["key"]: db.get_setting(r["key"]) for r in db.query_all("SELECT key FROM merchant_settings")}


# ---------- Soundbox voice ----------
class TTSIn(BaseModel):
    text: str
    lang: str = "en-IN"


@app.post("/api/tts", dependencies=[require("staff", "owner")])
async def tts(body: TTSIn):
    return await voice.speak(body.text, body.lang)


# ---------- Admin: data generation ----------
@app.post("/api/admin/generate", dependencies=[require("admin")])
def admin_generate():
    """Wipes payments/customers/alerts and creates fresh 7-month synthetic data (~15 seconds)."""
    return generator.generate()


@app.get("/api/admin/summary", dependencies=[require("admin")])
def admin_summary():
    out = generator.summary()
    out["accuracy"] = db.get_setting("last_accuracy")
    out["last_nightly"] = db.get_setting("last_nightly")
    out["live"] = db.query_one("SELECT COUNT(*) n, SUM(CASE WHEN risk_level!='safe' THEN 1 ELSE 0 END) flagged "
                               "FROM payments WHERE is_live=1")
    out["offers"] = db.query_one("SELECT COUNT(*) sent, SUM(CASE WHEN status='redeemed' THEN 1 ELSE 0 END) redeemed, "
                                 "SUM(paid_amount) revenue, SUM(cashback_amount) cost FROM offers")
    return out


# ---------- Payments + Suraksha (real-time path) ----------


def _new_payer_vpa():
    from faker import Faker
    name = Faker("en_IN").first_name().lower()
    return f"{name}{random.randint(10, 9999)}@{random.choice(['okaxis', 'ybl', 'oksbi', 'paytm'])}"


class PaymentIn(BaseModel):
    payer_vpa: Optional[str] = None      # None = brand-new customer
    amount: float
    mode: str = "P2M"                    # P2M (scan shop QR) | P2P (send to number)
    note: str = ""


@app.post("/api/payments", dependencies=[require("customer")])
async def create_payment(body: PaymentIn):
    if body.amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    now = datetime.now().replace(microsecond=0)
    vpa = body.payer_vpa or _new_payer_vpa()
    payer = db.query_one("SELECT * FROM payers WHERE vpa=?", (vpa,))
    if not payer:                        # first time this customer ever pays
        payer = {"vpa": vpa, "name": None, "kind": "casual", "first_seen": now.isoformat(), "is_reported": 0,
                 "account_age_days": random.randint(200, 3000), "network_shops_24h": random.randint(0, 3),
                 "pass_through": 0}
        db.execute("INSERT INTO payers(vpa,name,kind,first_seen,is_reported,account_age_days,network_shops_24h,pass_through) "
                   "VALUES (?,?,?,?,?,?,?,?)", tuple(payer.values()))
        db.execute("INSERT OR IGNORE INTO customers(customer_id,payer_vpa,opted_in) VALUES (?,?,0)",
                   (generator.customer_id(vpa), vpa))
    first_time = db.query_one("SELECT COUNT(*) n FROM payments WHERE payer_vpa=?", (vpa,))["n"] == 0
    score, level, reasons = suraksha.score_payment(body.amount, now.hour, payer, first_time)
    pid = db.execute(
        "INSERT INTO payments(ts,payer_vpa,amount,mode,note,risk_score,risk_level,risk_reasons,is_live) "
        "VALUES (?,?,?,?,?,?,?,?,1)",
        (now.isoformat(), vpa, body.amount, body.mode, body.note, score, level, json.dumps(reasons)))
    # Wapsi loop: is this a lost customer coming back with a valid offer? (never for risky payments)
    cashback = None
    cust = db.query_one("SELECT customer_id FROM customers WHERE payer_vpa=?", (vpa,))
    if cust and level == "safe":
        offer = db.query_one("SELECT * FROM offers WHERE customer_id=? AND status='sent' AND valid_until>=? "
                             "ORDER BY ts DESC", (cust["customer_id"], now.isoformat()))
        if offer:
            cb = float(min(offer["max_cashback"], round(body.amount * offer["cashback_pct"] / 100)))
            db.execute("UPDATE offers SET status='redeemed', payment_id=?, paid_amount=?, cashback_amount=? WHERE id=?",
                       (pid, body.amount, cb, offer["id"]))
            cashback = {"offer_id": offer["id"], "customer_id": cust["customer_id"], "pct": offer["cashback_pct"],
                        "amount": cb, "paid": body.amount, "net": body.amount - cb}

    tax_label = tax.classify_one(pid)                     # sale or personal, decided instantly

    speak = suraksha.speak_text(body.amount, level, reasons)
    if cashback:
        speak += f" Offer applied: {cashback['amount']:,.0f} rupees cashback to customer."
    event = {"type": "payment", "id": pid, "ts": now.isoformat(), "amount": body.amount, "mode": body.mode,
             "score": score, "level": level, "reasons": reasons[:2], "speak": speak, "cashback": cashback}
    if level != "safe":
        db.execute("INSERT INTO alerts(ts,module,severity,title,message,data) VALUES (?,?,?,?,?,?)",
                   (now.isoformat(), "suraksha", "high" if level == "hold" else "warn",
                    f"{'High-risk' if level == 'hold' else 'Check this'} payment of Rs {body.amount:,.0f}",
                    reasons[0], json.dumps({"payment_id": pid, "score": score, "reasons": reasons})))
    await hub.send("staff", event)
    await hub.send("owner", event)

    if cashback:
        await hub.send("owner", {"type": "offer_redeemed", **cashback})
        await hub.send("customer", {"type": "cashback", **cashback})
    if tax_label["category"] == "personal" and level == "safe":
        await hub.send("owner", {"type": "classified", "amount": body.amount, **tax_label})
    if db.get_setting("insight_tax") is not None:
        asyncio.create_task(_refresh_tax())                # Tax Kavach totals update within a second
    return {"payment_id": pid, "status": "success", "cashback": cashback}   # customer never sees the risk


async def _refresh_tax():
    try:
        res, _ = await asyncio.to_thread(tax.run, max(nightly.reference_date(), datetime.now().date()))
        db.set_setting("insight_tax", res)
        await hub.send("owner", {"type": "tax_updated"})
    except Exception as e:                                 # never break a payment because of analytics
        print("Tax refresh failed:", e)


class ActionIn(BaseModel):
    action: str                          # verified | held


@app.post("/api/payments/{pid}/action", dependencies=[require("staff")])
async def payment_action(pid: int, body: ActionIn):
    if body.action not in ("verified", "held"):
        raise HTTPException(400, "action must be verified or held")
    db.execute("UPDATE payments SET staff_action=? WHERE id=?", (body.action, pid))
    await hub.send("owner", {"type": "staff_action", "id": pid, "action": body.action})
    return {"id": pid, "action": body.action}


@app.get("/api/payments/recent", dependencies=[require("staff", "owner")])
def recent_payments(limit: int = 15):
    rows = db.query_all("SELECT id, ts, amount, mode, risk_level, risk_score, risk_reasons, staff_action, is_live "
                        "FROM payments ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
    for r in rows:
        r["risk_reasons"] = json.loads(r["risk_reasons"] or "[]")[:2]
    return rows


@app.get("/api/sim/payers", dependencies=[require("customer")])
def sim_payers():
    """Payer choices for the customer simulator."""
    regulars = db.query_all("SELECT vpa, name FROM payers WHERE kind='regular' ORDER BY vpa LIMIT 8")
    plant = db.get_setting("planted_events") or {}
    demo = [db.query_one("SELECT vpa, name FROM payers WHERE vpa=?", (v,))
            for v in plant.get("suraksha_live_demo_payers", [])]
    lost_ids = [x["customer_id"] for x in (db.get_setting("insight_wapsi") or {}).get("lost", [])]
    lost = [db.query_one("SELECT y.vpa, y.name, c.customer_id FROM customers c JOIN payers y ON y.vpa=c.payer_vpa "
                         "WHERE c.customer_id=?", (cid,)) for cid in lost_ids]
    newbie = db.query_one("SELECT vpa, name FROM payers WHERE vpa=?", (plant.get("suraksha_new_account_payer", ""),))
    return {"regulars": regulars, "suspicious": [d for d in demo if d], "lost": [x for x in lost if x],
            "new_account": [newbie] if newbie else []}


# ---------- Nightly check (batch path): Tax Kavach, Galla Check, Wapsi ----------
@app.post("/api/admin/nightly", dependencies=[require("owner")])
async def run_nightly():
    out = nightly.run()
    await hub.send("owner", {"type": "nightly_done", "alerts": out["alerts_created"]})
    return out


@app.get("/api/insights/{module}", dependencies=[require("owner")])
def get_insight(module: str):
    if module not in ("tax", "galla", "wapsi"):
        raise HTTPException(404, "Unknown module")
    data = db.get_setting(f"insight_{module}")
    if data is None:
        raise HTTPException(404, "Run the nightly check first")
    return data


@app.get("/api/alerts", dependencies=[require("owner")])
def list_alerts(module: Optional[str] = None, limit: int = 50):
    sql, params = "SELECT * FROM alerts", []
    if module:
        sql += " WHERE module=?"
        params.append(module)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(limit)
    rows = db.query_all(sql, tuple(params))
    for r in rows:
        r["data"] = json.loads(r["data"] or "{}")
    return rows


class FeedbackIn(BaseModel):
    status: str                          # useful | wrong | seen


@app.post("/api/alerts/{aid}/feedback", dependencies=[require("owner")])
def alert_feedback(aid: int, body: FeedbackIn):
    if body.status not in ("useful", "wrong", "seen"):
        raise HTTPException(400, "status must be useful, wrong or seen")
    db.execute("UPDATE alerts SET status=? WHERE id=?", (body.status, aid))
    return {"id": aid, "status": body.status}


class LabelIn(BaseModel):
    vpa: str
    category: str                        # sale | personal


@app.post("/api/tax/label", dependencies=[require("owner")])
def tax_label(body: LabelIn):
    """Owner answers 'is money from this person business or personal?' - re-runs the tax check."""
    try:
        tax.label_payer(body.vpa, body.category)
    except ValueError as e:
        raise HTTPException(400, str(e))
    res, _ = tax.run(max(nightly.reference_date(), datetime.now().date()))
    db.set_setting("insight_tax", res)
    return res


class QRIn(BaseModel):
    qr_text: str


@app.post("/api/qr/verify", dependencies=[require("owner")])
def qr_verify(body: QRIn):
    return galla.verify_qr(body.qr_text)


# ---------- Owner app data ----------
SALE_SQL = ("(COALESCE(owner_action,'')!='refunded' AND (category='sale' OR "
            "(category IS NULL AND mode='P2M' AND COALESCE(risk_level,'safe')!='hold')))")


@app.get("/api/owner/home", dependencies=[require("owner")])
def owner_home():
    ref = nightly.reference_date()
    today = datetime.now().date().isoformat()
    last_day = db.query_one(f"SELECT COALESCE(SUM(amount),0) total, COUNT(*) n FROM payments "
                            f"WHERE substr(ts,1,10)=? AND {SALE_SQL}", (ref.isoformat(),))
    live = db.query_one(
        "SELECT COUNT(*) n, "
        "COALESCE(SUM(CASE WHEN category='sale' AND COALESCE(owner_action,'')!='refunded' THEN amount END),0) total, "
        "SUM(CASE WHEN category='sale' AND COALESCE(owner_action,'')!='refunded' THEN 1 ELSE 0 END) sales_n, "
        "COALESCE(SUM(CASE WHEN category='personal' THEN amount END),0) personal, "
        "SUM(CASE WHEN category='personal' THEN 1 ELSE 0 END) personal_n, "
        "SUM(CASE WHEN risk_level!='safe' THEN 1 ELSE 0 END) flagged, "
        "COALESCE(SUM(CASE WHEN owner_action='refunded' THEN amount END),0) refunded "
        "FROM payments WHERE is_live=1 AND substr(ts,1,10)=?", (today,))
    today_rows = db.query_all(
        "SELECT p.id, p.ts, p.amount, p.mode, p.note, p.category, p.category_confidence, p.risk_level, p.owner_action, "
        "p.reported FROM payments p WHERE p.is_live=1 AND substr(p.ts,1,10)=? ORDER BY p.ts DESC, p.id DESC LIMIT 12",
        (today,))
    start = (ref - timedelta(days=13)).isoformat()
    series = db.query_all(f"SELECT substr(ts,1,10) d, ROUND(SUM(amount)) total FROM payments "
                          f"WHERE substr(ts,1,10) BETWEEN ? AND ? AND {SALE_SQL} GROUP BY d ORDER BY d",
                          (start, ref.isoformat()))
    counts = {r["module"]: r["n"] for r in db.query_all(
        "SELECT module, COUNT(*) n FROM alerts WHERE status IN ('new','seen') GROUP BY module")}
    return {"shop": db.get_setting("shop_name"), "owner": db.get_setting("owner_name"),
            "ref_date": ref.isoformat(), "last_day": last_day, "today_live": live, "today_payments": today_rows,
            "series": series,
            "alert_counts": counts, "last_nightly": db.get_setting("last_nightly")}


@app.get("/api/payments/risky", dependencies=[require("owner")])
def risky_payments(limit: int = 30):
    rows = db.query_all("SELECT id, ts, amount, mode, payer_vpa, risk_level, risk_score, risk_reasons, staff_action, "
                        "is_live, owner_action, owner_action_ts, reported, reported_ts "
                        "FROM payments WHERE risk_level IN ('hold','verify') ORDER BY ts DESC LIMIT ?", (limit,))
    for r in rows:
        r["risk_reasons"] = json.loads(r["risk_reasons"] or "[]")
        r["payer"] = _mask(r.pop("payer_vpa"))
    return rows


class OwnerActionIn(BaseModel):
    action: str                          # genuine | refund | report


@app.post("/api/payments/{pid}/owner-action", dependencies=[require("owner")])
async def owner_action(pid: int, body: OwnerActionIn):
    p = db.query_one("SELECT * FROM payments WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "Payment not found")
    if p["risk_level"] not in ("hold", "verify"):
        raise HTTPException(400, "Only flagged payments need a decision")
    payer = db.query_one("SELECT * FROM payers WHERE vpa=?", (p["payer_vpa"],)) or {}
    now = datetime.now().replace(microsecond=0).isoformat()
    amount, who = p["amount"], _mask(p["payer_vpa"])

    if body.action == "genuine":
        if p["reported"]:
            raise HTTPException(400, "This payment was reported as fraud")
        if payer.get("is_reported"):
            raise HTTPException(400, "This payer is on the reported fraud list and cannot be marked genuine")
        db.execute("UPDATE payments SET owner_action='genuine', owner_action_ts=? WHERE id=?", (now, pid))
        db.execute("UPDATE payers SET trusted=1 WHERE vpa=?", (p["payer_vpa"],))
        msg = "Marked genuine. You can hand over the goods. Munshi will trust this customer next time."
        staff = "Owner says this payment is genuine. You can hand over the goods."
    elif body.action == "refund":
        if p["owner_action"] == "refunded":
            raise HTTPException(400, "Already refunded")
        db.execute("UPDATE payments SET owner_action='refunded', owner_action_ts=?, category='refunded', "
                   "category_source='owner' WHERE id=?", (now, pid))
        msg = (f"Rs {amount:,.0f} will be returned to the original sender ({who}) through Paytm. It is removed from "
               f"your sales. Never send it to any other account or number the customer gives you.")
        staff = "Owner is refunding this payment. Do not hand over goods or cash."
    elif body.action == "report":
        if p["owner_action"] == "genuine":
            raise HTTPException(400, "You marked this payment genuine")
        if p["reported"]:
            raise HTTPException(400, "Already reported")
        db.execute("UPDATE payments SET reported=1, reported_ts=? WHERE id=?", (now, pid))
        db.execute("UPDATE payers SET is_reported=1, trusted=0 WHERE vpa=?", (p["payer_vpa"],))
        msg = (f"Reported. Any future payment from this payer will be flagged high risk. Also call the national "
               f"cybercrime helpline 1930 or file at cybercrime.gov.in with: Paytm transaction #{pid}, "
               f"{p['ts'].replace('T', ' ')[:16]}, Rs {amount:,.0f} from {who}.")
        staff = "Owner reported this payment as fraud. Do not hand over goods or cash."
    else:
        raise HTTPException(400, "action must be genuine, refund or report")

    await hub.send("staff", {"type": "owner_decision", "id": pid, "action": body.action, "speak": staff})
    return {"id": pid, "action": body.action, "message": msg}


@app.post("/api/wapsi/{customer_id}/offer", dependencies=[require("owner")])
async def send_offer(customer_id: str):
    c = db.query_one("SELECT * FROM customers WHERE customer_id=?", (customer_id,))
    if not c:
        raise HTTPException(404, "Customer not found")
    if not c["opted_in"]:
        raise HTTPException(400, "This customer has not agreed to receive offers")
    if db.query_one("SELECT 1 FROM offers WHERE customer_id=? AND status='sent' AND valid_until>=?",
                    (customer_id, datetime.now().isoformat())):
        raise HTTPException(400, "This customer already has an active offer")
    info = next((x for x in (db.get_setting("insight_wapsi") or {}).get("lost", []) if x["customer_id"] == customer_id), {})
    shop = db.get_setting("shop_name")
    pct, cap, days = 10, 50, 7                          # the offer terms the system will actually apply
    now = datetime.now().replace(microsecond=0)
    valid_until = (now + timedelta(days=days)).replace(hour=23, minute=59, second=59)
    terms = f"{pct}% cashback up to Rs {cap} on your next Paytm payment at {shop}, valid till {valid_until:%d %b}"
    fallback = (f"Namaste! We miss you at {shop}. Visit us this week and get {pct}% cashback (up to Rs {cap}) on your "
                f"next Paytm payment. Valid till {valid_until:%d %b}.")
    prompt = (f"Write a short, warm WhatsApp-style win-back message (max 40 words, English with a light Hindi touch) "
              f"from the kirana shop '{shop}' to a regular customer who used to visit every {info.get('usual_gap_days', 3)} "
              f"days but has not come for {info.get('days_since', 21)} days. The offer is EXACTLY: {terms}. "
              f"State these exact terms (percentage, cap and date). Do not offer anything else. No customer name. "
              f"Reply with only the message.")
    out = await llm.generate(prompt, fallback=fallback)
    text, source = out["text"], out["source"]
    if f"{pct}%" not in text or str(cap) not in text:  # AI changed the terms -> use the exact template
        text, source = fallback, "template"
    oid = db.execute("INSERT INTO offers(customer_id, ts, message, cashback_pct, max_cashback, valid_until) "
                     "VALUES (?,?,?,?,?,?)", (customer_id, now.isoformat(), text, pct, cap, valid_until.isoformat()))
    await hub.send("customer", {"type": "offer", "customer_id": customer_id, "message": text})
    return {"offer_id": oid, "message": text, "source": source, "terms": terms}


@app.get("/api/offers", dependencies=[require("owner")])
def list_offers():
    return db.query_all("SELECT * FROM offers ORDER BY ts DESC")


# ---------- Customer side: opt-in and offers inbox ----------
@app.get("/api/customer/profile", dependencies=[require("customer")])
def customer_profile(vpa: str):
    c = db.query_one("SELECT customer_id, opted_in FROM customers WHERE payer_vpa=?", (vpa,))
    if not c:
        return {"customer_id": None, "opted_in": False, "offers": []}
    c["offers"] = db.query_all("SELECT ts, message, status, cashback_pct, max_cashback, valid_until, paid_amount, "
                               "cashback_amount FROM offers WHERE customer_id=? ORDER BY ts DESC",
                               (c["customer_id"],))
    return c


class OptInIn(BaseModel):
    vpa: str
    opted_in: bool


@app.post("/api/customers/optin", dependencies=[require("customer")])
def customer_optin(body: OptInIn):
    if not db.query_one("SELECT 1 FROM customers WHERE payer_vpa=?", (body.vpa,)):
        raise HTTPException(404, "Pay the shop once before choosing offers")
    db.execute("UPDATE customers SET opted_in=? WHERE payer_vpa=?", (int(body.opted_in), body.vpa))
    return {"vpa": body.vpa, "opted_in": body.opted_in}


# ---------- Suraksha: account freeze help ----------
class FreezeIn(BaseModel):
    bank_name: str
    account_last4: str = ""
    frozen_on: str                       # date the owner noticed
    payment_id: Optional[int] = None     # the disputed payment, if known
    disputed_amount: Optional[float] = None
    complaint_ref: str = ""              # bank / cyber cell reference, if any
    goods_sold: str = ""                 # what was given against the payment
    city: str = "Mumbai"


def _mask(vpa):
    name, _, handle = (vpa or "").partition("@")
    return (name[:3] + "***@" + handle) if handle else "unknown"


@app.post("/api/freeze", dependencies=[require("owner")])
async def freeze_help(body: FreezeIn):
    shop, owner = db.get_setting("shop_name"), db.get_setting("owner_name")
    upi = ", ".join(db.get_setting("upi_ids"))
    pay = db.query_one("SELECT * FROM payments WHERE id=?", (body.payment_id,)) if body.payment_id else None
    amount = body.disputed_amount or (pay["amount"] if pay else None)
    if not amount:
        raise HTTPException(400, "Choose the disputed payment or enter the disputed amount")

    evidence = [
        f"Merchant: {shop}, owner {owner}, UPI ID {upi}",
        f"Bank account: {body.bank_name}" + (f" ending {body.account_last4}" if body.account_last4 else ""),
        f"Freeze noticed on {body.frozen_on}" + (f", reference {body.complaint_ref}" if body.complaint_ref else ""),
        f"Disputed amount: Rs {amount:,.0f}",
    ]
    if pay:
        reasons = json.loads(pay["risk_reasons"] or "[]")
        evidence += [
            f"Disputed payment: Paytm transaction #{pay['id']} on {pay['ts'].replace('T', ' ')[:16]}, "
            f"Rs {pay['amount']:,.0f} via {'shop QR' if pay['mode'] == 'P2M' else 'direct transfer'} from {_mask(pay['payer_vpa'])}",
            f"Munshi Suraksha risk record at the time: {pay['risk_level']} (score {pay['risk_score']:.0f})"
            + (f"; reasons: {'; '.join(reasons[:3])}" if reasons else ""),
            f"Counter staff action recorded: {pay['staff_action'] or 'none'}",
        ]
        if pay["owner_action"] == "refunded":
            evidence.append(f"Owner refunded Rs {pay['amount']:,.0f} to the original sender on {pay['owner_action_ts'].replace('T', ' ')[:16]}")
        if pay["reported"]:
            evidence.append(f"Owner reported this payment as fraud on {pay['reported_ts'].replace('T', ' ')[:16]}")
    if body.goods_sold:
        evidence.append(f"Goods/services given against this payment: {body.goods_sold}")
    sales = db.query_one("SELECT COUNT(*) n, ROUND(SUM(amount)) s FROM payments WHERE category='sale'")
    if sales and sales["n"]:
        evidence.append(f"Normal business record: {sales['n']:,} customer payments totalling Rs {sales['s']:,.0f} this financial year")

    today = datetime.now().strftime("%d %B %Y")
    fallback = (
        f"{today}\n\nTo,\nThe Branch Manager / Nodal Officer\n{body.bank_name}\n{body.city}\n\n"
        f"Subject: Request to limit the hold on my account to the disputed amount of Rs {amount:,.0f}\n\n"
        f"Respected Sir/Madam,\n\n"
        f"I, {owner}, run {shop} and receive customer payments on UPI ID {upi}. On {body.frozen_on} I found that my "
        f"account" + (f" ending {body.account_last4}" if body.account_last4 else "") + " has been frozen"
        + (f" (reference {body.complaint_ref})" if body.complaint_ref else "") + ". "
        f"I understand this relates to a payment of Rs {amount:,.0f} that I received in the normal course of business"
        + (f" against {body.goods_sold}" if body.goods_sold else "") + ". I had no knowledge of any fraud by the payer.\n\n"
        f"My shop's daily income and my suppliers' payments depend on this account. I humbly request you to keep a hold "
        f"(lien) only on the disputed amount of Rs {amount:,.0f} and release the rest of my account for normal use, "
        f"as per the applicable guidelines. I will fully cooperate with the bank and the police in the investigation.\n\n"
        f"Supporting details are attached below.\n\nThank you.\n\nYours faithfully,\n{owner}\n{shop}\n\n"
        f"Attached details:\n- " + "\n- ".join(evidence))
    prompt = (
        "Draft a polite, formal letter in simple English from an Indian small shop owner to their bank's branch manager / "
        "nodal officer. Their bank account was frozen because a customer payment they received is suspected to come from "
        "fraud. Ask the bank to keep a hold (lien) only on the disputed amount and release the rest of the account, as per "
        "the applicable guidelines, and say they will cooperate with the investigation. Do not invent laws, circular "
        "numbers, names or facts beyond those given. Put today's date at the top. End with a section 'Attached details' "
        f"listing every fact below as bullet points. Reply with only the letter.\n\nToday: {today}\nCity: {body.city}\n"
        "Facts:\n- " + "\n- ".join(evidence))

    def complete_letter(text):            # must end with a sign-off and mention the disputed amount
        tail = text.lower()[-500:]
        digits = re.sub(r"[,\s]", "", text)
        return ("yours" in tail or "regards" in tail) and str(int(amount)) in digits

    out = await llm.generate(prompt, fallback=fallback, validate=complete_letter)
    return {"letter": out["text"], "evidence": evidence, "source": out["source"], "reason": out.get("reason"),
            "disclaimer": "This is a draft to help you. It is not legal advice; check it before sending."}


# ---------- LLM test ----------
class AskIn(BaseModel):
    prompt: str = "Write a one-line friendly offer from a kirana store to a regular customer who has not visited for 3 weeks."


@app.post("/api/admin/llm-test", dependencies=[require("admin")])
async def llm_test(body: AskIn):
    return await llm.generate(body.prompt, fallback="(template) We miss you! Get 10% off on your next visit.")


# ---------- test: push a message to any screen ----------
class PushIn(BaseModel):
    channel: str = "staff"
    text: str = "Test alert from Munshi AI"


@app.post("/api/admin/push-test", dependencies=[require("admin")])
async def push_test(body: PushIn):
    if body.channel not in CHANNELS:
        raise HTTPException(400, "Unknown channel")
    await hub.send(body.channel, {"type": "test", "text": body.text})
    return {"sent_to": body.channel, "listeners": len(hub.channels[body.channel])}


# ---------- WebSocket endpoint ----------
@app.websocket("/ws/{channel}")
async def ws_endpoint(ws: WebSocket, channel: str, token: str = ""):
    role = role_of(token)
    if channel not in CHANNELS or role is None or (role != "admin" and role != channel):
        await ws.close(code=4401)
        return
    await hub.connect(channel, ws)
    try:
        while True:
            await ws.receive_text()   # keep connection alive
    except WebSocketDisconnect:
        hub.disconnect(channel, ws)
