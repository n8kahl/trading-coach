import os, json, asyncio, time, math
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from broker_tradier import TradierBroker, TradierCreds
from gpt_throttle import can_call, mark_called, budget_ok

APP = Path(__file__).resolve().parent
LOGS = APP/"logs"; LOGS.mkdir(exist_ok=True)
EVENTS = LOGS/"events.jsonl"
CFG_FILE = APP/"config"/"live_config.json"

def log_event(kind:str, **kw):
    kw.update(ts=time.time(), kind=kind)
    with EVENTS.open("a") as f:
        f.write(json.dumps(kw, separators=(",",":"))+"\n")

def now_eastern_str():
    return datetime.now(timezone.utc).astimezone().isoformat()

async def mid_price(bid, ask):
    try:
        b = float(bid) if bid is not None else None
        a = float(ask) if ask is not None else None
        if b is None or a is None: return None
        return round((b+a)/2.0, 2)
    except: return None

async def choose_atm_option(broker: TradierBroker, symbol: str):
    # Pick first available expiration, then choose strike near underlying last
    qs = await broker.quotes([symbol])
    q = (qs.get("quotes") or {}).get("quote", {})
    last = q.get("last") or q.get("last_trade")
    if isinstance(last, dict): last = last.get("price")
    if not last: return None
    last = float(last)
    exps = await broker.expirations(symbol)
    if not exps: return None
    exp = exps[0]
    chain = await broker.chains(symbol, exp)
    if not chain: return None
    # Find closest strike call
    target = min(chain, key=lambda c: abs(float(c.get("strike",0))-last))
    return {"underlying":symbol, "expiration":exp, "strike":target.get("strike"),
            "type":target.get("option_type"), "option_symbol":target.get("symbol"),
            "bid":target.get("bid"), "ask":target.get("ask")}

async def paper_demo_trade(broker: TradierBroker, symbol: str, risk_pct: float, max_contracts: int):
    """Places 1 ATM call/put with limit near mid; only if AUTO_EXECUTE=true."""
    opt = await choose_atm_option(broker, symbol)
    if not opt: 
        log_event("demo_skip", reason="no_option", symbol=symbol)
        return
    m = await mid_price(opt["bid"], opt["ask"]) or opt["ask"] or opt["bid"]
    if not m:
        log_event("demo_skip", reason="no_mid", details=opt)
        return
    qty = max(1, min( max_contracts, 1 ))
    js = await broker.place_option_single(
        underlying=symbol,
        option_symbol=opt["option_symbol"],
        side="buy_to_open",
        quantity=qty,
        order_type="limit",
        price=m
    )
    log_event("order_submitted", symbol=symbol, option=opt, price=m, qty=qty, broker_resp=js)

async def balances_snapshot(broker: TradierBroker):
    try:
        b = await broker.balances()
        acc = (b.get("balances") or {})
        return {
            "total_equity": acc.get("total_equity") or acc.get("equity"),
            "cash": acc.get("cash") or acc.get("cash_available"),
            "close_pl": acc.get("close_pl"),
            "open_pl": acc.get("open_pl"),
        }
    except Exception as e:
        return {"error": str(e)}

async def engine_loop():
    load_dotenv()
    creds = TradierCreds.from_env()
    auto_exec = os.getenv("AUTO_EXECUTE","false").lower()=="true"
    primary = ["SPY","QQQ","SPX"]  # dashboard also shows these
    # Config file (risk knobs)
    try:
        cfg = json.loads(CFG_FILE.read_text())
    except Exception:
        cfg = {"max_risk_pct":0.5,"safety_max_contracts":3,"allow_runners":True,"enable_spreads":True,"coach_mode":True}

    risk_pct = float(cfg.get("max_risk_pct",0.5))/100.0
    max_contracts = int(cfg.get("safety_max_contracts",3))

    broker = TradierBroker(creds)
    log_event("engine_start", mode=creds.mode, auto_exec=auto_exec, cfg=cfg)

    try:
        t0 = time.time()
        while True:
            # 1) sample balances
            bal = await balances_snapshot(broker)
            log_event("pnl_sample", **bal)

            # 2) quotes for primaries
            try:
                qs = await broker.quotes(primary)
                log_event("quotes", quotes=qs)
            except Exception as e:
                log_event("quotes_error", error=str(e))

            # 3) demo: place 1 small order on first symbol if AUTO_EXECUTE
            if auto_exec:
                try:
                    await paper_demo_trade(broker, primary[0], risk_pct, max_contracts)
                except Exception as e:
                    log_event("order_error", error=str(e))

            await asyncio.sleep(15)
    finally:
        await broker.close()

if __name__ == "__main__":
    asyncio.run(engine_loop())
