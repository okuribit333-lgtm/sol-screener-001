"""
Solana Auto Screener v5.2 — 完全統合版 main.py
Railway Worker モードで動作

■ 通知種別（Discordで色分け表示）:
  🔍 定期スキャン結果     — 1時間ごと（緑/黄/赤で色分け）
  ⚡ リアルタイム検知      — 5分ごと（急騰/TGE/卒業）
  🎓 Pump.fun 卒業        — Raydium上場の瞬間（紫色）
  ⚠️ 危険トークン         — ラグプル疑い（赤色）
  🧠 Smart Money          — 大口ウォレットの動き（金色）
  ✈️ エアドロップ情報     — 1日2回 9時/21時 JST（緑/黄/グレー）
  📊 日次レポート         — 毎朝のまとめ（青色）

■ 重複排除:
  全ての通知はStateManagerで管理。同じトークン/イベントは再通知しない。
  フルスキャン: token_address で管理
  卒業検知: grad_{token_address} で管理
  Meme急騰: meme_{token_address} で管理
  TGE検知: tge_{token_address} で管理
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# ── ログ設定 ──
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

# Railway ではファイルシステムが揮発性のため、FileHandler はオプション
if os.getenv("ENABLE_FILE_LOG", "false").lower() == "true":
    try:
        os.makedirs("logs", exist_ok=True)
        handlers.append(logging.FileHandler("logs/screener.log"))
    except Exception:
        pass

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=handlers,
)
logger = logging.getLogger("sol-screener")

# ── モジュールインポート ──
from src.config import config
from src.scanner import DexScreenerScanner
from src.scorer import Scorer
from src.notifier import Notifier
from src.safety import SafetyChecker
from src.state import StateManager
from src.pumpfun import PumpFunGraduationDetector
from src.mania import ManiaScorer
from src.expectation import ExpectationCalculator
from src.monitors import (
    WalletMonitor,
    LiquidityMonitor,
    SOLRangeMonitor,
)
from src.market_events import (
    TGEMonitor,
    NFTFloorMonitor,
    MemeChartMonitor,
)
from src.airdrop import AirdropScanner

# ── グローバル変数 ──
session: aiohttp.ClientSession = None
scanner: DexScreenerScanner = None
scorer: Scorer = None
notifier: Notifier = None
safety_checker: SafetyChecker = None
state: StateManager = None
pumpfun_detector: PumpFunGraduationDetector = None
mania_scorer: ManiaScorer = None
expectation_calc: ExpectationCalculator = None
wallet_monitor: WalletMonitor = None
liquidity_monitor: LiquidityMonitor = None
sol_range_monitor: SOLRangeMonitor = None
tge_monitor: TGEMonitor = None
nft_floor_monitor: NFTFloorMonitor = None
meme_monitor: MemeChartMonitor = None
airdrop_scanner: AirdropScanner = None


async def init():
    """全モジュールを初期化"""
    global session, scanner, scorer, notifier, safety_checker, state
    global pumpfun_detector, mania_scorer, expectation_calc
    global wallet_monitor, liquidity_monitor, sol_range_monitor
    global tge_monitor, nft_floor_monitor, meme_monitor
    global airdrop_scanner

    timeout = aiohttp.ClientTimeout(total=30)
    session = aiohttp.ClientSession(timeout=timeout)

    scanner = DexScreenerScanner(session)
    scorer = Scorer()
    notifier = Notifier(session)
    safety_checker = SafetyChecker(session)
    state = StateManager()
    pumpfun_detector = PumpFunGraduationDetector(session)
    mania_scorer = ManiaScorer(session)
    expectation_calc = ExpectationCalculator()
    wallet_monitor = WalletMonitor(session)
    liquidity_monitor = LiquidityMonitor(session)
    sol_range_monitor = SOLRangeMonitor(session)
    tge_monitor = TGEMonitor(session)
    nft_floor_monitor = NFTFloorMonitor(session)
    meme_monitor = MemeChartMonitor(session)
    airdrop_scanner = AirdropScanner(session)

    logger.info("✅ 全モジュール初期化完了（v5.2）")


# ============================================================
# リアルタイム監視（5分間隔）
# ============================================================
async def run_realtime_monitor():
    """リアルタイム監視サイクル（重複排除付き）"""
    logger.info("⚡ リアルタイム監視サイクル開始...")

    try:
        # ── 1. Pump.fun 卒業検知 ──
        if config.enable_pumpfun:
            try:
                graduations = await pumpfun_detector.detect_graduations()
                for grad in graduations:
                    state_key = f"grad_{grad.token_address}"
                    if state.is_notified(state_key):
                        continue

                    # 安全性チェック
                    from src.scanner import SolanaProject
                    dummy_project = SolanaProject(
                        token_address=grad.token_address,
                        pair_address=grad.pair_address,
                        name=grad.token_name,
                        symbol=grad.token_symbol,
                        created_at=grad.detected_at,
                        dex=grad.dex,
                        price_usd=grad.price_usd,
                        liquidity_usd=grad.initial_liquidity,
                        market_cap=grad.initial_mcap,
                        is_graduated=True,
                        graduation_source=grad.dex,
                    )

                    safety = await safety_checker.check(dummy_project)

                    # danger レベルは自動除外
                    if config.danger_auto_exclude and safety.get("risk_level") == "danger":
                        logger.info(f"  🚫 危険トークン除外: {grad.token_symbol}")
                        await notifier.send_danger_alert(dummy_project, safety)
                        state.mark_notified(state_key, grad.token_symbol)
                        continue

                    # スコアリング
                    sm = {}
                    if config.enable_smart_money:
                        sm = await mania_scorer.check_smart_money(grad.token_address)

                    scorer.score(dummy_project, safety=safety, smart_money=sm)

                    await notifier.send_graduation_alert(dummy_project, safety)

                    if sm and sm.get("smart_money_score", 0) >= 30:
                        await notifier.send_smart_money_alert(dummy_project, sm)

                    state.mark_notified(state_key, grad.token_symbol, dummy_project.total_score)

                pumpfun_detector.cleanup()
            except Exception as e:
                logger.error(f"卒業検知エラー: {e}")

        # ── 2. ウォレット監視 ──
        try:
            wallet_alerts = await wallet_monitor.check_all()
            for alert in wallet_alerts:
                wallet_key = f"wallet_{alert['signature']}"
                if state.is_notified(wallet_key):
                    continue
                await notifier.send_text(
                    f"👛 **{alert['label']}** に新規トランザクション\n"
                    f"TX: `{alert['signature'][:16]}...`\n"
                    f"[Solscan](https://solscan.io/tx/{alert['signature']})",
                    title="👛 ウォレット活動検知",
                )
                state.mark_notified(wallet_key, alert.get("label", "wallet"))
        except Exception as e:
            logger.debug(f"ウォレット監視エラー: {e}")

        # ── 3. 流動性監視 ──
        try:
            liq_alerts = await liquidity_monitor.check_all()
            for alert in liq_alerts:
                # 流動性は毎回通知OK（変動があるたびに通知）
                emoji = "📈" if alert["change_pct"] > 0 else "📉"
                await notifier.send_text(
                    f"{emoji} **{alert['symbol']}** の流動性が{alert['direction']}\n"
                    f"${alert['prev_liquidity']:,.0f} → ${alert['current_liquidity']:,.0f} "
                    f"({alert['change_pct']:+.1f}%)",
                    title=f"💧 流動性変動: {alert['symbol']}",
                )
        except Exception as e:
            logger.debug(f"流動性監視エラー: {e}")

        # ── 4. SOL レンジ監視 ──
        try:
            sol_alert = await sol_range_monitor.check()
            if sol_alert:
                await notifier.send_text(
                    sol_alert["message"],
                    title="💰 SOL 価格アラート",
                )
        except Exception as e:
            logger.debug(f"SOLレンジ監視エラー: {e}")

        # ── 5. Meme チャート急騰（重複排除付き） ──
        try:
            meme_alerts = await meme_monitor.scan_hot_memes()
            sent_count = 0
            for alert in meme_alerts:
                if sent_count >= 3:
                    break
                meme_key = f"meme_{alert.token_address}"
                if state.is_notified(meme_key):
                    continue
                await notifier.send_text(
                    f"**{alert.name}** (`{alert.symbol}`)\n"
                    f"5m: `{alert.price_change_5m:+.1f}%` | "
                    f"1h: `{alert.price_change_1h:+.1f}%` | "
                    f"Vol Surge: `{alert.volume_surge:.0f}%`\n"
                    f"[DexScreener](https://dexscreener.com/solana/{alert.token_address})",
                    title=f"🔥 Meme急騰: {alert.symbol}",
                )
                state.mark_notified(meme_key, alert.symbol)
                sent_count += 1
        except Exception as e:
            logger.debug(f"Meme監視エラー: {e}")

        # ── 6. TGE 検知（重複排除付き） ──
        try:
            tge_events = await tge_monitor.check_new_launches()
            sent_count = 0
            for event in tge_events:
                if sent_count >= 3:
                    break
                tge_key = f"tge_{event.token_address}"
                if state.is_notified(tge_key):
                    continue
                await notifier.send_text(
                    f"**{event.name}** (`{event.symbol}`)\n"
                    f"Platform: `{event.platform}`\n"
                    f"MC: `${event.initial_mcap:,.0f}` | Liq: `${event.initial_liquidity:,.0f}`\n"
                    f"[DexScreener](https://dexscreener.com/solana/{event.token_address})",
                    title=f"🚀 新規ローンチ: {event.symbol or event.name}",
                )
                state.mark_notified(tge_key, event.symbol or event.name)
                sent_count += 1
        except Exception as e:
            logger.debug(f"TGE検知エラー: {e}")

    except Exception as e:
        logger.error(f"リアルタイム監視エラー: {e}", exc_info=True)

    logger.info("⚡ リアルタイム監視サイクル完了")


# ============================================================
# 定期スキャン（1時間間隔）
# ============================================================
async def run_full_scan():
    """フルスキャン: 発見 → 安全性 → SM → スコア → 通知"""
    logger.info("🔍 フルスキャン開始...")

    try:
        # ── 1. スキャン ──
        projects = await scanner.fetch_new_pairs(hours_back=24)
        if not projects:
            logger.info("新規プロジェクトなし")
            return

        logger.info(f"発見: {len(projects)}件")

        # ── 2. 安全性チェック ──
        safety_results = await safety_checker.check_multiple(projects)

        # danger 除外
        if config.danger_auto_exclude:
            safe_projects = []
            for p in projects:
                s = safety_results.get(p.token_address, {})
                if s.get("risk_level") == "danger":
                    logger.info(f"  🚫 除外: {p.symbol} (danger)")
                    # danger通知も重複排除
                    danger_key = f"danger_{p.token_address}"
                    if not state.is_notified(danger_key):
                        await notifier.send_danger_alert(p, s)
                        state.mark_notified(danger_key, p.symbol)
                else:
                    safe_projects.append(p)
            projects = safe_projects

        if not projects:
            logger.info("安全フィルタ後: 0件")
            return

        # ── 3. スマートマネー分析 ──
        smart_money_results = {}
        if config.enable_smart_money:
            smart_money_results = await mania_scorer.check_multiple(
                [p.token_address for p in projects]
            )

        # ── 4. スコアリング ──
        for p in projects:
            safety = safety_results.get(p.token_address, {})
            sm = smart_money_results.get(p.token_address, {})
            scorer.score(p, safety=safety, smart_money=sm)

        # ── 5. ソート & 上位抽出（重複排除） ──
        projects.sort(key=lambda p: p.total_score, reverse=True)
        top = [p for p in projects[:config.top_n] if not state.is_notified(p.token_address)]

        if not top:
            logger.info("新規通知対象なし（全て通知済み）")
            return

        # ── 6. 通知 ──
        await notifier.send_scan_results(
            top,
            safety_results=safety_results,
            smart_money_results=smart_money_results,
            title=f"🔍 定期スキャン結果 (Top {len(top)})",
        )

        # スマートマネー通知（重複排除付き）
        for p in top:
            sm = smart_money_results.get(p.token_address, {})
            if sm and sm.get("smart_money_score", 0) >= 50:
                sm_key = f"sm_{p.token_address}"
                if not state.is_notified(sm_key):
                    await notifier.send_smart_money_alert(p, sm)
                    state.mark_notified(sm_key, p.symbol)

        # 通知済みマーク
        for p in top:
            state.mark_notified(p.token_address, p.symbol, p.total_score)

        state.cleanup()

    except Exception as e:
        logger.error(f"フルスキャンエラー: {e}", exc_info=True)

    logger.info("🔍 フルスキャン完了")


# ============================================================
# エアドロップスキャン（1日2回: 9時/21時 JST）
# ============================================================
async def run_airdrop_scan():
    """エアドロップ情報を複数ソースから収集してDiscordに通知"""
    logger.info("✈️ エアドロップスキャン開始...")

    try:
        # 全ソースからスキャン
        all_airdrops = await airdrop_scanner.scan_all()

        if not all_airdrops:
            logger.info("エアドロップ情報なし")
            return

        # 確度50%以上のみ通知
        high_conf = airdrop_scanner.filter_by_confidence(all_airdrops, min_confidence=50)

        if not high_conf:
            logger.info(f"エアドロ検出 {len(all_airdrops)}件、確度50%以上: 0件 → 通知スキップ")
            return

        # 上位20件に絞る
        top_airdrops = airdrop_scanner.get_top(high_conf, n=20)

        logger.info(
            f"✈️ エアドロ通知: {len(top_airdrops)}件 "
            f"(全{len(all_airdrops)}件中、確度50%以上: {len(high_conf)}件)"
        )

        # Discord に通知
        now_jst = datetime.now(timezone.utc).strftime("%H:%M UTC")
        await notifier.send_airdrop_report(
            top_airdrops,
            title=f"✈️ エアドロップ情報 ({now_jst})",
        )

        # カテゴリ別サマリーをログに出力
        by_cat = {}
        for a in top_airdrops:
            by_cat.setdefault(a.category or "other", []).append(a)
        for cat, items in sorted(by_cat.items()):
            logger.info(f"  [{cat}] {len(items)}件: {', '.join(a.name for a in items[:3])}...")

    except Exception as e:
        logger.error(f"エアドロップスキャンエラー: {e}", exc_info=True)

    logger.info("✈️ エアドロップスキャン完了")


# ============================================================
# 日次レポート
# ============================================================
async def run_daily_report():
    """日次レポートを生成して送信"""
    logger.info("📊 日次レポート生成中...")

    try:
        lines = [
            f"**日次レポート** — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            "",
            f"📋 通知済みトークン: {state.get_notified_count()}件",
            "",
        ]

        # フルスキャンを実行
        projects = await scanner.fetch_new_pairs(hours_back=24)
        if projects:
            safety_results = await safety_checker.check_multiple(projects[:10])
            for p in projects[:10]:
                safety = safety_results.get(p.token_address, {})
                scorer.score(p, safety=safety)

            projects.sort(key=lambda p: p.total_score, reverse=True)

            lines.append("**🏆 Top 10 トークン:**")
            for i, p in enumerate(projects[:10], 1):
                safety = safety_results.get(p.token_address, {})
                risk = safety.get("risk_level", "?")
                grad = " 🎓" if p.is_graduated else ""
                lines.append(
                    f"{i}. **{p.symbol}**{grad} — "
                    f"Score: {p.total_score:.1f} | "
                    f"MC: ${p.market_cap:,.0f} | "
                    f"Liq: ${p.liquidity_usd:,.0f} | "
                    f"Risk: {risk}"
                )

            # 卒業トークン
            graduated = [p for p in projects if p.is_graduated]
            if graduated:
                lines.append("")
                lines.append(f"**🎓 Pump.fun 卒業: {len(graduated)}件**")
                for p in graduated[:5]:
                    lines.append(f"  • {p.symbol} (Score: {p.total_score:.1f})")

        report_text = "\n".join(lines)
        await notifier.send_daily_report(report_text)

    except Exception as e:
        logger.error(f"日次レポートエラー: {e}", exc_info=True)

    logger.info("📊 日次レポート完了")


# ============================================================
# メイン
# ============================================================
async def main():
    """エントリーポイント"""
    logger.info("=" * 60)
    logger.info("🚀 Solana Auto Screener v5.2 起動")
    logger.info("=" * 60)

    # 設定確認
    if not config.discord_webhook_url:
        logger.warning("⚠️ DISCORD_WEBHOOK_URL が未設定です")

    logger.info(f"  リアルタイム間隔: {config.realtime_interval}分")
    logger.info(f"  スキャン間隔: {config.scan_interval_minutes}分")
    logger.info(f"  日次レポート: {config.daily_report_hour}時")
    logger.info(f"  エアドロスキャン: 9時/21時 JST")
    logger.info(f"  Pump.fun検知: {'ON' if config.enable_pumpfun else 'OFF'}")
    logger.info(f"  スマートマネー: {'ON' if config.enable_smart_money else 'OFF'}")

    await init()

    # 起動通知
    try:
        await notifier.send_text(
            "**Solana Auto Screener v5.2** が起動しました\n\n"
            f"⚡ リアルタイム: {config.realtime_interval}分間隔\n"
            f"🔍 フルスキャン: {config.scan_interval_minutes}分間隔\n"
            f"✈️ エアドロスキャン: 9時/21時 JST\n"
            f"🎓 Pump.fun検知: {'ON' if config.enable_pumpfun else 'OFF'}\n"
            f"🧠 スマートマネー: {'ON' if config.enable_smart_money else 'OFF'}\n"
            f"🛡️ 危険自動除外: {'ON' if config.danger_auto_exclude else 'OFF'}\n\n"
            "**■ 通知の見方:**\n"
            "🟢 緑 = 高スコア/高確度\n"
            "🟡 黄 = 中スコア/中確度\n"
            "🔴 赤 = 危険/低スコア\n"
            "🟣 紫 = Pump.fun卒業\n"
            "🟠 金 = Smart Money\n"
            "🔵 青 = レポート/情報",
            title="🚀 Bot 起動 v5.2",
        )
    except Exception as e:
        logger.warning(f"起動通知エラー: {e}")

    # スケジューラ設定
    scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")

    # リアルタイム監視
    scheduler.add_job(
        run_realtime_monitor,
        IntervalTrigger(minutes=config.realtime_interval),
        id="realtime_monitor",
        name="リアルタイム監視",
        max_instances=1,
        misfire_grace_time=60,
    )

    # フルスキャン
    scheduler.add_job(
        run_full_scan,
        IntervalTrigger(minutes=config.scan_interval_minutes),
        id="full_scan",
        name="フルスキャン",
        max_instances=1,
        misfire_grace_time=120,
    )

    # エアドロップスキャン（1日2回: 9時と21時 JST）
    scheduler.add_job(
        run_airdrop_scan,
        CronTrigger(hour="9,21", minute=0),
        id="airdrop_scan",
        name="エアドロップスキャン",
        max_instances=1,
        misfire_grace_time=300,
    )

    # 日次レポート
    scheduler.add_job(
        run_daily_report,
        CronTrigger(hour=config.daily_report_hour, minute=0),
        id="daily_report",
        name="日次レポート",
        max_instances=1,
    )

    scheduler.start()
    logger.info("📅 スケジューラ起動完了")

    # 初回実行
    logger.info("🔄 初回スキャン実行中...")
    await run_realtime_monitor()
    await run_full_scan()

    # 初回エアドロスキャンも実行
    logger.info("🔄 初回エアドロスキャン実行中...")
    await run_airdrop_scan()

    # 永続ループ
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("シャットダウン中...")
    finally:
        scheduler.shutdown(wait=False)
        if session and not session.closed:
            await session.close()
        logger.info("👋 シャットダウン完了")


if __name__ == "__main__":
    asyncio.run(main())
