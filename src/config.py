"""設定管理 — v5.5 フィルタ最適化版"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── 通知先 ──
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    line_notify_token: str = os.getenv("LINE_NOTIFY_TOKEN", "")

    # ── API キー（任意） ──
    github_token: str = os.getenv("GITHUB_TOKEN", "")
    helius_api_key: str = os.getenv("HELIUS_API_KEY", "")

    # ── 機能トグル ──
    enable_pumpfun: bool = os.getenv("ENABLE_PUMPFUN", "true").lower() == "true"
    enable_nft: bool = os.getenv("ENABLE_NFT", "false").lower() == "true"
    enable_mania_scoring: bool = os.getenv("ENABLE_MANIA_SCORING", "true").lower() == "true"
    enable_smart_money: bool = os.getenv("ENABLE_SMART_MONEY", "true").lower() == "true"

    # ── リアルタイム監視 ──
    realtime_interval: int = int(os.getenv("REALTIME_INTERVAL_MINUTES", "5"))
    daily_report_hour: int = int(os.getenv("DAILY_REPORT_HOUR", "9"))

    # ── Copy ウォレット: "addr1:ラベル1,addr2:ラベル2" ──
    watch_wallets: str = os.getenv("WATCH_WALLETS", "")

    # ── 流動性監視トークン: "addr1,addr2" ──
    watch_tokens: str = os.getenv("WATCH_TOKENS", "")

    # ── SOL レンジ ──
    sol_range_low: float = float(os.getenv("SOL_RANGE_LOW", "0"))
    sol_range_high: float = float(os.getenv("SOL_RANGE_HIGH", "0"))

    # ── NFT 監視: "mad_lads,tensorians" ──
    watch_nfts: str = os.getenv("WATCH_NFTS", "")

    # ── スクリーニング設定 ──
    top_n: int = int(os.getenv("TOP_N", "7"))
    scan_interval_minutes: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "60"))
    morning_scan_hour: int = int(os.getenv("MORNING_SCAN_HOUR", "7"))
    min_liquidity_usd: float = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
    min_volume_24h_usd: float = float(os.getenv("MIN_VOLUME_24H_USD", "5000"))
    scan_hours_back: int = int(os.getenv("SCAN_HOURS_BACK", "12"))

    # ── 品質フィルタ閾値（v5.5 強化） ──
    min_mcap_usd: float = float(os.getenv("MIN_MCAP_USD", "30000"))
    min_tx_count_24h: int = int(os.getenv("MIN_TX_COUNT_24H", "100"))
    min_makers_24h: int = int(os.getenv("MIN_MAKERS_24H", "30"))
    max_price_drop_24h: float = float(os.getenv("MAX_PRICE_DROP_24H", "-70"))

    # ── 安全性フィルタ閾値 ──
    danger_auto_exclude: bool = os.getenv("DANGER_AUTO_EXCLUDE", "true").lower() == "true"

    # ── スコアリングの重み（合計 1.0）──
    # v5.5: ソーシャル未取得分を実データ指標に再配分
    weights: dict = field(default_factory=lambda: {
        "liquidity": 0.22,
        "volume": 0.22,
        "price_change": 0.15,
        "tx_count": 0.15,
        "makers": 0.10,
        "website_exists": 0.06,
        "twitter_exists": 0.05,
        "audit_exists": 0.03,
        "age_bonus": 0.02,
    })


config = Config()
