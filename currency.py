import time
import requests
import json
import logging

logger = logging.getLogger(__name__)

# Fallback default rates if API is unavailable
FALLBACK_RATES = {
    "USD": 1.0,
    "TRY": 34.50,
    "KZT": 485.00
}

CACHE_FILE = "rates_cache.json"
CACHE_DURATION_SEC = 3600  # 1 hour

def fetch_live_rates():
    """Fetch live rates with USD base from open.er-api.com"""
    try:
        url = "https://open.er-api.com/v6/latest/USD"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            rates = data.get("rates", {})
            result = {
                "USD": 1.0,
                "TRY": float(rates.get("TRY", FALLBACK_RATES["TRY"])),
                "KZT": float(rates.get("KZT", FALLBACK_RATES["KZT"])),
                "last_updated": int(time.time()),
                "source": "live"
            }
            # Save to cache
            try:
                with open(CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(result, f)
            except Exception as e:
                logger.warning(f"Failed to cache rates: {e}")
            return result
    except Exception as e:
        logger.warning(f"Error fetching live rates: {e}")
    
    # Try reading from cache
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
            cached["source"] = "cache"
            return cached
    except Exception:
        pass

    return {
        **FALLBACK_RATES,
        "last_updated": int(time.time()),
        "source": "fallback"
    }

def get_current_rates(custom_rates=None):
    """
    Returns effective rates. If custom_rates are provided and enabled, they override.
    Format of rates: {'USD': 1.0, 'TRY': 34.5, 'KZT': 485.0} relative to USD.
    """
    # Check cache freshness first
    rates = None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
            if time.time() - cached.get("last_updated", 0) < CACHE_DURATION_SEC:
                rates = cached
    except Exception:
        pass

    if not rates:
        rates = fetch_live_rates()

    # Apply custom overrides if user specified them
    if custom_rates and isinstance(custom_rates, dict):
        if custom_rates.get("TRY"):
            try:
                rates["TRY"] = float(custom_rates["TRY"])
                rates["custom_TRY"] = True
            except (ValueError, TypeError):
                pass
        if custom_rates.get("KZT"):
            try:
                rates["KZT"] = float(custom_rates["KZT"])
                rates["custom_KZT"] = True
            except (ValueError, TypeError):
                pass

    return rates

def convert_currency(amount: float, from_curr: str, to_curr: str, rates: dict) -> float:
    """Converts an amount from one currency to another using USD-based rates."""
    from_curr = from_curr.upper()
    to_curr = to_curr.upper()
    if from_curr == to_curr:
        return round(amount, 2)
    
    rate_from = rates.get(from_curr, 1.0)
    rate_to = rates.get(to_curr, 1.0)

    # Convert to USD first
    amount_usd = amount / rate_from if rate_from else amount
    # Convert to target
    amount_target = amount_usd * rate_to
    return round(amount_target, 2)

def calculate_equivalents(amount: float, currency: str, rates: dict) -> dict:
    """Returns equivalent amounts in USD, TRY, and KZT."""
    curr = currency.upper()
    return {
        "USD": convert_currency(amount, curr, "USD", rates),
        "TRY": convert_currency(amount, curr, "TRY", rates),
        "KZT": convert_currency(amount, curr, "KZT", rates)
    }
