"""
通知モジュール v4 — Discord Embed 完全対応
DexScreener / RugCheck / BirdEye / Solscan への直リンク付き

Discord Webhook では ActionRow ボタンは使えないため、
Embed の description 内にマークダウンリンクを配置して対応。
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from .config import config
from .scanner import SolanaProject

logger = logging.getLogger(__name__)

# ── リンク生成ヘルパー ──
def _dexscreener_url(token_address: str) -> str:
    return f"https://dexscreener.com/solana/{token_address}"

def _rugcheck_url(token_address: str) -> str:
    return f"https://rugcheck.xyz/tokens/{token_address}"

def _birdeye_url(token_address: str) -> str:
    return f"https://birdeye.so/token/{token_address}?chain=solana"

def _solscan_url(token_address: str) -> str:
    return f"https://solscan.io/token/{token_address}"

def _photon_url(token_address: str) -> str:
    return f"https://photon-sol.tinyastro.io/en/lp/{token_address}"


class Notifier:
    """Discord Webhook 通知（Embed 形式）"""

    # Embed カラー
    COLOR_GREEN = 0x00FF88    # 安全 / 高スコア
    COLOR_YELLOW = 0xFFCC00   # 注意
    COLOR_RED = 0xFF3333      # 危険
    COLOR_BLUE = 0x5865F2     # 情報
    COLOR_PURPLE = 0x9B59B6   # 卒業
    COLOR_GOLD = 0xF1C40F     # スマートマネー

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.webhook_url = config.discord_webhook_url

    # ================================================================
    # メイン: スキャン結果通知
    # ================================================================
    async def send_scan_results(
        self,
        projects: list[SolanaProject],
        safety_results: Optional[dict] = None,
        smart_money_results: Optional[dict] = None,
        title: str = "🔍 Solana スキャン結果",
    ):
        """スキャン結果を Discord Embed で通知"""
        if not self.webhook_url:
            logger.warning("DISCORD_WEBHOOK_URL が未設定")
            return

        if not projects:
            await self._send_simple(f"{title}\n\n対象トークンなし")
            return

        # ── サマリー Embed ──
        summary_embed = {
            "title": title,
            "description": (
                f"**{len(projects)}件**のトークンを検出\n"
                f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            ),
            "color": self.COLOR_BLUE,
            "footer": {"text": "Sol Screener v4 | Powered by DexScreener + RugCheck"},
        }

        embeds = [summary_embed]

        # ── 各プロジェクトの Embed ──
        for p in projects[:10]:  # Discord は 10 embeds まで
            safety = (safety_results or {}).get(p.token_address, {})
            sm = (smart_money_results or {}).get(p.token_address, {})
            embed = self._build_project_embed(p, safety, sm)
            embeds.append(embed)

        # Discord は 1 メッセージ 10 embeds まで → 分割送信
        for i in range(0, len(embeds), 10):
            chunk = embeds[i:i + 10]
            await self._send_webhook({"embeds": chunk})
            if i + 10 < len(embeds):
                await asyncio.sleep(1)

    # ================================================================
    # Pump.fun 卒業通知（特別フォーマット）
    # ================================================================
    async def send_graduation_alert(
        self,
        project: SolanaProject,
        safety: Optional[dict] = None,
    ):
        """Pump.fun → Raydium 卒業をリアルタイム通知"""
        if not self.webhook_url:
            return

        addr = project.token_address
        risk_emoji = self._risk_emoji(safety)

        links = (
            f"[DexScreener]({_dexscreener_url(addr)}) | "
            f"[RugCheck]({_rugcheck_url(addr)}) | "
            f"[BirdEye]({_birdeye_url(addr)}) | "
            f"[Solscan]({_solscan_url(addr)})"
        )

        desc_lines = [
            f"**{project.name}** (`{project.symbol}`) が Raydium に上場しました！",
            "",
            f"💰 価格: `${project.price_usd:.8f}`",
            f"💧 流動性: `${project.liquidity_usd:,.0f}`",
            f"📊 時価総額: `${project.market_cap:,.0f}`",
            f"📈 5m: `{project.price_change_5m:+.1f}%` | 1h: `{project.price_change_1h:+.1f}%`",
            "",
        ]

        if safety:
            warnings = safety.get("warnings", [])
            if warnings:
                desc_lines.append(f"**安全性** {risk_emoji}")
                for w in warnings[:5]:
                    desc_lines.append(f"  {w}")
            else:
                desc_lines.append(f"**安全性** {risk_emoji} チェック済み・問題なし")

            if safety.get("top_holders_pct") is not None:
                desc_lines.append(
                    f"👥 Top10ホルダー: `{safety['top_holders_pct']:.1f}%`"
                )
            if safety.get("mint_authority"):
                mint_status = "❌ 未放棄" if safety["mint_authority"] != "None" else "✅ 放棄済み"
                desc_lines.append(f"🔑 ミント権限: {mint_status}")
            if safety.get("lp_locked") is not None:
                lp_status = "✅ ロック済み" if safety["lp_locked"] else "❌ 未ロック"
                desc_lines.append(f"🔒 LP: {lp_status}")

        desc_lines.append("")
        desc_lines.append(f"🔗 {links}")

        embed = {
            "title": f"🎓 Pump.fun 卒業: {project.symbol}",
            "description": "\n".join(desc_lines),
            "color": self.COLOR_PURPLE,
            "thumbnail": {"url": f"https://dd.dexscreener.com/ds-data/tokens/solana/{addr}.png"},
            "footer": {"text": f"DEX: {project.dex} | Score: {project.total_score:.1f}/100"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        await self._send_webhook({"embeds": [embed]})

    # ================================================================
    # ラグプル警告通知
    # ================================================================
    async def send_danger_alert(
        self,
        project: SolanaProject,
        safety: dict,
    ):
        """危険トークンの警告通知"""
        if not self.webhook_url:
            return

        addr = project.token_address
        warnings = safety.get("warnings", [])

        desc_lines = [
            f"**{project.name}** (`{project.symbol}`) に重大なリスクが検出されました",
            "",
        ]
        for w in warnings:
            desc_lines.append(f"  {w}")

        desc_lines.append("")
        desc_lines.append(
            f"🔗 [RugCheck で確認]({_rugcheck_url(addr)}) | "
            f"[DexScreener]({_dexscreener_url(addr)})"
        )

        embed = {
            "title": f"⚠️ 危険トークン検出: {project.symbol}",
            "description": "\n".join(desc_lines),
            "color": self.COLOR_RED,
            "footer": {"text": "Sol Screener v4 | 安全性チェック"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        await self._send_webhook({"embeds": [embed]})

    # ================================================================
    # スマートマネー通知
    # ================================================================
    async def send_smart_money_alert(
        self,
        project: SolanaProject,
        smart_money: dict,
    ):
        """スマートマネーの動きを通知"""
        if not self.webhook_url:
            return

        addr = project.token_address
        sm_score = smart_money.get("smart_money_score", 0)
        whale_count = smart_money.get("whale_count", 0)
        wallets = smart_money.get("notable_wallets", [])

        desc_lines = [
            f"**{project.name}** (`{project.symbol}`) にスマートマネーの動きを検出",
            "",
            f"🧠 SM スコア: `{sm_score}/100`",
            f"🐋 ホエール数: `{whale_count}`",
            "",
        ]

        if wallets:
            desc_lines.append("**注目ウォレット:**")
            for w in wallets[:5]:
                label = w.get("label", w.get("address", "")[:8] + "...")
                pnl = w.get("pnl", 0)
                desc_lines.append(f"  • `{label}` (PnL: ${pnl:,.0f})")

        desc_lines.append("")
        desc_lines.append(
            f"🔗 [DexScreener]({_dexscreener_url(addr)}) | "
            f"[BirdEye]({_birdeye_url(addr)}) | "
            f"[RugCheck]({_rugcheck_url(addr)})"
        )

        embed = {
            "title": f"🧠 Smart Money: {project.symbol}",
            "description": "\n".join(desc_lines),
            "color": self.COLOR_GOLD,
            "footer": {"text": "Sol Screener v4 | Smart Money Tracker"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        await self._send_webhook({"embeds": [embed]})

    # ================================================================
    # エアドロップ通知
    # ================================================================
    async def send_airdrop_report(self, airdrops: list, title: str = "✈️ エアドロップ情報"):
        """エアドロップ情報を Discord Embed で通知（マルチチェーン対応）"""
        if not self.webhook_url or not airdrops:
            return

        # チェーン別に分類
        by_chain = {}
        for a in airdrops:
            chain = getattr(a, 'chain', 'multi') or 'multi'
            by_chain.setdefault(chain, []).append(a)

        # カテゴリ別に分類
        by_cat = {}
        for a in airdrops:
            by_cat.setdefault(a.category or "other", []).append(a)

        cat_emoji = {
            "defi": "💰", "gamefi": "🎮", "nft": "🖼️",
            "infra": "🔧", "social": "💬", "l2": "⛓️", "other": "📦",
        }

        chain_emoji = {
            "solana": "◎", "ethereum": "⟠", "arbitrum": "🔵",
            "base": "🔷", "berachain": "🐻", "monad": "🟣",
            "scroll": "📜", "linea": "🌐", "blast": "💥", "multi": "🌍",
        }

        # サマリー Embed
        from datetime import datetime, timezone
        chain_lines = []
        for c, items in sorted(by_chain.items()):
            ce = chain_emoji.get(c, '🔗')
            chain_lines.append(f"{ce} **{c.upper()}**: {len(items)}件")
        cat_lines = []
        for c, items in sorted(by_cat.items()):
            ce = cat_emoji.get(c, '📦')
            cat_lines.append(f"{ce} **{c.upper()}**: {len(items)}件")

        summary = {
            "title": title,
            "description": (
                f"**{len(airdrops)}件**のエアドロップ候補を検出\n"
                f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"**チェーン別:**\n" + "\n".join(chain_lines) + "\n\n"
                f"**カテゴリ別:**\n" + "\n".join(cat_lines)
            ),
            "color": self.COLOR_BLUE,
            "footer": {"text": "Sol Screener v5 | Multi-Chain Airdrop Scanner"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        embeds = [summary]

        # 上位エアドロを個別 Embed で通知（確度順、最大9件）
        for a in airdrops[:9]:
            conf_bar = "🟢" if a.confidence >= 70 else "🟡" if a.confidence >= 50 else "🔴"
            emoji = cat_emoji.get(a.category, "📦")

            desc_lines = []
            if a.description:
                desc_lines.append(a.description[:200])
            desc_lines.append("")
            desc_lines.append(f"{conf_bar} 確度: **{a.confidence}%** | ステータス: `{a.status}`")
            chain_label = getattr(a, 'chain', 'multi') or 'multi'
            ch_e = chain_emoji.get(chain_label, '🔗')
            desc_lines.append(f"{ch_e} チェーン: `{chain_label}` | 📂 カテゴリ: `{a.category}` | ソース: `{a.source}`")

            if a.estimated_value:
                desc_lines.append(f"💰 推定規模: `{a.estimated_value}`")

            if a.requirements:
                desc_lines.append(f"📋 参加条件: {', '.join(a.requirements[:4])}")

            if a.url:
                desc_lines.append(f"\n🔗 [プロジェクトサイト]({a.url})")

            # 色: 確度に応じて
            if a.confidence >= 75:
                color = self.COLOR_GREEN
            elif a.confidence >= 50:
                color = self.COLOR_YELLOW
            else:
                color = 0x95A5A6  # グレー

            embed = {
                "title": f"{emoji} {a.name}",
                "description": "\n".join(desc_lines),
                "color": color,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            embeds.append(embed)

        # Discord は 1 メッセージ 10 embeds まで → 分割送信
        for i in range(0, len(embeds), 10):
            chunk = embeds[i:i + 10]
            await self._send_webhook({"embeds": chunk})
            if i + 10 < len(embeds):
                await asyncio.sleep(1)

    # ================================================================
    # 日次レポート
    # ================================================================
    async def send_daily_report(self, report_text: str):
        """日次レポートを送信"""
        embed = {
            "title": "📊 日次レポート",
            "description": report_text[:4000],
            "color": self.COLOR_BLUE,
            "footer": {"text": "Sol Screener v4"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._send_webhook({"embeds": [embed]})

    # ================================================================
    # 汎用テキスト通知
    # ================================================================
    async def send_text(self, text: str, title: str = "ℹ️ 通知"):
        """シンプルなテキスト通知"""
        embed = {
            "title": title,
            "description": text[:4000],
            "color": self.COLOR_BLUE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._send_webhook({"embeds": [embed]})

    # ================================================================
    # 内部ヘルパー
    # ================================================================
    def _build_project_embed(
        self,
        project: SolanaProject,
        safety: dict,
        smart_money: dict,
    ) -> dict:
        """プロジェクト用 Embed を構築"""
        addr = project.token_address
        risk_emoji = self._risk_emoji(safety)
        grad_badge = " 🎓" if project.is_graduated else ""

        links = (
            f"[DexScreener]({_dexscreener_url(addr)}) | "
            f"[RugCheck]({_rugcheck_url(addr)}) | "
            f"[BirdEye]({_birdeye_url(addr)}) | "
            f"[Solscan]({_solscan_url(addr)})"
        )

        # フィールド
        fields = [
            {
                "name": "💰 価格",
                "value": f"`${project.price_usd:.8f}`",
                "inline": True,
            },
            {
                "name": "💧 流動性",
                "value": f"`${project.liquidity_usd:,.0f}`",
                "inline": True,
            },
            {
                "name": "📊 時価総額",
                "value": f"`${project.market_cap:,.0f}`",
                "inline": True,
            },
            {
                "name": "📈 変動率",
                "value": (
                    f"5m: `{project.price_change_5m:+.1f}%`\n"
                    f"1h: `{project.price_change_1h:+.1f}%`\n"
                    f"24h: `{project.price_change_24h:+.1f}%`"
                ),
                "inline": True,
            },
            {
                "name": "🔄 24h取引",
                "value": f"Vol: `${project.volume_24h_usd:,.0f}`\nTx: `{project.tx_count_24h:,}`",
                "inline": True,
            },
        ]

        # 安全性フィールド
        safety_lines = []
        if safety:
            if safety.get("rugcheck_score") is not None:
                safety_lines.append(f"RC Score: `{safety['rugcheck_score']}`")
            if safety.get("mint_authority"):
                mint_s = "✅放棄" if safety["mint_authority"] == "None" else "❌未放棄"
                safety_lines.append(f"Mint: {mint_s}")
            if safety.get("lp_locked") is not None:
                lp_s = "✅ロック" if safety["lp_locked"] else "❌未ロック"
                safety_lines.append(f"LP: {lp_s}")
            if safety.get("top_holders_pct") is not None:
                safety_lines.append(f"Top10: `{safety['top_holders_pct']:.1f}%`")

        if safety_lines:
            fields.append({
                "name": f"{risk_emoji} 安全性",
                "value": "\n".join(safety_lines),
                "inline": True,
            })

        # スマートマネーフィールド
        if smart_money and smart_money.get("smart_money_score", 0) > 0:
            sm_score = smart_money["smart_money_score"]
            whale_count = smart_money.get("whale_count", 0)
            fields.append({
                "name": "🧠 Smart Money",
                "value": f"Score: `{sm_score}/100`\nWhales: `{whale_count}`",
                "inline": True,
            })

        # リンクフィールド
        fields.append({
            "name": "🔗 リンク",
            "value": links,
            "inline": False,
        })

        # カラー決定
        if project.total_score >= 70:
            color = self.COLOR_GREEN
        elif project.total_score >= 40:
            color = self.COLOR_YELLOW
        else:
            color = self.COLOR_RED

        if project.is_graduated:
            color = self.COLOR_PURPLE

        embed = {
            "title": f"#{projects_rank(project)} {project.symbol}{grad_badge} — Score: {project.total_score:.1f}/100",
            "description": f"**{project.name}** | DEX: `{project.dex}`",
            "color": color,
            "fields": fields,
            "thumbnail": {
                "url": f"https://dd.dexscreener.com/ds-data/tokens/solana/{addr}.png"
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return embed

    @staticmethod
    def _risk_emoji(safety: Optional[dict]) -> str:
        if not safety:
            return "❓"
        level = safety.get("risk_level", "unknown")
        return {"safe": "✅", "warning": "⚠️", "danger": "🔴"}.get(level, "❓")

    async def _send_webhook(self, payload: dict):
        """Discord Webhook に送信"""
        if not self.webhook_url:
            return
        try:
            async with self.session.post(
                self.webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    logger.warning(f"Discord Webhook error: {resp.status} {body[:200]}")
                else:
                    logger.info("Discord 通知送信成功")
        except Exception as e:
            logger.error(f"Discord 送信エラー: {e}")

    async def _send_simple(self, text: str):
        """シンプルなテキストメッセージ"""
        if not self.webhook_url:
            return
        await self._send_webhook({"content": text[:2000]})


def projects_rank(project: SolanaProject) -> str:
    """スコアに基づくランク表示"""
    s = project.total_score
    if s >= 80:
        return "S"
    elif s >= 60:
        return "A"
    elif s >= 40:
        return "B"
    elif s >= 20:
        return "C"
    return "D"
