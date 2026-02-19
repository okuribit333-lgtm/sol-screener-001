"""
状態管理 v4 — 通知済みトークンの追跡
JSON ファイルベース（Railway 環境では揮発性だが再起動間は保持）
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILE = os.getenv("STATE_FILE", "data/state.json")


class StateManager:
    """通知済みトークンの状態管理"""

    def __init__(self, filepath: str = STATE_FILE):
        self.filepath = filepath
        self.notified: dict[str, dict] = {}
        self._load()

    def _load(self):
        """ファイルから状態を読み込み"""
        try:
            if os.path.exists(self.filepath):
                with open(self.filepath, "r") as f:
                    self.notified = json.load(f)
                logger.info(f"状態読み込み: {len(self.notified)}件")
        except Exception as e:
            logger.warning(f"状態ファイル読み込みエラー: {e}")
            self.notified = {}

    def _save(self):
        """状態をファイルに保存"""
        try:
            os.makedirs(os.path.dirname(self.filepath) or ".", exist_ok=True)
            with open(self.filepath, "w") as f:
                json.dump(self.notified, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"状態ファイル保存エラー: {e}")

    def is_notified(self, token_address: str) -> bool:
        """既に通知済みか確認"""
        return token_address in self.notified

    def mark_notified(self, token_address: str, symbol: str = "", score: float = 0.0):
        """通知済みとしてマーク"""
        self.notified[token_address] = {
            "symbol": symbol,
            "score": score,
            "notified_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def get_notified_count(self) -> int:
        return len(self.notified)

    def cleanup(self, max_entries: int = 1000):
        """古いエントリを削除"""
        if len(self.notified) > max_entries:
            sorted_items = sorted(
                self.notified.items(),
                key=lambda x: x[1].get("notified_at", ""),
                reverse=True,
            )
            self.notified = dict(sorted_items[:max_entries // 2])
            self._save()
            logger.info(f"状態クリーンアップ: {len(self.notified)}件に削減")
