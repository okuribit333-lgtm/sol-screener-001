"""
エアドロップ情報自動収集 v4.1 — 7ソース対応

データソース:
  1. DeFiLlama API — Solanaプロトコル（トークン未発行 = エアドロ期待）
  2. DeFiLlama API — GameFi/ゲーム系プロトコル特化
  3. CoinGecko API — Solanaカテゴリの新規・低MC トークン（ポイント制検出）
  4. Airdrops.io スクレイピング — Solana関連エアドロ
  5. AirdropAlert.com スクレイピング — Solana専用ページ
  6. 手動キュレーション — 主要プロジェクトの既知エアドロ情報
  7. Twitter/Nitter 監視 — プロトコル公式のエアドロ言及検出

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
    platform: str = "solana"
    category: str = ""          # "defi" / "gamefi" / "nft" / "infra" / "social"
    description: str = ""
    url: str = ""
    status: str = "active"      # active / upcoming / ended / speculative
    estimated_value: str = ""
    requirements: list = field(default_factory=list)
    source: str = ""
    confidence: int = 50        # 0-100: エアドロ確度スコア

    def __repr__(self):
        return f"<Airdrop: {self.name} | {self.category} | {self.status} | conf={self.confidence}>"


# ============================================================
# メインスキャナー
# ============================================================
class AirdropScanner:
    """エアドロップ情報を7ソースから収集"""

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
        "farming", "quest", "earn", "incentive",
    ]

    # ── Solana エコシステム監視リスト（大幅拡充） ──

    # DeFi 系
    DEFI_PROTOCOLS = [
        "jupiter", "marginfi", "kamino", "drift", "tensor",
        "jito", "sanctum", "phantom", "backpack", "zeta",
        "parcl", "meteora", "marinade", "raydium", "orca",
        "solend", "hubble", "tulip", "francium", "port",
        "mango", "openbook", "lifinity", "axiom", "hylo",
        "vybe", "solayer", "flash", "symmetry", "hawksight",
    ]

    # ゲーム / GameFi 系
    GAMEFI_PROTOCOLS = [
        "star atlas", "aurory", "defi land", "genopets",
        "stepn", "nyan heroes", "br1 infinite", "photo finish",
        "honeyland", "solpump", "mixmob", "mini royale",
        "synergy land", "karate combat", "ev.io",
        "portals", "solice", "solanium", "cryowar",
        "monkeyball", "realy", "decimated",
    ]

    # NFT / マーケットプレイス系
    NFT_PROTOCOLS = [
        "magic eden", "tensor", "formfunction", "exchange art",
        "hyperspace", "solanart", "coral cube",
    ]

    # インフラ / ツール系
    INFRA_PROTOCOLS = [
        "helius", "triton", "quicknode", "ironforge",
        "shyft", "underdog", "dialect", "sphere",
        "streamflow", "squads", "realms", "mean finance",
        "openloop", "assisterr", "grass", "krain",
    ]

    # 全プロトコル統合リスト
    ALL_PROTOCOLS = DEFI_PROTOCOLS + GAMEFI_PROTOCOLS + NFT_PROTOCOLS + INFRA_PROTOCOLS

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    # ============================================================
    # メイン: 全ソーススキャン
    # ============================================================
    async def scan_all(self) -> list[AirdropInfo]:
        """全7ソースからエアドロ情報を収集"""
        results = await asyncio.gather(
            self._source_defillama_defi(),
            self._source_defillama_gamefi(),
            self._source_coingecko_solana(),
            self._source_airdrops_io(),
            self._source_airdropalert(),
            self._source_curated_list(),
            self._source_twitter(),
            return_exceptions=True,
        )

        all_airdrops = []
        source_names = [
            "DeFiLlama-DeFi", "DeFiLlama-GameFi", "CoinGecko",
            "Airdrops.io", "AirdropAlert", "Curated", "Twitter",
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

        # 確度スコア降順でソート
        unique.sort(key=lambda a: a.confidence, reverse=True)

        logger.info(f"✈️ エアドロスキャン完了: {len(unique)}件（重複排除後）")
        return unique

    # ============================================================
    # ソース 1: DeFiLlama — DeFi プロトコル（トークン未発行）
    # ============================================================
    async def _source_defillama_defi(self) -> list[AirdropInfo]:
        """DeFiLlama: Solana DeFiプロトコルでトークン未発行 → エアドロ期待"""
        airdrops = []
        try:
            url = "https://api.llama.fi/protocols"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return airdrops
                data = await resp.json()

            for protocol in data:
                chains = protocol.get("chains", [])
                if "Solana" not in chains:
                    continue

                name = protocol.get("name", "")
                symbol = protocol.get("symbol", "")
                tvl = protocol.get("tvl", 0) or 0
                category = protocol.get("category", "").lower()

                # トークン未発行判定
                has_token = symbol and symbol != "-" and symbol.strip() != ""

                if not has_token and tvl > 500_000:
                    # カテゴリ判定
                    cat = "defi"
                    if any(g in category for g in ["game", "gaming", "play"]):
                        cat = "gamefi"
                    elif any(n in category for n in ["nft", "collectible"]):
                        cat = "nft"

                    # 確度スコア: TVLが高いほど確度UP
                    conf = 40
                    if tvl > 50_000_000:
                        conf = 85
                    elif tvl > 10_000_000:
                        conf = 75
                    elif tvl > 5_000_000:
                        conf = 65
                    elif tvl > 1_000_000:
                        conf = 55

                    airdrops.append(AirdropInfo(
                        name=name,
                        category=cat,
                        description=f"TVL: ${tvl:,.0f} | カテゴリ: {category} | トークン未発行 → エアドロ期待",
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
    # ソース 2: DeFiLlama — GameFi / ゲーム系特化
    # ============================================================
    async def _source_defillama_gamefi(self) -> list[AirdropInfo]:
        """DeFiLlama: ゲーム系Solanaプロトコルを特化検出"""
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
                if "Solana" not in chains:
                    continue

                name = protocol.get("name", "")
                category = protocol.get("category", "").lower()
                desc = protocol.get("description", "").lower()
                tvl = protocol.get("tvl", 0) or 0
                symbol = protocol.get("symbol", "")

                is_game = (
                    any(kw in category for kw in game_keywords)
                    or any(kw in desc for kw in game_keywords)
                    or any(kw in name.lower() for kw in game_keywords)
                )

                if not is_game:
                    continue

                has_token = symbol and symbol != "-" and symbol.strip() != ""

                # ゲーム系はトークン発行済みでもエアドロの可能性あり（シーズン報酬等）
                status = "speculative" if not has_token else "upcoming"
                conf = 60 if not has_token else 35

                if tvl > 1_000_000:
                    conf += 15
                if tvl > 10_000_000:
                    conf += 10

                airdrops.append(AirdropInfo(
                    name=f"{name} (GameFi)",
                    category="gamefi",
                    description=(
                        f"ゲーム系プロトコル | TVL: ${tvl:,.0f} | "
                        f"{'トークン未発行' if not has_token else f'${symbol}'} | "
                        f"{protocol.get('description', '')[:100]}"
                    ),
                    url=protocol.get("url", ""),
                    status=status,
                    estimated_value=f"TVL ${tvl / 1e6:.1f}M" if tvl > 0 else "不明",
                    source="defillama-gamefi",
                    confidence=min(95, conf),
                ))

        except Exception as e:
            logger.debug(f"DeFiLlama GameFi error: {e}")

        return airdrops

    # ============================================================
    # ソース 3: CoinGecko — Solana カテゴリ新規トークン
    # ============================================================
    async def _source_coingecko_solana(self) -> list[AirdropInfo]:
        """CoinGecko: Solanaエコシステムの新規・低MCトークンからエアドロ候補を検出"""
        airdrops = []

        categories = [
            ("solana-ecosystem", "defi"),
            ("gaming", "gamefi"),
            ("play-to-earn", "gamefi"),
            ("non-fungible-tokens-nft", "nft"),
            ("move-to-earn", "gamefi"),
            ("metaverse", "gamefi"),
        ]

        for cat_id, cat_label in categories:
            try:
                url = f"https://api.coingecko.com/api/v3/coins/markets"
                params = {
                    "vs_currency": "usd",
                    "category": cat_id,
                    "order": "market_cap_asc",
                    "per_page": 50,
                    "page": 1,
                    "sparkline": "false",
                }
                async with self.session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()

                for coin in data:
                    name = coin.get("name", "")
                    symbol = coin.get("symbol", "").upper()
                    mcap = coin.get("market_cap", 0) or 0

                    # Solana関連かチェック（CoinGeckoのカテゴリは複数チェーン含む）
                    name_lower = name.lower()
                    is_likely_solana = any(
                        kw in name_lower
                        for kw in ["sol", "solana", "raydium", "serum", "phantom"]
                    ) or cat_id == "solana-ecosystem"

                    if not is_likely_solana:
                        continue

                    # 低時価総額 = まだ初期 = エアドロ可能性
                    if mcap > 0 and mcap < 50_000_000:
                        conf = 30
                        if mcap < 1_000_000:
                            conf = 50
                        elif mcap < 5_000_000:
                            conf = 40

                        airdrops.append(AirdropInfo(
                            name=f"{name} ({symbol})",
                            category=cat_label,
                            description=(
                                f"MC: ${mcap:,.0f} | "
                                f"カテゴリ: {cat_id} | "
                                f"低MC → ポイント制/エアドロの可能性"
                            ),
                            url=f"https://www.coingecko.com/en/coins/{coin.get('id', '')}",
                            status="speculative",
                            estimated_value=f"MC ${mcap / 1e6:.1f}M",
                            source="coingecko",
                            confidence=conf,
                        ))

                await asyncio.sleep(1.5)  # CoinGecko レート制限対策

            except Exception as e:
                logger.debug(f"CoinGecko [{cat_id}] error: {e}")

        return airdrops

    # ============================================================
    # ソース 4: Airdrops.io スクレイピング
    # ============================================================
    async def _source_airdrops_io(self) -> list[AirdropInfo]:
        """Airdrops.io: Solana関連のエアドロを取得"""
        airdrops = []

        pages = [
            ("https://airdrops.io/speculative/", "speculative"),
            ("https://airdrops.io/latest/", "active"),
        ]

        solana_keywords = [
            "solana", "sol", "spl", "phantom", "jupiter",
            "raydium", "serum", "anchor", "metaplex",
        ]

        for page_url, status in pages:
            try:
                async with self.session.get(
                    page_url,
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                ) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()

                soup = BeautifulSoup(html, "html.parser")

                # 複数のセレクタパターンで取得
                cards = soup.select(
                    ".airdrop-card, .card, article, "
                    ".airdrop-list-item, .airdrop-item, "
                    "[class*='airdrop'], [class*='card']"
                )

                for card in cards[:40]:
                    title_el = card.select_one(
                        "h3, h2, h4, .title, .card-title, "
                        "[class*='title'], [class*='name']"
                    )
                    desc_el = card.select_one(
                        "p, .description, .card-text, "
                        "[class*='desc'], [class*='text']"
                    )
                    link_el = card.select_one("a[href]")

                    if not title_el:
                        continue

                    name = title_el.get_text(strip=True)
                    desc = desc_el.get_text(strip=True) if desc_el else ""
                    link = link_el.get("href", "") if link_el else ""

                    text = f"{name} {desc}".lower()
                    is_solana = (
                        any(kw in text for kw in solana_keywords)
                        or any(p in text for p in self.ALL_PROTOCOLS)
                    )

                    if is_solana:
                        airdrops.append(AirdropInfo(
                            name=name,
                            category="defi",
                            description=desc[:200],
                            url=link if link.startswith("http") else f"https://airdrops.io{link}",
                            status=status,
                            source="airdrops.io",
                            confidence=65,
                        ))

            except Exception as e:
                logger.debug(f"Airdrops.io [{page_url}] error: {e}")

        return airdrops

    # ============================================================
    # ソース 5: AirdropAlert.com スクレイピング
    # ============================================================
    async def _source_airdropalert(self) -> list[AirdropInfo]:
        """AirdropAlert.com: Solana専用ページからエアドロ情報を取得"""
        airdrops = []
        try:
            url = "https://airdropalert.com/airdrops/solana/"
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            ) as resp:
                if resp.status != 200:
                    return airdrops
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")

            # カード要素を取得
            cards = soup.select(
                ".airdrop-card, .card, article, "
                "[class*='airdrop'], [class*='listing'], "
                ".row, .col"
            )

            for card in cards[:30]:
                title_el = card.select_one(
                    "h3, h2, h4, .title, [class*='title'], [class*='name'], strong"
                )
                desc_el = card.select_one("p, .description, [class*='desc']")
                link_el = card.select_one("a[href]")

                if not title_el:
                    continue

                name = title_el.get_text(strip=True)
                if len(name) < 3 or len(name) > 100:
                    continue

                desc = desc_el.get_text(strip=True) if desc_el else ""
                link = link_el.get("href", "") if link_el else ""

                # ゲーム系判定
                text = f"{name} {desc}".lower()
                cat = "defi"
                if any(kw in text for kw in ["game", "play", "earn", "nft", "metaverse"]):
                    cat = "gamefi"
                elif any(kw in text for kw in ["nft", "collectible", "art"]):
                    cat = "nft"

                airdrops.append(AirdropInfo(
                    name=name,
                    category=cat,
                    description=desc[:200],
                    url=link if link.startswith("http") else f"https://airdropalert.com{link}",
                    status="active",
                    source="airdropalert",
                    confidence=60,
                ))

        except Exception as e:
            logger.debug(f"AirdropAlert error: {e}")

        return airdrops

    # ============================================================
    # ソース 6: 手動キュレーションリスト（既知の有力エアドロ）
    # ============================================================
    async def _source_curated_list(self) -> list[AirdropInfo]:
        """手動キュレーション: 2026年に期待される主要Solanaエアドロ"""
        curated = [
            # DeFi 系
            AirdropInfo(
                name="Jupiter (JUP) Season 3+",
                category="defi",
                description="Solana最大DEXアグリゲーター。Season 1で$616M配布。JUPステーキング・投票で対象。",
                url="https://jup.ag",
                status="upcoming",
                requirements=["JUPステーキング", "ガバナンス投票", "DEX利用"],
                source="curated",
                confidence=90,
            ),
            AirdropInfo(
                name="Meteora (MET) Season 2",
                category="defi",
                description="流動性プール特化。LP提供者にMETトークン配布。高ボリュームLP提供が有利。",
                url="https://meteora.ag",
                status="active",
                requirements=["流動性提供", "高ボリュームプール参加"],
                source="curated",
                confidence=90,
            ),
            AirdropInfo(
                name="Magic Eden (ME) Season 3",
                category="nft",
                description="Solana最大NFTマーケットプレイス。Season 3進行中。ガバナンス参加・クエスト完了で対象。",
                url="https://magiceden.io",
                status="active",
                requirements=["MEウォレット", "ガバナンス参加", "クエスト完了"],
                source="curated",
                confidence=95,
            ),
            AirdropInfo(
                name="Kamino (KMNO) Season 2+",
                category="defi",
                description="レンディング・ステーキング・LP。Season 1で1ウォレット平均$300配布。",
                url="https://kamino.finance",
                status="upcoming",
                requirements=["レンディング", "ステーキング", "LP提供"],
                source="curated",
                confidence=80,
            ),
            AirdropInfo(
                name="Sanctum (CLOUD)",
                category="defi",
                description="リキッドステーキングインフラ。カスタムLST作成。ポイントプログラム進行中。",
                url="https://sanctum.so",
                status="active",
                requirements=["SOLステーキング", "LST保有", "ポイント獲得"],
                source="curated",
                confidence=80,
            ),
            AirdropInfo(
                name="Vybe Network",
                category="infra",
                description="Solanaミドルレイヤー。VybeSOL（リキッドステーキング）。トークン未発行。",
                url="https://vybenetwork.com",
                status="speculative",
                requirements=["VybeSOL購入", "コミュニティ参加"],
                source="curated",
                confidence=70,
            ),
            AirdropInfo(
                name="Axiom Trade",
                category="defi",
                description="Perp取引プロトコル。ポイントベースの報酬システム。",
                url="https://axiom.trade",
                status="active",
                requirements=["Perp取引", "ポイント獲得"],
                source="curated",
                confidence=75,
            ),
            AirdropInfo(
                name="Hylo",
                category="defi",
                description="SOLレバレッジ・ステーブルコイン利回り。ポイントシステム進行中。",
                url="https://hylo.finance",
                status="active",
                requirements=["SOL預入", "ポイント獲得"],
                source="curated",
                confidence=70,
            ),

            # ゲーム / GameFi 系
            AirdropInfo(
                name="Star Atlas (ATLAS/POLIS)",
                category="gamefi",
                description="大型宇宙MMO。ゲーム内活動・NFT保有でシーズン報酬。Unreal Engine 5。",
                url="https://staratlas.com",
                status="upcoming",
                requirements=["ゲームプレイ", "NFT保有", "DAO参加"],
                source="curated",
                confidence=65,
            ),
            AirdropInfo(
                name="Aurory (AURY)",
                category="gamefi",
                description="ターンベースRPG。Seekers of Tokane。ゲーム内報酬・NFTエアドロ。",
                url="https://aurory.io",
                status="upcoming",
                requirements=["ゲームプレイ", "NFT保有"],
                source="curated",
                confidence=55,
            ),
            AirdropInfo(
                name="Genopets (GENE/KI)",
                category="gamefi",
                description="Move-to-Earn RPG。歩数でトークン獲得。新シーズン報酬。",
                url="https://genopets.me",
                status="upcoming",
                requirements=["アプリ利用", "歩数記録", "ペット育成"],
                source="curated",
                confidence=50,
            ),
            AirdropInfo(
                name="Nyan Heroes",
                category="gamefi",
                description="猫×メカのバトルロイヤルFPS。Epic Games Store配信。トークンローンチ予定。",
                url="https://nyanheroes.com",
                status="speculative",
                requirements=["ゲームプレイ", "NFT保有", "コミュニティ参加"],
                source="curated",
                confidence=60,
            ),
            AirdropInfo(
                name="SolPump",
                category="gamefi",
                description="Play & Earnゲーム。アクティブプレイヤーにエアドロ。Binance上場の噂。",
                url="https://solpump.fun",
                status="active",
                requirements=["ゲームプレイ", "デイリータスク"],
                source="curated",
                confidence=55,
            ),
            AirdropInfo(
                name="Photo Finish LIVE",
                category="gamefi",
                description="競馬シミュレーション。馬NFT保有・レース参加で報酬。",
                url="https://photofinish.live",
                status="upcoming",
                requirements=["馬NFT保有", "レース参加"],
                source="curated",
                confidence=45,
            ),
            AirdropInfo(
                name="DeFi Land",
                category="gamefi",
                description="農業シミュレーション × DeFi。ゲーム内でDeFi操作。シーズン報酬。",
                url="https://defi.land",
                status="upcoming",
                requirements=["ゲームプレイ", "DeFi操作"],
                source="curated",
                confidence=45,
            ),
            AirdropInfo(
                name="Honeyland",
                category="gamefi",
                description="養蜂シミュレーション。ハチNFT保有・ミッション完了で報酬。",
                url="https://honey.land",
                status="upcoming",
                requirements=["NFT保有", "ミッション完了"],
                source="curated",
                confidence=40,
            ),

            # インフラ系
            AirdropInfo(
                name="Grass (GRASS)",
                category="infra",
                description="分散型AIデータネットワーク。帯域共有でポイント獲得。Season 2進行中。",
                url="https://getgrass.io",
                status="active",
                requirements=["ブラウザ拡張インストール", "帯域共有"],
                source="curated",
                confidence=75,
            ),
            AirdropInfo(
                name="OpenLoop",
                category="infra",
                description="分散型帯域共有ネットワーク。ノード運用でポイント獲得。",
                url="https://openloop.so",
                status="active",
                requirements=["ノード運用", "ポイント獲得"],
                source="curated",
                confidence=60,
            ),
        ]

        return curated

    # ============================================================
    # ソース 7: Twitter/Nitter 監視
    # ============================================================
    async def _source_twitter(self) -> list[AirdropInfo]:
        """Nitter経由: プロトコル公式のエアドロ言及を検出"""
        airdrops = []

        # DeFi + GameFi + NFT から主要なものを監視
        protocols_to_check = (
            self.DEFI_PROTOCOLS[:8]
            + self.GAMEFI_PROTOCOLS[:6]
            + self.NFT_PROTOCOLS[:3]
            + self.INFRA_PROTOCOLS[:4]
        )

        for protocol in protocols_to_check:
            for inst in self.NITTER_INSTANCES:
                try:
                    search_url = f"{inst}/search?q={protocol.replace(' ', '+')}+airdrop+solana"
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
                                # カテゴリ判定
                                cat = "defi"
                                if protocol in [p.lower() for p in self.GAMEFI_PROTOCOLS]:
                                    cat = "gamefi"
                                elif protocol in [p.lower() for p in self.NFT_PROTOCOLS]:
                                    cat = "nft"

                                airdrops.append(AirdropInfo(
                                    name=f"{protocol.title()} Airdrop",
                                    category=cat,
                                    description=tweet.get_text(strip=True)[:200],
                                    status="active",
                                    source=f"twitter/{protocol}",
                                    confidence=55,
                                ))
                                break
                    break  # 1つのNitterで成功したら次のプロトコルへ
                except Exception:
                    continue

            await asyncio.sleep(0.3)

        return airdrops

    # ============================================================
    # ユーティリティ
    # ============================================================
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

        # カテゴリ別に集計
        by_cat = {}
        for a in airdrops:
            by_cat.setdefault(a.category or "other", []).append(a)

        cat_emoji = {
            "defi": "💰",
            "gamefi": "🎮",
            "nft": "🖼️",
            "infra": "🔧",
            "social": "💬",
            "other": "📦",
        }

        lines = [f"**✈️ エアドロップ情報 ({len(airdrops)}件)**\n"]

        for cat, items in sorted(by_cat.items()):
            emoji = cat_emoji.get(cat, "📦")
            lines.append(f"\n{emoji} **{cat.upper()}** ({len(items)}件)")
            for a in items[:5]:
                conf_bar = "🟢" if a.confidence >= 70 else "🟡" if a.confidence >= 50 else "🔴"
                lines.append(
                    f"  {conf_bar} **{a.name}** [{a.status}] "
                    f"(確度: {a.confidence}%)"
                )
                if a.description:
                    lines.append(f"    {a.description[:80]}...")
                if a.requirements:
                    lines.append(f"    📋 {', '.join(a.requirements[:3])}")

        return "\n".join(lines)
