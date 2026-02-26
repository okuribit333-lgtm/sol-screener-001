"""
スコアラー v5.5 — 実データ重視の多次元スコアリング

v5.5 変更点:
  - ソーシャル未取得分（45%）を実データ指標に再配分
  - makers（ユニークトレーダー数）を新規追加
  - twitter_exists（Xアカウント有無）を新規追加
  - age_bonus（ペア作成からの経過時間）を新規追加
  - スコアは 0-100 で正規化
"""
import math
import logging
from datetime import datetime, timezone
from typing import Optional

from .config import config
from .scanner import SolanaProject

logger = logging.getLogger(__name__)


class Scorer:
    """多次元スコアリングエンジン v5.5"""

    def __init__(self):
        self.weights = config.weights

    def score(
        self,
        project: SolanaProject,
        safety: Optional[dict] = None,
        smart_money: Optional[dict] = None,
    ) -> float:
        """
        プロジェクトを総合スコアリング
        safety: SafetyChecker.check() の結果
        smart_money: ManiaScorer.check_smart_money() の結果
        """
        scores: dict[str, float] = {}

        # ── マーケット指標（実データ — 合計74%） ──
        scores["liquidity"] = self._log_score(project.liquidity_usd, 5_000, 5_000_000)
        scores["volume"] = self._log_score(project.volume_24h_usd, 2_000, 10_000_000)
        scores["price_change"] = self._price_change_score(project.price_change_24h)
        scores["tx_count"] = self._log_score(project.tx_count_24h, 50, 50_000)
        scores["makers"] = self._log_score(project.makers_24h, 20, 10_000)

        # ── プロジェクト信頼性指標（合計14%） ──
        scores["website_exists"] = 80.0 if project.website_url else 0.0
        scores["twitter_exists"] = 80.0 if project.twitter_handle else 0.0
        scores["audit_exists"] = 0.0  # 将来的に監査情報を取得

        # ── 年齢ボーナス（2%）──
        # 作成から3〜12時間が最も高評価（初期の熱狂期）
        scores["age_bonus"] = self._age_score(project.created_at)

        # ── 重み付き合計 ──
        weighted = sum(
            scores.get(k, 0) * w for k, w in self.weights.items()
        )

        # ── 安全性ボーナス / ペナルティ ──
        safety_adj = 0.0
        if safety:
            risk = safety.get("risk_level", "unknown")
            if risk == "danger":
                safety_adj = -25.0
            elif risk == "warning":
                safety_adj = -10.0
            elif risk == "safe":
                safety_adj = +5.0

            # LP ロック済みボーナス
            if safety.get("lp_locked"):
                safety_adj += 5.0

            # ミント権限放棄ボーナス
            if safety.get("mint_authority") == "None":
                safety_adj += 5.0

            # RugCheck スコアが高い（低リスク）
            rc_score = safety.get("rugcheck_score")
            if rc_score is not None:
                if rc_score >= 800:
                    safety_adj += 5.0
                elif rc_score <= 200:
                    safety_adj -= 10.0

        # ── Pump.fun 卒業ボーナス ──
        graduation_bonus = 0.0
        if project.is_graduated:
            graduation_bonus = 10.0
            logger.info(f"  🎓 卒業ボーナス +10: {project.symbol}")

        # ── スマートマネーボーナス ──
        smart_money_adj = 0.0
        if smart_money:
            sm_score = smart_money.get("smart_money_score", 0)
            if sm_score >= 80:
                smart_money_adj = 15.0
            elif sm_score >= 50:
                smart_money_adj = 8.0
            elif sm_score >= 20:
                smart_money_adj = 3.0

            whale_count = smart_money.get("whale_count", 0)
            if whale_count >= 3:
                smart_money_adj += 5.0

        # ── ソーシャル存在ボーナス（Twitter + Website 両方あれば追加） ──
        social_bonus = 0.0
        if project.twitter_handle and project.website_url:
            social_bonus = 3.0  # 両方揃っている = 真面目なプロジェクト

        total = weighted + safety_adj + graduation_bonus + smart_money_adj + social_bonus
        total = max(0, min(100, total))

        # 結果保存
        project.scores = scores
        project.scores["_safety_adj"] = safety_adj
        project.scores["_graduation_bonus"] = graduation_bonus
        project.scores["_smart_money_adj"] = smart_money_adj
        project.scores["_social_bonus"] = social_bonus
        project.total_score = round(total, 1)

        return project.total_score

    # ================================================================
    # スコア関数
    # ================================================================
    @staticmethod
    def _log_score(value: float, low: float, high: float) -> float:
        """対数スケールで 0-100 にマッピング"""
        if value <= 0:
            return 0.0
        if value <= low:
            return (value / low) * 20
        log_val = math.log10(value)
        log_low = math.log10(low)
        log_high = math.log10(high)
        if log_high == log_low:
            return 50.0
        ratio = (log_val - log_low) / (log_high - log_low)
        return min(100.0, 20 + ratio * 80)

    @staticmethod
    def _price_change_score(change_24h: float) -> float:
        """
        価格変動スコア
        +10~50% → 高評価（健全な上昇）
        +50%超 → やや減点（過熱）
        +200%超 → さらに減点（バブル警戒）
        マイナス → 低評価
        """
        if change_24h >= 200:
            return 30.0   # バブル警戒
        elif change_24h >= 100:
            return 50.0   # 過熱気味
        elif change_24h >= 50:
            return 70.0
        elif change_24h >= 20:
            return 90.0
        elif change_24h >= 10:
            return 100.0  # 最も健全な上昇
        elif change_24h >= 0:
            return 60.0 + change_24h * 4
        elif change_24h >= -20:
            return max(20, 60 + change_24h * 2)
        elif change_24h >= -50:
            return max(5, 20 + change_24h * 0.5)
        else:
            return 0.0    # -50%超は評価ゼロ

    @staticmethod
    def _age_score(created_at: datetime) -> float:
        """
        ペア年齢スコア
        3〜12時間: 最高評価（初期の熱狂期、まだ早期参入可能）
        1〜3時間: やや高い（超初期、リスク高め）
        12〜24時間: 中程度（安定期に入りつつある）
        24時間超: 低評価（もう初期ではない）
        """
        now = datetime.now(timezone.utc)
        age_hours = (now - created_at).total_seconds() / 3600

        if age_hours < 1:
            return 40.0   # 超初期 — リスク高い
        elif age_hours < 3:
            return 70.0   # 初期 — まだリスクあり
        elif age_hours < 12:
            return 100.0  # ゴールデンタイム
        elif age_hours < 24:
            return 60.0   # 安定期
        else:
            return 30.0   # もう初期ではない
