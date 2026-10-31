"""Database setup for Munshi AI (SQLite). One shop: Ramesh Kirana Store."""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "munshi.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL UNIQUE,          -- owner | staff | customer | admin
    name TEXT NOT NULL,
    pin TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merchant_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL                 -- JSON-encoded
);

CREATE TABLE IF NOT EXISTS payers (
    vpa TEXT PRIMARY KEY,               -- e.g. rahul.k@okaxis
    name TEXT,
    kind TEXT,                          -- regular | casual | family | fraud
    first_seen TEXT,
    is_reported INTEGER DEFAULT 0,      -- on reported-fraud list
    account_age_days INTEGER,           -- Paytm network signal (simulated)
    network_shops_24h INTEGER DEFAULT 0,-- how many different shops this payer paid in last 24h
    pass_through INTEGER DEFAULT 0,     -- money lands and leaves within minutes (mule pattern)
    trusted INTEGER DEFAULT 0           -- owner marked this payer genuine
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                   -- ISO timestamp
    payer_vpa TEXT NOT NULL,
    amount REAL NOT NULL,
    mode TEXT NOT NULL,                 -- P2M (merchant QR) | P2P (direct transfer)
    note TEXT DEFAULT '',
    cashback_requested INTEGER DEFAULT 0,
    risk_score REAL,
    risk_level TEXT,                    -- safe | verify | hold
    risk_reasons TEXT,                  -- JSON list
    staff_action TEXT,                  -- verified | held | NULL
    category TEXT,                      -- sale | personal | exempt | refund
    category_confidence REAL,
    category_source TEXT,               -- rule | llm | owner
    is_live INTEGER DEFAULT 0,          -- 1 = made from simulator during demo
    truth TEXT,                         -- hidden ground truth: sale | personal | fraud (never shown in UI)
    owner_action TEXT,                  -- genuine | refunded
    owner_action_ts TEXT,
    reported INTEGER DEFAULT 0,         -- owner reported this payment as fraud
    reported_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_pay_ts ON payments(ts);
CREATE INDEX IF NOT EXISTS idx_pay_payer ON payments(payer_vpa);

CREATE TABLE IF NOT EXISTS day_context (
    date TEXT PRIMARY KEY,
    weekday INTEGER,                    -- 0 = Monday
    is_rain INTEGER DEFAULT 0,
    festival TEXT,
    salary_week INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,       -- hashed payer id, owner never sees VPA
    payer_vpa TEXT UNIQUE,
    visits INTEGER DEFAULT 0,
    total_spent REAL DEFAULT 0,
    first_visit TEXT,
    last_visit TEXT,
    avg_gap_days REAL,
    opted_in INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    module TEXT NOT NULL,               -- suraksha | galla | tax | wapsi
    severity TEXT NOT NULL,             -- info | warn | high
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT,                          -- JSON
    status TEXT DEFAULT 'new'           -- new | seen | useful | wrong
);

CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT DEFAULT 'sent',         -- sent | redeemed | expired
    cashback_pct INTEGER DEFAULT 10,    -- % of the next payment credited back
    max_cashback INTEGER DEFAULT 50,    -- cap in rupees
    valid_until TEXT,
    payment_id INTEGER,                 -- payment that used the offer
    paid_amount REAL,
    cashback_amount REAL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    created TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    created TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

DEFAULT_USERS = [
    ("owner", "Ramesh (Owner)", "1111"),
    ("staff", "Counter Staff", "2222"),
    ("customer", "Customer", "3333"),
    ("admin", "Demo Admin", "9999"),
]

DEFAULT_SETTINGS = {
    "shop_name": "Ramesh Kirana Store",
    "owner_name": "Ramesh Gupta",
    "category": "kirana",
    "upi_ids": ["rameshkirana@paytm"],
    "shifts": [
        {"name": "Morning", "start": 8, "end": 14, "staff": "Suresh"},
        {"name": "Evening", "start": 14, "end": 22, "staff": "Vikram"},
    ],
    "gst_limit": 4000000,             # Rs 40 lakh (goods, most states)
    "fy_start": "2026-04-01",
}


UPGRADE_COLUMNS = {   # columns added after the first version; older databases are upgraded in place
    "offers": {"cashback_pct": "INTEGER DEFAULT 10", "max_cashback": "INTEGER DEFAULT 50", "valid_until": "TEXT",
               "payment_id": "INTEGER", "paid_amount": "REAL", "cashback_amount": "REAL"},
    "payments": {"owner_action": "TEXT", "owner_action_ts": "TEXT", "reported": "INTEGER DEFAULT 0", "reported_ts": "TEXT"},
    "payers": {"trusted": "INTEGER DEFAULT 0"},
}


def init_db():
    with get_conn() as c:
        c.executescript(SCHEMA)
        for table, cols in UPGRADE_COLUMNS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, typ in cols.items():
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        c.executemany("INSERT OR IGNORE INTO users(role, name, pin) VALUES (?,?,?)", DEFAULT_USERS)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO merchant_settings(key, value) VALUES (?,?)", (k, json.dumps(v)))


# ---------- small helpers used everywhere ----------
def query_all(sql, params=()):
    with get_conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def query_one(sql, params=()):
    rows = query_all(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    with get_conn() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def get_setting(key):
    row = query_one("SELECT value FROM merchant_settings WHERE key=?", (key,))
    return json.loads(row["value"]) if row else None


def set_setting(key, value):
    execute("INSERT OR REPLACE INTO merchant_settings(key, value) VALUES (?,?)", (key, json.dumps(value)))


TABLES = ["users", "merchant_settings", "payers", "payments", "day_context", "customers", "alerts", "offers", "llm_cache"]
DATA_TABLES = ["payers", "payments", "day_context", "customers", "alerts", "offers"]


def reset_data_tables():
    """Drop generated data (keeps users, settings, llm cache) and recreate with latest schema."""
    with get_conn() as c:
        for t in DATA_TABLES:
            c.execute(f"DROP TABLE IF EXISTS {t}")
    init_db()


def table_counts():
    return {t: query_one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in TABLES}
