import sqlite3
import os
import json
import hashlib

DB_FILE = "finances.db"

def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def hash_pin(pin: str) -> str:
    """Hash PIN code using SHA-256 with salt."""
    salt = "fin_track_sec_2026"
    return hashlib.sha256((salt + pin).encode()).hexdigest()

def init_db():
    conn = get_connection()
    cur = conn.cursor()

    # Settings table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    # Accounts table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        account_type TEXT DEFAULT 'other', -- 'card', 'cash', 'savings', 'other'
        currency TEXT NOT NULL,
        balance REAL DEFAULT 0.0,
        icon TEXT DEFAULT 'wallet',
        color TEXT DEFAULT '#10b981',
        is_archived INTEGER DEFAULT 0
    )
    """)

    # Check if account_type column exists
    cur.execute("PRAGMA table_info(accounts)")
    cols = [r["name"] for r in cur.fetchall()]
    if "account_type" not in cols:
        cur.execute("ALTER TABLE accounts ADD COLUMN account_type TEXT DEFAULT 'other'")

    # Categories table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL, -- 'expense', 'income', 'investment'
        icon TEXT DEFAULT 'tag',
        color TEXT DEFAULT '#64748b'
    )
    """)

    # Transactions table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL, -- 'expense', 'income', 'investment', 'transfer'
        amount REAL NOT NULL,
        currency TEXT NOT NULL, -- 'USD', 'TRY', 'KZT'
        account_id INTEGER,
        to_account_id INTEGER,
        category_id INTEGER,
        date TEXT NOT NULL,
        time TEXT DEFAULT '',
        note TEXT DEFAULT '',
        equivalent_usd REAL DEFAULT 0.0,
        equivalent_try REAL DEFAULT 0.0,
        equivalent_kzt REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES accounts(id),
        FOREIGN KEY (to_account_id) REFERENCES accounts(id),
        FOREIGN KEY (category_id) REFERENCES categories(id)
    )
    """)

    # Goals / Investments tracking table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        target_amount REAL NOT NULL,
        currency TEXT NOT NULL,
        current_amount REAL DEFAULT 0.0,
        deadline TEXT DEFAULT '',
        icon TEXT DEFAULT 'target',
        color TEXT DEFAULT '#8b5cf6',
        note TEXT DEFAULT ''
    )
    """)

    # Seed simplified accounts (Card, Cash, Savings)
    cur.execute("SELECT COUNT(*) FROM accounts")
    count = cur.fetchone()[0]
    if count == 0:
        cur.executemany("""
        INSERT INTO accounts (name, account_type, currency, balance, icon, color) VALUES (?, ?, ?, ?, ?, ?)
        """, [
            ("Карта (Банк)", "card", "USD", 0.0, "credit-card", "#0ea5e9"),
            ("Наличные (Кэш)", "cash", "USD", 0.0, "banknote", "#10b981"),
            ("Накопления", "savings", "USD", 0.0, "piggy-bank", "#8b5cf6")
        ])
    else:
        # If older accounts exist, update them to simple names
        cur.execute("SELECT id, name FROM accounts ORDER BY id ASC")
        rows = cur.fetchall()
        if len(rows) >= 2 and any("Kaspi" in r["name"] or "Ziraat" in r["name"] for r in rows):
            cur.execute("DELETE FROM accounts")
            cur.executemany("""
            INSERT INTO accounts (id, name, account_type, currency, balance, icon, color, is_archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (1, "Карта (Банк)", "card", "USD", 0.0, "credit-card", "#0ea5e9", 0),
                (2, "Наличные (Кэш)", "cash", "USD", 0.0, "banknote", "#10b981", 0),
                (3, "Накопления", "savings", "USD", 0.0, "piggy-bank", "#8b5cf6", 0)
            ])

    # Seed initial categories if empty
    cur.execute("SELECT COUNT(*) FROM categories")
    if cur.fetchone()[0] == 0:
        default_categories = [
            # Expenses
            ("Продукты и супермаркеты", "expense", "shopping-cart", "#10b981"),
            ("Кафе и рестораны", "expense", "utensils", "#f59e0b"),
            ("Жилье и аренда", "expense", "home", "#6366f1"),
            ("Транспорт и такси", "expense", "car", "#3b82f6"),
            ("Здоровье и аптеки", "expense", "heart-pulse", "#ec4899"),
            ("Покупки и одежда", "expense", "shopping-bag", "#8b5cf6"),
            ("Связь и интернет", "expense", "wifi", "#06b6d4"),
            ("Подписки и сервисы", "expense", "film", "#a855f7"),
            ("Личные расходы на себя", "expense", "smile", "#ec4899"),
            ("Развлечения и отдых", "expense", "gamepad-2", "#f97316"),
            ("Прочие расходы", "expense", "more-horizontal", "#64748b"),
            # Incomes
            ("Зарплата / Доход", "income", "briefcase", "#10b981"),
            ("Фриланс / Проекты", "income", "laptop", "#3b82f6"),
            ("Прочий доход", "income", "plus-circle", "#64748b"),
            # Investments / Savings
            ("Подушка безопасности", "investment", "shield-check", "#10b981"),
            ("Накопления", "investment", "piggy-bank", "#8b5cf6")
        ]
        cur.executemany("""
        INSERT INTO categories (name, type, icon, color) VALUES (?, ?, ?, ?)
        """, default_categories)

    # Seed default personal monthly budget setting if not present
    cur.execute("SELECT value FROM settings WHERE key = 'monthly_budget'")
    if not cur.fetchone():
        default_budget = json.dumps({
            "amount": 1000.0,
            "currency": "USD",
            "card_percent": 50,
            "cash_percent": 50
        })
        cur.execute("INSERT INTO settings (key, value) VALUES ('monthly_budget', ?)", (default_budget,))

    conn.commit()
    conn.close()

def get_setting(key: str, default=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()
