from fastapi.responses import JSONResponse
import os, asyncio, random, csv, json, time, base64
from pathlib import Path
from typing import Dict, Any
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Request, Body, Depends, HTTPException
from starlette import status as http_status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

# Paths & env
BASE = Path(__file__).parent
BOT_DIR = Path("/home/ubuntu/trading-coach-bot")
JOURNAL = BOT_DIR / "journal.csv"
CFG_DIR = BASE / "config"
CFG_PATH = CFG_DIR / "live_config.json"
METRICS = BASE / "metrics"
BAL_CSV = METRICS / "balances.csv"

load_dotenv(BASE / ".env")

CURRENT_MODE = os.getenv('TRADIER_MODE','paper').lower()

app = FastAPI(title="HoneyDrip Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
templates = Jinja2Templates(directory=str(BASE / "templates"))

# ---------- Basic Auth ----------
DASH_USER = os.getenv("DASH_USER","admin")
DASH_PASS = os.getenv("DASH_PASS","changeme")
HASHED=None

def basic_auth(request: Request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Basic "):
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate":"Basic"})
    try:
        userpass = base64.b64decode(auth.split(" ",1)[1]).decode()
        u, p = userpass.split(":",1)
    except Exception:
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate":"Basic"})
    if u != DASH_USER or not p == DASH_PASS:
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate":"Basic"})
    return True

# ---------- Config ----------
DEFAULT_CFG = {
    "max_risk_pct": 0.5,
    "safety_max_contracts": 3,
    "allow_runners": True,
    "enable_spreads": True,
    "coach_mode": True,
    "primary_symbols": ["SPY","QQQ","SPX"]
}
CFG_DIR.mkdir(exist_ok=True)
if not CFG_PATH.exists():
    CFG_PATH.write_text(json.dumps(DEFAULT_CFG, indent=2))

def read_cfg() -> Dict[str,Any]:
    try: return json.loads(CFG_PATH.read_text())
    except: return DEFAULT_CFG.copy()

def write_cfg(d: Dict[str,Any]):
    CFG_PATH.write_text(json.dumps(d, indent=2))

# ---------- Tradier helpers ----------

def tradier_cfg():
    mode = (globals().get("CURRENT_MODE") or os.getenv("TRADIER_MODE","paper")).lower()
    if mode == "live":
        base = "https://api.tradier.com/v1"
        token = os.getenv("TRADIER_LIVE_TOKEN","")
        account = os.getenv("TRADIER_LIVE_ACCOUNT_ID","")
    else:
        base = "https://sandbox.tradier.com/v1"
        token = os.getenv("TRADIER_PAPER_TOKEN","")
        account = os.getenv("TRADIER_PAPER_ACCOUNT_ID","")
    return {"mode": mode, "base": base, "token": token, "account": account}
    if mode == "live":
        return {
            "base": "https://api.tradier.com/v1",
            "token": os.getenv("TRADIER_LIVE_TOKEN","").strip(),
            "account": os.getenv("TRADIER_LIVE_ACCOUNT_ID","").strip(),
            "mode": mode
        }
    return {
        "base": "https://sandbox.tradier.com/v1",
        "token": os.getenv("TRADIER_PAPER_TOKEN","").strip(),
        "account": os.getenv("TRADIER_PAPER_ACCOUNT_ID","").strip(),
        "mode": mode
    }

async def tget(path:str, params:dict|None=None, timeout=5.0):
    cfg = tradier_cfg()
    if not cfg["token"]: raise RuntimeError("Tradier token missing")
    url = f"{cfg['base']}{path}"
    headers = {"Authorization": f"Bearer {cfg['token']}", "Accept":"application/json"}
    async with httpx.AsyncClient(timeout=timeout) as cli:
        r = await cli.get(url, headers=headers, params=params or {})
        if r.status_code >= 400: raise RuntimeError(f"Tradier {r.status_code}: {r.text}")
        return r.json()

async def tpost_form(path:str, form:dict, timeout=8.0):
    cfg = tradier_cfg()
    if not cfg["token"]: raise RuntimeError("Tradier token missing")
    url = f"{cfg['base']}{path}"
    headers = {"Authorization": f"Bearer {cfg['token']}",
               "Accept":"application/json", "Content-Type":"application/x-www-form-urlencoded"}
    async with httpx.AsyncClient(timeout=timeout) as cli:
        r = await cli.post(url, headers=headers, content=urlencode(form))
        if r.status_code >= 400: raise RuntimeError(f"Tradier {r.status_code}: {r.text}")
        return r.json()

async def ping_openai(timeout=3.0)->bool:
    key = os.getenv("OPENAI_API_KEY","").strip()
    if not key: return False
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.get("https://api.openai.com/v1/models",
                              headers={"Authorization":f"Bearer {key}"})
            return r.status_code < 400
    except: return False

async def ping_tradier(timeout=3.0)->bool:
    try:
        await tget("/markets/quotes", {"symbols":"SPY"})
        return True
    except: return False

async def ping_uw(timeout=3.0)->bool:
    url = os.getenv("UNUSUAL_WHALES_API_URL","").strip()
    tok = os.getenv("UNUSUAL_WHALES_API_TOKEN","").strip()
    if not url or not tok: return False
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cli:
            r = await cli.get(url, headers={"Authorization": f"Bearer {tok}"})
            if r.status_code in (401,403):
                r = await cli.get(url, headers={"x-api-key": tok})
            return r.status_code < 400
    except: return False

# ---------- OCC symbol ----------
def occ_symbol(underlying:str, expiry:str, right:str, strike:int|float) -> str:
    expiry = expiry.replace("-","")
    yymmdd = expiry[2:8]
    right = right.upper()[0]
    strike_thou = int(round(float(strike)*1000))
    return f"{underlying.upper()}{yymmdd}{right}{strike_thou:08d}"

# ---------- PAGES ----------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, _:bool=Depends(basic_auth)):
    return templates.TemplateResponse("index.html", {"request": request})

# Simple ping for uptime checks
@app.get("/api/ping")
async def ping(_:bool=Depends(basic_auth)):
    return {"ok": True, "ts": time.time()}

# ---------- HEALTH / STATUS ----------
@app.get("/status")
async def app_status(_:bool=Depends(basic_auth)):
    ok_openai, ok_tradier, ok_uw = await asyncio.gather(
        ping_openai(), ping_tradier(), ping_uw()
    )
    mode = tradier_cfg()["mode"]
    return {"openai": ok_openai, "tradier": ok_tradier, "uw": ok_uw, "mode": mode, "cfg": read_cfg()}

@app.post("/toggle-mode")
async def toggle_mode(_:bool=Depends(basic_auth)):
    mode = tradier_cfg()["mode"]
    new_mode = "live" if mode=="paper" else "paper"
    os.environ["TRADIER_MODE"] = new_mode
    return {"ok": True, "mode": new_mode}

# ---------- CONFIG ----------
@app.get("/api/config")
async def get_cfg(_:bool=Depends(basic_auth)): return read_cfg()

@app.post("/api/config")
async def set_cfg(body: Dict[str,Any] = Body(...), _:bool=Depends(basic_auth)):
    cfg = read_cfg()
    cfg.update({k:v for k,v in body.items() if k in read_cfg().keys()})
    (CFG_DIR).mkdir(exist_ok=True)
    (CFG_PATH).write_text(json.dumps(cfg, indent=2))
    return {"ok": True, "cfg": cfg}

# ---------- ACCOUNT ----------
@app.get("/api/account")
async def account(_:bool=Depends(basic_auth)):
    cfg = tradier_cfg()
    balances = {}
    try:
        balances = await tget(f"/accounts/{cfg['account']}/balances")
    except Exception as e:
        balances = {"error": str(e)}
    return {"balances": balances}


    # If Tradier (or tget) gave us a plain string, surface it as error.
    if isinstance(js, str):
        return {"positions": [], "error": js}

    # Coerce into list
    positions_node = (js or {}).get("positions")
    if not positions_node:            # None, {}, or missing -> no positions
        return {"positions": []}
    pos = positions_node.get("position", [])
    if pos is None:
        pos = []
    if isinstance(pos, dict):
        pos = [pos]
    if not isinstance(pos, list):
        pos = []

    # Optional: light normalization for UI safety (no key errors)
    norm = []
    for p in pos:
        if not isinstance(p, dict): 
            continue
        norm.append({
            "symbol": p.get("symbol") or p.get("underlying") or "",
            "quantity": p.get("quantity") or p.get("qty") or 0,
            "cost_basis": p.get("cost_basis") or p.get("cost_basis_price") or 0,
            "close_price": p.get("close_price") or p.get("last") or 0,
            "market_value": p.get("market_value") or 0,
            "id": p.get("id") or p.get("position_id") or "",
            "class": p.get("class") or "",
            "option_symbol": p.get("option_symbol") or "",
        })
    return {"positions": norm}
@app.get("/api/orders")
async def orders(_: bool = Depends(basic_auth)):
    cfg = tradier_cfg()
    try:
        js = await tget(f"/accounts/{cfg['account']}/orders")
    except Exception as e:
        return {"orders": [], "error": str(e)}
    if isinstance(js, str):
        return {"orders": [], "error": js}
    d = js if isinstance(js, dict) else {}
    od = (d.get("orders") or {}).get("order", [])
    if isinstance(od, dict):
        od = [od]
    return {"orders": od}
@app.websocket("/ws/quotes")
async def ws_quotes(ws: WebSocket):
    # no auth for WS (browser sends no header easily); soft-expose only quotes
    await ws.accept()
    symbols = read_cfg().get("primary_symbols", ["SPY","QQQ","SPX"])
    try:
        while True:
            payload = {}
            for sym in symbols:
                sym_q = "SPY" if sym=="SPX" else ("QQQ" if sym=="NDX" else sym)
                price = await fetch_quote(sym_q)
                payload[sym] = price
            await ws.send_json(payload)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return

# ---------- JOURNAL ----------
def parse_journal_line(line:str)->Dict[str,Any]:
    try:
        row = next(csv.reader([line]))
        return {
            "ts": float(row[0]),
            "ticker": row[1], "expiry": row[2], "strike": row[3], "right": row[4],
            "qty": int(row[5]), "limit": float(row[6]), "stop": float(row[7]),
            "target": float(row[8]), "note": row[9] if len(row)>9 else ""
        }
    except: return {"raw": line.strip()}

@app.get("/journal/last")
async def journal_last(n:int=50, _:bool=Depends(basic_auth)):
    if not JOURNAL.exists(): return {"rows":[]}
    lines = JOURNAL.read_text().splitlines()[-n:]
    rows = [parse_journal_line(ln) for ln in lines]
    rows.reverse()
    return {"rows": rows}

@app.websocket("/ws/journal")
async def ws_journal(ws: WebSocket):
    await ws.accept()
    pos = JOURNAL.stat().st_size if JOURNAL.exists() else 0
    try:
        while True:
            if JOURNAL.exists():
                sz = JOURNAL.stat().st_size
                if sz > pos:
                    with JOURNAL.open() as f:
                        f.seek(pos)
                        for ln in f:
                            await ws.send_json(parse_journal_line(ln))
                    pos = sz
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return

# ---------- ORDERS ----------
@app.post("/api/place/simple")
async def place_simple(body: Dict[str,Any] = Body(...), _:bool=Depends(basic_auth)):
    symbol = body.get("symbol","SPY").upper()
    expiry = body.get("expiry")
    right = body.get("right","C").upper()
    strike = body.get("strike")
    qty = int(body.get("qty",1))
    limit = float(body.get("limit",0))
    action = body.get("action","BTO").upper()
    if not expiry or strike is None:
        return JSONResponse({"error":"expiry and strike required"}, status_code=400)
    side = {"BTO":"buy_to_open","STC":"sell_to_close"}.get(action)
    if not side:
        return JSONResponse({"error":"action must be BTO or STC"}, status_code=400)
    occ = body.get("occ") or occ_symbol(symbol, expiry, right, strike)
    form = {"class":"option","option_symbol":occ,"side":side,"quantity":qty,"type":"limit","price":f"{limit:.2f}","duration":"day"}
    try:
        js = await tpost_form(f"/accounts/{tradier_cfg()['account']}/orders", form)
        return {"ok":True, "order":js}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)}, status_code=400)

@app.post("/api/place/vertical")
async def place_vertical(body: Dict[str,Any] = Body(...), _:bool=Depends(basic_auth)):
    symbol = body.get("symbol","SPY").upper()
    qty = int(body.get("qty",1))
    limit = float(body.get("limit",0))
    L = body.get("long",{}); S = body.get("short",{})
    if not all(k in L for k in ("expiry","right","strike")) or not all(k in S for k in ("expiry","right","strike")):
        return JSONResponse({"error":"long/short legs require expiry,right,strike"}, status_code=400)
    occL = occ_symbol(symbol, L["expiry"], L["right"], L["strike"])
    occS = occ_symbol(symbol, S["expiry"], S["right"], S["strike"])
    form = {
        "class":"multileg","symbol":symbol,"type":"limit","duration":"day","price":f"{limit:.2f}","quantity":qty,
        "option_symbol[0]":occL,"side[0]":"buy_to_open","quantity[0]":qty,
        "option_symbol[1]":occS,"side[1]":"sell_to_open","quantity[1]":qty
    }
    try:
        js = await tpost_form(f"/accounts/{tradier_cfg()['account']}/orders", form)
        return {"ok":True, "order":js}
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)}, status_code=400)

# ---------- PnL sampling ----------
async def sample_balances_forever():
    METRICS.mkdir(exist_ok=True)
    # header
    if not BAL_CSV.exists():
        BAL_CSV.write_text("ts,total_equity,cash,open_pl,close_pl\n")
    while True:
        try:
            b = await tget(f"/accounts/{tradier_cfg()['account']}/balances")
            bb = b.get("balances", {})
            row = [
                str(time.time()),
                str(bb.get("total_equity","")),
                str(bb.get("cash","")),
                str(bb.get("open_pl","")),
                str(bb.get("close_pl","")),
            ]
            with BAL_CSV.open("a") as f:
                f.write(",".join(row) + "\n")
        except Exception:
            pass
        await asyncio.sleep(60)  # every minute

@app.get("/api/pnl")
async def pnl(_:bool=Depends(basic_auth)):
    if not BAL_CSV.exists(): return {"rows":[]}
    rows=[]
    with BAL_CSV.open() as f:
        next(f, None)
        for ln in f:
            ts, eq, cash, opl, cpl = (ln.strip().split(",") + ["","","",""])[:5]
            try:
                rows.append({"ts": float(ts), "equity": float(eq or 0), "cash": float(cash or 0)})
            except: pass
    return {"rows": rows[-720:]}  # last ~12 hours

# launch sampler
@app.on_event("startup")
async def _startup():
    asyncio.create_task(sample_balances_forever())


@app.post("/api/orders/cancel/{order_id}")
async def cancel_order(order_id: str, _:bool=Depends(basic_auth)):
    cfg = tradier_cfg()
    try:
        js = await tpost_form(f"/accounts/{cfg['account']}/orders/{order_id}/cancel", {})
        return {"ok": True, "result": js}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/options/expirations")
async def options_expirations(symbol: str, _:bool=Depends(basic_auth)):
    try:
        js = await tget("/markets/options/expirations", {"symbol": symbol})
        exps = js.get("expirations", {}).get("date", [])
        if isinstance(exps, str): exps = [exps]
        return {"symbol": symbol, "expirations": exps}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.get("/api/options/chains")
async def options_chains(symbol: str, expiration: str, _:bool=Depends(basic_auth)):
    try:
        js = await tget("/markets/options/chains", {"symbol": symbol, "expiration": expiration})
        contracts = js.get("options", {}).get("option", [])
        if isinstance(contracts, dict): contracts = [contracts]
        out = []
        for c in contracts:
            out.append({
                "symbol": c.get("symbol"),
                "strike": c.get("strike"),
                "option_type": c.get("option_type"),
                "bid": c.get("bid"),
                "ask": c.get("ask"),
                "last": c.get("last"),
                "volume": c.get("volume"),
                "open_interest": c.get("open_interest")
            })
        return {"symbol": symbol, "expiration": expiration, "contracts": out}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.get("/api/positions/raw")
async def positions_raw(_: bool = Depends(basic_auth)):
    cfg = tradier_cfg()
    try:
        js = await tget(f"/accounts/{cfg['account']}/positions")
    except Exception as e:
        return {"error": str(e)}
    return js


from typing import Optional
GPT_TOKEN = os.getenv("GPT_ACTIONS_TOKEN","")

def gpt_token_auth(request: Request) -> bool:
    tok = request.headers.get("x-api-key") or request.headers.get("X-API-Key")
    if not GPT_TOKEN or tok != GPT_TOKEN:
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Bad API key")
    return True


async def _safe_tradier(path: str, params: Optional[dict]=None):
    try:
        js = await tget(path, params or {})
        return js if isinstance(js, dict) else {"raw": js}
    except Exception as e:
        return {"error": str(e)}


@app.get("/gpt/context")
async def gpt_context(_:bool = Depends(gpt_token_auth)):
    cfg = tradier_cfg()
    # balances / positions / orders
    bal = await _safe_tradier(f"/accounts/{cfg['account']}/balances")
    pos = await _safe_tradier(f"/accounts/{cfg['account']}/positions")
    ords = await _safe_tradier(f"/accounts/{cfg['account']}/orders")
    prim = CPRIMARY if 'CPRIMARY' in globals() else ['SPY','QQQ','SPX']
    q = await _safe_tradier("/markets/quotes", {"symbols": ",".join(prim)})
    return {
        "mode": cfg["mode"],
        "symbols": prim,
        "balances": bal,
        "positions": pos,
        "orders": ords,
        "quotes": q
    }

@app.get("/gpt/quotes")
async def gpt_quotes(symbols: str, _:bool = Depends(gpt_token_auth)):
    return await _safe_tradier("/markets/quotes", {"symbols": symbols})

@app.get("/gpt/options/expirations")
async def gpt_exps(symbol: str, _:bool = Depends(gpt_token_auth)):
    return await _safe_tradier("/markets/options/expirations", {"symbol": symbol})

@app.get("/gpt/options/chains")
async def gpt_chains(symbol: str, expiration: str, _:bool = Depends(gpt_token_auth)):
    return await _safe_tradier("/markets/options/chains", {"symbol": symbol, "expiration": expiration})

@app.get("/gpt/status")
async def gpt_status(_:bool = Depends(gpt_token_auth)):
    return await status()


@app.get("/gpt/openapi.json")
async def gpt_openapi(_:bool = Depends(gpt_token_auth)):
    return {
      "openapi":"3.1.0",
      "info":{"title":"HoneyDrip GPT Actions API","version":"1.0.0"},
      "paths":{
        "/gpt/context":{"get":{"summary":"Context snapshot","security":[{"apiKeyAuth":[]}]}},
        "/gpt/quotes":{"get":{"summary":"Quotes","security":[{"apiKeyAuth":[]}],"parameters":[{"name":"symbols","in":"query","required":True,"schema":{"type":"string"}}]}},
        "/gpt/options/expirations":{"get":{"summary":"Option expirations","security":[{"apiKeyAuth":[]}],"parameters":[{"name":"symbol","in":"query","required":True,"schema":{"type":"string"}}]}},
        "/gpt/options/chains":{"get":{"summary":"Option chains","security":[{"apiKeyAuth":[]}],"parameters":[
          {"name":"symbol","in":"query","required":True,"schema":{"type":"string"}},
          {"name":"expiration","in":"query","required":True,"schema":{"type":"string"}}
        ]}},
        "/gpt/status":{"get":{"summary":"Service status","security":[{"apiKeyAuth":[]}]} }
      },
      "components":{"securitySchemes":{"apiKeyAuth":{"type":"apiKey","in":"header","name":"X-API-Key"}}}
    }


from pathlib import Path as _Path
EVT = _Path(__file__).resolve().parent / "logs" / "events.jsonl"

def _read_events(limit=5000):
    if not EVT.exists(): return []
    out=[]
    with EVT.open() as f:
        for ln in f:
            ln=ln.strip()
            if not ln: continue
            try: out.append(json.loads(ln))
            except: pass
    if limit and len(out)>limit: out=out[-limit:]
    return out


@app.get("/api/analytics/summary")
async def analytics_summary(_: bool = Depends(basic_auth)):
    ev = _read_events()
    # PnL samples
    pnl = [e for e in ev if e.get("kind")=="pnl_sample" and isinstance(e.get("total_equity"), (int,float))]
    pnl_series = [{"ts":e.get("ts"), "equity": e.get("total_equity")} for e in pnl][-200:]
    # Orders + outcomes (expects later wiring for realized_*. For now basic counts.)
    orders = [e for e in ev if e.get("kind")=="order_submitted"]
    wins = [e for e in ev if e.get("kind")=="trade_close" and e.get("realized_r",0)>0]
    losses = [e for e in ev if e.get("kind")=="trade_close" and e.get("realized_r",0)<=0]
    closes = wins + losses
    win_rate = (len(wins)/len(closes)*100.0) if closes else None
    expectancy = None
    if closes:
        rvals = [e.get("realized_r",0) for e in closes]
        try: expectancy = sum(rvals)/len(rvals)
        except: expectancy = None
    # Latency / health (quotes errors in last 5 min)
    now=time.time()
    recent_errors = [e for e in ev if e.get("kind")=="quotes_error" and now - e.get("ts",now) < 300]
    return {
        "counts": {"orders": len(orders), "trades": len(closes), "wins": len(wins), "losses": len(losses)},
        "win_rate_pct": win_rate,
        "expectancy_r": expectancy,
        "equity_series": pnl_series,
        "recent_quote_errors": len(recent_errors)
    }


import os, subprocess

def _set_env_line(key: str, val: str):
    """
    Update or append KEY=VAL in .env safely.
    """
    path = os.path.join(os.path.dirname(__file__), ".env")
    lines=[]
    if os.path.exists(path):
        with open(path,'r') as f: lines=f.read().splitlines()
    found=False
    out=[]
    for ln in lines:
        if ln.startswith(key+"="):
            out.append(f"{key}={val}"); found=True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={val}")
    with open(path,'w') as f: f.write("\n".join(out)+"\n")

def _try_restart():
    cmds = [
        ["sudo","systemctl","restart","honeydrip-dashboard"],
        ["sudo","systemctl","restart","honeydrip-bot"],
    ]
    ok=True
    for c in cmds:
        try:
            subprocess.run(c, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception:
            ok=False
    return ok


@app.get("/api/mode")
async def get_mode(_: bool = Depends(basic_auth)):
    cfg = tradier_cfg()
    syms = (os.getenv("PRIMARY_SYMBOLS","SPY,QQQ,SPX").split(","))
    return {"mode": cfg["mode"], "symbols":[s.strip() for s in syms if s.strip()]}

@app.post("/api/mode")
async def set_mode(body: dict = Body(...), _: bool = Depends(basic_auth)):
    want = (body or {}).get("mode","").lower()
    if want not in ("paper","live"):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="mode must be paper|live")
    if want=="live":
        if (body or {}).get("confirm") != "LIVE":
            raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="missing confirm LIVE")
    _set_env_line("TRADIER_MODE", want)
    # Safety: do NOT auto-enable orders here; AUTO_EXECUTE remains as-is.
    restarted = _try_restart()
    return {"ok": True, "mode": want, "restarted": restarted}


@app.post("/api/manual/order")
async def manual_order(body: dict = Body(...), _: bool = Depends(basic_auth)):
    try:
        sym  = body.get("underlying"); opt = body.get("option_symbol"); side = body.get("side")
        qty  = int(body.get("quantity",1)); typ = body.get("type","limit"); pr  = body.get("price")
        if not sym or not opt or not side: 
            return {"ok":False, "error":"missing fields"}
        # Reuse tradier client from existing tpost() helper if present; else call our broker
        cfg = tradier_cfg()
        data = {
            "class":"option","symbol":sym,"option_symbol":opt,
            "side":side,"quantity":qty,"type":typ,"duration":"day"
        }
        if typ=="limit" and pr not in (None,""): data["price"]=str(pr)
        js = await tpost(f"/accounts/{cfg['account']}/orders", data=data)
        return {"ok":True,"resp":js}
    except Exception as e:
        return {"ok":False,"error":str(e)}


@app.get("/api/quotes")
async def api_quotes(symbols: str, _: bool = Depends(basic_auth)):
    syms = ",".join([s.strip().upper() for s in symbols.split(",") if s.strip()])
    return await tget('/markets/quotes', {'symbols': syms})

async def tpost(path: str, data: dict=None):
    cfg = tradier_cfg()
    if not cfg["token"]: return {"error":"no_token_for_mode_"+cfg["mode"]}
    async with httpx.AsyncClient(timeout=10) as a:
        r = await a.post(cfg["base"]+path, headers={"Authorization":f"Bearer {cfg['token']}","Accept":"application/json","Content-Type":"application/x-www-form-urlencoded"}, data=data or {})
        try: return r.json()
        except Exception: return {"error": f"http_{r.status_code}", "text": r.text[:200]}

from fastapi import Query

@app.post("/api/orders/cancel")
async def api_cancel_order(id: str = Query(...), _: bool = Depends(basic_auth)):
    cfg = tradier_cfg()
    js = await tpost(f"/accounts/{cfg['account']}/orders/cancel", data={"id": id})
    ok = ('error' not in js)
    return {"ok": ok, "resp": js}



@app.get("/api/uw/flow")
async def uw_flow(limit: int = 10, symbol: str = "SPY", _: bool = Depends(basic_auth)):
    # If a full URL is provided in env, use it; else build a sensible default for flow alerts
    base = os.getenv("UNUSUAL_WHALES_API_URL","").strip()
    token = os.getenv("UNUSUAL_WHALES_API_TOKEN","").strip()
    if not token:
        return {"error":"uw_missing_token"}

    if base and base.startswith("http"):
        url = base
        # allow overriding symbol/limit if the base is generic
        if "ticker_symbol" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}ticker_symbol={symbol}"
        if "limit=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}limit={limit}"
    else:
        # fallback guess (adjust later if their docs differ in your plan)
        url = f"https://api.unusualwhales.com/api/option-trades/flow-alerts?ticker_symbol={symbol}&limit={limit}"

    js = await uw_get(url)
    # normalize a shallow table
    rows=[]
    # Try common shapes
    payload = None
    if isinstance(js, dict):
        for k in ("data","results","alerts","items"):
            if isinstance(js.get(k), list):
                payload = js.get(k); break
        if payload is None and isinstance(js.get("data"), dict):
            # sometimes nested under data->items
            if isinstance(js["data"].get("items"), list):
                payload = js["data"]["items"]

    if isinstance(payload, list):
        for r in payload[:limit]:
            if not isinstance(r, dict): continue
            rows.append({
                "ts": r.get("timestamp") or r.get("time") or r.get("created_at"),
                "ticker": r.get("ticker") or r.get("symbol") or symbol,
                "side": r.get("side") or r.get("type"),
                "exp": r.get("expiration") or r.get("expiry"),
                "strike": r.get("strike"),
                "notional": r.get("notional") or r.get("amount") or r.get("dollar_value"),
                "ask_hit": r.get("askImpact") or r.get("at_ask") or r.get("above_ask"),
                "raw": r
            })
    return {"symbol": symbol, "rows": rows, "raw": js if js and isinstance(js, dict) and js.get("error") else None}


@app.get("/api/options/chain")
async def options_chain(symbol: str, expiration: str, _: bool = Depends(basic_auth)):
    """
    Robustly fetch and normalize Tradier option chain.
    Surfaces upstream errors as 502 instead of 500.
    """
    js = await tget(f"/markets/options/chains?symbol={symbol}&expiration={expiration}&greeks=false")

    # If upstream isn't a dict, return it so UI can show real message
    if not isinstance(js, dict):
        return JSONResponse(status_code=502, content={"error":"bad_upstream", "note":"non-JSON response from Tradier", "raw": str(js)[:800]})

    # Explicit error shapes from Tradier
    if js.get("fault") or js.get("errors") or js.get("error"):
        return JSONResponse(status_code=502, content={"error":"tradier_error", "raw": js})

    node = js.get("options")
    if not isinstance(node, dict):
        node = {}
    chain = node.get("option", [])
    if isinstance(chain, dict):
        chain = [chain]
    if not isinstance(chain, list):
        chain = []

    rows = []
    for o in chain:
        if not isinstance(o, dict):
            continue
        rows.append({
            "symbol": o.get("symbol"),
            "root_symbol": o.get("root_symbol"),
            "underlying": o.get("underlying"),
            "strike": o.get("strike"),
            "option_type": o.get("option_type"),
            "bid": o.get("bid"),
            "ask": o.get("ask"),
            "last": o.get("last"),
            "volume": o.get("volume"),
            "open_interest": o.get("open_interest"),
            "expiration_date": o.get("expiration_date"),
            "trade_date": o.get("trade_date"),
        })

    return {"symbol": symbol, "expiration": expiration, "contracts": rows, "raw": None if rows else js}


@app.get("/api/positions")
async def api_positions(_: bool = Depends(basic_auth)):
    """
    Normalize Tradier positions so UI never 500s.
    """
    cfg = tradier_cfg()
    js = await tget(f"/accounts/{cfg['account']}/positions")

    # If upstream isn't a dict, expose it
    if not isinstance(js, dict):
        return JSONResponse(status_code=502, content={"error":"bad_upstream", "raw": str(js)[:800]})

    # Tradier error shapes
    if js.get("fault") or js.get("errors") or js.get("error"):
        return JSONResponse(status_code=502, content={"error":"tradier_error", "raw": js})

    positions_node = js.get("positions")
    # Some edge cases return a string or null; guard it
    if not isinstance(positions_node, dict):
        positions_node = {}

    pos = positions_node.get("position", [])
    if isinstance(pos, dict):
        pos = [pos]
    if not isinstance(pos, list):
        pos = []

    out = []
    for p in pos:
        if not isinstance(p, dict):
            continue
        ins = p.get("instrument", {})
        if not isinstance(ins, dict): ins = {}
        out.append({
            "symbol": ins.get("symbol"),
            "asset_type": ins.get("asset_type"),
            "quantity": p.get("quantity"),
            "cost_basis": p.get("cost_basis"),
            "date_acquired": p.get("date_acquired"),
            "close_price": p.get("close_price"),
            "market_value": p.get("market_value"),
            "total_gain": p.get("total_gain"),
            "today_gain": p.get("today_gain"),
        })
    return {"positions": out, "raw": None if out else js}


# ===== HDN: robust chain endpoint (chain2) =====
from fastapi.responses import JSONResponse  # ok if duplicated

@app.get("/api/options/chain2")
async def options_chain2(symbol: str, expiration: str, _: bool = Depends(basic_auth)):
    """
    Safe option-chain fetcher. Never raises; returns 502 with upstream payload on error.
    """
    js = await tget(f"/markets/options/chains", {"symbol": symbol, "expiration": expiration, "greeks": "false"})

    # Non-dict upstream: surface it verbatim
    if not isinstance(js, dict):
        return JSONResponse(status_code=502, content={"error":"bad_upstream", "raw": str(js)[:1000]})

    # Explicit Tradier error shapes
    if js.get("fault") or js.get("errors") or js.get("error"):
        return JSONResponse(status_code=502, content={"error":"tradier_error", "raw": js})

    node = js.get("options")
    if not isinstance(node, dict):
        node = {}
    chain = node.get("option", [])
    if isinstance(chain, dict):
        chain = [chain]
    if not isinstance(chain, list):
        chain = []

    rows = []
    for o in chain:
        if not isinstance(o, dict): 
            continue
        rows.append({
            "symbol": o.get("symbol"),
            "root_symbol": o.get("root_symbol"),
            "underlying": o.get("underlying"),
            "strike": o.get("strike"),
            "option_type": o.get("option_type"),
            "bid": o.get("bid"),
            "ask": o.get("ask"),
            "last": o.get("last"),
            "volume": o.get("volume"),
            "open_interest": o.get("open_interest"),
            "expiration_date": o.get("expiration_date"),
            "trade_date": o.get("trade_date"),
        })

    return {"symbol": symbol, "expiration": expiration, "contracts": rows, "raw": None if rows else js}
# ===== end chain2 =====
