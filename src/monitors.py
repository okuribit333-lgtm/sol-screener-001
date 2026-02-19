"""
リアルタイム監視 v4 — ウォレット / 流動性 / SOL価格レンジ

全て「通知のみ」。自動売買はしない。
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from .config import config

logger = logging.getLogger(__name__)


class WalletMonitor:
    """ウォレットの動きを監視（Copy Trading 参考用）"""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.wallets = self._load_wallets()
        self.rpc_url = self._get_rpc_url()
        self.last_signatures: dict[str, str] = {}

    def _load_wallets(self) -> dict[str, str]:
        raw = config.watch_wallets
        wallets = {}
        if raw:
            for entry in raw.split(","):
                entry = entry.strip()
                if ":" in entry:
                    addr, label = entry.split(":", 1)
                    wallets[addr.strip()] = label.strip()
                elif entry:
                    wallets[entry] = "Unknown"
        return wallets

    def _get_rpc_url(self) -> str:
        helius_key = getattr(config, "helius_api_key", "")
        if helius_key:
            return f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
        return "https://api.mainnet-beta.solana.com"

    async def check_all(self) -> list[dict]:
        """全監視ウォレットの新規トランザクションを確認"""
        alerts = []
        for addr, label in self.wallets.items():
            try:
                new_txs = await self._check_wallet(addr, label)
                alerts.extend(new_txs)
            except Exception as e:
                logger.debug(f"Wallet monitor error {label}: {e}")
            await asyncio.sleep(0.3)
        return alerts

    async def _check_wallet(self, address: str, label: str) -> list[dict]:
        """1ウォレットの新規トランザクションを確認"""
        alerts = []
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [address, {"limit": 5}],
            }
            async with self.session.post(
                self.rpc_url, json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return alerts
                data = await resp.json()

            sigs = data.get("result", [])
            if not sigs:
                return alerts

            last_known = self.last_signatures.get(address)
            self.last_signatures[address] = sigs[0].get("signature", "")

            if last_known is None:
                return alerts  # 初回は記録のみ

            for sig_info in sigs:
                sig = sig_info.get("signature", "")
                if sig == last_known:
                    break
                if not sig_info.get("err"):
                    alerts.append({
                        "type": "wallet_activity",
                        "wallet": address,
                        "label": label,
                        "signature": sig,
                        "block_time": sig_info.get("blockTime", 0),
                    })

        except Exception as e:
            logger.debug(f"Wallet check error: {e}")

        return alerts


class LiquidityMonitor:
    """トークンの流動性変動を監視"""

    DEXSCREENER_API = "https://api.dexscreener.com"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.tokens = self._load_tokens()
        self.prev_liquidity: dict[str, float] = {}

    def _load_tokens(self) -> list[str]:
        raw = config.watch_tokens
        return [t.strip() for t in raw.split(",") if t.strip()] if raw else []

    async def check_all(self) -> list[dict]:
        """全監視トークンの流動性を確認"""
        alerts = []
        for addr in self.tokens:
            try:
                alert = await self._check_token(addr)
                if alert:
                    alerts.append(alert)
            except Exception as e:
                logger.debug(f"Liquidity monitor error: {e}")
            await asyncio.sleep(0.3)
        return alerts

    async def _check_token(self, token_address: str) -> Optional[dict]:
        """1トークンの流動性変動を確認"""
        try:
            url = f"{self.DEXSCREENER_API}/tokens/v1/solana/{token_address}"
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            if not data or not isinstance(data, list):
                return None

            pair = data[0]
            current_liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            symbol = pair.get("baseToken", {}).get("symbol", "???")

            prev = self.prev_liquidity.get(token_address)
            self.prev_liquidity[token_address] = current_liq

            if prev is None or prev == 0:
                return None

            change_pct = ((current_liq - prev) / prev) * 100

            # 20%以上の変動で通知
            if abs(change_pct) >= 20:
                return {
                    "type": "liquidity_change",
                    "token_address": token_address,
                    "symbol": symbol,
                    "prev_liquidity": prev,
                    "current_liquidity": current_liq,
                    "change_pct": round(change_pct, 1),
                    "direction": "増加" if change_pct > 0 else "減少",
                }

        except Exception as e:
            logger.debug(f"Liquidity check error: {e}")

        return None


class SOLRangeMonitor:
    """SOL価格のレンジ監視"""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.low = config.sol_range_low
        self.high = config.sol_range_high
        self.last_alert_type: Optional[str] = None

    async def check(self) -> Optional[dict]:
        """SOL価格がレンジを超えたか確認"""
        if self.low == 0 and self.high == 0:
            return None  # レンジ未設定

        try:
            url = "https://api.dexscreener.com/latest/dex/pairs/solana/So11111111111111111111111111111111111111112"
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    # フォールバック: CoinGecko
                    return await self._check_coingecko()
                data = await resp.json()

            pair = data.get("pair") or (data.get("pairs", [{}])[0] if data.get("pairs") else {})
            price = float(pair.get("priceUsd", 0) or 0)

            if price == 0:
                return await self._check_coingecko()

            return self._evaluate(price)

        except Exception:
            return await self._check_coingecko()

    async def _check_coingecko(self) -> Optional[dict]:
        """CoinGecko フォールバック"""
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            price = data.get("solana", {}).get("usd", 0)
            return self._evaluate(price)
        except Exception:
            return None

    def _evaluate(self, price: float) -> Optional[dict]:
        """価格をレンジと比較"""
        if price <= 0:
            return None

        alert_type = None
        if self.low > 0 and price <= self.low:
            alert_type = "below_range"
        elif self.high > 0 and price >= self.high:
            alert_type = "above_range"

        if alert_type and alert_type != self.last_alert_type:
            self.last_alert_type = alert_type
            return {
                "type": "sol_range",
                "price": price,
                "alert_type": alert_type,
                "low": self.low,
                "high": self.high,
                "message": (
                    f"SOL ${price:.2f} がレンジ下限 ${self.low:.2f} を下回りました"
                    if alert_type == "below_range"
                    else f"SOL ${price:.2f} がレンジ上限 ${self.high:.2f} を超えました"
                ),
            }

        if alert_type is None:
            self.last_alert_type = None

        return None
