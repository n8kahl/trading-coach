import time
_last = {}
spent_today_usd = 0.0
DAILY_BUDGET_USD = 3.0

def can_call(symbol: str, cooldown_sec: int = 180) -> bool:
    now = time.time()
    t = _last.get(symbol, 0)
    return (now - t) >= cooldown_sec

def mark_called(symbol: str, est_cost_usd: float = 0.02):
    global spent_today_usd
    _last[symbol] = time.time()
    spent_today_usd += est_cost_usd

def budget_ok() -> bool:
    return spent_today_usd < DAILY_BUDGET_USD
