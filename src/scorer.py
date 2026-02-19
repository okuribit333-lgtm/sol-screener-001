"""
スコアラー v4 — 多次元スコアリング
マーケット指標 + ソーシャル + 安全性 + Pump.fun卒業ボーナス + スマートマネー

スコアは 0-100 で正規化
"""
import math
import logging
from typing import Optional

from .config import config
from .scanner import SolanaProject

logger = logging.getLogger(__name__)


class Scorer:
    """多次元スコアリングエンジン"""

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

        # ── マーケット指標 ──
        scores["liquidity"] = self._log_score(project.liquidity_usd, 1000, 5_000_000)
        scores["volume"] = self._log_score(project.volume_24h_usd, 500, 10_000_000)
        scores["price_change"] = self._price_change_score(project.price_change_24h)
        scores["tx_count"] = self._log_score(project.tx_count_24h, 10, 50_000)

        # ── ソーシャル（scanner では直接取得しないので 0 or 推定） ──
        scores["twitter_followers"] = 0.0
        scores["twitter_engagement"] = 0.0
        scores["discord_members"] = 0.0
        scores["discord_activity"] = 0.0
        scores["github_commits"] = 0.0
        scores["github_stars"] = 0.0
        scores["website_exists"] = 80.0 if project.website_url else 0.0
        scores["audit_exists"] = 0.0

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

        total = weighted + safety_adj + graduation_bonus + smart_money_adj
        total = max(0, min(100, total))

        # 結果保存
        project.scores = scores
        project.scores["_safety_adj"] = safety_adj
        project.scores["_graduation_bonus"] = graduation_bonus
        project.scores["_smart_money_adj"] = smart_money_adj
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
        +10~50% → 高評価
        +50%超 → やや減点（過熱）
        マイナス → 低評価
        """
        if change_24h >= 100:
            return 50.0  # 過熱気味
        elif change_24h >= 50:
            return 70.0
        elif change_24h >= 20:
            return 90.0
        elif change_24h >= 10:
            return 100.0
        elif change_24h >= 0:
            return 60.0 + change_24h * 4
        elif change_24h >= -20:
            return max(20, 60 + change_24h * 2)
        else:
            return max(0, 20 + change_24h)
