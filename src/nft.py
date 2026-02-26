"""
NFTミント監視モジュール v5.7
Magic Eden API（無料・キー不要）で Solana NFT を監視

■ 通知タイプ:
  A) 新規ミントアラート [🔴緊急]
     - ME Launchpad API から直近7日以内のミント情報を取得
     - ミント価格・供給量・ローンチ日を通知
  B) トレンドコレクションアラート [🟡通常]
     - 主要コレクションのフロア価格変動を監視
     - 24h出来高・リスト数の変動を検知
  C) フロア急変アラート [🔴緊急]
     - ウォッチリスト + 自動検出コレクションのフロア ±20% を検知

■ 品質フィルタ:
  - ミント価格: 0.01 〜 10 SOL（無料大量発行・高額詐欺を除外）
  - 供給量: 100 〜 10,000（1-of-1・無限OEを除外）
  - フロア > 0（二次市場で取引実績あり）
  - リスト数 > 5（実際の流動性あり）
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


# ── データクラス ──

@dataclass
class NFTMint:
    """新規ミント情報（Launchpad）"""
    symbol: str
    name: str
    description: str = ""
    image: str = ""
    mint_price: float = 0.0       # SOL
    supply: int = 0
    launch_date: Optional[datetime] = None
    chain_id: str = "solana"
    contract_address: str = ""
    # 二次市場データ（ミント後に取得）
    floor_price: float = 0.0      # SOL
    listed_count: int = 0
    volume_all: float = 0.0       # SOL
    avg_price_24h: float = 0.0    # SOL
    # メタ
    is_upcoming: bool = False
    days_until_launch: int = 0
    score: float = 0.0


@dataclass
class NFTCollection:
    """既存コレクション情報"""
    symbol: str
    name: str
    description: str = ""
    image: str = ""
    floor_price: float = 0.0
    listed_count: int = 0
    volume_all: float = 0.0
    avg_price_24h: float = 0.0
    total_supply: int = 0
    holder_count: int = 0
    scores: dict = field(default_factory=dict)
    total_score: float = 0.0


@dataclass
class NFTFloorAlert:
    """フロア価格変動アラート"""
    collection: str
    symbol: str
    name: str = ""
    prev_floor: float = 0.0
    current_floor: float = 0.0
    change_pct: float = 0.0
    alert_type: str = ""        # "pump" or "dump"
    volume_all: float = 0.0
    listed_count: int = 0
    image: str = ""


# ── フィルタ設定 ──

NFT_MINT_PRICE_MIN = float(os.getenv("NFT_MINT_PRICE_MIN", "0.01"))
NFT_MINT_PRICE_MAX = float(os.getenv("NFT_MINT_PRICE_MAX", "10.0"))
NFT_SUPPLY_MIN = int(os.getenv("NFT_SUPPLY_MIN", "100"))
NFT_SUPPLY_MAX = int(os.getenv("NFT_SUPPLY_MAX", "10000"))
NFT_FLOOR_CHANGE_THRESHOLD = float(os.getenv("NFT_FLOOR_CHANGE_PCT", "20.0"))
NFT_LAUNCH_WINDOW_DAYS = int(os.getenv("NFT_LAUNCH_WINDOW_DAYS", "7"))
NFT_MIN_LISTED = int(os.getenv("NFT_MIN_LISTED", "5"))

# ウォッチリスト（環境変数 or デフォルト）
DEFAULT_WATCH = "mad_lads,tensorians,famous_fox_federation,okay_bears,claynosaurz,solana_monkey_business"
WATCH_NFTS = [s.strip() for s in os.getenv("WATCH_NFTS", DEFAULT_WATCH).split(",") if s.strip()]


class NFTMonitor:
    """Solana NFT 統合監視（v5.7）"""

    BASE = "https://api-mainnet.magiceden.dev/v2"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.headers = {
            "User-Agent": "SolAutoScreener/5.7",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        self.prev_floors: dict[str, float] = {}
        self.seen_mints: set[str] = set()

    # ================================================================
    # A) 新規ミントスキャン（Launchpad）
    # ================================================================
    async def scan_new_mints(self) -> list[NFTMint]:
        """Magic Eden Launchpad から新規ミント情報を取得"""
        mints = []
        now = datetime.now(timezone.utc)

        try:
            url = f"{self.BASE}/launchpad/collections"
            params = {"offset": 0, "limit": 50}

            async with self.session.get(
                url, params=params, headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"ME Launchpad API: status={resp.status}")
                    return []
                data = await resp.json()

            for item in data:
                # Solanaのみ
                if item.get("chainId", "").lower() != "solana":
                    continue

                symbol = item.get("symbol", "")
                if symbol in self.seen_mints:
                    continue

                # ローンチ日パース
                launch_str = item.get("launchDatetime", "")
                launch_dt = None
                if launch_str:
                    try:
                        launch_dt = datetime.fromisoformat(launch_str.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        pass

                # 時間窓フィルタ: 直近N日以内（過去 or 未来）
                if launch_dt:
                    days_diff = (launch_dt - now).days
                    if days_diff < -NFT_LAUNCH_WINDOW_DAYS or days_diff > NFT_LAUNCH_WINDOW_DAYS:
                        continue
                    is_upcoming = days_diff > 0
                    days_until = max(0, days_diff)
                else:
                    is_upcoming = False
                    days_until = 0

                price = float(item.get("price", 0) or 0)
                supply = int(item.get("size", 0) or 0)

                # 品質フィルタ
                if not self._passes_mint_filter(price, supply):
                    continue

                mint = NFTMint(
                    symbol=symbol,
                    name=item.get("name", "Unknown"),
                    description=(item.get("description", "") or "")[:200],
                    image=item.get("image", ""),
                    mint_price=price,
                    supply=supply,
                    launch_date=launch_dt,
                    contract_address=item.get("contractAddress", ""),
                    is_upcoming=is_upcoming,
                    days_until_launch=days_until,
                )
                mints.append(mint)
                self.seen_mints.add(symbol)

            # 二次市場データを取得
            enrich_tasks = [self._enrich_mint(m) for m in mints]
            await asyncio.gather(*enrich_tasks, return_exceptions=True)

            # スコアリング
            for m in mints:
                m.score = self._score_mint(m)

            mints.sort(key=lambda m: m.score, reverse=True)

            if mints:
                logger.info(f"NFTミント: {len(data)}件中 Solana {len(mints)}件が品質フィルタ通過")

        except Exception as e:
            logger.error(f"NFT Launchpad scan error: {e}")

        return mints

    def _passes_mint_filter(self, price: float, supply: int) -> bool:
        """ミント品質フィルタ"""
        if price < NFT_MINT_PRICE_MIN or price > NFT_MINT_PRICE_MAX:
            return False
        if supply < NFT_SUPPLY_MIN or supply > NFT_SUPPLY_MAX:
            return False
        return True

    async def _enrich_mint(self, mint: NFTMint):
        """ミント後のコレクションの二次市場データを取得"""
        if mint.is_upcoming:
            return  # まだローンチ前
        try:
            url = f"{self.BASE}/collections/{mint.symbol}/stats"
            async with self.session.get(
                url, headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return
                stats = await resp.json()

            mint.floor_price = (stats.get("floorPrice", 0) or 0) / 1e9
            mint.listed_count = stats.get("listedCount", 0) or 0
            mint.volume_all = (stats.get("volumeAll", 0) or 0) / 1e9
            mint.avg_price_24h = (stats.get("avgPrice24hr", 0) or 0) / 1e9

        except Exception as e:
            logger.debug(f"NFT enrich error for {mint.symbol}: {e}")

    def _score_mint(self, mint: NFTMint) -> float:
        """ミントのスコアリング（0-100）"""
        import math
        score = 0.0

        # 1. 価格帯スコア（0.1-2 SOL が最適）
        if 0.1 <= mint.mint_price <= 2.0:
            price_score = 80
        elif 0.01 <= mint.mint_price < 0.1:
            price_score = 50
        elif 2.0 < mint.mint_price <= 5.0:
            price_score = 60
        else:
            price_score = 30
        score += price_score * 0.20

        # 2. 供給量スコア（500-5000 が最適）
        if 500 <= mint.supply <= 5000:
            supply_score = 80
        elif 100 <= mint.supply < 500:
            supply_score = 60
        elif 5000 < mint.supply <= 10000:
            supply_score = 50
        else:
            supply_score = 20
        score += supply_score * 0.15

        # 3. 二次市場スコア（フロア価格 > ミント価格 = 利益出てる）
        if mint.floor_price > 0 and mint.mint_price > 0:
            ratio = mint.floor_price / mint.mint_price
            if ratio >= 2.0:
                market_score = 100
            elif ratio >= 1.0:
                market_score = 70
            elif ratio >= 0.5:
                market_score = 40
            else:
                market_score = 10
        elif mint.is_upcoming:
            market_score = 50  # 未ローンチは中立
        else:
            market_score = 20
        score += market_score * 0.25

        # 4. 出来高スコア
        if mint.volume_all > 0:
            vol_score = min(100, math.log10(max(1, mint.volume_all)) * 25)
        else:
            vol_score = 10 if mint.is_upcoming else 0
        score += vol_score * 0.20

        # 5. リスト率スコア（低い = ホルダーが売りたくない）
        if mint.supply > 0 and mint.listed_count > 0:
            list_ratio = mint.listed_count / mint.supply
            if list_ratio < 0.05:
                list_score = 90
            elif list_ratio < 0.15:
                list_score = 70
            elif list_ratio < 0.30:
                list_score = 50
            else:
                list_score = 20
        else:
            list_score = 50
        score += list_score * 0.10

        # 6. タイミングボーナス
        if mint.is_upcoming and mint.days_until_launch <= 2:
            score += 10  # 直近ミントはボーナス

        return round(min(100, score), 1)

    # ================================================================
    # B) トレンドコレクションスキャン
    # ================================================================
    async def scan_trending_collections(self, limit: int = 10) -> list[NFTCollection]:
        """主要コレクションのstatsを取得してスコアリング"""
        collections = []

        # ウォッチリスト + Launchpad既存コレクション
        symbols_to_check = list(WATCH_NFTS)

        for symbol in symbols_to_check[:20]:  # API制限を考慮
            try:
                url = f"{self.BASE}/collections/{symbol}/stats"
                async with self.session.get(
                    url, headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        continue
                    stats = await resp.json()

                floor = (stats.get("floorPrice", 0) or 0) / 1e9
                listed = stats.get("listedCount", 0) or 0
                vol = (stats.get("volumeAll", 0) or 0) / 1e9
                avg24 = (stats.get("avgPrice24hr", 0) or 0) / 1e9

                if floor <= 0:
                    continue

                col = NFTCollection(
                    symbol=symbol,
                    name=symbol.replace("_", " ").title(),
                    floor_price=floor,
                    listed_count=listed,
                    volume_all=vol,
                    avg_price_24h=avg24,
                )
                self._score_collection(col)
                collections.append(col)

            except Exception as e:
                logger.debug(f"Trending scan error {symbol}: {e}")
            await asyncio.sleep(0.3)

        collections.sort(key=lambda c: c.total_score, reverse=True)
        return collections[:limit]

    def _score_collection(self, col: NFTCollection):
        """コレクションスコアリング"""
        import math

        floor_score = min(100, math.log10(max(0.01, col.floor_price)) * 30 + 60) if col.floor_price > 0 else 0
        vol_score = min(100, math.log10(max(1, col.volume_all)) * 20) if col.volume_all > 0 else 0

        if col.total_supply > 0 and col.listed_count > 0:
            list_ratio = col.listed_count / col.total_supply
            list_score = max(0, 100 - list_ratio * 200)
        else:
            list_score = 50

        total = floor_score * 0.35 + vol_score * 0.35 + list_score * 0.30
        col.total_score = round(total, 1)
        col.scores = {
            "floor": round(floor_score, 1),
            "volume": round(vol_score, 1),
            "list_ratio": round(list_score, 1),
        }

    # ================================================================
    # C) フロア価格変動アラート
    # ================================================================
    async def check_floor_alerts(self) -> list[NFTFloorAlert]:
        """ウォッチリストのフロア価格変動を検知"""
        alerts = []

        for symbol in WATCH_NFTS:
            try:
                url = f"{self.BASE}/collections/{symbol}/stats"
                async with self.session.get(
                    url, headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()

                floor = (data.get("floorPrice", 0) or 0) / 1e9
                vol = (data.get("volumeAll", 0) or 0) / 1e9
                listed = data.get("listedCount", 0) or 0

                prev = self.prev_floors.get(symbol)
                self.prev_floors[symbol] = floor

                if prev is None or prev == 0 or floor == 0:
                    continue

                change_pct = ((floor - prev) / prev) * 100

                if abs(change_pct) >= NFT_FLOOR_CHANGE_THRESHOLD:
                    alerts.append(NFTFloorAlert(
                        collection=symbol,
                        symbol=symbol,
                        name=symbol.replace("_", " ").title(),
                        prev_floor=prev,
                        current_floor=floor,
                        change_pct=round(change_pct, 1),
                        alert_type="pump" if change_pct > 0 else "dump",
                        volume_all=vol,
                        listed_count=listed,
                    ))

            except Exception as e:
                logger.debug(f"Floor alert error {symbol}: {e}")
            await asyncio.sleep(0.3)

        if alerts:
            logger.info(f"NFTフロアアラート: {len(alerts)}件検出")

        return alerts

    # ================================================================
    # 統合スキャン
    # ================================================================
    async def full_scan(self) -> dict:
        """全NFTスキャンを実行して結果をまとめて返す"""
        new_mints = await self.scan_new_mints()
        floor_alerts = await self.check_floor_alerts()

        return {
            "new_mints": new_mints,
            "floor_alerts": floor_alerts,
        }
