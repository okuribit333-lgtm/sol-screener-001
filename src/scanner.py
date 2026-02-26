"""
スキャナー v4 — DexScreener API から新規 Solana プロジェクトを発見
3 系統 + Pump.fun 卒業 (Raydium 上場) 検知ルート追加

ルート 1: 最新プロファイル  /token-profiles/latest/v1
ルート 2: ブーストトークン  /token-boosts/top/v1
ルート 3: トレンドペア      /latest/dex/search?q=SOL
ルート 4: Pump.fun 卒業     /latest/dex/search?q=solana  → dexId=="raydium" & 新規
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from .config import config

logger = logging.getLogger(__name__)


@dataclass
class SolanaProject:
    """発見された Solana プロジェクト"""
    token_address: str
    pair_address: str
    name: str
    symbol: str
    created_at: datetime
    dex: str

    # マーケットデータ
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_24h_usd: float = 0.0
    market_cap: float = 0.0
    fdv: float = 0.0
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0
    price_change_24h: float = 0.0
    tx_count_24h: int = 0
    makers_24h: int = 0

    # ソーシャルリンク
    website_url: Optional[str] = None
    twitter_handle: Optional[str] = None
    discord_url: Optional[str] = None
    telegram_url: Optional[str] = None
    github_url: Optional[str] = None

    # Pump.fun 卒業フラグ
    is_graduated: bool = False
    graduation_source: str = ""  # "raydium" / "pumpswap"

    # スコア
    scores: dict = field(default_factory=dict)
    total_score: float = 0.0

    def __repr__(self):
        grad = " 🎓" if self.is_graduated else ""
        return (
            f"<{self.symbol}{grad} | ${self.price_usd:.8f} "
            f"| Liq: ${self.liquidity_usd:,.0f} | Score: {self.total_score:.1f}>"
        )


class DexScreenerScanner:
    """DexScreener API スキャナー v4"""

    BASE = "https://api.dexscreener.com"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    # ================================================================
    # メイン: 全ルートから新規ペアを収集
    # ================================================================
    async def fetch_new_pairs(self, hours_back: int = 0) -> list[SolanaProject]:
        """4 系統から新規ペアを収集"""
        if hours_back <= 0:
            hours_back = config.scan_hours_back
        results = await asyncio.gather(
            self._fetch_latest_profiles(),
            self._fetch_boosted_tokens(),
            self._fetch_trending(),
            self._fetch_graduated_raydium(),
            return_exceptions=True,
        )

        route_names = ["最新プロファイル", "ブーストトークン", "トレンドペア", "Raydium卒業"]
        projects: list[SolanaProject] = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning(f"ルート{i+1}({route_names[i]})でエラー: {r}")
            else:
                logger.info(f"ルート{i+1}({route_names[i]}): {len(r)}件")
                projects.extend(r)

        # 重複排除
        seen: set[str] = set()
        unique: list[SolanaProject] = []
        for p in projects:
            if p.token_address not in seen:
                seen.add(p.token_address)
                unique.append(p)

        # フィルタ
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        filtered = [
            p for p in unique
            if p.liquidity_usd >= config.min_liquidity_usd
            and p.volume_24h_usd >= config.min_volume_24h_usd
            and p.created_at >= cutoff
        ]

        logger.info(f"スキャン完了: {len(unique)}件 → フィルタ後 {len(filtered)}件")
        return filtered

    # ================================================================
    # ルート 1: 最新プロファイル
    # ================================================================
    async def _fetch_latest_profiles(self) -> list[SolanaProject]:
        try:
            async with self.session.get(
                f"{self.BASE}/token-profiles/latest/v1"
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

            tokens = [
                t for t in (data if isinstance(data, list) else [])
                if t.get("chainId") == "solana"
            ][:50]

            projects: list[SolanaProject] = []
            for t in tokens:
                addr = t.get("tokenAddress", "")
                if addr:
                    p = await self._get_pair(addr)
                    if p:
                        projects.append(p)
                    await asyncio.sleep(0.3)
            return projects
        except Exception as e:
            logger.error(f"最新プロファイル取得エラー: {e}")
            return []

    # ================================================================
    # ルート 2: ブーストトークン
    # ================================================================
    async def _fetch_boosted_tokens(self) -> list[SolanaProject]:
        try:
            async with self.session.get(
                f"{self.BASE}/token-boosts/top/v1"
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

            tokens = [
                t for t in (data if isinstance(data, list) else [])
                if t.get("chainId") == "solana"
            ][:20]

            projects: list[SolanaProject] = []
            for t in tokens:
                addr = t.get("tokenAddress", "")
                if addr:
                    p = await self._get_pair(addr)
                    if p:
                        projects.append(p)
                    await asyncio.sleep(0.3)
            return projects
        except Exception as e:
            logger.error(f"ブーストトークン取得エラー: {e}")
            return []

    # ================================================================
    # ルート 3: トレンドペア
    # ================================================================
    async def _fetch_trending(self) -> list[SolanaProject]:
        try:
            async with self.session.get(
                f"{self.BASE}/latest/dex/search", params={"q": "SOL"}
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

            pairs = [
                p for p in data.get("pairs", [])
                if p.get("chainId") == "solana"
            ][:30]
            return [parsed for p in pairs if (parsed := self._parse(p))]
        except Exception as e:
            logger.error(f"トレンドペア取得エラー: {e}")
            return []

    # ================================================================
    # ルート 4: Pump.fun 卒業 → Raydium 上場検知
    # ================================================================
    async def _fetch_graduated_raydium(self) -> list[SolanaProject]:
        """
        DexScreener search で dexId=="raydium" かつ作成直後のペアを抽出。
        Pump.fun から Raydium に移行（卒業）した瞬間の新規ペアを検知する。
        """
        try:
            async with self.session.get(
                f"{self.BASE}/latest/dex/search", params={"q": "solana"}
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

            cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
            graduated: list[SolanaProject] = []

            for pair in data.get("pairs", []):
                if pair.get("chainId") != "solana":
                    continue
                if pair.get("dexId") != "raydium":
                    continue

                created_ms = pair.get("pairCreatedAt", 0)
                if not created_ms:
                    continue
                created = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
                if created < cutoff:
                    continue

                # 新規 Raydium ペア → 卒業候補
                p = self._parse(pair)
                if p:
                    p.is_graduated = True
                    p.graduation_source = "raydium"
                    graduated.append(p)

            if graduated:
                logger.info(f"🎓 Raydium卒業候補: {len(graduated)}件（直近2時間）")
            return graduated

        except Exception as e:
            logger.error(f"Raydium卒業検知エラー: {e}")
            return []

    # ================================================================
    # ペアデータ取得
    # ================================================================
    async def _get_pair(self, token_address: str) -> Optional[SolanaProject]:
        """新 API → 旧 API フォールバック"""
        # 新 API: /tokens/v1/solana/{address}
        try:
            url = f"{self.BASE}/tokens/v1/solana/{token_address}"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        best = max(
                            data,
                            key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0,
                        )
                        return self._parse(best)
        except Exception:
            pass

        # 旧 API フォールバック
        try:
            url = f"{self.BASE}/latest/dex/tokens/{token_address}"
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                pairs = [
                    p for p in data.get("pairs", [])
                    if p.get("chainId") == "solana"
                ]
                if pairs:
                    best = max(
                        pairs,
                        key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0,
                    )
                    return self._parse(best)
        except Exception:
            pass
        return None

    # ================================================================
    # パーサー
    # ================================================================
    def _parse(self, pair: dict) -> Optional[SolanaProject]:
        try:
            base = pair.get("baseToken", {})
            info = pair.get("info", {})
            socials = {s.get("type"): s.get("url") for s in info.get("socials", [])}
            websites = info.get("websites", [])
            pc = pair.get("priceChange", {})
            txns = pair.get("txns", {}).get("h24", {})

            created_ms = pair.get("pairCreatedAt", 0)
            created = (
                datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
                if created_ms
                else datetime.now(timezone.utc)
            )

            p = SolanaProject(
                token_address=base.get("address", ""),
                pair_address=pair.get("pairAddress", ""),
                name=base.get("name", "Unknown"),
                symbol=base.get("symbol", "???"),
                created_at=created,
                dex=pair.get("dexId", "unknown"),
                price_usd=float(pair.get("priceUsd", 0) or 0),
                liquidity_usd=pair.get("liquidity", {}).get("usd", 0) or 0,
                volume_24h_usd=pair.get("volume", {}).get("h24", 0) or 0,
                market_cap=float(pair.get("marketCap", 0) or 0),
                fdv=float(pair.get("fdv", 0) or 0),
                price_change_5m=pc.get("m5", 0) or 0,
                price_change_1h=pc.get("h1", 0) or 0,
                price_change_24h=pc.get("h24", 0) or 0,
                tx_count_24h=txns.get("buys", 0) + txns.get("sells", 0),
                makers_24h=pair.get("txns", {}).get("h24", {}).get("makers", 0) or 0,
                website_url=websites[0].get("url") if websites else None,
                twitter_handle=self._extract_handle(socials.get("twitter", "")),
                discord_url=socials.get("discord"),
                telegram_url=socials.get("telegram"),
            )
            return p if p.token_address else None
        except Exception as e:
            logger.debug(f"パースエラー: {e}")
            return None

    @staticmethod
    def _extract_handle(url: str) -> Optional[str]:
        if not url:
            return None
        for prefix in [
            "https://twitter.com/", "https://x.com/",
            "http://twitter.com/", "http://x.com/",
        ]:
            if url.startswith(prefix):
                h = url[len(prefix):].strip("/").split("?")[0]
                return h if h else None
        return None

    # ================================================================
    # Web サイトから GitHub リンク探索
    # ================================================================
    async def enrich_github(self, project: SolanaProject):
        if project.github_url or not project.website_url:
            return
        try:
            async with self.session.get(
                project.website_url,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    m = re.search(
                        r'https?://github\.com/[\w\-]+(?:/[\w\-]+)?', html
                    )
                    if m:
                        project.github_url = m.group(0)
        except Exception:
            pass
