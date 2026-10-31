"""Munshi AI - synthetic data generator.

Creates 7 months (1 Apr - 30 Oct 2026) of UPI payments for Ramesh Kirana Store, Mumbai,
with realistic patterns and HIDDEN planted events that the AI modules must find:

  1. Suraksha  : 5 fraud payers (mule accounts) made large risky payments; 3 more kept for the live demo,
                 plus 1 brand-new (9-day-old) account for the "please verify" demo
  2. Galla     : from 12 Oct the evening-shift QR was swapped; ~55% of evening QR payments were diverted
  3. Tax       : family / rent / self transfers mixed into UPI, so the raw UPI total looks bigger than real sales
  4. Wapsi     : 6 high-value regulars stopped coming between 12 Sep and 2 Oct

Run directly:  python generator.py
"""
import hashlib
import json
import math
from datetime import date, datetime, timedelta

import numpy as np
from faker import Faker

import db

SEED = 42
START = date(2026, 4, 1)
END = date(2026, 10, 30)
LEAK_START = date(2026, 10, 12)
LEAK_RATE = 0.55                      # share of evening QR payments diverted
SHOP_UPI = "rameshkirana@paytm"

FESTIVALS = {
    date(2026, 8, 15): "Independence Day",
    date(2026, 8, 28): "Raksha Bandhan",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 20): "Dussehra",
}
OPEN_HOURS = list(range(7, 22))                                   # 7 am to 10 pm
HOUR_WEIGHTS = np.array([3, 7, 9, 8, 6, 4, 3, 3, 3, 4, 6, 9, 10, 8, 5], dtype=float)
HOUR_WEIGHTS /= HOUR_WEIGHTS.sum()
WEEKDAY_FACTOR = [0.95, 0.95, 0.95, 1.0, 1.05, 1.15, 1.2]        # Mon..Sun
UPI_HANDLES = ["okaxis", "oksbi", "ybl", "paytm", "okhdfcbank", "ibl", "okicici", "axl"]

N_REGULARS = 320
CASUAL_PER_DAY = 46


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


class Gen:
    def __init__(self):
        self.rng = np.random.default_rng(SEED)
        self.fake = Faker("en_IN")
        Faker.seed(SEED)
        self.used_vpas = set()
        self.payers = {}          # vpa -> dict
        self.payments = []        # list of dicts
        self.day_ctx = {}
        self.planted = {}

    # ---------- helpers ----------
    def vpa_for(self, name):
        first, *rest = name.lower().replace(".", "").split()
        last = rest[-1] if rest else ""
        while True:
            style = self.rng.integers(0, 3)
            if style == 0:
                local = f"{first}.{last}" if last else first
            elif style == 1:
                local = f"{first}{self.rng.integers(10, 9999)}"
            else:
                local = f"{first}{last[:1]}{self.rng.integers(1, 99)}"
            vpa = f"{local}@{self.rng.choice(UPI_HANDLES)}"
            if vpa not in self.used_vpas:
                self.used_vpas.add(vpa)
                return vpa

    def add_payer(self, kind, name=None, vpa=None, **extra):
        name = name or self.fake.name()
        vpa = vpa or self.vpa_for(name)
        p = {"vpa": vpa, "name": name, "kind": kind, "first_seen": None, "is_reported": 0,
             "account_age_days": int(self.rng.integers(200, 3000)),
             "network_shops_24h": int(self.rng.integers(0, 4)), "pass_through": 0}
        p.update(extra)
        self.payers[vpa] = p
        return p

    def pay(self, d, hour, minute, vpa, amount, mode="P2M", note="", truth="sale"):
        ts = datetime(d.year, d.month, d.day, hour, minute, int(self.rng.integers(0, 60)))
        self.payments.append({"ts": ts.isoformat(), "payer_vpa": vpa, "amount": float(round(amount)),
                              "mode": mode, "note": note, "truth": truth})

    def bill(self, median, spread=0.5):
        amt = self.rng.lognormal(math.log(median), spread)
        amt = max(10, amt)
        return round(amt / 10) * 10 if self.rng.random() < 0.25 else round(amt)

    def rand_hour(self, preferred=None):
        if preferred is None:
            return int(self.rng.choice(OPEN_HOURS, p=HOUR_WEIGHTS))
        return int(np.clip(round(preferred + self.rng.normal(0, 1.5)), 7, 21))

    def diverted(self, d, hour, mode):
        """Planted QR swap: evening-shift merchant-QR payments partly go to staff's own QR."""
        return d >= LEAK_START and hour >= 14 and mode == "P2M" and self.rng.random() < LEAK_RATE

    # ---------- 1. calendar ----------
    def build_calendar(self):
        for d in daterange(START, END):
            monsoon = date(2026, 6, 10) <= d <= date(2026, 9, 30)
            rain = int(self.rng.random() < (0.55 if monsoon else 0.03))
            fest = FESTIVALS.get(d)
            pre_fest = any(0 <= (f - d).days <= 2 for f in FESTIVALS)
            self.day_ctx[d] = {"date": d.isoformat(), "weekday": d.weekday(), "is_rain": rain,
                               "festival": fest, "salary_week": int(d.day <= 7), "pre_fest": pre_fest}

    def day_factor(self, d):
        c = self.day_ctx[d]
        f = WEEKDAY_FACTOR[c["weekday"]]
        f *= 1.10 if c["salary_week"] else 1.0
        f *= 1.35 if c["pre_fest"] else 1.0
        f *= 0.80 if c["is_rain"] else 1.0
        f *= 1 + 0.0005 * (d - START).days                       # slow business growth
        return f

    # ---------- 2. regular customers ----------
    def build_regulars(self):
        regs = []
        for _ in range(N_REGULARS):
            p = self.add_payer("regular")
            regs.append({
                "vpa": p["vpa"],
                "gap": float(self.rng.uniform(2, 10)),           # usual days between visits
                "median": float(self.rng.lognormal(math.log(150), 0.35)),
                "pref_hour": self.rand_hour(),
                "p2p_habit": self.rng.random() < 0.10,           # some pay shop via P2P transfer
                "start": START + timedelta(days=int(self.rng.integers(0, 60)) if self.rng.random() < 0.15 else 0),
                "churn": None,
            })
        # plant churn: 6 high-value regulars stop coming
        value = sorted(regs, key=lambda r: -r["median"] / r["gap"])
        churned = value[5:11]
        churn_dates = [date(2026, 9, 12) + timedelta(days=int(x)) for x in np.linspace(0, 20, 6)]
        for r, cd in zip(churned, churn_dates):
            r["churn"] = cd
        self.planted["wapsi_churned"] = [{"vpa": r["vpa"], "stopped_after": r["churn"].isoformat(),
                                          "usual_gap_days": round(r["gap"], 1),
                                          "avg_bill": round(r["median"])} for r in churned]
        self.regulars = regs

    def simulate_regulars(self):
        lost = 0.0
        for r in self.regulars:
            d = r["start"] + timedelta(days=int(self.rng.integers(0, max(1, round(r["gap"])))))
            while d <= END:
                if r["churn"] and d > r["churn"]:
                    break
                if self.day_ctx[d]["is_rain"] and self.rng.random() < 0.25:
                    d += timedelta(days=1)                        # skip rainy day, come tomorrow
                    continue
                hour = self.rand_hour(r["pref_hour"])
                mode = "P2P" if r["p2p_habit"] and self.rng.random() < 0.7 else "P2M"
                amt = self.bill(r["median"]) * (1.3 if self.day_ctx[d]["pre_fest"] else 1)
                if self.diverted(d, hour, mode):
                    lost += amt
                else:
                    self.pay(d, hour, int(self.rng.integers(0, 60)), r["vpa"], amt, mode)
                step = r["gap"] * float(self.rng.gamma(4, 0.25))
                d += timedelta(days=max(1, round(step)))
        return lost

    # ---------- 3. walk-in customers ----------
    def simulate_casuals(self):
        lost = 0.0
        for d in daterange(START, END):
            n = self.rng.poisson(CASUAL_PER_DAY * self.day_factor(d))
            for _ in range(n):
                p = self.add_payer("casual")
                hour = self.rand_hour()
                mode = "P2P" if self.rng.random() < 0.05 else "P2M"
                amt = self.bill(75, 0.6) * (1.3 if self.day_ctx[d]["pre_fest"] else 1)
                if self.diverted(d, hour, mode):
                    lost += amt
                    p["kind"] = "casual_diverted"
                else:
                    self.pay(d, hour, int(self.rng.integers(0, 60)), p["vpa"], amt, mode)
        return lost

    # ---------- 4. personal transfers (Tax Kavach trap) ----------
    def simulate_personal(self):
        bro = self.add_payer("family", "Mahesh Gupta", "mahesh.gupta@oksbi")
        tenant = self.add_payer("family", "Anil Deshmukh", "anil.deshmukh@okhdfcbank")
        wife = self.add_payer("family", "Sunita Gupta", "sunita.gupta@ybl")
        son = self.add_payer("family", "Rohan Gupta", "rohan.g21@okaxis")
        self_acc = self.add_payer("family", "Ramesh Gupta", "ramesh.gupta@oksbi")
        sister = self.add_payer("family", "Kavita Sharma", "kavita.sharma@ibl")
        months = ["Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct"]
        total = 0
        for i, m in enumerate(range(4, 11)):
            entries = [
                (date(2026, m, 5), 20000, bro, self.rng.choice(["for ghar", "", "mummy medicine", ""])),
                (date(2026, m, 3), 15000, tenant, f"rent {months[i]}"),
                (date(2026, m, 10), 30000, self_acc, ""),
                (date(2026, m, 12), 10000, wife, ""),
                (date(2026, m, 26), 10000, wife, self.rng.choice(["", "kitty"])),
                (date(2026, m, int(self.rng.integers(1, 28))), int(self.rng.integers(2, 6)) * 1000, son, self.rng.choice(["papa", "", "fees"])),
            ]
            for d, amt, payer, note in entries:
                if d <= END:
                    self.pay(d, int(self.rng.integers(9, 22)), int(self.rng.integers(0, 60)),
                             payer["vpa"], amt, "P2P", note, truth="personal")
                    total += amt
        self.pay(date(2026, 8, 28), 11, 5, sister["vpa"], 5100, "P2P", "rakhi", truth="personal")
        total += 5100
        self.planted["tax_personal_total"] = total

    # ---------- 5. fraud payers (Suraksha) ----------
    def simulate_fraud(self):
        def mule(reported):
            local = f"{self.rng.choice(['quickpay', 'cashnow', 'rapidpay', 'paynow', 'easymoney'])}{self.rng.integers(1000, 9999)}"
            vpa = f"{local}@{self.rng.choice(['ybl', 'axl', 'ibl'])}"
            self.used_vpas.add(vpa)
            return self.add_payer("fraud", self.fake.name(), vpa, is_reported=reported,
                                  account_age_days=int(self.rng.integers(2, 25)),
                                  network_shops_24h=int(self.rng.integers(14, 40)), pass_through=1)

        history = []
        for i, d in enumerate([date(2026, 7, 18), date(2026, 8, 22), date(2026, 9, 9), date(2026, 10, 3), date(2026, 10, 24)]):
            p = mule(reported=int(i % 2 == 0))
            amt = int(self.rng.integers(6, 16)) * 1000
            hour = int(self.rng.choice([7, 21]))
            self.pay(d, hour, int(self.rng.integers(0, 60)), p["vpa"], amt, "P2M",
                     "", truth="fraud")
            history.append({"vpa": p["vpa"], "date": d.isoformat(), "amount": amt})
        live = [mule(reported=r)["vpa"] for r in (1, 0, 0)]
        newbie = self.add_payer("casual", "Arjun Mehta", "arjun.mehta.new@ybl", account_age_days=9, network_shops_24h=2)
        self.planted["suraksha_new_account_payer"] = newbie["vpa"]
        self.planted["suraksha_history"] = history
        self.planted["suraksha_live_demo_payers"] = live

    # ---------- run ----------
    def run(self):
        self.build_calendar()
        self.build_regulars()
        lost = self.simulate_regulars() + self.simulate_casuals()
        self.planted["galla_leak"] = {"start": LEAK_START.isoformat(), "shift": "Evening (14:00-22:00)",
                                      "staff": "Vikram", "diverted_rate": LEAK_RATE,
                                      "amount_diverted": round(lost)}
        self.simulate_personal()
        self.simulate_fraud()
        self.payments.sort(key=lambda p: p["ts"])
        for p in self.payments:                                   # first_seen
            payer = self.payers[p["payer_vpa"]]
            if payer["first_seen"] is None:
                payer["first_seen"] = p["ts"]
        return self


def customer_id(vpa):
    return "C-" + hashlib.sha1(vpa.encode()).hexdigest()[:8].upper()


def save(g: Gen):
    db.reset_data_tables()
    rng = np.random.default_rng(SEED + 1)
    churned = {c["vpa"] for c in g.planted["wapsi_churned"]}
    with db.get_conn() as c:
        c.executemany(
            "INSERT INTO payers(vpa,name,kind,first_seen,is_reported,account_age_days,network_shops_24h,pass_through) "
            "VALUES (:vpa,:name,:kind,:first_seen,:is_reported,:account_age_days,:network_shops_24h,:pass_through)",
            list(g.payers.values()))
        c.executemany(
            "INSERT INTO payments(ts,payer_vpa,amount,mode,note,truth) "
            "VALUES (:ts,:payer_vpa,:amount,:mode,:note,:truth)", g.payments)
        c.executemany(
            "INSERT INTO day_context(date,weekday,is_rain,festival,salary_week) "
            "VALUES (:date,:weekday,:is_rain,:festival,:salary_week)", list(g.day_ctx.values()))
        cust_rows = []
        for p in g.payers.values():
            if p["kind"] in ("regular", "casual") and p["first_seen"]:
                if p["vpa"] in churned:
                    opted = 1 if p["vpa"] != g.planted["wapsi_churned"][-1]["vpa"] else 0
                else:
                    opted = int(rng.random() < (0.6 if p["kind"] == "regular" else 0.3))
                cust_rows.append((customer_id(p["vpa"]), p["vpa"], opted))
        c.executemany("INSERT INTO customers(customer_id,payer_vpa,opted_in) VALUES (?,?,?)", cust_rows)
    db.set_setting("planted_events", g.planted)
    db.set_setting("data_range", {"start": START.isoformat(), "end": END.isoformat()})


def summary():
    s = db.query_one("""SELECT COUNT(*) n, ROUND(SUM(amount)) total,
                         ROUND(SUM(CASE WHEN truth='sale' THEN amount END)) sales,
                         ROUND(SUM(CASE WHEN truth='personal' THEN amount END)) personal,
                         MIN(ts) first, MAX(ts) last FROM payments""")
    s["avg_per_day"] = db.query_one("SELECT ROUND(AVG(n)) a FROM (SELECT COUNT(*) n FROM payments GROUP BY substr(ts,1,10))")["a"]
    s["planted"] = db.get_setting("planted_events")
    return s


def generate():
    from modules import suraksha            # score history so past fraud shows up
    db.init_db()
    save(Gen().run())
    out = summary()
    out["suraksha_backfill"] = suraksha.backfill()
    return out


if __name__ == "__main__":
    out = generate()
    p = out["planted"]
    print(f"\nGenerated {out['n']:,} payments  ({out['first'][:10]} to {out['last'][:10]}, ~{out['avg_per_day']:.0f}/day)")
    print(f"  Raw UPI received : Rs {out['total']:,.0f}   <- what a tax officer sees")
    print(f"  Real shop sales  : Rs {out['sales']:,.0f}")
    print(f"  Personal money   : Rs {out['personal']:,.0f}")
    print("\nPlanted events (hidden from the app):")
    print(f"  Galla  : QR swap from {p['galla_leak']['start']} on evening shift, Rs {p['galla_leak']['amount_diverted']:,} diverted")
    print(f"  Wapsi  : {len(p['wapsi_churned'])} regulars stopped coming")
    print(f"  Suraksha: {len(p['suraksha_history'])} past fraud payments, {len(p['suraksha_live_demo_payers'])} payers saved for live demo")
    print(f"  Tax    : Rs {p['tax_personal_total']:,} of personal transfers mixed in")
    print(f"\nSuraksha scored history: {out['suraksha_backfill']}\n")
