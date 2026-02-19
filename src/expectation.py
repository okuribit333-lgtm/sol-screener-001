"""
期待値計算 v4 — トークンの将来性を推定

DexScreener のマーケットデータから、
「このトークンが 2x / 5x / 10x する確率」を簡易推定。
投資判断の参考情報として通知に添付。
"""
import math
import logging
from dataclasses import dataclass
from typing import Optional

from .scanner import SolanaProject

logger = logging.getLogger(__name__)


@dataclass
class ExpectationResult:
    """期待値計算結果"""
    symbol: str
    current_mcap: float
    target_2x_mcap: float
    target_5x_mcap: float
    target_10x_mcap: float
    probability_2x: float  # 0-100%
    probability_5x: float
    probability_10x: float
    expected_value: float   # 期待値（倍率）
    risk_reward: str        # "良好" / "普通" / "リスク高"
    reasoning: str

    def summary(self) -> str:
        return (
            f"📐 {self.symbol} 期待値分析\n"
            f"  現在 MC: ${self.current_mcap:,.0f}\n"
            f"  2x ({self.probability_2x:.0f}%) → ${self.target_2x_mcap:,.0f}\n"
            f"  5x ({self.probability_5x:.0f}%) → ${self.target_5x_mcap:,.0f}\n"
            f"  10x ({self.probability_10x:.0f}%) → ${self.target_10x_mcap:,.0f}\n"
            f"  期待値: {self.expected_value:.2f}x | {self.risk_reward}\n"
            f"  {self.reasoning}"
        )


class ExpectationCalculator:
    """トークンの期待値を計算"""

    # 時価総額レンジ別の成長確率（経験的パラメータ）
    MCAP_GROWTH_PROBS = {
        # (mcap_range, 2x_prob, 5x_prob, 10x_prob)
        "micro":    (0, 100_000, 40, 15, 5),
        "small":    (100_000, 1_000_000, 30, 10, 3),
        "mid":      (1_000_000, 10_000_000, 20, 5, 1.5),
        "large":    (10_000_000, 100_000_000, 10, 2, 0.5),
        "mega":     (100_000_000, float("inf"), 5, 1, 0.2),
    }

    def calculate(
        self,
        project: SolanaProject,
        safety: Optional[dict] = None,
    ) -> ExpectationResult:
        """期待値を計算"""
        mcap = project.market_cap or project.fdv or 0

        if mcap <= 0:
            return ExpectationResult(
                symbol=project.symbol,
                current_mcap=0,
                target_2x_mcap=0, target_5x_mcap=0, target_10x_mcap=0,
                probability_2x=0, probability_5x=0, probability_10x=0,
                expected_value=0, risk_reward="データ不足",
                reasoning="時価総額データなし",
            )

        # ベース確率
        prob_2x, prob_5x, prob_10x = self._base_probabilities(mcap)

        # 調整要因
        adjustments = []

        # 流動性が高い → 安定性UP
        if project.liquidity_usd > 100_000:
            prob_2x *= 1.2
            prob_5x *= 1.1
            adjustments.append("高流動性(+)")

        # 取引量が活発
        if project.volume_24h_usd > 500_000:
            prob_2x *= 1.15
            prob_5x *= 1.1
            adjustments.append("高取引量(+)")

        # 価格上昇トレンド
        if project.price_change_24h > 20:
            prob_2x *= 1.1
            adjustments.append("上昇トレンド(+)")
        elif project.price_change_24h < -30:
            prob_2x *= 0.8
            adjustments.append("下落トレンド(-)")

        # Pump.fun 卒業
        if project.is_graduated:
            prob_2x *= 1.3
            prob_5x *= 1.2
            prob_10x *= 1.1
            adjustments.append("卒業ボーナス(++)")

        # 安全性
        if safety:
            risk = safety.get("risk_level", "unknown")
            if risk == "danger":
                prob_2x *= 0.3
                prob_5x *= 0.1
                prob_10x *= 0.05
                adjustments.append("危険トークン(---)")
            elif risk == "warning":
                prob_2x *= 0.7
                prob_5x *= 0.5
                adjustments.append("警告あり(-)")
            elif risk == "safe":
                prob_2x *= 1.1
                adjustments.append("安全確認(+)")

        # 確率を 0-100 にクランプ
        prob_2x = min(100, max(0, prob_2x))
        prob_5x = min(100, max(0, prob_5x))
        prob_10x = min(100, max(0, prob_10x))

        # 期待値 = Σ(確率 × 倍率) + (1-Σ確率) × 0.5（損失想定）
        ev = (
            (prob_2x / 100 * 2)
            + (prob_5x / 100 * 5)
            + (prob_10x / 100 * 10)
            + ((100 - prob_2x) / 100 * 0.5)
        )

        # リスクリワード判定
        if ev >= 2.0:
            rr = "良好"
        elif ev >= 1.0:
            rr = "普通"
        else:
            rr = "リスク高"

        reasoning = " / ".join(adjustments) if adjustments else "標準パラメータ"

        return ExpectationResult(
            symbol=project.symbol,
            current_mcap=mcap,
            target_2x_mcap=mcap * 2,
            target_5x_mcap=mcap * 5,
            target_10x_mcap=mcap * 10,
            probability_2x=round(prob_2x, 1),
            probability_5x=round(prob_5x, 1),
            probability_10x=round(prob_10x, 1),
            expected_value=round(ev, 2),
            risk_reward=rr,
            reasoning=reasoning,
        )

    def _base_probabilities(self, mcap: float) -> tuple[float, float, float]:
        """時価総額レンジに基づくベース確率"""
        for _name, (low, high, p2, p5, p10) in self.MCAP_GROWTH_PROBS.items():
            if low <= mcap < high:
                return p2, p5, p10
        return 5, 1, 0.2
