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

# Mount static folder
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

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

# --- Root Endpoint: Serve PWA HTML ---
@app.get("/")
def get_index():
    if os.path.exists("static/index.html"):
        return FileResponse("static/index.html")
    return HTMLResponse("<h1>Загрузка приложения...</h1>")

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
