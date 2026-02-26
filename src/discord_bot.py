"""
Discord Bot モジュール v5.6

スラッシュコマンドでBotを操作可能にする。
Webhook通知と並行して動作する。

■ コマンド:
  /scan     — 即時フルスキャンを実行
  /filter   — 現在のフィルタ条件を表示
  /status   — Botのステータスを表示

■ 注意:
  - DISCORD_BOT_TOKEN 環境変数が未設定の場合は起動しない
  - Webhook通知は従来通り動作する（Bot化はオプション）
  - discord.py v2 の app_commands を使用
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)

# discord.py はオプション依存
try:
    import discord
    from discord import app_commands
    DISCORD_PY_AVAILABLE = True
except ImportError:
    DISCORD_PY_AVAILABLE = False
    logger.info("Discord Bot: discord.py 未インストール（Bot機能は無効）")


class DiscordBot:
    """
    Discord Bot（スラッシュコマンド対応）

    discord.py が未インストールまたは DISCORD_BOT_TOKEN が未設定の場合は
    何もしない（Webhook通知のみで動作）。
    """

    def __init__(self):
        self._client: Optional[object] = None
        self._tree: Optional[object] = None
        self._token = os.getenv("DISCORD_BOT_TOKEN", "")
        self._running = False

        # コールバック（main.pyから注入）
        self._on_scan: Optional[Callable[[], Awaitable]] = None
        self._get_filter_info: Optional[Callable[[], dict]] = None
        self._get_status_info: Optional[Callable[[], dict]] = None

    @property
    def is_available(self) -> bool:
        return DISCORD_PY_AVAILABLE and bool(self._token)

    def set_callbacks(
        self,
        on_scan: Optional[Callable[[], Awaitable]] = None,
        get_filter_info: Optional[Callable[[], dict]] = None,
        get_status_info: Optional[Callable[[], dict]] = None,
    ):
        """main.pyからコールバックを注入"""
        self._on_scan = on_scan
        self._get_filter_info = get_filter_info
        self._get_status_info = get_status_info

    async def start(self):
        """Botを起動（バックグラウンドタスクとして）"""
        if not self.is_available:
            if not DISCORD_PY_AVAILABLE:
                logger.info("Discord Bot: discord.py 未インストール → スキップ")
            elif not self._token:
                logger.info("Discord Bot: DISCORD_BOT_TOKEN 未設定 → スキップ")
            return

        intents = discord.Intents.default()
        self._client = discord.Client(intents=intents)
        self._tree = app_commands.CommandTree(self._client)

        self._register_commands()

        @self._client.event
        async def on_ready():
            logger.info(f"Discord Bot: ログイン成功 ({self._client.user})")
            try:
                synced = await self._tree.sync()
                logger.info(f"Discord Bot: {len(synced)}個のコマンドを同期")
            except Exception as e:
                logger.error(f"Discord Bot: コマンド同期エラー: {e}")
            self._running = True

        # バックグラウンドで起動
        asyncio.create_task(self._run_bot())
        logger.info("Discord Bot: 起動タスク作成完了")

    async def _run_bot(self):
        """Botを実行"""
        try:
            await self._client.start(self._token)
        except discord.LoginFailure:
            logger.error("Discord Bot: ログイン失敗（トークンが無効）")
        except Exception as e:
            logger.error(f"Discord Bot: 実行エラー: {e}")

    def _register_commands(self):
        """スラッシュコマンドを登録"""

        @self._tree.command(name="scan", description="即時フルスキャンを実行")
        async def cmd_scan(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            try:
                if self._on_scan:
                    await self._on_scan()
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="🔍 スキャン完了",
                            description="フルスキャンを実行しました。結果は通知チャンネルに送信されます。",
                            color=0x5865F2,
                            timestamp=datetime.now(timezone.utc),
                        )
                    )
                else:
                    await interaction.followup.send("⚠️ スキャン機能が初期化されていません")
            except Exception as e:
                logger.error(f"Discord Bot: /scan エラー: {e}")
                await interaction.followup.send(f"❌ スキャンエラー: {str(e)[:200]}")

        @self._tree.command(name="filter", description="現在のフィルタ条件を表示")
        async def cmd_filter(interaction: discord.Interaction):
            if self._get_filter_info:
                info = self._get_filter_info()
            else:
                from .config import config
                info = {
                    "min_mcap": config.min_mcap_usd,
                    "min_liquidity": config.min_liquidity_usd,
                    "min_volume": config.min_volume_24h_usd,
                    "min_tx": config.min_tx_count_24h,
                    "min_makers": config.min_makers_24h,
                    "max_drop": config.max_price_drop_24h,
                    "hours_back": config.scan_hours_back,
                    "top_n": config.top_n,
                }

            embed = discord.Embed(
                title="⚙️ 現在のフィルタ条件",
                color=0x5865F2,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(
                name="💰 時価総額 (MC)",
                value=f"≥ ${info['min_mcap']:,.0f}",
                inline=True,
            )
            embed.add_field(
                name="💧 流動性 (Liq)",
                value=f"≥ ${info['min_liquidity']:,.0f}",
                inline=True,
            )
            embed.add_field(
                name="📊 取引量 (Vol)",
                value=f"≥ ${info['min_volume']:,.0f}",
                inline=True,
            )
            embed.add_field(
                name="🔄 TX数",
                value=f"≥ {info['min_tx']}",
                inline=True,
            )
            embed.add_field(
                name="👥 Makers数",
                value=f"≥ {info['min_makers']}",
                inline=True,
            )
            embed.add_field(
                name="📉 暴落除外",
                value=f"> {info['max_drop']}%",
                inline=True,
            )
            embed.add_field(
                name="⏰ 時間窓",
                value=f"直近 {info['hours_back']}時間",
                inline=True,
            )
            embed.add_field(
                name="🏆 表示件数",
                value=f"Top {info['top_n']}",
                inline=True,
            )
            embed.set_footer(text="Sol Screener v5.6 | Railway環境変数で変更可能")

            await interaction.response.send_message(embed=embed)

        @self._tree.command(name="status", description="Botのステータスを表示")
        async def cmd_status(interaction: discord.Interaction):
            if self._get_status_info:
                info = self._get_status_info()
            else:
                info = {}

            from .state import StateManager
            state = StateManager()

            embed = discord.Embed(
                title="📊 Bot ステータス",
                color=0x00FF88,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(
                name="🤖 バージョン",
                value="v5.6",
                inline=True,
            )
            embed.add_field(
                name="📋 通知済みトークン",
                value=f"{state.get_notified_count()}件",
                inline=True,
            )
            embed.add_field(
                name="⏱️ 稼働状態",
                value="✅ 正常稼働中",
                inline=True,
            )
            embed.set_footer(text="Sol Screener v5.6")

            await interaction.response.send_message(embed=embed)

    async def shutdown(self):
        """Botを停止"""
        if self._client and self._running:
            await self._client.close()
            self._running = False
            logger.info("Discord Bot: シャットダウン完了")
