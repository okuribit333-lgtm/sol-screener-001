import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional
import aiohttp
from config import config

logger = logging.getLogger(__name__ )

@dataclass
class SolanaProject:
    token_address: str
    pair_address: str
    name: str
    symbol: str
    created_at: datetime
    dex: str
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_24h_usd: float = 0.0
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0
    fdv: float = 0.0
    pair_url: str = ""
    github_url: Optional[str] = None
    website_url: Optional[str] = None
    twitter_url: Optional[str] = None
    score: float = 0.0
    analysis_results: dict = field(default_factory=dict)

    def __repr__(self):
        return f"<{self.symbol} | ${self.price_usd} | Liq: ${self.liquidity_usd}>"

class DexScreenerScanner:
    def __init__(self, session: aiohttp.ClientSession ):
        self.session = session
        self.base_url = "https://api.dexscreener.com/latest/dex"

    async def fetch_new_pairs(self, hours_back: int = 24 ) -> list[SolanaProject]:
        logger.info(f"🔍 DexScreenerから新規ペアを収集中 (過去 {hours_back}時間)...")
        projects = []
        try:
            async with self.session.get(f"{self.base_url}/search?q=solana") as resp:
                if resp.status != 200: return []
                data = await resp.json()
                pairs = data.get("pairs", [])
                cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
                for p in pairs:
                    if p.get("chainId") != "solana": continue
                    created_at = datetime.fromtimestamp(p.get("pairCreatedAt", 0) / 1000, timezone.utc)
                    if created_at < cutoff: continue
                    base = p.get("baseToken", {})
                    projects.append(SolanaProject(
                        token_address=base.get("address", ""),
                        pair_address=p.get("pairAddress", ""),
                        name=base.get("name", "Unknown"),
                        symbol=base.get("symbol", "???"),
                        created_at=created_at,
                        dex=p.get("dexId", "unknown"),
                        price_usd=float(p.get("priceUsd", 0)),
                        liquidity_usd=float(p.get("liquidity", {}).get("usd", 0)),
                        volume_24h_usd=float(p.get("volume", {}).get("h24", 0)),
                        price_change_5m=float(p.get("priceChange", {}).get("m5", 0)),
                        price_change_1h=float(p.get("priceChange", {}).get("h1", 0)),
                        fdv=float(p.get("fdv", 0)),
                        pair_url=p.get("url", ""),
                        website_url=next((i.get("url") for i in p.get("info", {}).get("websites", [])), None),
                        twitter_url=next((s.get("url") for s in p.get("info", {}).get("socials", []) if s.get("type") == "twitter"), None)
                    ))
        except Exception as e:
            logger.error(f"❌ スキャン中にエラー: {e}")
        return projects

    async def enrich_github(self, project: SolanaProject):
        if not project.website_url: return
        try:
            async with self.session.get(project.website_url, timeout=10) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    match = re.search(r'https?://github\.com/[\w\-\.]+/[\w\-\.]+', html )
                    if match: project.github_url = match.group(0)
        except: pass
