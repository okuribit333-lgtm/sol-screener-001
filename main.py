"""
SOL Auto Screener - 統合版
Railway / Oracle Cloud / ローカル どこでも動く

使い方:
  python main.py          → 1回実行
  python main.py daemon   → 定期実行（Railway / VPS向け）
"""
import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.config import config
from src.scanner import DexScreenerScanner
from src.scorer import ScoringEngine
from src.notifier import NotificationHub

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("screener.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("sol-screener")
JST = timezone(timedelta(hours=9))


async def run_screening_cycle():
    """1回のスクリーニングサイクル"""
    now = datetime.now(JST)
    logger.info(f"{'='*50}")
    logger.info(f"🚀 スクリーニング開始: {now.strftime('%Y/%m/%d %H:%M:%S')} JST")

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60),
        headers={"User-Agent": "SolAutoScreener/2.0"}
    ) as session:

        # Step 1: スキャン
        logger.info("📡 Step 1: 新規プロジェクトスキャン...")
        scanner = DexScreenerScanner(session)
        projects = await scanner.fetch_new_pairs(hours_back=24)

        if not projects:
            logger.info("⚠️ 新規プロジェクトなし")
            return

        # GitHub情報の補強
        for p in projects[:30]:
            await scanner.enrich_github(p)

        # Step 2: スコアリング
        logger.info(f"📊 Step 2: {len(projects)}件をスコアリング...")
        engine = ScoringEngine(session)
        scored = await engine.score_projects(projects)

        # Step 3: 上位N件
        top = scored[:config.top_n]
        logger.info(f"🏆 Step 3: TOP {config.top_n}:")
        for i, p in enumerate(top, 1):
            logger.info(f"  #{i} {p}")

        # Step 4: 通知
        logger.info("📢 Step 4: 通知送信...")
        hub = NotificationHub(session)
        await hub.broadcast(top)

        # Step 5: 人が判断
        logger.info("✋ Step 5: セキュリティ確認・ガス調整は人が判断")
        logger.info(f"🏁 完了: {datetime.now(JST).strftime('%H:%M:%S')} JST")

    return top


async def run_daemon():
    """定期実行デーモン（Railway / VPS向け）"""
    scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")

    # 毎朝定時
    scheduler.add_job(run_screening_cycle, "cron", hour=config.morning_scan_hour, minute=0, id="morning")

    # 定期間隔
    scheduler.add_job(run_screening_cycle, "interval", minutes=config.scan_interval_minutes, id="interval")

    scheduler.start()
    logger.info(f"⏰ デーモン起動")
    logger.info(f"   毎朝 {config.morning_scan_hour}:00 JST + {config.scan_interval_minutes}分間隔")

    # 起動直後に1回実行
    await run_screening_cycle()

    # シグナルハンドリング
    stop = asyncio.Event()

    def shutdown():
        logger.info("シャットダウン...")
        stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            # Windows対応
            pass

    await stop.wait()
    scheduler.shutdown()
    logger.info("👋 停止完了")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"

    if mode == "once":
        print("🔍 1回実行...")
        asyncio.run(run_screening_cycle())
    elif mode == "daemon":
        print("🔄 デーモンモード...")
        asyncio.run(run_daemon())
    else:
        print("Usage: python main.py [once|daemon]")
        sys.exit(1)


if __name__ == "__main__":
    main()
