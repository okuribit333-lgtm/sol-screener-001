"""
状態管理 v5.4 — 24時間TTL付き通知重複排除

■ 改善点 (v5.4):
  - 24時間TTL: 通知済みエントリは24時間後に自動失効
  - 正規化キー: エアドロップ名の正規化でスペース/大文字小文字の揺れを吸収
  - 自動クリーンアップ: 期限切れエントリを定期的に削除
  - メモリ上限: 最大2000エントリ（超過時は古い順に削除）
"""
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILE = os.getenv("STATE_FILE", "data/state.json")

# デフォルトTTL: 24時間
DEFAULT_TTL_HOURS = 24


class StateManager:
    """通知済みトークンの状態管理（24時間TTL付き）"""

    MAX_ENTRIES = 2000

    def __init__(self, filepath: str = STATE_FILE, ttl_hours: int = DEFAULT_TTL_HOURS):
        self.filepath = filepath
        self.ttl_hours = ttl_hours
        self.notified: dict[str, dict] = {}
        self._load()
        # 起動時にクリーンアップ
        self._cleanup_expired()

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

    @staticmethod
    def normalize_key(key: str) -> str:
        """キーを正規化（小文字化、スペース→アンダースコア、特殊文字除去）"""
        key = key.lower().strip()
        key = re.sub(r'\s+', '_', key)
        key = re.sub(r'[^a-z0-9_\-]', '', key)
        return key

    def is_notified(self, key: str) -> bool:
        """既に通知済みか確認（TTL考慮）"""
        entry = self.notified.get(key)
        if entry is None:
            return False

        # TTLチェック
        notified_at_str = entry.get("notified_at", "")
        if notified_at_str:
            try:
                notified_at = datetime.fromisoformat(notified_at_str.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - notified_at > timedelta(hours=self.ttl_hours):
                    # 期限切れ → 削除して未通知扱い
                    del self.notified[key]
                    return False
            except (ValueError, TypeError):
                pass

        return True

    def mark_notified(self, key: str, symbol: str = "", score: float = 0.0):
        """通知済みとしてマーク"""
        self.notified[key] = {
            "symbol": symbol,
            "score": score,
            "notified_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def get_notified_count(self) -> int:
        """有効な（TTL内の）通知済み件数"""
        self._cleanup_expired()
        return len(self.notified)

    def _cleanup_expired(self):
        """期限切れエントリを削除"""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self.ttl_hours)
        expired_keys = []

        for key, entry in self.notified.items():
            notified_at_str = entry.get("notified_at", "")
            if notified_at_str:
                try:
                    notified_at = datetime.fromisoformat(notified_at_str.replace("Z", "+00:00"))
                    if notified_at < cutoff:
                        expired_keys.append(key)
                except (ValueError, TypeError):
                    # パース不能 → 古いエントリとみなして削除
                    expired_keys.append(key)
            else:
                # notified_at がない → 古い形式、削除
                expired_keys.append(key)

        if expired_keys:
            for key in expired_keys:
                del self.notified[key]
            logger.info(f"期限切れエントリ削除: {len(expired_keys)}件 (残: {len(self.notified)}件)")
            self._save()

    def cleanup(self, max_entries: int = None):
        """期限切れ削除 + エントリ数上限チェック"""
        self._cleanup_expired()

        limit = max_entries or self.MAX_ENTRIES
        if len(self.notified) > limit:
            # 古い順にソートして半分に削減
            sorted_items = sorted(
                self.notified.items(),
                key=lambda x: x[1].get("notified_at", ""),
                reverse=True,
            )
            self.notified = dict(sorted_items[:limit // 2])
            self._save()
            logger.info(f"状態クリーンアップ: {len(self.notified)}件に削減")
