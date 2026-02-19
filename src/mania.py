"""
マニア向けスコアリング v4 — スマートマネー追跡 + 高度な分析

機能:
  1. スマートマネーウォレット追跡（既知の利益ウォレット）
  2. RugCheck topHolders からホエール分析
  3. Helius API（任意）でウォレット履歴分析
  4. 総合スマートマネースコア算出
"""
import asyncio
import logging
from typing import Optional

import aiohttp

from .config import config

logger = logging.getLogger(__name__)

# ── 既知のスマートマネーウォレット（公開情報ベース） ──
# ラベル付きで管理。環境変数 WATCH_WALLETS で追加可能。
KNOWN_SMART_WALLETS: dict[str, str] = {
    # 有名なSolanaトレーダー / ファンド（公開アドレス）
    # 実際の運用時はユーザーが環境変数で追加
}


class ManiaScorer:
    """スマートマネー追跡 & 高度なスコアリング"""

    RUGCHECK_API = "https://api.rugcheck.xyz/v1"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.smart_wallets = self._load_smart_wallets()
        self.rpc_url = self._get_rpc_url()

    def _load_smart_wallets(self) -> dict[str, str]:
        """環境変数 + 既知ウォレットをマージ"""
        wallets = dict(KNOWN_SMART_WALLETS)
        raw = config.watch_wallets
        if raw:
            for entry in raw.split(","):
                entry = entry.strip()
                if ":" in entry:
                    addr, label = entry.split(":", 1)
                    wallets[addr.strip()] = label.strip()
                elif entry:
                    wallets[entry] = "Custom"
        return wallets

    def _get_rpc_url(self) -> str:
        helius_key = getattr(config, "helius_api_key", "")
        if helius_key:
            return f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
        return "https://api.mainnet-beta.solana.com"

    # ================================================================
    # メイン: スマートマネーチェック
    # ================================================================
    async def check_smart_money(self, token_address: str) -> dict:
        """
        トークンのスマートマネー関与度を分析

        Returns:
            {
                "smart_money_score": 0-100,
                "whale_count": int,
                "notable_wallets": [{"address", "label", "pct", "pnl"}],
                "holder_concentration": float,
                "details": str,
            }
        """
        result = {
            "smart_money_score": 0,
            "whale_count": 0,
            "notable_wallets": [],
            "holder_concentration": 0.0,
            "details": "",
        }

        # ── RugCheck から topHolders を取得 ──
        top_holders = await self._get_top_holders(token_address)
        if not top_holders:
            result["details"] = "ホルダー情報取得不可"
            return result

        # ── ホエール分析 ──
        whale_threshold = 2.0  # 2% 以上保有 = ホエール
        whales = [h for h in top_holders if h.get("pct", 0) >= whale_threshold]
        result["whale_count"] = len(whales)

        # ── 既知スマートマネーとの照合 ──
        notable: list[dict] = []
        sm_score = 0

        for holder in top_holders[:20]:
            addr = holder.get("address", "")
            pct = holder.get("pct", 0)
            is_insider = holder.get("isInsider", False)

            if addr in self.smart_wallets:
                label = self.smart_wallets[addr]
                notable.append({
                    "address": addr,
                    "label": label,
                    "pct": round(pct, 2),
                    "pnl": 0,  # PnL は Helius API で後から取得可能
                    "is_insider": is_insider,
                })
                sm_score += 20  # 既知ウォレット1つにつき +20

        # ── ホエール保有パターン分析 ──
        total_whale_pct = sum(h.get("pct", 0) for h in whales)
        result["holder_concentration"] = round(total_whale_pct, 1)

        # ホエールが多いが分散している → ポジティブ
        if len(whales) >= 3 and total_whale_pct < 30:
            sm_score += 15  # 複数のホエールが分散保有
        elif len(whales) >= 2 and total_whale_pct < 20:
            sm_score += 10

        # インサイダーが少ない → ポジティブ
        insider_count = sum(1 for h in top_holders[:10] if h.get("isInsider"))
        if insider_count == 0:
            sm_score += 10
        elif insider_count <= 2:
            sm_score += 5

        # ── Helius API でウォレット履歴分析（オプション） ──
        if config.helius_api_key and whales:
            helius_bonus = await self._analyze_whale_history(
                [w.get("address", "") for w in whales[:5]],
                token_address,
            )
            sm_score += helius_bonus

        result["smart_money_score"] = min(100, sm_score)
        result["notable_wallets"] = notable

        # 詳細テキスト
        details = []
        if notable:
            details.append(f"既知SM: {len(notable)}件")
        details.append(f"ホエール: {len(whales)}件 ({total_whale_pct:.1f}%)")
        details.append(f"インサイダー: {insider_count}件")
        result["details"] = " | ".join(details)

        logger.info(f"  SM分析: score={result['smart_money_score']}, {result['details']}")
        return result

    # ================================================================
    # RugCheck topHolders 取得
    # ================================================================
    async def _get_top_holders(self, token_address: str) -> list[dict]:
        """RugCheck API から上位ホルダーを取得"""
        try:
            url = f"{self.RUGCHECK_API}/tokens/{token_address}/report/summary"
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("topHolders", [])
        except Exception as e:
            logger.debug(f"RugCheck topHolders error: {e}")
            return []

    # ================================================================
    # Helius API: ウォレット取引履歴分析
    # ================================================================
    async def _analyze_whale_history(
        self,
        wallet_addresses: list[str],
        token_address: str,
    ) -> int:
        """
        Helius API でホエールの過去取引を分析
        過去に利益を出しているウォレットが保有 → スコア加算
        """
        bonus = 0
        if not config.helius_api_key:
            return bonus

        for addr in wallet_addresses[:3]:
            try:
                url = f"https://api.helius.xyz/v0/addresses/{addr}/transactions"
                params = {"api-key": config.helius_api_key, "limit": 20}
                async with self.session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        continue
                    txns = await resp.json()

                # トークン関連の取引があるか
                token_related = sum(
                    1 for tx in txns
                    if token_address in str(tx.get("tokenTransfers", []))
                )

                if token_related > 0:
                    bonus += 5  # このウォレットがこのトークンを取引中

                # 過去の成功トレード数（簡易判定）
                swap_count = sum(
                    1 for tx in txns
                    if tx.get("type") in ("SWAP", "TOKEN_MINT")
                )
                if swap_count >= 10:
                    bonus += 3  # アクティブトレーダー

            except Exception as e:
                logger.debug(f"Helius wallet analysis error: {e}")

            await asyncio.sleep(0.3)

        return min(bonus, 20)  # 最大 +20

    # ================================================================
    # 一括チェック
    # ================================================================
    async def check_multiple(self, token_addresses: list[str]) -> dict[str, dict]:
        """複数トークンのスマートマネーを一括チェック"""
        results = {}
        for addr in token_addresses:
            try:
                result = await self.check_smart_money(addr)
                results[addr] = result
            except Exception as e:
                logger.warning(f"SM check failed for {addr}: {e}")
                results[addr] = {"smart_money_score": 0, "whale_count": 0}
            await asyncio.sleep(0.5)
        return results
