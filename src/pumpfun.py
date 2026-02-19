"""
Pump.fun 卒業検知 v4 — Raydium / PumpSwap 上場のリアルタイム検知

検知ルート:
  1. DexScreener search → dexId=="raydium" & 新規ペア（scanner.py ルート4 と連動）
  2. Solana RPC getSignaturesForAddress → Migration TX 解析
  3. DexScreener token-profiles → 新規 Solana トークン → Raydium チェック

Pump.fun の Migration Program:
  39azUYFWPz3VHgKCf3VchUkGGkFdCx4Eo（Pump.fun → Raydium マイグレーション）
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from .config import config

logger = logging.getLogger(__name__)

# Pump.fun Migration Program ID
PUMPFUN_MIGRATION_PROGRAM = "39azUYFWPz3VHgKCf3VchUkGGkFdCx4Eo"

# Raydium AMM Program ID
RAYDIUM_AMM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"


@dataclass
class GraduationEvent:
    """Pump.fun 卒業イベント"""
    token_address: str
    token_name: str = ""
    token_symbol: str = ""
    pair_address: str = ""
    dex: str = "raydium"
    migration_tx: str = ""
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    initial_liquidity: float = 0.0
    initial_mcap: float = 0.0
    price_usd: float = 0.0
    source: str = ""  # "rpc" / "dexscreener"


class PumpFunGraduationDetector:
    """Pump.fun → Raydium 卒業をリアルタイム検知"""

    DEXSCREENER_API = "https://api.dexscreener.com"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.seen_migrations: set[str] = set()
        self.rpc_url = self._get_rpc_url()

    def _get_rpc_url(self) -> str:
        helius_key = getattr(config, "helius_api_key", "")
        if helius_key:
            return f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
        return "https://api.mainnet-beta.solana.com"

    # ================================================================
    # メイン: 卒業イベントを検出
    # ================================================================
    async def detect_graduations(self) -> list[GraduationEvent]:
        """全ルートから卒業イベントを検出"""
        results = await asyncio.gather(
            self._detect_via_dexscreener(),
            self._detect_via_rpc(),
            return_exceptions=True,
        )

        events: list[GraduationEvent] = []
        route_names = ["DexScreener", "RPC"]
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning(f"卒業検知ルート{i+1}({route_names[i]})エラー: {r}")
            else:
                events.extend(r)

        # 重複排除
        unique: list[GraduationEvent] = []
        seen_addrs: set[str] = set()
        for e in events:
            if e.token_address not in seen_addrs:
                seen_addrs.add(e.token_address)
                unique.append(e)

        if unique:
            logger.info(f"🎓 卒業検出: {len(unique)}件")

        return unique

    # ================================================================
    # ルート 1: DexScreener 経由
    # ================================================================
    async def _detect_via_dexscreener(self) -> list[GraduationEvent]:
        """DexScreener search で新規 Raydium ペアを検出"""
        events: list[GraduationEvent] = []
        try:
            url = f"{self.DEXSCREENER_API}/latest/dex/search"
            async with self.session.get(
                url, params={"q": "solana"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return events
                data = await resp.json()

            cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)

            for pair in data.get("pairs", []):
                if pair.get("chainId") != "solana":
                    continue
                if pair.get("dexId") not in ("raydium", "pumpswap"):
                    continue

                created_ms = pair.get("pairCreatedAt", 0)
                if not created_ms:
                    continue
                created = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
                if created < cutoff:
                    continue

                token_addr = pair.get("baseToken", {}).get("address", "")
                if not token_addr or token_addr in self.seen_migrations:
                    continue

                self.seen_migrations.add(token_addr)

                event = GraduationEvent(
                    token_address=token_addr,
                    token_name=pair.get("baseToken", {}).get("name", ""),
                    token_symbol=pair.get("baseToken", {}).get("symbol", ""),
                    pair_address=pair.get("pairAddress", ""),
                    dex=pair.get("dexId", "raydium"),
                    detected_at=created,
                    initial_liquidity=float(
                        pair.get("liquidity", {}).get("usd", 0) or 0
                    ),
                    initial_mcap=float(pair.get("marketCap", 0) or 0),
                    price_usd=float(pair.get("priceUsd", 0) or 0),
                    source="dexscreener",
                )
                events.append(event)

        except Exception as e:
            logger.error(f"DexScreener卒業検知エラー: {e}")

        return events

    # ================================================================
    # ルート 2: Solana RPC 経由（Migration TX 解析）
    # ================================================================
    async def _detect_via_rpc(self) -> list[GraduationEvent]:
        """Pump.fun Migration Program の最新トランザクションを監視"""
        events: list[GraduationEvent] = []
        try:
            # Migration Program の最新シグネチャを取得
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [
                    PUMPFUN_MIGRATION_PROGRAM,
                    {"limit": 10},
                ],
            }
            async with self.session.post(
                self.rpc_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return events
                data = await resp.json()

            signatures = data.get("result", [])
            if not signatures:
                return events

            for sig_info in signatures:
                sig = sig_info.get("signature", "")
                if not sig or sig in self.seen_migrations:
                    continue
                if sig_info.get("err"):
                    continue

                self.seen_migrations.add(sig)

                # トランザクション詳細を取得
                event = await self._parse_migration_tx(sig)
                if event:
                    events.append(event)

                await asyncio.sleep(0.3)

        except Exception as e:
            logger.debug(f"RPC卒業検知エラー: {e}")

        return events

    async def _parse_migration_tx(self, signature: str) -> Optional[GraduationEvent]:
        """Migration トランザクションを解析してトークン情報を抽出"""
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    signature,
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
                ],
            }
            async with self.session.post(
                self.rpc_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            tx = data.get("result")
            if not tx:
                return None

            # アカウントキーからトークンアドレスを推定
            account_keys = (
                tx.get("transaction", {})
                .get("message", {})
                .get("accountKeys", [])
            )

            # Token Program を使っているアカウントを探す
            token_address = None
            for key in account_keys:
                pubkey = key if isinstance(key, str) else key.get("pubkey", "")
                # Migration Program と Raydium 以外のアカウント
                if pubkey not in (
                    PUMPFUN_MIGRATION_PROGRAM,
                    RAYDIUM_AMM_PROGRAM,
                    "11111111111111111111111111111111",
                    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    "SysvarRent111111111111111111111111111111111",
                ):
                    # 最初の未知アカウント = トークンアドレスの可能性
                    if not token_address and len(pubkey) > 30:
                        token_address = pubkey

            if not token_address:
                return None

            block_time = tx.get("blockTime", 0)
            detected = (
                datetime.fromtimestamp(block_time, tz=timezone.utc)
                if block_time
                else datetime.now(timezone.utc)
            )

            event = GraduationEvent(
                token_address=token_address,
                migration_tx=signature,
                detected_at=detected,
                source="rpc",
            )

            # DexScreener で詳細を補完
            await self._enrich_from_dexscreener(event)

            return event

        except Exception as e:
            logger.debug(f"Migration TX 解析エラー: {e}")
            return None

    async def _enrich_from_dexscreener(self, event: GraduationEvent):
        """DexScreener API でトークン詳細を補完"""
        try:
            url = f"{self.DEXSCREENER_API}/tokens/v1/solana/{event.token_address}"
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()

            if not data or not isinstance(data, list):
                return

            pair = data[0]
            event.token_name = pair.get("baseToken", {}).get("name", "")
            event.token_symbol = pair.get("baseToken", {}).get("symbol", "")
            event.pair_address = pair.get("pairAddress", "")
            event.dex = pair.get("dexId", "raydium")
            event.initial_liquidity = float(
                pair.get("liquidity", {}).get("usd", 0) or 0
            )
            event.initial_mcap = float(pair.get("marketCap", 0) or 0)
            event.price_usd = float(pair.get("priceUsd", 0) or 0)

        except Exception as e:
            logger.debug(f"DexScreener enrich error: {e}")

    def cleanup(self):
        """古い seen_migrations をクリーンアップ"""
        if len(self.seen_migrations) > 500:
            self.seen_migrations = set(list(self.seen_migrations)[-250:])
