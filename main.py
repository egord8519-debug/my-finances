from fastapi import FastAPI, HTTPException, Header, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import datetime
import json
import os
import secrets
from database import get_connection, init_db, hash_pin, get_setting, set_setting
from currency import get_current_rates, calculate_equivalents, convert_currency

app = FastAPI(title="Финансовый Трекер & Инвестиции", version="1.1.0")

# Ensure DB initialized
init_db()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def find_file(filename: str) -> Optional[str]:
    clean_name = os.path.basename(filename)
    candidates = [
        os.path.join(BASE_DIR, "static", clean_name),
        os.path.join(BASE_DIR, clean_name),
        os.path.join(os.getcwd(), "static", clean_name),
        os.path.join(os.getcwd(), clean_name),
        os.path.join(BASE_DIR, filename),
        os.path.join(os.getcwd(), filename),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None

# Serve static files robustly from static/ or root
@app.get("/static/{file_path:path}")
def serve_static_file(file_path: str):
    p = find_file(file_path)
    if p:
        return FileResponse(p)
    raise HTTPException(status_code=404, detail=f"Static file not found: {file_path}")

@app.get("/manifest.json")
def get_manifest():
    p = find_file("manifest.json")
    if p:
        return FileResponse(p)
    raise HTTPException(status_code=404, detail="Manifest not found")

@app.get("/sw.js")
def get_service_worker():
    p = find_file("sw.js")
    if p:
        return FileResponse(p, media_type="application/javascript")
    raise HTTPException(status_code=404, detail="SW not found")

# Helper to verify auth
def check_auth(authorization: Optional[str] = Header(None)):
    pin_hash = get_setting("pin_hash")
    if not pin_hash:
        return True  # PIN not set yet, allow access
    
    if not authorization:
        raise HTTPException(status_code=401, detail="Требуется авторизация (PIN-код)")
    
    token = authorization.replace("Bearer ", "").strip()
    session_token = get_setting("session_token")
    if token != session_token and token != pin_hash:
        raise HTTPException(status_code=401, detail="Неверный PIN-код или сессия истекла")
    return True

# --- Models ---
class PinSetup(BaseModel):
    pin: str

class PinLogin(BaseModel):
    pin: str

class AccountCreate(BaseModel):
    name: str
    account_type: Optional[str] = 'card'  # 'card', 'cash', 'savings', 'other'
    currency: str  # 'USD', 'TRY', 'KZT'
    balance: float = 0.0
    icon: str = 'wallet'
    color: str = '#10b981'

class CategoryCreate(BaseModel):
    name: str
    type: str  # 'expense', 'income', 'investment'
    icon: str = 'tag'
    color: str = '#64748b'

class TransactionCreate(BaseModel):
    type: str  # 'expense', 'income', 'investment', 'transfer'
    amount: float
    currency: str  # 'USD', 'TRY', 'KZT'
    account_id: Optional[int] = None
    to_account_id: Optional[int] = None
    category_id: Optional[int] = None
    date: str  # YYYY-MM-DD
    time: Optional[str] = ''
    note: Optional[str] = ''
    goal_id: Optional[int] = None

class GoalCreate(BaseModel):
    title: str
    target_amount: float
    currency: str
    current_amount: float = 0.0
    deadline: Optional[str] = ''
    icon: Optional[str] = 'target'
    color: Optional[str] = '#8b5cf6'
    note: Optional[str] = ''

class GoalContribute(BaseModel):
    amount: float
    account_id: Optional[int] = None

class CustomRatesModel(BaseModel):
    TRY: Optional[float] = None
    KZT: Optional[float] = None
    enabled: bool = False

class BudgetModel(BaseModel):
    amount: float
    currency: str = "USD"
    card_percent: float = 50.0
    cash_percent: float = 50.0

# --- Helper: Get Rates with User Settings ---
def get_user_rates():
    custom_str = get_setting("custom_rates")
    custom_rates = None
    if custom_str:
        try:
            cfg = json.loads(custom_str)
            if cfg.get("enabled"):
                custom_rates = cfg
        except Exception:
            pass
    return get_current_rates(custom_rates)

# Helper: Compute monthly budget and 50/50 Card vs Cash breakdown
def compute_budget_status(cur, rates):
    budget_setting = get_setting("monthly_budget")
    if budget_setting:
        try:
            b_cfg = json.loads(budget_setting)
        except Exception:
            b_cfg = {"amount": 1000.0, "currency": "USD", "card_percent": 50.0, "cash_percent": 50.0}
    else:
        b_cfg = {"amount": 1000.0, "currency": "USD", "card_percent": 50.0, "cash_percent": 50.0}

    b_amount = float(b_cfg.get("amount", 1000.0))
    b_curr = b_cfg.get("currency", "USD").upper()
    card_pct = float(b_cfg.get("card_percent", 50.0))
    cash_pct = float(b_cfg.get("cash_percent", 50.0))

    card_budget = round(b_amount * (card_pct / 100.0), 2)
    cash_budget = round(b_amount * (cash_pct / 100.0), 2)

    now = datetime.datetime.now()
    current_month_prefix = now.strftime("%Y-%m")

    cur.execute("""
    SELECT t.amount, t.currency, a.name as account_name, a.account_type
    FROM transactions t
    LEFT JOIN accounts a ON t.account_id = a.id
    WHERE t.type = 'expense' AND t.date LIKE ?
    """, (f"{current_month_prefix}%",))

    card_spent = 0.0
    cash_spent = 0.0

    for r in cur.fetchall():
        amt = r["amount"]
        curr = r["currency"]
        amt_in_b_curr = convert_currency(amt, curr, b_curr, rates)

        acc_name = (r["account_name"] or "").lower()
        acc_type = (r["account_type"] or "").lower()

        if acc_type == "cash" or "налич" in acc_name or "cash" in acc_name:
            cash_spent += amt_in_b_curr
        else:
            card_spent += amt_in_b_curr

    card_spent = round(card_spent, 2)
    cash_spent = round(cash_spent, 2)
    card_remaining = round(card_budget - card_spent, 2)
    cash_remaining = round(cash_budget - cash_spent, 2)
    total_spent = round(card_spent + cash_spent, 2)
    total_remaining = round(b_amount - total_spent, 2)

    return {
        "amount": b_amount,
        "currency": b_curr,
        "card_percent": card_pct,
        "cash_percent": cash_pct,
        "card_budget": card_budget,
        "cash_budget": cash_budget,
        "card_spent": card_spent,
        "cash_spent": cash_spent,
        "card_remaining": card_remaining,
        "cash_remaining": cash_remaining,
        "total_spent": total_spent,
        "total_remaining": total_remaining,
        "card_progress": min(100.0, round((card_spent / card_budget * 100.0), 1)) if card_budget > 0 else 0.0,
        "cash_progress": min(100.0, round((cash_spent / cash_budget * 100.0), 1)) if cash_budget > 0 else 0.0,
        "equivalents": {
            "total_USD": convert_currency(b_amount, b_curr, "USD", rates),
            "total_TRY": convert_currency(b_amount, b_curr, "TRY", rates),
            "total_KZT": convert_currency(b_amount, b_curr, "KZT", rates),
            "card_USD": convert_currency(card_budget, b_curr, "USD", rates),
            "card_TRY": convert_currency(card_budget, b_curr, "TRY", rates),
            "card_KZT": convert_currency(card_budget, b_curr, "KZT", rates),
            "cash_USD": convert_currency(cash_budget, b_curr, "USD", rates),
            "cash_TRY": convert_currency(cash_budget, b_curr, "TRY", rates),
            "cash_KZT": convert_currency(cash_budget, b_curr, "KZT", rates),
            "card_rem_USD": convert_currency(card_remaining, b_curr, "USD", rates),
            "card_rem_TRY": convert_currency(card_remaining, b_curr, "TRY", rates),
            "card_rem_KZT": convert_currency(card_remaining, b_curr, "KZT", rates),
            "cash_rem_USD": convert_currency(cash_remaining, b_curr, "USD", rates),
            "cash_rem_TRY": convert_currency(cash_remaining, b_curr, "TRY", rates),
            "cash_rem_KZT": convert_currency(cash_remaining, b_curr, "KZT", rates),
            "rem_USD": convert_currency(total_remaining, b_curr, "USD", rates),
            "rem_TRY": convert_currency(total_remaining, b_curr, "TRY", rates),
            "rem_KZT": convert_currency(total_remaining, b_curr, "KZT", rates),
        }
    }

import zlib
import base64

EMBEDDED_HTML_B64 = """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"""

def get_html_content() -> str:
    path = find_file("index.html")
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    # Decompress embedded fallback
    try:
        return zlib.decompress(base64.b64decode(EMBEDDED_HTML_B64.encode("ascii"))).decode("utf-8")
    except Exception as e:
        return f"<h1>Ошибка: {e}</h1>"

# --- Root Endpoint: Serve PWA HTML ---
@app.get("/")
def get_index():
    return HTMLResponse(get_html_content())

# --- Auth Endpoints ---
@app.get("/api/auth/status")
def auth_status(authorization: Optional[str] = Header(None)):
    pin_hash = get_setting("pin_hash")
    if not pin_hash:
        return {"has_pin": False, "authenticated": True}
    
    if not authorization:
        return {"has_pin": True, "authenticated": False}
    
    token = authorization.replace("Bearer ", "").strip()
    session_token = get_setting("session_token")
    if token == session_token or token == pin_hash:
        return {"has_pin": True, "authenticated": True}
    return {"has_pin": True, "authenticated": False}

@app.post("/api/auth/setup")
def auth_setup(body: PinSetup):
    if len(body.pin) < 4:
        raise HTTPException(status_code=400, detail="PIN-код должен быть не менее 4 цифр")
    hashed = hash_pin(body.pin)
    token = secrets.token_hex(24)
    set_setting("pin_hash", hashed)
    set_setting("session_token", token)
    return {"status": "ok", "token": token}

@app.post("/api/auth/login")
def auth_login(body: PinLogin):
    pin_hash = get_setting("pin_hash")
    if not pin_hash:
        return {"status": "ok", "token": "open"}
    if hash_pin(body.pin) != pin_hash:
        raise HTTPException(status_code=401, detail="Неверный PIN-код")
    token = secrets.token_hex(24)
    set_setting("session_token", token)
    return {"status": "ok", "token": token}

@app.post("/api/auth/remove-pin")
def auth_remove_pin(auth=Depends(check_auth)):
    set_setting("pin_hash", "")
    set_setting("session_token", "")
    return {"status": "ok", "message": "PIN-код удален"}

# --- Rates Endpoints ---
@app.get("/api/rates")
def get_rates_api():
    rates = get_user_rates()
    custom_str = get_setting("custom_rates")
    custom_cfg = json.loads(custom_str) if custom_str else {"enabled": False, "TRY": None, "KZT": None}
    return {
        "rates": rates,
        "custom": custom_cfg
    }

@app.post("/api/rates/custom")
def set_custom_rates(body: CustomRatesModel, auth=Depends(check_auth)):
    set_setting("custom_rates", body.model_dump_json())
    return {"status": "ok", "rates": get_user_rates()}

# --- Personal Monthly Budget (50% Card / 50% Cash) ---
@app.get("/api/budget")
def get_budget(auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()
    rates = get_user_rates()
    status = compute_budget_status(cur, rates)
    conn.close()
    return status

@app.post("/api/budget")
def update_budget(body: BudgetModel, auth=Depends(check_auth)):
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Сумма лимита должна быть больше 0")
    
    cfg = {
        "amount": body.amount,
        "currency": body.currency.upper(),
        "card_percent": body.card_percent,
        "cash_percent": body.cash_percent
    }
    set_setting("monthly_budget", json.dumps(cfg))
    
    conn = get_connection()
    cur = conn.cursor()
    rates = get_user_rates()
    status = compute_budget_status(cur, rates)
    conn.close()
    return status

# --- Dashboard & Summary ---
@app.get("/api/dashboard")
def get_dashboard(auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()
    rates = get_user_rates()

    # Accounts and Net Worth
    cur.execute("SELECT * FROM accounts WHERE is_archived = 0 ORDER BY id ASC")
    accounts = [dict(row) for row in cur.fetchall()]

    total_net_worth_usd = 0.0
    for acc in accounts:
        bal = acc["balance"]
        curr = acc["currency"]
        bal_usd = convert_currency(bal, curr, "USD", rates)
        acc["equivalent_usd"] = bal_usd
        acc["equivalent_try"] = convert_currency(bal, curr, "TRY", rates)
        acc["equivalent_kzt"] = convert_currency(bal, curr, "KZT", rates)
        total_net_worth_usd += bal_usd

    net_worth_equivalents = {
        "USD": round(total_net_worth_usd, 2),
        "TRY": round(total_net_worth_usd * rates.get("TRY", 1.0), 2),
        "KZT": round(total_net_worth_usd * rates.get("KZT", 1.0), 2)
    }

    # Month statistics (current month)
    now = datetime.datetime.now()
    current_month_prefix = now.strftime("%Y-%m")

    cur.execute("""
    SELECT type, amount, currency FROM transactions
    WHERE date LIKE ?
    """, (f"{current_month_prefix}%",))
    month_txs = cur.fetchall()

    month_income_usd = 0.0
    month_expense_usd = 0.0
    month_investment_usd = 0.0

    for tx in month_txs:
        t_type = tx["type"]
        t_amt = tx["amount"]
        t_curr = tx["currency"]
        amt_usd = convert_currency(t_amt, t_curr, "USD", rates)
        if t_type == "income":
            month_income_usd += amt_usd
        elif t_type == "expense":
            month_expense_usd += amt_usd
        elif t_type == "investment":
            month_investment_usd += amt_usd

    month_stats = {
        "income": {
            "USD": round(month_income_usd, 2),
            "TRY": round(month_income_usd * rates.get("TRY", 1.0), 2),
            "KZT": round(month_income_usd * rates.get("KZT", 1.0), 2)
        },
        "expense": {
            "USD": round(month_expense_usd, 2),
            "TRY": round(month_expense_usd * rates.get("TRY", 1.0), 2),
            "KZT": round(month_expense_usd * rates.get("KZT", 1.0), 2)
        },
        "investment": {
            "USD": round(month_investment_usd, 2),
            "TRY": round(month_investment_usd * rates.get("TRY", 1.0), 2),
            "KZT": round(month_investment_usd * rates.get("KZT", 1.0), 2)
        }
    }

    # Top spending categories for this month
    cur.execute("""
    SELECT c.id, c.name, c.icon, c.color, t.amount, t.currency
    FROM transactions t
    JOIN categories c ON t.category_id = c.id
    WHERE t.type = 'expense' AND t.date LIKE ?
    """, (f"{current_month_prefix}%",))
    cat_txs = cur.fetchall()
    category_totals = {}
    for row in cat_txs:
        cid = row["id"]
        cname = row["name"]
        amt_usd = convert_currency(row["amount"], row["currency"], "USD", rates)
        if cid not in category_totals:
            category_totals[cid] = {
                "id": cid,
                "name": cname,
                "icon": row["icon"],
                "color": row["color"],
                "total_usd": 0.0
            }
        category_totals[cid]["total_usd"] += amt_usd

    top_categories = sorted(category_totals.values(), key=lambda x: x["total_usd"], reverse=True)[:5]
    for c in top_categories:
        c["total_usd"] = round(c["total_usd"], 2)
        c["total_try"] = round(c["total_usd"] * rates.get("TRY", 1.0), 2)
        c["total_kzt"] = round(c["total_usd"] * rates.get("KZT", 1.0), 2)

    # Recent 10 transactions
    cur.execute("""
    SELECT t.*, c.name as category_name, c.icon as category_icon, c.color as category_color,
           a.name as account_name, a2.name as to_account_name
    FROM transactions t
    LEFT JOIN categories c ON t.category_id = c.id
    LEFT JOIN accounts a ON t.account_id = a.id
    LEFT JOIN accounts a2 ON t.to_account_id = a2.id
    ORDER BY t.date DESC, t.id DESC LIMIT 10
    """)
    recent_transactions = [dict(row) for row in cur.fetchall()]

    # Currency allocation percentages
    currency_balances_usd = {"USD": 0.0, "TRY": 0.0, "KZT": 0.0}
    for acc in accounts:
        curr = acc["currency"]
        amt_usd = convert_currency(acc["balance"], curr, "USD", rates)
        currency_balances_usd[curr] = currency_balances_usd.get(curr, 0.0) + amt_usd
    
    tot = sum(currency_balances_usd.values())
    allocation = {}
    for c_code, val in currency_balances_usd.items():
        allocation[c_code] = round((val / tot * 100), 1) if tot > 0 else 0

    # Monthly Personal Budget calculation
    budget_status = compute_budget_status(cur, rates)

    conn.close()
    return {
        "net_worth": net_worth_equivalents,
        "month_stats": month_stats,
        "accounts": accounts,
        "top_categories": top_categories,
        "recent_transactions": recent_transactions,
        "currency_allocation": allocation,
        "budget": budget_status,
        "rates": rates
    }

# --- Accounts Endpoints ---
@app.get("/api/accounts")
def list_accounts(auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM accounts WHERE is_archived = 0 ORDER BY id ASC")
    accounts = [dict(r) for r in cur.fetchall()]
    rates = get_user_rates()
    for acc in accounts:
        acc["equivalent_usd"] = convert_currency(acc["balance"], acc["currency"], "USD", rates)
        acc["equivalent_try"] = convert_currency(acc["balance"], acc["currency"], "TRY", rates)
        acc["equivalent_kzt"] = convert_currency(acc["balance"], acc["currency"], "KZT", rates)
    conn.close()
    return accounts

@app.post("/api/accounts")
def create_account(acc: AccountCreate, auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO accounts (name, account_type, currency, balance, icon, color)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (acc.name, acc.account_type or 'card', acc.currency.upper(), acc.balance, acc.icon, acc.color))
    acc_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": acc_id, "status": "ok"}

@app.put("/api/accounts/{account_id}")
def update_account(account_id: int, acc: AccountCreate, auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    UPDATE accounts SET name = ?, account_type = ?, currency = ?, balance = ?, icon = ?, color = ?
    WHERE id = ?
    """, (acc.name, acc.account_type or 'card', acc.currency.upper(), acc.balance, acc.icon, acc.color, account_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int, auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE accounts SET is_archived = 1 WHERE id = ?", (account_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# --- Categories Endpoints ---
@app.get("/api/categories")
def list_categories(type: Optional[str] = None, auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()
    if type:
        cur.execute("SELECT * FROM categories WHERE type = ? ORDER BY name ASC", (type,))
    else:
        cur.execute("SELECT * FROM categories ORDER BY type ASC, name ASC")
    categories = [dict(r) for r in cur.fetchall()]
    conn.close()
    return categories

@app.post("/api/categories")
def create_category(cat: CategoryCreate, auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO categories (name, type, icon, color)
    VALUES (?, ?, ?, ?)
    """, (cat.name, cat.type, cat.icon, cat.color))
    cat_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": cat_id, "status": "ok"}

@app.delete("/api/categories/{cat_id}")
def delete_category(cat_id: int, auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# --- Transactions Endpoints ---
@app.get("/api/transactions")
def list_transactions(
    type: Optional[str] = None,
    month: Optional[str] = None,
    category_id: Optional[int] = None,
    account_id: Optional[int] = None,
    currency: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    auth=Depends(check_auth)
):
    conn = get_connection()
    cur = conn.cursor()
    
    query = """
    SELECT t.*, c.name as category_name, c.icon as category_icon, c.color as category_color,
           a.name as account_name, a.account_type as account_type, a2.name as to_account_name
    FROM transactions t
    LEFT JOIN categories c ON t.category_id = c.id
    LEFT JOIN accounts a ON t.account_id = a.id
    LEFT JOIN accounts a2 ON t.to_account_id = a2.id
    WHERE 1=1
    """
    params = []

    if type:
        query += " AND t.type = ?"
        params.append(type)
    if month:
        query += " AND t.date LIKE ?"
        params.append(f"{month}%")
    if category_id:
        query += " AND t.category_id = ?"
        params.append(category_id)
    if account_id:
        query += " AND (t.account_id = ? OR t.to_account_id = ?)"
        params.extend([account_id, account_id])
    if currency:
        query += " AND t.currency = ?"
        params.append(currency.upper())
    if search:
        query += " AND (t.note LIKE ? OR c.name LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    query += " ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cur.execute(query, params)
    txs = [dict(r) for r in cur.fetchall()]
    conn.close()
    return txs

@app.post("/api/transactions")
def create_transaction(tx: TransactionCreate, auth=Depends(check_auth)):
    rates = get_user_rates()
    eq = calculate_equivalents(tx.amount, tx.currency, rates)

    conn = get_connection()
    cur = conn.cursor()

    time_str = tx.time or datetime.datetime.now().strftime("%H:%M")

    cur.execute("""
    INSERT INTO transactions (type, amount, currency, account_id, to_account_id, category_id, date, time, note, equivalent_usd, equivalent_try, equivalent_kzt)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        tx.type,
        tx.amount,
        tx.currency.upper(),
        tx.account_id,
        tx.to_account_id,
        tx.category_id,
        tx.date,
        time_str,
        tx.note or '',
        eq["USD"],
        eq["TRY"],
        eq["KZT"]
    ))
    tx_id = cur.lastrowid

    # Update account balances
    if tx.account_id:
        cur.execute("SELECT balance, currency FROM accounts WHERE id = ?", (tx.account_id,))
        acc_row = cur.fetchone()
        if acc_row:
            curr_acc_bal = acc_row["balance"]
            acc_currency = acc_row["currency"]

            tx_amount_in_acc_curr = convert_currency(tx.amount, tx.currency, acc_currency, rates)

            if tx.type == "expense" or tx.type == "investment":
                new_bal = curr_acc_bal - tx_amount_in_acc_curr
            elif tx.type == "income":
                new_bal = curr_acc_bal + tx_amount_in_acc_curr
            elif tx.type == "transfer":
                new_bal = curr_acc_bal - tx_amount_in_acc_curr
            else:
                new_bal = curr_acc_bal
            cur.execute("UPDATE accounts SET balance = ? WHERE id = ?", (new_bal, tx.account_id))

    if tx.type == "transfer" and tx.to_account_id:
        cur.execute("SELECT balance, currency FROM accounts WHERE id = ?", (tx.to_account_id,))
        acc_to_row = cur.fetchone()
        if acc_to_row:
            curr_to_bal = acc_to_row["balance"]
            to_curr = acc_to_row["currency"]
            tx_amount_in_to_curr = convert_currency(tx.amount, tx.currency, to_curr, rates)
            new_to_bal = curr_to_bal + tx_amount_in_to_curr
            cur.execute("UPDATE accounts SET balance = ? WHERE id = ?", (new_to_bal, tx.to_account_id))

    # If linked to a goal
    if tx.goal_id and (tx.type == "investment" or tx.type == "expense"):
        cur.execute("SELECT current_amount, currency FROM goals WHERE id = ?", (tx.goal_id,))
        g_row = cur.fetchone()
        if g_row:
            g_amt = g_row["current_amount"]
            g_curr = g_row["currency"]
            added = convert_currency(tx.amount, tx.currency, g_curr, rates)
            cur.execute("UPDATE goals SET current_amount = ? WHERE id = ?", (g_amt + added, tx.goal_id))

    conn.commit()
    conn.close()
    return {"id": tx_id, "status": "ok", "equivalents": eq}

@app.delete("/api/transactions/{tx_id}")
def delete_transaction(tx_id: int, auth=Depends(check_auth)):
    rates = get_user_rates()
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,))
    tx = cur.fetchone()
    if not tx:
        conn.close()
        raise HTTPException(status_code=404, detail="Операция не найдена")

    if tx["account_id"]:
        cur.execute("SELECT balance, currency FROM accounts WHERE id = ?", (tx["account_id"],))
        acc_row = cur.fetchone()
        if acc_row:
            curr_acc_bal = acc_row["balance"]
            acc_currency = acc_row["currency"]
            amt_in_acc = convert_currency(tx["amount"], tx["currency"], acc_currency, rates)

            if tx["type"] in ("expense", "investment", "transfer"):
                reverted_bal = curr_acc_bal + amt_in_acc
            elif tx["type"] == "income":
                reverted_bal = curr_acc_bal - amt_in_acc
            else:
                reverted_bal = curr_acc_bal
            cur.execute("UPDATE accounts SET balance = ? WHERE id = ?", (reverted_bal, tx["account_id"]))

    if tx["type"] == "transfer" and tx["to_account_id"]:
        cur.execute("SELECT balance, currency FROM accounts WHERE id = ?", (tx["to_account_id"],))
        acc_to_row = cur.fetchone()
        if acc_to_row:
            curr_to_bal = acc_to_row["balance"]
            to_curr = acc_to_row["currency"]
            amt_in_to = convert_currency(tx["amount"], tx["currency"], to_curr, rates)
            cur.execute("UPDATE accounts SET balance = ? WHERE id = ?", (curr_to_bal - amt_in_to, tx["to_account_id"]))

    cur.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# --- Goals / Investments Endpoints ---
@app.get("/api/goals")
def list_goals(auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM goals ORDER BY id ASC")
    goals = [dict(r) for r in cur.fetchall()]
    rates = get_user_rates()
    for g in goals:
        curr = g["currency"]
        g["current_usd"] = convert_currency(g["current_amount"], curr, "USD", rates)
        g["current_try"] = convert_currency(g["current_amount"], curr, "TRY", rates)
        g["current_kzt"] = convert_currency(g["current_amount"], curr, "KZT", rates)
        g["target_usd"] = convert_currency(g["target_amount"], curr, "USD", rates)
        g["target_try"] = convert_currency(g["target_amount"], curr, "TRY", rates)
        g["target_kzt"] = convert_currency(g["target_amount"], curr, "KZT", rates)
        g["progress_percent"] = min(round((g["current_amount"] / g["target_amount"] * 100), 1), 100.0) if g["target_amount"] > 0 else 0.0
    conn.close()
    return goals

@app.post("/api/goals")
def create_goal(goal: GoalCreate, auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO goals (title, target_amount, currency, current_amount, deadline, icon, color, note)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (goal.title, goal.target_amount, goal.currency.upper(), goal.current_amount, goal.deadline or '', goal.icon or 'target', goal.color or '#8b5cf6', goal.note or ''))
    gid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": gid, "status": "ok"}

@app.post("/api/goals/{goal_id}/contribute")
def contribute_goal(goal_id: int, body: GoalContribute, auth=Depends(check_auth)):
    rates = get_user_rates()
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM goals WHERE id = ?", (goal_id,))
    goal = cur.fetchone()
    if not goal:
        conn.close()
        raise HTTPException(status_code=404, detail="Цель не найдена")

    goal_curr = goal["currency"]
    amt = body.amount

    if body.account_id:
        cur.execute("SELECT balance, currency FROM accounts WHERE id = ?", (body.account_id,))
        acc = cur.fetchone()
        if acc:
            acc_amt = convert_currency(amt, goal_curr, acc["currency"], rates)
            cur.execute("UPDATE accounts SET balance = balance - ? WHERE id = ?", (acc_amt, body.account_id))

    cur.execute("UPDATE goals SET current_amount = current_amount + ? WHERE id = ?", (amt, goal_id))

    eq = calculate_equivalents(amt, goal_curr, rates)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.datetime.now().strftime("%H:%M")
    cur.execute("""
    INSERT INTO transactions (type, amount, currency, account_id, date, time, note, equivalent_usd, equivalent_try, equivalent_kzt)
    VALUES ('investment', ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        amt,
        goal_curr,
        body.account_id,
        today,
        now_time,
        f"Пополнение цели: {goal['title']}",
        eq["USD"],
        eq["TRY"],
        eq["KZT"]
    ))

    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/goals/{goal_id}")
def delete_goal(goal_id: int, auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# --- Analytics Endpoint ---
@app.get("/api/analytics")
def get_analytics(months: int = 6, auth=Depends(check_auth)):
    rates = get_user_rates()
    conn = get_connection()
    cur = conn.cursor()

    today = datetime.date.today()
    month_keys = []
    for i in range(months - 1, -1, -1):
        year = today.year
        month = today.month - i
        while month <= 0:
            month += 12
            year -= 1
        month_keys.append(f"{year:04d}-{month:02d}")

    history = {m: {"income": 0.0, "expense": 0.0, "investment": 0.0} for m in month_keys}

    cur.execute("""
    SELECT type, amount, currency, SUBSTR(date, 1, 7) as ym
    FROM transactions
    WHERE SUBSTR(date, 1, 7) IN ({})
    """.format(','.join('?' * len(month_keys))), month_keys)

    for row in cur.fetchall():
        ym = row["ym"]
        t_type = row["type"]
        amt_usd = convert_currency(row["amount"], row["currency"], "USD", rates)
        if ym in history and t_type in history[ym]:
            history[ym][t_type] += amt_usd

    cur.execute("""
    SELECT c.name, c.color, c.icon, t.amount, t.currency
    FROM transactions t
    JOIN categories c ON t.category_id = c.id
    WHERE t.type = 'expense'
    """)
    cat_rows = cur.fetchall()
    categories_all = {}
    for r in cat_rows:
        name = r["name"]
        amt_usd = convert_currency(r["amount"], r["currency"], "USD", rates)
        if name not in categories_all:
            categories_all[name] = {"name": name, "color": r["color"], "total_usd": 0.0}
        categories_all[name]["total_usd"] += amt_usd

    cat_list = sorted(categories_all.values(), key=lambda x: x["total_usd"], reverse=True)
    total_spent_usd = sum(x["total_usd"] for x in cat_list)
    for c in cat_list:
        c["total_usd"] = round(c["total_usd"], 2)
        c["total_try"] = round(c["total_usd"] * rates.get("TRY", 1.0), 2)
        c["total_kzt"] = round(c["total_usd"] * rates.get("KZT", 1.0), 2)
        c["percent"] = round((c["total_usd"] / total_spent_usd * 100), 1) if total_spent_usd > 0 else 0

    conn.close()
    return {
        "monthly_history": {
            m: {
                "income_usd": round(v["income"], 2),
                "income_try": round(v["income"] * rates.get("TRY", 1.0), 2),
                "income_kzt": round(v["income"] * rates.get("KZT", 1.0), 2),
                "expense_usd": round(v["expense"], 2),
                "expense_try": round(v["expense"] * rates.get("TRY", 1.0), 2),
                "expense_kzt": round(v["expense"] * rates.get("KZT", 1.0), 2),
                "investment_usd": round(v["investment"], 2),
                "investment_try": round(v["investment"] * rates.get("TRY", 1.0), 2),
                "investment_kzt": round(v["investment"] * rates.get("KZT", 1.0), 2)
            } for m, v in history.items()
        },
        "categories_breakdown": cat_list,
        "rates": rates
    }

# --- Backup & Restore ---
@app.get("/api/backup")
def export_backup(auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM accounts")
    accounts = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM categories")
    categories = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM transactions")
    transactions = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM goals")
    goals = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM settings")
    settings = [dict(r) for r in cur.fetchall()]

    conn.close()
    return {
        "version": "1.1",
        "exported_at": datetime.datetime.now().isoformat(),
        "accounts": accounts,
        "categories": categories,
        "transactions": transactions,
        "goals": goals,
        "settings": settings
    }

@app.post("/api/restore")
def import_backup(data: Dict[str, Any], auth=Depends(check_auth)):
    conn = get_connection()
    cur = conn.cursor()

    try:
        if "accounts" in data:
            cur.execute("DELETE FROM accounts")
            for a in data["accounts"]:
                cur.execute("""
                INSERT INTO accounts (id, name, account_type, currency, balance, icon, color, is_archived)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (a["id"], a["name"], a.get("account_type", "card"), a["currency"], a["balance"], a.get("icon", "wallet"), a.get("color", "#10b981"), a.get("is_archived", 0)))

        if "categories" in data:
            cur.execute("DELETE FROM categories")
            for c in data["categories"]:
                cur.execute("""
                INSERT INTO categories (id, name, type, icon, color)
                VALUES (?, ?, ?, ?, ?)
                """, (c["id"], c["name"], c["type"], c.get("icon", "tag"), c.get("color", "#64748b")))

        if "goals" in data:
            cur.execute("DELETE FROM goals")
            for g in data["goals"]:
                cur.execute("""
                INSERT INTO goals (id, title, target_amount, currency, current_amount, deadline, icon, color, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (g["id"], g["title"], g["target_amount"], g["currency"], g.get("current_amount", 0.0), g.get("deadline", ""), g.get("icon", "target"), g.get("color", "#8b5cf6"), g.get("note", "")))

        if "transactions" in data:
            cur.execute("DELETE FROM transactions")
            for t in data["transactions"]:
                cur.execute("""
                INSERT INTO transactions (id, type, amount, currency, account_id, to_account_id, category_id, date, time, note, equivalent_usd, equivalent_try, equivalent_kzt, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    t["id"], t["type"], t["amount"], t["currency"],
                    t.get("account_id"), t.get("to_account_id"), t.get("category_id"),
                    t["date"], t.get("time", ""), t.get("note", ""),
                    t.get("equivalent_usd", 0.0), t.get("equivalent_try", 0.0), t.get("equivalent_kzt", 0.0),
                    t.get("created_at")
                ))

        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=400, detail=f"Ошибка восстановления: {str(e)}")

    conn.close()
    return {"status": "ok", "message": "Данные успешно восстановлены"}
