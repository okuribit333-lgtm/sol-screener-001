"""
エアドロップ情報自動収集 v5.1 — マルチチェーン対応（品質フィルタ強化版）

修正点 (v5.0 → v5.1):
  - CEX（取引所）カテゴリを完全除外
  - 大手ブリッジ / ラップドトークンを除外
  - 既にトークン発行済みの有名プロジェクトを除外リストに追加
  - キュレーションリストの確度を最優先
  - DeFiLlama DeFi ソースの品質フィルタを大幅強化
  - GameFi ソースからトークン発行済みを除外
  - 最終フィルタで「本当にエアドロしそうか」を再判定

データソース:
  1. DeFiLlama API — 全チェーン DeFi プロトコル（トークン未発行 & CEX除外）
  2. DeFiLlama API — GameFi / ゲーム系プロトコル特化（トークン未発行のみ）
  3. CoinGecko API — 新規・低MC トークン（ポイント制検出）
  4. AirdropAlert.com スクレイピング — 全チェーン対応エアドロ
  5. Airdrops.io スクレイピング — 全チェーン対応エアドロ
  6. CryptoTotem スクレイピング — Retrodrop / テストネット情報
  7. DeFiLlama Raises API — 最近の資金調達プロジェクト（エアドロ予測）
  8. 手動キュレーション — 2026年注目エアドロ（マルチチェーン）
  9. Twitter/Nitter 監視 — プロトコル公式のエアドロ言及検出
  10. Binance Launchpool — 取引所のエアドロ情報

全て無料API / スクレイピングで動作（APIキー不要）
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# ============================================================
# データクラス
# ============================================================
@dataclass
class AirdropInfo:
    """エアドロップ情報"""
    name: str
    chain: str = "multi"          # "solana" / "ethereum" / "arbitrum" / "base" / "multi" etc.
    category: str = ""            # "defi" / "gamefi" / "nft" / "infra" / "social" / "l2"
    description: str = ""
    url: str = ""
    status: str = "active"        # active / upcoming / ended / speculative
    estimated_value: str = ""
    requirements: list = field(default_factory=list)
    source: str = ""
    confidence: int = 50          # 0-100: エアドロ確度スコア

    def __repr__(self):
        return f"<Airdrop: {self.name} | {self.chain} | {self.category} | conf={self.confidence}>"


# ============================================================
# チェーン判定ヘルパー
# ============================================================
CHAIN_ALIASES = {
    "Solana": "solana", "Ethereum": "ethereum", "Arbitrum": "arbitrum",
    "Optimism": "optimism", "Base": "base", "Polygon": "polygon",
    "BSC": "bsc", "Binance": "bsc", "Avalanche": "avalanche",
    "Sui": "sui", "Aptos": "aptos", "Sei": "sei",
    "Cosmos": "cosmos", "Near": "near", "Fantom": "fantom",
    "zkSync Era": "zksync", "Linea": "linea", "Scroll": "scroll",
    "Blast": "blast", "Starknet": "starknet", "Manta": "manta",
    "Mantle": "mantle", "Mode": "mode", "Berachain": "berachain",
    "Monad": "monad", "MegaETH": "megaeth",
}


def _detect_chain(chains: list[str]) -> str:
    """チェーンリストから主要チェーンを判定"""
    if not chains:
        return "multi"
    for c in chains:
        if c in CHAIN_ALIASES:
            return CHAIN_ALIASES[c]
    return chains[0].lower() if chains else "multi"


# ============================================================
# 除外リスト（エアドロしない / 既にトークン発行済み）
# ============================================================
# CEX（中央集権取引所）— 絶対にエアドロしない
EXCLUDED_CATEGORIES = {
    "cex", "centralized exchange", "exchange",
}

# 名前ベースの除外リスト（大手取引所・ブリッジ・ラップドトークン）
EXCLUDED_NAMES = {
    # 取引所
    "okx", "binance", "bybit", "bitfinex", "robinhood", "gemini",
    "mexc", "kucoin", "htx", "huobi", "crypto.com", "crypto-com",
    "bitget", "gate.io", "deribit", "kraken", "coinbase", "bitstamp",
    "upbit", "bithumb", "bitflyer", "poloniex", "lbank", "whitebit",
    "bitmart", "phemex", "backpack exchange", "hashkey exchange",
    # ブリッジ / ラップドトークン
    "wbtc", "coinbase bridge", "base bridge", "binance bitcoin",
    "arbitrum bridge", "optimism bridge", "polygon bridge",
    "usdt0", "multichain", "anyswap", "cbridge",
    # ステーブルコイン
    "circle usyc", "tether", "usdc", "usdt", "dai", "frax",
    "binance staked eth", "coinbase staked eth",
    # 既にトークン発行済みの有名プロジェクト
    "ssv network",  # SSV
    "steakhouse financial",
}

# DeFiLlama カテゴリの除外（エアドロ期待が低い）
EXCLUDED_DEFI_CATEGORIES = {
    "cex", "bridge", "cross-chain", "canonical bridge",
    "stablecoin", "stablecoins", "rwa", "insurance",
    "algo-stables", "synthetics",
}


# ============================================================
# メインスキャナー
# ============================================================
class AirdropScanner:
    """エアドロップ情報を10ソースから収集（マルチチェーン対応・品質フィルタ強化版）"""

    # ── Nitter インスタンス ──
    NITTER_INSTANCES = [
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
        "https://nitter.net",
    ]

    # ── エアドロ関連キーワード ──
    AIRDROP_KEYWORDS = [
        "airdrop", "claim", "token distribution", "retroactive",
        "points program", "rewards", "season", "drop", "genesis",
        "farming", "quest", "earn", "incentive", "testnet",
    ]

    # ── 注目プロトコル監視リスト（マルチチェーン） ──

    # Solana DeFi 系
    SOL_DEFI = [
        "jupiter", "marginfi", "kamino", "drift", "tensor",
        "jito", "sanctum", "phantom", "backpack", "zeta",
        "parcl", "meteora", "marinade", "raydium", "orca",
        "solend", "mango", "lifinity", "axiom", "hylo",
        "vybe", "solayer", "flash", "symmetry", "hawksight",
    ]

    # Ethereum / L2 DeFi 系
    ETH_DEFI = [
        "eigenlayer", "etherfi", "pendle", "morpho", "aave",
        "lido", "renzo", "kelp", "puffer", "swell",
        "ethena", "symbiotic", "karak", "mellow",
    ]

    # L2 / 新興チェーン
    L2_CHAINS = [
        "zksync", "linea", "scroll", "blast", "starknet",
        "manta", "mantle", "mode", "berachain", "monad",
        "megaeth", "abstract", "soneium", "taiko", "fuel",
    ]

    # ゲーム / GameFi 系（マルチチェーン）
    GAMEFI_PROTOCOLS = [
        "star atlas", "aurory", "defi land", "genopets",
        "stepn", "nyan heroes", "pixels", "illuvium",
        "big time", "shrapnel", "parallel", "gods unchained",
        "axie infinity", "the sandbox", "decentraland",
        "gala games", "immutable x", "ronin",
        "treasure dao", "beam", "xai",
    ]

    # NFT / マーケットプレイス系
    NFT_PROTOCOLS = [
        "magic eden", "tensor", "opensea", "blur",
        "foundation", "zora", "manifold",
    ]

    # インフラ / ツール系
    INFRA_PROTOCOLS = [
        "helius", "grass", "openloop", "assisterr", "krain",
        "layerzero", "wormhole", "across", "hop",
        "chainlink", "pyth", "switchboard",
    ]

    # Twitter監視用統合リスト
    ALL_PROTOCOLS = SOL_DEFI[:10] + ETH_DEFI[:8] + L2_CHAINS[:8] + GAMEFI_PROTOCOLS[:8] + NFT_PROTOCOLS[:4]

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    # ============================================================
    # メイン: 全ソーススキャン
    # ============================================================
    async def scan_all(self) -> list[AirdropInfo]:
        """全10ソースからエアドロ情報を収集（マルチチェーン）"""
        results = await asyncio.gather(
            self._source_defillama_defi(),
            self._source_defillama_gamefi(),
            self._source_coingecko(),
            self._source_airdropalert(),
            self._source_airdrops_io(),
            self._source_cryptototem(),
            self._source_defillama_raises(),
            self._source_curated_list(),
            self._source_twitter(),
            self._source_exchange_news(),
            return_exceptions=True,
        )

        all_airdrops = []
        source_names = [
            "DeFiLlama-DeFi", "DeFiLlama-GameFi", "CoinGecko",
            "AirdropAlert", "Airdrops.io", "CryptoTotem",
            "DeFiLlama-Raises", "Curated", "Twitter", "ExchangeNews",
        ]

        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning(f"エアドロソース [{source_names[i]}] エラー: {r}")
            elif r:
                all_airdrops.extend(r)
                logger.info(f"  [{source_names[i]}] {len(r)}件")

        # 重複排除（名前の正規化ベース）
        seen = set()
        unique = []
        for a in all_airdrops:
            key = re.sub(r'[^a-z0-9]', '', a.name.lower())
            if key and key not in seen:
                seen.add(key)
                unique.append(a)

        # 最終品質フィルタ: 除外リストに引っかかるものを排除
        filtered = self._apply_quality_filter(unique)

        # 確度スコア降順でソート
        filtered.sort(key=lambda a: a.confidence, reverse=True)

        logger.info(
            f"✈️ エアドロスキャン完了: {len(filtered)}件（収集{len(all_airdrops)} → 重複排除{len(unique)} → 品質フィルタ{len(filtered)}）"
        )
        return filtered

    def _apply_quality_filter(self, airdrops: list[AirdropInfo]) -> list[AirdropInfo]:
        """最終品質フィルタ: ゴミ情報を排除"""
        filtered = []
        for a in airdrops:
            name_lower = a.name.lower().strip()

            # 除外名リストチェック
            if any(exc in name_lower for exc in EXCLUDED_NAMES):
                continue

            # 名前が短すぎる / 長すぎる
            if len(a.name.strip()) < 3 or len(a.name.strip()) > 80:
                continue

            # status が "ended" のものは除外
            if a.status == "ended":
                continue

            # 確度が極端に低いものは除外
            if a.confidence < 30:
                continue

            filtered.append(a)

        return filtered

    # ============================================================
    # ソース 1: DeFiLlama — 全チェーン DeFi（トークン未発行・CEX除外）
    # ============================================================
    async def _source_defillama_defi(self) -> list[AirdropInfo]:
        """DeFiLlama: 全チェーンのDeFiプロトコルでトークン未発行 → エアドロ期待
        
        v5.1 改善:
          - CEX（取引所）カテゴリを完全除外
          - ブリッジ / ラップドトークン / ステーブルコインを除外
          - 名前ベースの除外リストを適用
          - TVL閾値を$5Mに引き上げ（ノイズ削減）
          - DeFi特化カテゴリのみ通過
        """
        airdrops = []
        try:
            url = "https://api.llama.fi/protocols"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return airdrops
                data = await resp.json()

            # エアドロ期待が高いDeFiカテゴリ
            GOOD_CATEGORIES = {
                "dexes", "lending", "yield", "derivatives", "liquid staking",
                "yield aggregator", "farm", "leveraged farming",
                "liquidity manager", "prediction market",
                "options", "perpetuals", "launchpad",
                "restaking", "liquid restaking",
                "nft marketplace", "nft lending",
            }

            for protocol in data:
                chains = protocol.get("chains", [])
                if not chains:
                    continue

                name = protocol.get("name", "")
                symbol = protocol.get("symbol", "")
                tvl = protocol.get("tvl", 0) or 0
                category = protocol.get("category", "").lower()
                slug = protocol.get("slug", "").lower()

                # ── 除外フィルタ ──

                # 1. CEX / 取引所カテゴリを除外
                if category in EXCLUDED_CATEGORIES or "cex" in category:
                    continue

                # 2. エアドロ期待が低いカテゴリを除外
                if category in EXCLUDED_DEFI_CATEGORIES:
                    continue

                # 3. 名前ベースの除外
                name_lower = name.lower()
                if any(exc in name_lower for exc in EXCLUDED_NAMES):
                    continue

                # 4. トークン未発行判定
                has_token = symbol and symbol != "-" and symbol.strip() != ""
                if has_token:
                    continue

                # 5. TVL $5M以上（ノイズ削減のため閾値を引き上げ）
                if tvl < 5_000_000:
                    continue

                # 6. エアドロ期待が高いカテゴリのみ通過
                if category not in GOOD_CATEGORIES:
                    # カテゴリが不明でもTVL $50M以上なら通す
                    if tvl < 50_000_000:
                        continue

                chain = _detect_chain(chains)

                # カテゴリ判定
                cat = "defi"
                if any(g in category for g in ["game", "gaming", "play"]):
                    cat = "gamefi"
                elif any(n in category for n in ["nft", "collectible"]):
                    cat = "nft"

                # 確度スコア: TVLが高いほど確度UP
                conf = 50
                if tvl > 500_000_000:
                    conf = 90
                elif tvl > 100_000_000:
                    conf = 85
                elif tvl > 50_000_000:
                    conf = 80
                elif tvl > 20_000_000:
                    conf = 70
                elif tvl > 10_000_000:
                    conf = 60

                chain_display = ", ".join(chains[:3])
                if len(chains) > 3:
                    chain_display += f" +{len(chains)-3}"

                airdrops.append(AirdropInfo(
                    name=name,
                    chain=chain,
                    category=cat,
                    description=f"TVL: ${tvl:,.0f} | {chain_display} | {category} | トークン未発行",
                    url=protocol.get("url", ""),
                    status="speculative",
                    estimated_value=f"TVL ${tvl / 1e6:.1f}M",
                    source="defillama",
                    confidence=conf,
                ))

        except Exception as e:
            logger.debug(f"DeFiLlama DeFi error: {e}")

        return airdrops

    # ============================================================
    # ソース 2: DeFiLlama — GameFi / ゲーム系特化（トークン未発行のみ）
    # ============================================================
    async def _source_defillama_gamefi(self) -> list[AirdropInfo]:
        """DeFiLlama: 全チェーンのゲーム系プロトコル（トークン未発行のみ）"""
        airdrops = []
        try:
            url = "https://api.llama.fi/protocols"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return airdrops
                data = await resp.json()

            game_keywords = [
                "game", "gaming", "play", "metaverse", "virtual world",
                "p2e", "play-to-earn", "move-to-earn",
            ]

            for protocol in data:
                chains = protocol.get("chains", [])
                name = protocol.get("name", "")
                category = protocol.get("category", "").lower()
                desc = protocol.get("description", "").lower()
                tvl = protocol.get("tvl", 0) or 0
                symbol = protocol.get("symbol", "")

                is_game = (
                    any(kw in category for kw in game_keywords)
                    or any(kw in desc for kw in game_keywords)
                )

                if not is_game:
                    continue

                # v5.1: トークン発行済みは除外
                has_token = symbol and symbol != "-" and symbol.strip() != ""
                if has_token:
                    continue

                # 名前ベースの除外
                name_lower = name.lower()
                if any(exc in name_lower for exc in EXCLUDED_NAMES):
                    continue

                chain = _detect_chain(chains)

                conf = 55
                if tvl > 10_000_000:
                    conf = 80
                elif tvl > 5_000_000:
                    conf = 70
                elif tvl > 1_000_000:
                    conf = 60

                airdrops.append(AirdropInfo(
                    name=f"{name} (GameFi)",
                    chain=chain,
                    category="gamefi",
                    description=(
                        f"ゲーム系 | TVL: ${tvl:,.0f} | "
                        f"トークン未発行 | "
                        f"{protocol.get('description', '')[:100]}"
                    ),
                    url=protocol.get("url", ""),
                    status="speculative",
                    estimated_value=f"TVL ${tvl / 1e6:.1f}M" if tvl > 0 else "不明",
                    source="defillama-gamefi",
                    confidence=min(90, conf),
                ))

        except Exception as e:
            logger.debug(f"DeFiLlama GameFi error: {e}")

        return airdrops

    # ============================================================
    # ソース 3: CoinGecko — 新規トークン（全チェーン）
    # ============================================================
    async def _source_coingecko(self) -> list[AirdropInfo]:
        """CoinGecko: 新規・低MCトークンからエアドロ候補を検出"""
        airdrops = []
        try:
            categories = [
                ("solana-ecosystem", "solana"),
                ("arbitrum-ecosystem", "arbitrum"),
                ("base-ecosystem", "base"),
                ("layer-2", "l2"),
            ]

            for cat_id, chain_label in categories:
                url = (
                    f"https://api.coingecko.com/api/v3/coins/markets"
                    f"?vs_currency=usd&category={cat_id}"
                    f"&order=market_cap_asc&per_page=30&page=1"
                )
                try:
                    async with self.session.get(
                        url, timeout=aiohttp.ClientTimeout(total=15),
                        headers={"Accept": "application/json"},
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()

                    for coin in data:
                        name = coin.get("name", "")
                        symbol = coin.get("symbol", "").upper()
                        mc = coin.get("market_cap", 0) or 0
                        vol = coin.get("total_volume", 0) or 0

                        # ポイント制・エアドロ系の特徴を検出
                        name_lower = name.lower()
                        is_airdrop_related = any(
                            kw in name_lower for kw in
                            ["point", "reward", "earn", "season", "quest"]
                        )

                        if mc < 50_000_000 and vol > 10_000:
                            conf = 30
                            if is_airdrop_related:
                                conf += 20
                            if mc < 5_000_000:
                                conf += 10

                            airdrops.append(AirdropInfo(
                                name=f"{name} ({symbol})",
                                chain=chain_label,
                                category="defi",
                                description=f"MC: ${mc:,.0f} | Vol: ${vol:,.0f} | {chain_label}",
                                status="speculative",
                                estimated_value=f"MC ${mc / 1e6:.1f}M",
                                source="coingecko",
                                confidence=min(80, conf),
                            ))
                except Exception:
                    continue

                await asyncio.sleep(1.5)  # CoinGecko レート制限対策

        except Exception as e:
            logger.debug(f"CoinGecko error: {e}")

        return airdrops

    # ============================================================
    # ソース 4: AirdropAlert.com スクレイピング（全チェーン）
    # ============================================================
    async def _source_airdropalert(self) -> list[AirdropInfo]:
        """AirdropAlert: 全チェーンのエアドロ情報を取得"""
        airdrops = []
        try:
            url = "https://airdropalert.com/new-airdrops"
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            ) as resp:
                if resp.status != 200:
                    return airdrops
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")

            cards = soup.select("div.airdrop-card, div.card, div[class*='airdrop']")
            if not cards:
                cards = soup.select("h4, h3, .project-name")

            for card in cards[:30]:
                text = card.get_text(strip=True)
                if not text or len(text) < 3:
                    continue

                name = text.split("⇆")[0].split("KYC")[0].split("APP")[0].split("OTH")[0].strip()
                if len(name) > 60:
                    name = name[:60]
                if len(name) < 2:
                    continue

                desc_parts = text.replace(name, "").strip()[:150]

                chain = "multi"
                text_lower = text.lower()
                for chain_name, chain_id in CHAIN_ALIASES.items():
                    if chain_name.lower() in text_lower:
                        chain = chain_id
                        break

                cat = "defi"
                if any(kw in text_lower for kw in ["game", "play", "nft game"]):
                    cat = "gamefi"
                elif any(kw in text_lower for kw in ["nft", "collectible", "art"]):
                    cat = "nft"
                elif any(kw in text_lower for kw in ["layer", "chain", "bridge", "oracle"]):
                    cat = "infra"

                airdrops.append(AirdropInfo(
                    name=name,
                    chain=chain,
                    category=cat,
                    description=desc_parts if desc_parts else "AirdropAlertで掲載中",
                    url="https://airdropalert.com",
                    status="active",
                    source="airdropalert",
                    confidence=60,
                ))

        except Exception as e:
            logger.debug(f"AirdropAlert error: {e}")

        return airdrops

    # ============================================================
    # ソース 5: Airdrops.io スクレイピング（全チェーン）
    # ============================================================
    async def _source_airdrops_io(self) -> list[AirdropInfo]:
        """Airdrops.io: 全チェーンのエアドロ情報を取得"""
        airdrops = []
        try:
            url = "https://airdrops.io/"
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            ) as resp:
                if resp.status != 200:
                    return airdrops
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")

            items = soup.select("a[href*='/airdrop/'], .airdrop-item, .card")
            if not items:
                items = soup.select("h3, h4, .title")

            for item in items[:30]:
                text = item.get_text(strip=True)
                if not text or len(text) < 3:
                    continue

                name = text[:60].strip()
                if len(name) < 2:
                    continue

                href = item.get("href", "")
                item_url = f"https://airdrops.io{href}" if href.startswith("/") else href

                chain = "multi"
                text_lower = text.lower()
                for chain_name, chain_id in CHAIN_ALIASES.items():
                    if chain_name.lower() in text_lower:
                        chain = chain_id
                        break

                airdrops.append(AirdropInfo(
                    name=name,
                    chain=chain,
                    category="defi",
                    description="Airdrops.ioで掲載中",
                    url=item_url if item_url else "https://airdrops.io",
                    status="active",
                    source="airdrops.io",
                    confidence=55,
                ))

        except Exception as e:
            logger.debug(f"Airdrops.io error: {e}")

        return airdrops

    # ============================================================
    # ソース 6: CryptoTotem スクレイピング（Retrodrop / テストネット）
    # ============================================================
    async def _source_cryptototem(self) -> list[AirdropInfo]:
        """CryptoTotem: Retrodrop / テストネット / エアドロ情報"""
        airdrops = []
        try:
            url = "https://cryptototem.com/airdrops/"
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            ) as resp:
                if resp.status != 200:
                    return airdrops
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")

            rows = soup.select("tr, .airdrop-item, .project-row")
            if not rows:
                rows = soup.select("a[href*='airdrop']")

            for row in rows[:40]:
                text = row.get_text(strip=True)
                if not text or len(text) < 5:
                    continue

                links = row.select("a")
                name = ""
                item_url = ""
                for link in links:
                    link_text = link.get_text(strip=True)
                    if "airdrop" in link_text.lower() and len(link_text) > 3:
                        name = link_text.replace(" airdrop", "").replace(" Airdrop", "").strip()
                        href = link.get("href", "")
                        item_url = f"https://cryptototem.com{href}" if href.startswith("/") else href
                        break

                if not name or len(name) < 2:
                    continue

                cat = "defi"
                text_lower = text.lower()
                if any(kw in text_lower for kw in ["gaming", "game"]):
                    cat = "gamefi"
                elif any(kw in text_lower for kw in ["nft", "collectible"]):
                    cat = "nft"
                elif any(kw in text_lower for kw in ["blockchain", "infrastructure", "oracle"]):
                    cat = "infra"
                elif any(kw in text_lower for kw in ["ai", "data", "machine learning"]):
                    cat = "infra"

                chain = "multi"
                for chain_name, chain_id in CHAIN_ALIASES.items():
                    if chain_name.lower() in text_lower:
                        chain = chain_id
                        break

                conf = 55
                if "$" in text:
                    money_match = re.search(r'\$(\d+(?:\.\d+)?)\s*[MB]', text)
                    if money_match:
                        amount = float(money_match.group(1))
                        if "B" in text[money_match.end()-1:money_match.end()]:
                            amount *= 1000
                        if amount > 100:
                            conf = 80
                        elif amount > 20:
                            conf = 70
                        elif amount > 5:
                            conf = 60

                if "highest" in text_lower:
                    conf = min(95, conf + 15)
                elif "high" in text_lower:
                    conf = min(90, conf + 10)
                elif "medium" in text_lower:
                    conf = min(80, conf + 5)

                airdrops.append(AirdropInfo(
                    name=name,
                    chain=chain,
                    category=cat,
                    description=f"CryptoTotem掲載 | {text[:100]}",
                    url=item_url if item_url else "https://cryptototem.com/airdrops/",
                    status="active",
                    source="cryptototem",
                    confidence=conf,
                ))

        except Exception as e:
            logger.debug(f"CryptoTotem error: {e}")

        return airdrops

    # ============================================================
    # ソース 7: DeFiLlama Raises — 最近の資金調達（エアドロ予測）
    # ============================================================
    async def _source_defillama_raises(self) -> list[AirdropInfo]:
        """DeFiLlama Raises: 最近資金調達したプロジェクト → トークン未発行ならエアドロ期待"""
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

            for raise_info in raises[:100]:
                name = raise_info.get("name", "")
                amount = raise_info.get("amount", 0) or 0
                chains = raise_info.get("chains", [])
                category = raise_info.get("category", "").lower()

                if not name or amount < 5_000_000:
                    continue

                # 名前ベースの除外
                name_lower = name.lower()
                if any(exc in name_lower for exc in EXCLUDED_NAMES):
                    continue

                chain = _detect_chain(chains) if chains else "multi"

                cat = "defi"
                if any(g in category for g in ["game", "gaming"]):
                    cat = "gamefi"
                elif any(n in category for n in ["nft"]):
                    cat = "nft"
                elif any(i in category for i in ["infrastructure", "bridge", "oracle"]):
                    cat = "infra"
                elif any(l in category for l in ["chain", "layer"]):
                    cat = "l2"

                conf = 50
                if amount > 100_000_000:
                    conf = 85
                elif amount > 50_000_000:
                    conf = 75
                elif amount > 20_000_000:
                    conf = 65
                elif amount > 10_000_000:
                    conf = 55

                chain_display = ", ".join(chains[:3]) if chains else "不明"

                airdrops.append(AirdropInfo(
                    name=name,
                    chain=chain,
                    category=cat,
                    description=f"資金調達: ${amount/1e6:.1f}M | チェーン: {chain_display} | {category}",
                    url="",
                    status="speculative",
                    estimated_value=f"Raised ${amount/1e6:.1f}M",
                    source="defillama-raises",
                    confidence=conf,
                ))

        except Exception as e:
            logger.debug(f"DeFiLlama Raises error: {e}")

        return airdrops

    # ============================================================
    # ソース 8: 手動キュレーションリスト（2026年注目 マルチチェーン）
    # ============================================================
    async def _source_curated_list(self) -> list[AirdropInfo]:
        """手動キュレーション: 2026年に期待される主要エアドロ（マルチチェーン）"""
        curated = [
            # ─── Solana DeFi ───
            AirdropInfo(
                name="Jupiter (JUP) Season 3+",
                chain="solana", category="defi",
                description="Solana最大DEXアグリゲーター。Season 1で$616M配布。JUPステーキング・投票で対象。",
                url="https://jup.ag", status="upcoming",
                requirements=["JUPステーキング", "ガバナンス投票", "DEX利用"],
                source="curated", confidence=92,
            ),
            AirdropInfo(
                name="Meteora (MET) Season 2",
                chain="solana", category="defi",
                description="流動性プール特化。LP提供者にMETトークン配布。",
                url="https://meteora.ag", status="active",
                requirements=["流動性提供", "高ボリュームプール参加"],
                source="curated", confidence=92,
            ),
            AirdropInfo(
                name="Kamino (KMNO) Season 2+",
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

            # ─── NFT / マーケットプレイス ───
            AirdropInfo(
                name="Magic Eden (ME) Season 3",
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

            # ─── GameFi ───
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

            # ─── インフラ ───
            AirdropInfo(
                name="Grass (GRASS) Season 2",
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
                name="Wormhole (W) Season 2",
                chain="multi", category="infra",
                description="クロスチェーンブリッジ。W追加配布期待。ブリッジ利用で対象。",
                url="https://wormhole.com", status="upcoming",
                requirements=["ブリッジ利用", "マルチチェーン送金"],
                source="curated", confidence=68,
            ),
        ]

        return curated

    # ============================================================
    # ソース 9: Twitter/Nitter 監視
    # ============================================================
    async def _source_twitter(self) -> list[AirdropInfo]:
        """Nitter経由: プロトコル公式のエアドロ言及を検出"""
        airdrops = []

        for protocol in self.ALL_PROTOCOLS[:15]:
            for inst in self.NITTER_INSTANCES:
                try:
                    search_url = f"{inst}/search?q={protocol.replace(' ', '+')}+airdrop"
                    async with self.session.get(
                        search_url,
                        timeout=aiohttp.ClientTimeout(total=8),
                        headers={"User-Agent": "Mozilla/5.0"},
                    ) as resp:
                        if resp.status != 200:
                            continue
                        html = await resp.text()

                    soup = BeautifulSoup(html, "html.parser")
                    tweets = soup.select(".timeline-item, .tweet, [class*='tweet']")

                    if tweets:
                        for tweet in tweets[:3]:
                            text = tweet.get_text(strip=True).lower()
                            if any(kw in text for kw in self.AIRDROP_KEYWORDS):
                                chain = "multi"
                                if protocol in [p.lower() for p in self.SOL_DEFI]:
                                    chain = "solana"
                                elif protocol in [p.lower() for p in self.ETH_DEFI]:
                                    chain = "ethereum"
                                elif protocol in [p.lower() for p in self.L2_CHAINS]:
                                    chain = protocol

                                cat = "defi"
                                if protocol in [p.lower() for p in self.GAMEFI_PROTOCOLS]:
                                    cat = "gamefi"
                                elif protocol in [p.lower() for p in self.NFT_PROTOCOLS]:
                                    cat = "nft"

                                airdrops.append(AirdropInfo(
                                    name=f"{protocol.title()} Airdrop",
                                    chain=chain,
                                    category=cat,
                                    description=tweet.get_text(strip=True)[:200],
                                    status="active",
                                    source=f"twitter/{protocol}",
                                    confidence=55,
                                ))
                                break
                    break
                except Exception:
                    continue

            await asyncio.sleep(0.3)

        return airdrops

    # ============================================================
    # ソース 10: 取引所ニュース（Binance Launchpool）
    # ============================================================
    async def _source_exchange_news(self) -> list[AirdropInfo]:
        """取引所のエアドロ・ローンチプール情報を取得"""
        airdrops = []

        # Binance Launchpool（公開API）
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
                                ))
        except Exception as e:
            logger.debug(f"Exchange news error: {e}")

        return airdrops

    # ============================================================
    # ユーティリティ
    # ============================================================
    def filter_by_chain(
        self, airdrops: list[AirdropInfo], chain: str
    ) -> list[AirdropInfo]:
        """チェーンでフィルタ"""
        return [a for a in airdrops if a.chain == chain or a.chain == "multi"]

    def filter_by_category(
        self, airdrops: list[AirdropInfo], category: str
    ) -> list[AirdropInfo]:
        """カテゴリでフィルタ"""
        return [a for a in airdrops if a.category == category]

    def filter_by_confidence(
        self, airdrops: list[AirdropInfo], min_confidence: int = 50
    ) -> list[AirdropInfo]:
        """確度スコアでフィルタ"""
        return [a for a in airdrops if a.confidence >= min_confidence]

    def get_top(
        self, airdrops: list[AirdropInfo], n: int = 10
    ) -> list[AirdropInfo]:
        """確度スコア上位N件を返す"""
        return sorted(airdrops, key=lambda a: a.confidence, reverse=True)[:n]

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
            "blast": "💥", "multi": "🌍",
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
                    lines.append(
                        f"  {conf_bar} {ce} **{a.name}** [{a.status}] "
                        f"(確度: {a.confidence}%)"
                    )
                    if a.description:
                        lines.append(f"    {a.description[:80]}...")
                    if a.requirements:
                        lines.append(f"    📋 {', '.join(a.requirements[:3])}")

        return "\n".join(lines)
