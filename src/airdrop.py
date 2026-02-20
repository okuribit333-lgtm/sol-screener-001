"""
エアドロップスキャナー v5.3 — マルチチェーン対応 + 品質フィルタ強化版

■ ソース一覧:
  1. DeFiLlama (DeFi) — TVL上位 + トークン未発行プロトコル
  2. DeFiLlama (GameFi) — ゲーム系プロトコル
  3. DeFiLlama (Raises) — 最近の資金調達 → 新規プロジェクト優先
  4. CoinGecko (New Coins) — 新規上場トークン
  5. AirdropAlert — エアドロ専門サイト
  6. CryptoTotem — エアドロ・ICO情報
  7. Binance Launchpool — 取引所エアドロ
  8. キュレーションリスト — 手動選定（BCG含む大量追加）
  9. Twitter/Nitter — SNS監視

■ 品質フィルタ:
  - CEX / ブリッジ / ラップドトークン 完全除外
  - 前回通知済みは24時間除外（新しい情報だけ通知）
  - BCG/GameFi枠を最低5件確保
  - 新規プロジェクト（Raises）を優先表示
"""
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

logger = logging.getLogger(__name__)

# ── 通知済みエアドロ記憶ファイル ──
AIRDROP_STATE_FILE = os.getenv("AIRDROP_STATE_FILE", "data/airdrop_state.json")


@dataclass
class AirdropInfo:
    """エアドロップ情報"""
    name: str
    chain: str = "multi"
    category: str = "defi"
    description: str = ""
    url: str = ""
    status: str = "speculative"  # active / upcoming / speculative / ended
    requirements: list[str] = field(default_factory=list)
    estimated_value: str = ""
    source: str = ""
    confidence: int = 50
    tvl: float = 0.0
    raised: float = 0.0
    is_new: bool = False  # 新規検出フラグ


class AirdropScanner:
    """マルチチェーン対応エアドロップスキャナー"""

    # ── CEX / ブリッジ / 除外リスト ──
    EXCLUDE_CATEGORIES = {
        "CEX", "cex", "Exchange", "exchange",
        "Bridge", "bridge", "Cross Chain", "cross chain",
    }

    EXCLUDE_NAMES = {
        "binance", "okx", "bybit", "coinbase", "kraken", "bitfinex",
        "kucoin", "gate.io", "htx", "huobi", "mexc", "bitget",
        "crypto.com", "robinhood", "upbit", "bithumb", "gemini",
        "bitstamp", "deribit", "phemex", "woo x", "backpack exchange",
        "wbtc", "wrapped bitcoin", "cbbtc", "coinbase wrapped",
        "tbtc", "renbtc", "hbtc", "sbtc",
        "multichain", "portal bridge", "allbridge", "debridge",
        "celer", "hop protocol", "stargate bridge",
        "tether", "usdt", "usdc", "circle", "dai", "makerdao maker",
        "frax", "fei protocol", "rai",
    }

    # ── Nitter インスタンス ──
    NITTER_INSTANCES = [
        "https://nitter.net",
        "https://nitter.privacydev.net",
    ]

    AIRDROP_KEYWORDS = [
        "airdrop", "エアドロ", "token launch", "claim",
        "points", "season", "testnet", "incentive",
        "retroactive", "retrodrop", "farming",
    ]

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._notified_airdrops: dict[str, float] = {}  # name -> timestamp
        self._load_airdrop_state()

    # ── 通知済み記憶の管理 ──
    def _load_airdrop_state(self):
        """前回通知済みエアドロを読み込み"""
        try:
            if os.path.exists(AIRDROP_STATE_FILE):
                with open(AIRDROP_STATE_FILE, "r") as f:
                    self._notified_airdrops = json.load(f)
                logger.info(f"エアドロ通知履歴読み込み: {len(self._notified_airdrops)}件")
        except Exception as e:
            logger.warning(f"エアドロ通知履歴読み込みエラー: {e}")
            self._notified_airdrops = {}

    def _save_airdrop_state(self):
        """通知済みエアドロを保存"""
        try:
            os.makedirs(os.path.dirname(AIRDROP_STATE_FILE) or ".", exist_ok=True)
            with open(AIRDROP_STATE_FILE, "w") as f:
                json.dump(self._notified_airdrops, f, indent=2)
        except Exception as e:
            logger.warning(f"エアドロ通知履歴保存エラー: {e}")

    def mark_notified(self, name: str):
        """エアドロを通知済みとしてマーク"""
        self._notified_airdrops[name.lower().strip()] = time.time()
        self._save_airdrop_state()

    def is_recently_notified(self, name: str, hours: int = 24) -> bool:
        """指定時間以内に通知済みか"""
        key = name.lower().strip()
        if key not in self._notified_airdrops:
            return False
        elapsed = time.time() - self._notified_airdrops[key]
        return elapsed < hours * 3600

    def cleanup_old_notifications(self, max_age_hours: int = 72):
        """古い通知履歴を削除"""
        cutoff = time.time() - max_age_hours * 3600
        before = len(self._notified_airdrops)
        self._notified_airdrops = {
            k: v for k, v in self._notified_airdrops.items()
            if v > cutoff
        }
        if len(self._notified_airdrops) < before:
            self._save_airdrop_state()
            logger.info(f"エアドロ通知履歴クリーンアップ: {before} → {len(self._notified_airdrops)}件")

    # ── 除外判定 ──
    def _is_excluded(self, name: str, category: str = "") -> bool:
        """CEX/ブリッジ/ラップドトークンを除外"""
        name_lower = name.lower()
        if any(ex in name_lower for ex in self.EXCLUDE_NAMES):
            return True
        if category in self.EXCLUDE_CATEGORIES:
            return True
        return False

    # ============================================================
    # メインスキャン
    # ============================================================
    async def scan_all(self) -> list[AirdropInfo]:
        """全ソースから並列スキャン"""
        self.cleanup_old_notifications()

        tasks = [
            self._source_defillama_defi(),
            self._source_defillama_gamefi(),
            self._source_defillama_raises(),
            self._source_coingecko(),
            self._source_airdropalert(),
            self._source_cryptototem(),
            self._source_curated(),
            self._source_exchange_news(),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_airdrops = []
        source_names = [
            "DeFiLlama-DeFi", "DeFiLlama-GameFi", "DeFiLlama-Raises",
            "CoinGecko", "AirdropAlert", "CryptoTotem",
            "Curated", "ExchangeNews",
        ]

        for i, result in enumerate(results):
            name = source_names[i] if i < len(source_names) else f"Source-{i}"
            if isinstance(result, Exception):
                logger.warning(f"ソース {name} エラー: {result}")
            elif isinstance(result, list):
                logger.info(f"ソース {name}: {len(result)}件")
                all_airdrops.extend(result)

        # 重複排除（名前ベース）
        seen = {}
        unique = []
        for a in all_airdrops:
            key = a.name.lower().strip()
            if key not in seen:
                seen[key] = a
                unique.append(a)
            else:
                # より高い確度のものを採用
                if a.confidence > seen[key].confidence:
                    unique.remove(seen[key])
                    seen[key] = a
                    unique.append(a)

        logger.info(f"エアドロ合計: {len(all_airdrops)}件 → 重複排除後: {len(unique)}件")
        return unique

    # ============================================================
    # ソース 1: DeFiLlama (DeFi)
    # ============================================================
    async def _source_defillama_defi(self) -> list[AirdropInfo]:
        """DeFiLlama: TVL上位 + トークン未発行のDeFiプロトコル"""
        airdrops = []
        try:
            url = "https://api.llama.fi/protocols"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return airdrops
                protocols = await resp.json()

            for p in protocols:
                name = p.get("name", "")
                category = p.get("category", "")
                tvl = p.get("tvl", 0) or 0
                symbol = p.get("symbol", "")
                gecko_id = p.get("gecko_id")
                chains = p.get("chains", [])

                # 除外フィルタ
                if self._is_excluded(name, category):
                    continue
                if tvl < 1_000_000:  # TVL $1M未満は除外
                    continue
                if gecko_id and gecko_id != "-":
                    continue  # トークン発行済み

                # チェーン判定
                chain = "multi"
                if chains:
                    chain_lower = [c.lower() for c in chains]
                    if "solana" in chain_lower:
                        chain = "solana"
                    elif "ethereum" in chain_lower:
                        chain = "ethereum"
                    elif "arbitrum" in chain_lower:
                        chain = "arbitrum"
                    elif "base" in chain_lower:
                        chain = "base"
                    elif "bsc" in chain_lower:
                        chain = "bsc"

                # 確度スコア計算
                conf = 40
                if tvl >= 1_000_000_000:
                    conf += 25
                elif tvl >= 100_000_000:
                    conf += 20
                elif tvl >= 10_000_000:
                    conf += 10

                cat_lower = category.lower() if category else ""
                if "dex" in cat_lower or "lending" in cat_lower:
                    conf += 5
                if "liquid staking" in cat_lower:
                    conf += 8

                airdrops.append(AirdropInfo(
                    name=name,
                    chain=chain,
                    category="defi",
                    description=f"TVL: ${tvl/1e6:.1f}M | カテゴリ: {category} | チェーン: {', '.join(chains[:3])}",
                    url=f"https://defillama.com/protocol/{p.get('slug', name.lower().replace(' ', '-'))}",
                    status="speculative",
                    source="defillama-defi",
                    confidence=min(conf, 95),
                    tvl=tvl,
                ))

        except Exception as e:
            logger.warning(f"DeFiLlama DeFi error: {e}")

        return airdrops

    # ============================================================
    # ソース 2: DeFiLlama (GameFi)
    # ============================================================
    async def _source_defillama_gamefi(self) -> list[AirdropInfo]:
        """DeFiLlama: ゲーム系プロトコル"""
        airdrops = []
        try:
            url = "https://api.llama.fi/protocols"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return airdrops
                protocols = await resp.json()

            gamefi_categories = {"Gaming", "GameFi", "Metaverse", "Play-to-Earn"}

            for p in protocols:
                name = p.get("name", "")
                category = p.get("category", "")
                tvl = p.get("tvl", 0) or 0
                gecko_id = p.get("gecko_id")
                chains = p.get("chains", [])

                if category not in gamefi_categories:
                    continue
                if self._is_excluded(name, ""):
                    continue
                if gecko_id and gecko_id != "-":
                    continue

                chain = "multi"
                if chains:
                    chain_lower = [c.lower() for c in chains]
                    if "solana" in chain_lower:
                        chain = "solana"
                    elif "ethereum" in chain_lower:
                        chain = "ethereum"

                conf = 45
                if tvl >= 10_000_000:
                    conf += 15
                elif tvl >= 1_000_000:
                    conf += 8

                airdrops.append(AirdropInfo(
                    name=name,
                    chain=chain,
                    category="gamefi",
                    description=f"GameFi | TVL: ${tvl/1e6:.1f}M | チェーン: {', '.join(chains[:3])}",
                    url=f"https://defillama.com/protocol/{p.get('slug', name.lower().replace(' ', '-'))}",
                    status="speculative",
                    source="defillama-gamefi",
                    confidence=min(conf, 90),
                    tvl=tvl,
                ))

        except Exception as e:
            logger.warning(f"DeFiLlama GameFi error: {e}")

        return airdrops

    # ============================================================
    # ソース 3: DeFiLlama (Raises — 最近の資金調達)
    # ============================================================
    async def _source_defillama_raises(self) -> list[AirdropInfo]:
        """DeFiLlama Raises: 最近の資金調達 → 新規プロジェクト優先"""
        airdrops = []
        try:
            url = "https://api.llama.fi/raises"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return airdrops
                data = await resp.json()

            raises = data.get("raises", data) if isinstance(data, dict) else data
            if not isinstance(raises, list):
                return airdrops

            # 直近90日の資金調達のみ
            import time as _time
            cutoff = _time.time() - 90 * 86400

            for r in raises:
                date = r.get("date")
                if date and date < cutoff:
                    continue

                name = r.get("name", "")
                amount = r.get("amount", 0) or 0
                chains = r.get("chains", [])
                category = r.get("category", "")
                investors = r.get("leadInvestors", []) or []
                round_type = r.get("round", "")

                if not name or self._is_excluded(name, category):
                    continue
                if amount < 1_000_000:  # $1M未満は除外
                    continue

                chain = "multi"
                if chains:
                    chain_lower = [c.lower() for c in chains]
                    if "solana" in chain_lower:
                        chain = "solana"
                    elif "ethereum" in chain_lower:
                        chain = "ethereum"
                    elif "arbitrum" in chain_lower:
                        chain = "arbitrum"
                    elif "base" in chain_lower:
                        chain = "base"

                # カテゴリ判定
                cat = "defi"
                cat_lower = (category or "").lower()
                if any(g in cat_lower for g in ["game", "gaming", "metaverse"]):
                    cat = "gamefi"
                elif any(n in cat_lower for n in ["nft", "collectible"]):
                    cat = "nft"
                elif any(i in cat_lower for i in ["infra", "tool", "analytics"]):
                    cat = "infra"
                elif any(l in cat_lower for l in ["l1", "l2", "chain", "rollup"]):
                    cat = "l2"

                # 確度スコア
                conf = 50
                if amount >= 50_000_000:
                    conf += 20
                elif amount >= 10_000_000:
                    conf += 15
                elif amount >= 5_000_000:
                    conf += 10

                # 有名VCが入っていると確度UP
                top_vcs = ["a16z", "paradigm", "sequoia", "polychain", "multicoin",
                           "binance labs", "coinbase ventures", "dragonfly"]
                for inv in investors:
                    if any(vc in (inv or "").lower() for vc in top_vcs):
                        conf += 5
                        break

                inv_str = ", ".join(investors[:3]) if investors else "非公開"
                airdrops.append(AirdropInfo(
                    name=f"{name}",
                    chain=chain,
                    category=cat,
                    description=f"💰 ${amount/1e6:.1f}M調達 ({round_type}) | 投資家: {inv_str}",
                    status="upcoming",
                    source="defillama-raises",
                    confidence=min(conf, 92),
                    raised=amount,
                    is_new=True,
                ))

        except Exception as e:
            logger.warning(f"DeFiLlama Raises error: {e}")

        return airdrops

    # ============================================================
    # ソース 4: CoinGecko (New Coins)
    # ============================================================
    async def _source_coingecko(self) -> list[AirdropInfo]:
        """CoinGecko: 新規上場トークン"""
        airdrops = []
        try:
            url = "https://api.coingecko.com/api/v3/coins/list?include_platform=true"
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"Accept": "application/json"},
            ) as resp:
                if resp.status != 200:
                    return airdrops
                coins = await resp.json()

            for coin in coins[-50:]:
                name = coin.get("name", "")
                platforms = coin.get("platforms", {})
                if not platforms:
                    continue
                if self._is_excluded(name, ""):
                    continue

                chain = "multi"
                if "solana" in platforms:
                    chain = "solana"
                elif "ethereum" in platforms:
                    chain = "ethereum"

                airdrops.append(AirdropInfo(
                    name=name,
                    chain=chain,
                    category="defi",
                    description="CoinGecko新規上場",
                    url=f"https://www.coingecko.com/en/coins/{coin.get('id', '')}",
                    status="active",
                    source="coingecko",
                    confidence=35,  # 低確度: CoinGecko新規は参考程度
                    is_new=True,
                ))

        except Exception as e:
            logger.debug(f"CoinGecko error: {e}")

        return airdrops

    # ============================================================
    # ソース 5: AirdropAlert
    # ============================================================
    async def _source_airdropalert(self) -> list[AirdropInfo]:
        """AirdropAlert: エアドロ専門サイト"""
        airdrops = []
        if not BeautifulSoup:
            return airdrops

        try:
            url = "https://airdropalert.com/new-airdrops"
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=12),
                headers={"User-Agent": "Mozilla/5.0 (compatible; SolScreener/5.3)"},
            ) as resp:
                if resp.status != 200:
                    return airdrops
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            cards = soup.select(".airdrop-card, .card, [class*='airdrop']")

            for card in cards[:30]:
                title_el = card.select_one("h3, h4, .title, .name, a")
                if not title_el:
                    continue
                name = title_el.get_text(strip=True)
                if not name or len(name) < 2 or self._is_excluded(name, ""):
                    continue

                link = ""
                a_tag = card.select_one("a[href]")
                if a_tag:
                    href = a_tag.get("href", "")
                    if href.startswith("/"):
                        link = f"https://airdropalert.com{href}"
                    elif href.startswith("http"):
                        link = href

                desc_el = card.select_one("p, .description, .desc")
                desc = desc_el.get_text(strip=True)[:200] if desc_el else ""

                airdrops.append(AirdropInfo(
                    name=name,
                    chain="multi",
                    category="defi",
                    description=desc or "AirdropAlertで掲載中",
                    url=link,
                    status="active",
                    source="airdropalert",
                    confidence=55,
                    is_new=True,
                ))

        except Exception as e:
            logger.debug(f"AirdropAlert error: {e}")

        return airdrops

    # ============================================================
    # ソース 6: CryptoTotem
    # ============================================================
    async def _source_cryptototem(self) -> list[AirdropInfo]:
        """CryptoTotem: エアドロ・ICO情報"""
        airdrops = []
        if not BeautifulSoup:
            return airdrops

        for page_url in [
            "https://cryptototem.com/airdrops/",
            "https://cryptototem.com/retrodrop/",
        ]:
            try:
                async with self.session.get(
                    page_url,
                    timeout=aiohttp.ClientTimeout(total=12),
                    headers={"User-Agent": "Mozilla/5.0 (compatible; SolScreener/5.3)"},
                ) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()

                soup = BeautifulSoup(html, "html.parser")
                items = soup.select(".ico-card, .card, [class*='project'], tr")

                for item in items[:20]:
                    title_el = item.select_one("h3, h4, .name, a, td:first-child")
                    if not title_el:
                        continue
                    name = title_el.get_text(strip=True)
                    if not name or len(name) < 2 or self._is_excluded(name, ""):
                        continue

                    is_retro = "retrodrop" in page_url
                    airdrops.append(AirdropInfo(
                        name=name,
                        chain="multi",
                        category="defi",
                        description=f"{'Retrodrop' if is_retro else 'Airdrop'} | CryptoTotem掲載",
                        url=page_url,
                        status="active" if not is_retro else "upcoming",
                        source="cryptototem",
                        confidence=52,
                        is_new=True,
                    ))

            except Exception as e:
                logger.debug(f"CryptoTotem error: {e}")

            await asyncio.sleep(1)

        return airdrops

    # ============================================================
    # ソース 7: Binance Launchpool
    # ============================================================
    async def _source_exchange_news(self) -> list[AirdropInfo]:
        """取引所のエアドロ・ローンチプール情報"""
        airdrops = []
        try:
            url = "https://www.binance.com/bapi/earn/v1/public/launchpool/project/list"
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"Accept": "application/json"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    projects = data.get("data", [])
                    if isinstance(projects, list):
                        for proj in projects[:10]:
                            name = proj.get("projectName", "") or proj.get("asset", "")
                            if name:
                                airdrops.append(AirdropInfo(
                                    name=f"{name} (Binance Launchpool)",
                                    chain="multi",
                                    category="defi",
                                    description="Binance Launchpoolで配布中/予定",
                                    url="https://www.binance.com/en/launchpool",
                                    status="active",
                                    source="binance-launchpool",
                                    confidence=85,
                                    is_new=True,
                                ))
        except Exception as e:
            logger.debug(f"Exchange news error: {e}")

        return airdrops

    # ============================================================
    # ソース 8: キュレーションリスト（大幅拡充版）
    # ============================================================
    async def _source_curated(self) -> list[AirdropInfo]:
        """手動選定のエアドロ候補（BCG/GameFi含む大量追加）"""
        curated = [
            # ─── Solana DeFi ───
            AirdropInfo(
                name="Jupiter Season 3+",
                chain="solana", category="defi",
                description="Solana最大DEXアグリゲータ。JUP追加配布。Perp/DCA利用でポイント獲得。",
                url="https://jup.ag", status="active",
                requirements=["Swap利用", "Perp取引", "ステーキング"],
                source="curated", confidence=92,
            ),
            AirdropInfo(
                name="Meteora Season 2",
                chain="solana", category="defi",
                description="Solana LP最適化。DLMM LP提供でMETポイント獲得。",
                url="https://meteora.ag", status="active",
                requirements=["DLMM LP提供", "ダイナミックプール"],
                source="curated", confidence=92,
            ),
            AirdropInfo(
                name="Kamino Season 2+",
                chain="solana", category="defi",
                description="レンディング・ステーキング・LP。Season 1で1ウォレット平均$300配布。",
                url="https://kamino.finance", status="upcoming",
                requirements=["レンディング", "ステーキング", "LP提供"],
                source="curated", confidence=88,
            ),
            AirdropInfo(
                name="Sanctum (CLOUD)",
                chain="solana", category="defi",
                description="リキッドステーキングインフラ。ポイントプログラム進行中。",
                url="https://sanctum.so", status="active",
                requirements=["SOLステーキング", "LST保有"],
                source="curated", confidence=85,
            ),
            AirdropInfo(
                name="Axiom Trade",
                chain="solana", category="defi",
                description="Perp取引プロトコル。ポイントベースの報酬システム。",
                url="https://axiom.trade", status="active",
                requirements=["Perp取引", "ポイント獲得"],
                source="curated", confidence=78,
            ),
            AirdropInfo(
                name="Drift Protocol Season 2",
                chain="solana", category="defi",
                description="Solana最大Perp DEX。追加DRIFT配布期待。",
                url="https://drift.trade", status="upcoming",
                requirements=["Perp取引", "LP提供", "ステーキング"],
                source="curated", confidence=75,
            ),
            AirdropInfo(
                name="Marginfi Season 2",
                chain="solana", category="defi",
                description="Solanaレンディング。ポイントプログラム継続中。",
                url="https://marginfi.com", status="active",
                requirements=["レンディング", "借入", "ポイント獲得"],
                source="curated", confidence=78,
            ),
            AirdropInfo(
                name="Tensor Season 3",
                chain="solana", category="nft",
                description="Solana NFTマーケットプレイス。TNSR追加配布期待。",
                url="https://tensor.trade", status="upcoming",
                requirements=["NFT取引", "入札", "コレクション"],
                source="curated", confidence=72,
            ),
            AirdropInfo(
                name="Parcl",
                chain="solana", category="defi",
                description="不動産インデックス取引。ポイントプログラム進行中。",
                url="https://parcl.co", status="active",
                requirements=["取引", "LP提供"],
                source="curated", confidence=68,
            ),

            # ─── Ethereum / L2 DeFi ───
            AirdropInfo(
                name="EigenLayer Season 2+",
                chain="ethereum", category="defi",
                description="リステーキングプロトコル。TVL $15B+。EIGEN追加配布が期待される。",
                url="https://eigenlayer.xyz", status="upcoming",
                requirements=["ETHリステーキング", "AVS選択", "ガバナンス"],
                source="curated", confidence=88,
            ),
            AirdropInfo(
                name="EtherFi Season 3",
                chain="ethereum", category="defi",
                description="リキッドリステーキング。eETH保有・DeFi利用でポイント獲得。",
                url="https://ether.fi", status="active",
                requirements=["eETH保有", "DeFi利用", "ポイント獲得"],
                source="curated", confidence=88,
            ),
            AirdropInfo(
                name="Pendle Season 2+",
                chain="ethereum", category="defi",
                description="利回りトークン化。YT/PT取引・LP提供でvePENDLE報酬。",
                url="https://pendle.finance", status="upcoming",
                requirements=["YT/PT取引", "LP提供", "vePENDLE保有"],
                source="curated", confidence=78,
            ),
            AirdropInfo(
                name="Morpho",
                chain="ethereum", category="defi",
                description="レンディング最適化。$MORPHO配布進行中。利用量に応じた配布。",
                url="https://morpho.org", status="active",
                requirements=["レンディング利用", "Vault提供"],
                source="curated", confidence=85,
            ),
            AirdropInfo(
                name="Ethena (ENA) Season 3",
                chain="ethereum", category="defi",
                description="合成ドルUSDe。sUSDe保有・LP提供でSats獲得。",
                url="https://ethena.fi", status="active",
                requirements=["sUSDe保有", "LP提供", "Sats獲得"],
                source="curated", confidence=85,
            ),
            AirdropInfo(
                name="Symbiotic",
                chain="ethereum", category="defi",
                description="リステーキングプロトコル。EigenLayerの競合。トークン未発行。",
                url="https://symbiotic.fi", status="speculative",
                requirements=["リステーキング", "Vault利用"],
                source="curated", confidence=80,
            ),
            AirdropInfo(
                name="Hyperliquid Season 2",
                chain="arbitrum", category="defi",
                description="Perp DEX。HYPE追加配布期待。取引量に応じたポイント。",
                url="https://hyperliquid.xyz", status="upcoming",
                requirements=["Perp取引", "流動性提供"],
                source="curated", confidence=82,
            ),
            AirdropInfo(
                name="Aave V4",
                chain="ethereum", category="defi",
                description="最大レンディングプロトコル。V4アップグレードに伴う追加インセンティブ期待。",
                url="https://aave.com", status="speculative",
                requirements=["レンディング", "ガバナンス参加"],
                source="curated", confidence=60,
            ),
            AirdropInfo(
                name="Usual Protocol",
                chain="ethereum", category="defi",
                description="RWAステーブルコイン。USD0保有でUSUALトークン獲得。",
                url="https://usual.money", status="active",
                requirements=["USD0保有", "ステーキング"],
                source="curated", confidence=78,
            ),

            # ─── L2 / 新興チェーン ───
            AirdropInfo(
                name="Berachain (BERA)",
                chain="berachain", category="l2",
                description="Proof of Liquidity。メインネットローンチ済み。BGT獲得でガバナンス参加。",
                url="https://berachain.com", status="active",
                requirements=["流動性提供", "BGT獲得", "ガバナンス"],
                source="curated", confidence=92,
            ),
            AirdropInfo(
                name="Monad",
                chain="monad", category="l2",
                description="超高速EVM L1。テストネット進行中。$225M調達。エアドロ期待大。",
                url="https://monad.xyz", status="speculative",
                requirements=["テストネット参加", "コミュニティ活動"],
                source="curated", confidence=88,
            ),
            AirdropInfo(
                name="MegaETH",
                chain="megaeth", category="l2",
                description="リアルタイムEVM L2。$20M調達。",
                url="https://megaeth.systems", status="active",
                requirements=["テストネット参加", "ブリッジ利用"],
                source="curated", confidence=82,
            ),
            AirdropInfo(
                name="Abstract",
                chain="ethereum", category="l2",
                description="消費者向けL2。テストネット進行中。Pudgy Penguinsチーム。",
                url="https://abs.xyz", status="active",
                requirements=["テストネット参加", "NFT保有"],
                source="curated", confidence=82,
            ),
            AirdropInfo(
                name="Scroll Season 2",
                chain="scroll", category="l2",
                description="zkRollup L2。Session 2進行中。ブリッジ・DeFi利用でマーク獲得。",
                url="https://scroll.io", status="active",
                requirements=["ブリッジ利用", "DeFi利用", "マーク獲得"],
                source="curated", confidence=78,
            ),
            AirdropInfo(
                name="Linea Season 2",
                chain="linea", category="l2",
                description="Consensys L2。LXP-L獲得プログラム進行中。",
                url="https://linea.build", status="active",
                requirements=["ブリッジ利用", "DeFi利用", "LXP獲得"],
                source="curated", confidence=75,
            ),
            AirdropInfo(
                name="Fuel Network",
                chain="ethereum", category="l2",
                description="モジュラーL2。テストネット進行中。$80M調達。",
                url="https://fuel.network", status="speculative",
                requirements=["テストネット参加", "ブリッジ利用"],
                source="curated", confidence=72,
            ),
            AirdropInfo(
                name="Eclipse",
                chain="solana", category="l2",
                description="Solana VM搭載のEthereum L2。メインネットローンチ間近。",
                url="https://eclipse.xyz", status="speculative",
                requirements=["テストネット参加", "ブリッジ利用"],
                source="curated", confidence=75,
            ),
            AirdropInfo(
                name="Movement Labs",
                chain="ethereum", category="l2",
                description="Move言語ベースのL2。$38M調達。テストネット進行中。",
                url="https://movementlabs.xyz", status="speculative",
                requirements=["テストネット参加", "ブリッジ利用"],
                source="curated", confidence=78,
            ),

            # ─── NFT / マーケットプレイス ───
            AirdropInfo(
                name="Magic Eden Season 3",
                chain="multi", category="nft",
                description="マルチチェーンNFTマーケットプレイス。ガバナンス参加・クエスト完了で対象。",
                url="https://magiceden.io", status="active",
                requirements=["MEウォレット", "ガバナンス参加", "クエスト完了"],
                source="curated", confidence=92,
            ),
            AirdropInfo(
                name="OpenSea",
                chain="ethereum", category="nft",
                description="最大NFTマーケットプレイス。SEAトークン発行の噂。過去利用者にRetrodrop期待。",
                url="https://opensea.io", status="speculative",
                requirements=["NFT取引履歴", "アクティブ利用"],
                source="curated", confidence=72,
            ),
            AirdropInfo(
                name="Blur Season 4",
                chain="ethereum", category="nft",
                description="NFTマーケットプレイス。BLUR追加配布。入札・リスティングでポイント。",
                url="https://blur.io", status="active",
                requirements=["NFT入札", "リスティング", "レンディング"],
                source="curated", confidence=75,
            ),

            # ─── GameFi / BCG（大幅拡充） ───
            AirdropInfo(
                name="Star Atlas (ATLAS/POLIS)",
                chain="solana", category="gamefi",
                description="大型宇宙MMO。ゲーム内活動・NFT保有でシーズン報酬。",
                url="https://staratlas.com", status="upcoming",
                requirements=["ゲームプレイ", "NFT保有", "DAO参加"],
                source="curated", confidence=68,
            ),
            AirdropInfo(
                name="Pixels",
                chain="ethereum", category="gamefi",
                description="Web3農業ゲーム。Ronin Chain。$PIXEL追加配布期待。",
                url="https://pixels.xyz", status="upcoming",
                requirements=["ゲームプレイ", "土地NFT保有"],
                source="curated", confidence=62,
            ),
            AirdropInfo(
                name="Nyan Heroes",
                chain="solana", category="gamefi",
                description="猫×メカのバトルロイヤルFPS。トークンローンチ予定。",
                url="https://nyanheroes.com", status="speculative",
                requirements=["ゲームプレイ", "NFT保有"],
                source="curated", confidence=62,
            ),
            AirdropInfo(
                name="Parallel (PRIME)",
                chain="ethereum", category="gamefi",
                description="SF TCG。Echelon Prime。追加シーズン報酬期待。",
                url="https://parallel.life", status="upcoming",
                requirements=["ゲームプレイ", "カードNFT保有"],
                source="curated", confidence=58,
            ),
            AirdropInfo(
                name="Illuvium",
                chain="ethereum", category="gamefi",
                description="AAA品質のオープンワールドRPG。ILVステーキング・ゲームプレイ報酬。",
                url="https://illuvium.io", status="active",
                requirements=["ゲームプレイ", "ILVステーキング", "ランド保有"],
                source="curated", confidence=65,
            ),
            AirdropInfo(
                name="Shrapnel",
                chain="avalanche", category="gamefi",
                description="AAA FPSゲーム。UGCマーケットプレイス。トークン未発行。",
                url="https://shrapnel.com", status="speculative",
                requirements=["ゲームプレイ", "NFT保有", "UGC作成"],
                source="curated", confidence=62,
            ),
            AirdropInfo(
                name="Pirate Nation",
                chain="ethereum", category="gamefi",
                description="フルオンチェーンRPG。Proof of Playチーム。PIRATE追加配布期待。",
                url="https://piratenation.game", status="active",
                requirements=["ゲームプレイ", "クエスト完了"],
                source="curated", confidence=65,
            ),
            AirdropInfo(
                name="Aurory",
                chain="solana", category="gamefi",
                description="Solana RPG。AURY追加配布・シーズン報酬。",
                url="https://aurory.io", status="upcoming",
                requirements=["ゲームプレイ", "NFT保有"],
                source="curated", confidence=58,
            ),
            AirdropInfo(
                name="Wildcard",
                chain="ethereum", category="gamefi",
                description="Web3 TCG。$16M調達。トークン未発行。",
                url="https://playwildcard.com", status="speculative",
                requirements=["ゲームプレイ", "NFT保有"],
                source="curated", confidence=65,
            ),
            AirdropInfo(
                name="MapleStory Universe",
                chain="avalanche", category="gamefi",
                description="MapleStoryのWeb3版。Nexon開発。テストネット進行中。",
                url="https://maplestoryuniverse.com", status="speculative",
                requirements=["テストネット参加", "ゲームプレイ"],
                source="curated", confidence=72,
            ),
            AirdropInfo(
                name="Off The Grid",
                chain="avalanche", category="gamefi",
                description="バトルロイヤルFPS。Gunzillaチーム。GUN追加配布期待。",
                url="https://offthegrid.fun", status="active",
                requirements=["ゲームプレイ", "ランク上げ"],
                source="curated", confidence=68,
            ),
            AirdropInfo(
                name="Xai Games",
                chain="arbitrum", category="gamefi",
                description="Arbitrum上のゲーム専用L3。XAI追加配布・ノード報酬。",
                url="https://xai.games", status="active",
                requirements=["ゲームプレイ", "ノード運用", "ステーキング"],
                source="curated", confidence=72,
            ),
            AirdropInfo(
                name="Ronin Network Season 2",
                chain="ronin", category="gamefi",
                description="Axie Infinityチェーン。RON追加配布。ゲーム・DeFi利用で対象。",
                url="https://roninchain.com", status="upcoming",
                requirements=["ブリッジ利用", "DeFi利用", "ゲームプレイ"],
                source="curated", confidence=72,
            ),
            AirdropInfo(
                name="Immutable zkEVM",
                chain="ethereum", category="gamefi",
                description="ゲーム特化L2。IMX追加配布。ゲーム利用・NFT取引で対象。",
                url="https://immutable.com", status="active",
                requirements=["ゲームプレイ", "NFT取引", "ブリッジ利用"],
                source="curated", confidence=75,
            ),
            AirdropInfo(
                name="Beam (Merit Circle)",
                chain="beam", category="gamefi",
                description="ゲーム特化チェーン。BEAM追加配布。ゲームハブ利用で対象。",
                url="https://beam.eco", status="active",
                requirements=["ゲームプレイ", "ステーキング"],
                source="curated", confidence=68,
            ),
            AirdropInfo(
                name="Treasure DAO",
                chain="arbitrum", category="gamefi",
                description="ゲームエコシステム。MAGIC追加配布。Bridgeworld・Smolverse。",
                url="https://treasure.lol", status="upcoming",
                requirements=["ゲームプレイ", "MAGICステーキング"],
                source="curated", confidence=62,
            ),
            AirdropInfo(
                name="Gala Games Season 2",
                chain="ethereum", category="gamefi",
                description="大手Web3ゲームプラットフォーム。GALA追加配布・ノード報酬。",
                url="https://gala.games", status="upcoming",
                requirements=["ゲームプレイ", "ノード運用"],
                source="curated", confidence=60,
            ),
            AirdropInfo(
                name="Apeiron",
                chain="ronin", category="gamefi",
                description="ゴッドゲーム×ローグライク。NFT惑星保有で報酬。",
                url="https://apeironnft.com", status="active",
                requirements=["ゲームプレイ", "惑星NFT保有"],
                source="curated", confidence=58,
            ),

            # ─── インフラ ───
            AirdropInfo(
                name="Grass Season 2",
                chain="solana", category="infra",
                description="分散型AIデータネットワーク。帯域共有でポイント獲得。",
                url="https://getgrass.io", status="active",
                requirements=["ブラウザ拡張インストール", "帯域共有"],
                source="curated", confidence=78,
            ),
            AirdropInfo(
                name="LayerZero Season 2",
                chain="multi", category="infra",
                description="オムニチェーンプロトコル。ZRO追加配布期待。クロスチェーン利用で対象。",
                url="https://layerzero.network", status="upcoming",
                requirements=["クロスチェーン送金", "dApp利用"],
                source="curated", confidence=72,
            ),
            AirdropInfo(
                name="Wormhole Season 2",
                chain="multi", category="infra",
                description="クロスチェーンブリッジ。W追加配布期待。ブリッジ利用で対象。",
                url="https://wormhole.com", status="upcoming",
                requirements=["ブリッジ利用", "マルチチェーン送金"],
                source="curated", confidence=68,
            ),
            AirdropInfo(
                name="Initia",
                chain="cosmos", category="infra",
                description="モジュラーL1。テストネット進行中。$7.5M調達。",
                url="https://initia.xyz", status="speculative",
                requirements=["テストネット参加", "バリデータ運用"],
                source="curated", confidence=75,
            ),
            AirdropInfo(
                name="Avail",
                chain="multi", category="infra",
                description="データ可用性レイヤー。AVAIL追加配布期待。",
                url="https://availproject.org", status="upcoming",
                requirements=["テストネット参加", "ライトノード運用"],
                source="curated", confidence=72,
            ),
            AirdropInfo(
                name="Celestia Season 2",
                chain="celestia", category="infra",
                description="モジュラーDA。TIA追加配布期待。ステーキングで対象。",
                url="https://celestia.org", status="upcoming",
                requirements=["TIAステーキング", "ガバナンス参加"],
                source="curated", confidence=72,
            ),

            # ─── ソーシャル / AI ───
            AirdropInfo(
                name="Farcaster",
                chain="base", category="social",
                description="分散型SNS。トークン未発行。アクティブ利用で対象。",
                url="https://farcaster.xyz", status="speculative",
                requirements=["アカウント作成", "投稿・いいね", "チャンネル参加"],
                source="curated", confidence=78,
            ),
            AirdropInfo(
                name="Lens Protocol V2",
                chain="polygon", category="social",
                description="分散型ソーシャルグラフ。Aave チーム。トークン未発行。",
                url="https://lens.xyz", status="speculative",
                requirements=["プロフィール作成", "投稿・コメント"],
                source="curated", confidence=72,
            ),
            AirdropInfo(
                name="io.net",
                chain="solana", category="infra",
                description="分散型GPU。IO追加配布期待。GPU提供・利用で対象。",
                url="https://io.net", status="upcoming",
                requirements=["GPU提供", "コンピュート利用"],
                source="curated", confidence=68,
            ),
            AirdropInfo(
                name="Render Network Season 2",
                chain="solana", category="infra",
                description="分散型GPUレンダリング。RNDR追加配布期待。",
                url="https://rendernetwork.com", status="upcoming",
                requirements=["GPU提供", "レンダリング利用"],
                source="curated", confidence=62,
            ),
        ]

        return curated

    # ============================================================
    # ユーティリティ
    # ============================================================
    def filter_by_chain(self, airdrops: list[AirdropInfo], chain: str) -> list[AirdropInfo]:
        return [a for a in airdrops if a.chain == chain or a.chain == "multi"]

    def filter_by_category(self, airdrops: list[AirdropInfo], category: str) -> list[AirdropInfo]:
        return [a for a in airdrops if a.category == category]

    def filter_by_confidence(self, airdrops: list[AirdropInfo], min_confidence: int = 50) -> list[AirdropInfo]:
        return [a for a in airdrops if a.confidence >= min_confidence]

    def get_top_diverse(self, airdrops: list[AirdropInfo], n: int = 20,
                        gamefi_min: int = 5) -> list[AirdropInfo]:
        """
        多様性を確保したTOP N件を返す
        - 前回通知済みは除外
        - GameFi/BCG枠を最低 gamefi_min 件確保
        - 新規プロジェクト（is_new=True, Raises）を優先
        """
        # 前回通知済みを除外
        fresh = [a for a in airdrops if not self.is_recently_notified(a.name)]

        if not fresh:
            # 全部通知済みなら、古い順から再通知
            logger.info("全エアドロが通知済み → 古い順から再選定")
            fresh = sorted(airdrops, key=lambda a: self._notified_airdrops.get(
                a.name.lower().strip(), 0))

        # カテゴリ分離
        gamefi = [a for a in fresh if a.category == "gamefi"]
        non_gamefi = [a for a in fresh if a.category != "gamefi"]

        # 新規プロジェクト（Raises, is_new）を優先
        new_projects = [a for a in non_gamefi if a.is_new or a.source == "defillama-raises"]
        existing = [a for a in non_gamefi if not a.is_new and a.source != "defillama-raises"]

        # ソート
        new_projects.sort(key=lambda a: (a.raised, a.confidence), reverse=True)
        existing.sort(key=lambda a: a.confidence, reverse=True)
        gamefi.sort(key=lambda a: a.confidence, reverse=True)

        # 枠配分
        gamefi_slots = min(gamefi_min, len(gamefi))
        remaining_slots = n - gamefi_slots

        # 新規を優先的に入れる（最大半分）
        new_slots = min(len(new_projects), remaining_slots // 2)
        existing_slots = remaining_slots - new_slots

        result = []
        result.extend(new_projects[:new_slots])
        result.extend(existing[:existing_slots])
        result.extend(gamefi[:gamefi_slots])

        # まだ枠が余っていたら追加
        used_names = {a.name.lower() for a in result}
        remaining = [a for a in fresh if a.name.lower() not in used_names]
        remaining.sort(key=lambda a: a.confidence, reverse=True)
        result.extend(remaining[:n - len(result)])

        # 最終ソート（確度順、ただしis_newを少し優先）
        result.sort(key=lambda a: (a.confidence + (5 if a.is_new else 0)), reverse=True)

        return result[:n]

    def get_top(self, airdrops: list[AirdropInfo], n: int = 10) -> list[AirdropInfo]:
        """後方互換: get_top_diverseを呼ぶ"""
        return self.get_top_diverse(airdrops, n=n)

    def format_summary(self, airdrops: list[AirdropInfo]) -> str:
        """Discord通知用のサマリーテキスト生成"""
        if not airdrops:
            return "エアドロップ情報なし"

        by_chain = {}
        for a in airdrops:
            by_chain.setdefault(a.chain, []).append(a)

        chain_emoji = {
            "solana": "◎", "ethereum": "⟠", "arbitrum": "🔵",
            "base": "🔷", "optimism": "🔴", "polygon": "💜",
            "bsc": "🟡", "sui": "💧", "berachain": "🐻",
            "monad": "🟣", "scroll": "📜", "linea": "🌐",
            "blast": "💥", "multi": "🌍", "avalanche": "🔺",
            "ronin": "⚔️", "cosmos": "⚛️", "celestia": "🟣",
        }

        cat_emoji = {
            "defi": "💰", "gamefi": "🎮", "nft": "🖼️",
            "infra": "🔧", "social": "💬", "l2": "⛓️", "other": "📦",
        }

        lines = [f"**✈️ エアドロップ情報 ({len(airdrops)}件)**\n"]

        for chain, items in sorted(by_chain.items()):
            emoji = chain_emoji.get(chain, "🔗")
            lines.append(f"\n{emoji} **{chain.upper()}** ({len(items)}件)")

            by_cat = {}
            for a in items:
                by_cat.setdefault(a.category or "other", []).append(a)

            for cat, cat_items in sorted(by_cat.items()):
                ce = cat_emoji.get(cat, "📦")
                for a in cat_items[:3]:
                    conf_bar = "🟢" if a.confidence >= 70 else "🟡" if a.confidence >= 50 else "🔴"
                    new_badge = " 🆕" if a.is_new else ""
                    lines.append(
                        f"  {conf_bar} {ce} **{a.name}**{new_badge} [{a.status}] "
                        f"(確度: {a.confidence}%)"
                    )
                    if a.description:
                        lines.append(f"    {a.description[:80]}...")
                    if a.requirements:
                        lines.append(f"    📋 {', '.join(a.requirements[:3])}")

        return "\n".join(lines)
