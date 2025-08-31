import os, asyncio, httpx, time
from dataclasses import dataclass
from typing import Dict, Any, List, Optional

API_LIVE  = "https://api.tradier.com/v1"
API_PAPER = "https://sandbox.tradier.com/v1"

@dataclass
class TradierCreds:
    mode: str
    token: str
    account_id: str
    base: str

    @classmethod
    def from_env(cls) -> "TradierCreds":
        mode = os.getenv("TRADIER_MODE","paper").lower()
        if mode not in ("paper","live"): mode = "paper"
        if mode == "paper":
            token = os.getenv("TRADIER_PAPER_TOKEN","")
            acct  = os.getenv("TRADIER_PAPER_ACCOUNT_ID","")
            base  = API_PAPER
        else:
            token = os.getenv("TRADIER_LIVE_TOKEN","")
            acct  = os.getenv("TRADIER_LIVE_ACCOUNT_ID","")
            base  = API_LIVE
        return cls(mode, token, acct, base)

class TradierBroker:
    def __init__(self, creds: TradierCreds, timeout=10.0):
        self.creds = creds
        self.client = httpx.AsyncClient(
            base_url=creds.base, timeout=timeout,
            headers={
                "Authorization": f"Bearer {creds.token}",
                "Accept": "application/json",
            },
        )

    async def close(self):
        await self.client.aclose()

    # ---- Market data ----
    async def quotes(self, symbols: List[str]) -> Dict[str, Any]:
        r = await self.client.get("/markets/quotes", params={"symbols": ",".join(symbols)})
        return r.json()

    async def expirations(self, symbol: str) -> List[str]:
        js = (await self.client.get("/markets/options/expirations", params={"symbol":symbol})).json()
        exps = js.get("expirations",{}).get("date",[])
        if isinstance(exps, str): exps=[exps]
        return exps

    async def chains(self, symbol: str, expiration: str) -> List[Dict[str,Any]]:
        js = (await self.client.get("/markets/options/chains", params={"symbol":symbol,"expiration":expiration})).json()
        rows = js.get("options",{}).get("option",[])
        if isinstance(rows, dict): rows=[rows]
        return rows

    # ---- Account ----
    async def balances(self) -> Dict[str,Any]:
        js = (await self.client.get(f"/accounts/{self.creds.account_id}/balances")).json()
        return js

    async def positions(self) -> List[Dict[str,Any]]:
        js = (await self.client.get(f"/accounts/{self.creds.account_id}/positions")).json()
        if isinstance(js, str): return []
        p = (js.get("positions") or {}).get("position", [])
        if isinstance(p, dict): p=[p]
        return p or []

    async def orders(self) -> List[Dict[str,Any]]:
        js = (await self.client.get(f"/accounts/{self.creds.account_id}/orders")).json()
        if isinstance(js, str): return []
        o = (js.get("orders") or {}).get("order", [])
        if isinstance(o, dict): o=[o]
        return o or []

    async def cancel(self, order_id: str) -> Dict[str,Any]:
        js = (await self.client.post(f"/accounts/{self.creds.account_id}/orders/{order_id}/cancel")).json()
        return js

    # ---- Place single-leg option order ----
    async def place_option_single(self,
        underlying: str,
        option_symbol: str,
        side: str,               # 'buy_to_open' | 'sell_to_close'
        quantity: int,
        order_type: str,         # 'market' | 'limit'
        price: Optional[float]=None,
        duration: str="day",
    ) -> Dict[str,Any]:
        data = {
            "class": "option",
            "symbol": underlying,
            "option_symbol": option_symbol,
            "side": side,
            "quantity": quantity,
            "type": order_type,
            "duration": duration,
        }
        if order_type == "limit" and price is not None:
            data["price"] = f"{price:.2f}"
        js = (await self.client.post(f"/accounts/{self.creds.account_id}/orders", data=data)).json()
        return js
