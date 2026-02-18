"""
スキャナー：DexScreener APIから新規Solanaプロジェクトを発見
3系統（最新プロファイル / ブーストトークン / トレンドペア）で収集
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

# 修正：ドットを削除
from config import config

logger = logging.getLogger(__name__)

@dataclass
class SolanaProject:
        """発見されたSolanaプロジェクト"""
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

    # スコア
        scores: dict = field(default_factory=dict)
        total_score: float = 0.0

    def __repr__(self):
                return f"<{self.symbol} | ${self.price_usd:.8f} | Liq: ${self.liquidity_usd:,.0f} | Score: {self.total_score:.1f}>"

class DexScreenerScanner:
        """DexScreener APIスキャナー"""
        BASE = "https://api.dexscreener.com"

    def __init__(self, session: aiohttp.ClientSession):
                self.session = session

    async def fetch_new_pairs(self, hours_back: int = 24) -> list[SolanaProject]:
                """3系統から新規ペアを収集"""
                results = await asyncio.gather(
                    self._fetch_latest_profiles(),
                    self._fetch_boosted_tokens(),
                    self._fetch_trending(),
                    return_exceptions=True,
                )

        projects = []
        for i, r in enumerate(results):
                        if isinstance(r, Exception):
                                            logger.warning(f"ルート{i+1}でエラー: {r}")
else:
                    logger.info(f"ルート{i+1}: {len(r)}件")
                    projects.extend(r)

        # 重複排除
            seen = set()
        unique = []
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

    # --- ルート1: 最新プロファイル ---
    async def _f
