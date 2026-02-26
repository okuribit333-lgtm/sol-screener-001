"""
スコアラー v5.8 — 信頼性チェック強化版

v5.8 変更点:
  - ソーシャルリンクスコアリングを3%→15%に引き上げ
  - Twitter/Discord/Telegram/Website の有無を個別評価
  - 安全性データ（LP lock, top holders, insider）をスコアに反映
  - 信頼性ボーナス: 複数ソーシャル存在 + LP locked + 低集中度
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
    """多次元スコアリングエンジン v5.8"""

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

        # ── マーケット指標（実データ — 合計60%） ──
        scores["liquidity"] = self._log_score(project.liquidity_usd, 5_000, 5_000_000)
        scores["volume"] = self._log_score(project.volume_24h_usd, 2_000, 10_000_000)
        scores["price_change"] = self._price_change_score(project.price_change_24h)
        scores["tx_count"] = self._log_score(project.tx_count_24h, 50, 50_000)
        scores["makers"] = self._log_score(project.makers_24h, 20, 10_000)

        # ── ソーシャル信頼性（合計15%） ──
        scores["social_presence"] = self._social_presence_score(project)

        # ── 安全性スコア（合計15%） ──
        scores["safety_score"] = self._safety_data_score(safety)

        # ── 年齢ボーナス（2%）──
        scores["age_bonus"] = self._age_score(project.created_at)

        # ── 重み付き合計 ──
        weighted = sum(
            scores.get(k, 0) * w for k, w in self.weights.items()
        )

        # ── 安全性ボーナス / ペナルティ（加算式） ──
        safety_adj = 0.0
        if safety:
            risk = safety.get("risk_level", "unknown")
            if risk == "danger":
                safety_adj = -25.0
            elif risk == "warning":
                safety_adj = -10.0
            elif risk == "safe":
                safety_adj = +5.0

            # RugCheck スコアが高い（低リスク）
            rc_score = safety.get("rugcheck_score")
            if rc_score is not None:
                if rc_score >= 800:
                    safety_adj += 3.0
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

        # ── 信頼性コンボボーナス ──
        trust_bonus = self._trust_combo_bonus(project, safety)

        total = weighted + safety_adj + graduation_bonus + smart_money_adj + trust_bonus
        total = max(0, min(100, total))

        # 結果保存
        project.scores = scores
        project.scores["_safety_adj"] = safety_adj
        project.scores["_graduation_bonus"] = graduation_bonus
        project.scores["_smart_money_adj"] = smart_money_adj
        project.scores["_trust_bonus"] = trust_bonus
        project.total_score = round(total, 1)

        return project.total_score

    # ================================================================
    # ソーシャル信頼性スコア（0-100）
    # ================================================================
    def _social_presence_score(self, project: SolanaProject) -> float:
        """
        ソーシャルリンクの存在を評価
        Twitter + Website + Discord + Telegram の有無で段階的にスコア
        """
        score = 0.0
        count = 0

        # Twitter（最重要: 40点）
        if project.twitter_handle:
            score += 40.0
            count += 1

        # Website（重要: 30点）
        if project.website_url:
            score += 30.0
            count += 1

        # Discord（中: 15点）
        if project.discord_url:
            score += 15.0
            count += 1

        # Telegram（中: 15点）
        if project.telegram_url:
            score += 15.0
            count += 1

        return min(100.0, score)

    # ================================================================
    # 安全性データスコア（0-100）
    # ================================================================
    def _safety_data_score(self, safety: Optional[dict]) -> float:
        """
        RugCheck / LP Lock / Top Holders / Mint権限のデータからスコアリング
        """
        if not safety:
            return 30.0  # データなし = 中立

        score = 0.0

        # 1. LP ロック状態（30点）
        lp_locked_pct = safety.get("lp_locked_pct", 0)
        if lp_locked_pct is not None and lp_locked_pct > 0:
            if lp_locked_pct >= 90:
                score += 30.0
            elif lp_locked_pct >= 50:
                score += 20.0
            elif lp_locked_pct > 0:
                score += 10.0
        elif safety.get("lp_locked"):
            score += 15.0  # 旧形式の互換
        # LP未ロック = 0点

        # 2. ミント権限（25点）
        mint_auth = safety.get("mint_authority")
        if mint_auth == "None":
            score += 25.0  # 放棄済み = 最高
        elif mint_auth is None:
            score += 10.0  # 不明 = 中立
        # 未放棄 = 0点

        # 3. フリーズ権限（10点）
        freeze_auth = safety.get("freeze_authority")
        if freeze_auth == "None":
            score += 10.0  # なし = 安全
        elif freeze_auth is None:
            score += 5.0   # 不明

        # 4. Top Holders 集中度（25点）
        top_pct = safety.get("top_holders_pct")
        if top_pct is not None:
            if top_pct < 20:
                score += 25.0  # 分散型
            elif top_pct < 30:
                score += 20.0
            elif top_pct < 50:
                score += 10.0
            # 50%以上 = 0点（集中リスク）

        # 5. インサイダー検出（10点）
        insider_count = safety.get("insider_count", 0)
        if insider_count == 0:
            score += 10.0
        elif insider_count <= 2:
            score += 5.0
        # 3以上 = 0点

        return min(100.0, score)

    # ================================================================
    # 信頼性コンボボーナス
    # ================================================================
    def _trust_combo_bonus(self, project: SolanaProject, safety: Optional[dict]) -> float:
        """
        複数の信頼性指標が揃っている場合のボーナス
        「公式サイト + Twitter + LP locked + 低集中度」= 真面目なプロジェクト
        """
        bonus = 0.0
        checks_passed = 0

        # ソーシャル存在
        if project.twitter_handle:
            checks_passed += 1
        if project.website_url:
            checks_passed += 1
        if project.discord_url:
            checks_passed += 1

        # 安全性
        if safety:
            if safety.get("lp_locked") or (safety.get("lp_locked_pct", 0) or 0) > 50:
                checks_passed += 1
            if safety.get("mint_authority") == "None":
                checks_passed += 1
            top_pct = safety.get("top_holders_pct")
            if top_pct is not None and top_pct < 30:
                checks_passed += 1

        # コンボボーナス
        if checks_passed >= 5:
            bonus = 8.0   # 5/6以上 = 非常に信頼性が高い
        elif checks_passed >= 4:
            bonus = 5.0
        elif checks_passed >= 3:
            bonus = 3.0

        return bonus

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
            return 30.0
        elif change_24h >= 100:
            return 50.0
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
        elif change_24h >= -50:
            return max(5, 20 + change_24h * 0.5)
        else:
            return 0.0

    @staticmethod
    def _age_score(created_at: datetime) -> float:
        """
        ペア年齢スコア
        3〜12時間: 最高評価（初期の熱狂期、まだ早期参入可能）
        """
        now = datetime.now(timezone.utc)
        age_hours = (now - created_at).total_seconds() / 3600

        if age_hours < 1:
            return 40.0
        elif age_hours < 3:
            return 70.0
        elif age_hours < 12:
            return 100.0
        elif age_hours < 24:
            return 60.0
        else:
            return 30.0
