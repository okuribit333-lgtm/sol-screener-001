"""
安全性チェック v5.8 — 信頼性チェック強化版

v5.8 変更点:
  - RugCheck フルレポート（/report）に切替: LP lock%, insider, markets 取得
  - LP locked percentage を数値で取得
  - インサイダーネットワーク検出
  - Top Holders 集中度チェック強化（config閾値対応）
  - launchpad / deployPlatform 情報取得
  - 安全性サマリーを通知用に構造化
"""
import asyncio
import logging
from typing import Optional

import aiohttp

from .config import config
from .scanner import SolanaProject

logger = logging.getLogger(__name__)


class SafetyChecker:
    """
    RugCheck.xyz フルレポート + Solana RPC でトークンの安全性をチェック
    """

    RUGCHECK_API = "https://api.rugcheck.xyz/v1"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.rpc_url = self._get_rpc_url()

    def _get_rpc_url(self) -> str:
        helius_key = getattr(config, "helius_api_key", "")
        if helius_key:
            return f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
        return "https://api.mainnet-beta.solana.com"

    # ================================================================
    # メイン: 単体チェック
    # ================================================================
    async def check(self, project: SolanaProject) -> dict:
        """全チェックを実行して結果を返す"""
        results = await asyncio.gather(
            self._rugcheck_full(project.token_address),
            self._check_mint_authority_rpc(project.token_address),
            return_exceptions=True,
        )

        rugcheck = results[0] if not isinstance(results[0], Exception) else {}
        mint_info = results[1] if not isinstance(results[1], Exception) else {}

        safety: dict = {
            "is_safe": True,
            "risk_level": "unknown",
            "warnings": [],
            # RugCheck
            "rugcheck_score": None,
            "rugcheck_normalized": None,
            "rugcheck_status": None,
            # 権限
            "mint_authority": None,
            "freeze_authority": None,
            # LP
            "lp_locked": None,
            "lp_locked_pct": None,
            "lp_locked_usd": None,
            "lp_providers": None,
            # Holders
            "top_holders_pct": None,
            "top_holders_detail": [],
            "insider_count": 0,
            "total_holders": None,
            # メタ
            "launchpad": None,
            "deploy_platform": None,
            "creator": None,
            "creator_balance_pct": None,
        }

        # ── RugCheck フルレポート結果を反映 ──
        if rugcheck:
            self._process_rugcheck(rugcheck, safety)

        # ── ミント権限（RPC 直接チェック — RugCheckのフォールバック） ──
        if mint_info:
            # RugCheckで取得できなかった場合のみRPC結果を使用
            if safety["mint_authority"] is None:
                safety["mint_authority"] = mint_info.get("mint_authority")
            if safety["freeze_authority"] is None:
                safety["freeze_authority"] = mint_info.get("freeze_authority")

        # ── ミント/フリーズ権限の警告 ──
        if safety["mint_authority"] and safety["mint_authority"] != "None":
            safety["warnings"].append("🔴 ミント権限が放棄されていない（無限発行リスク）")
        if safety["freeze_authority"] and safety["freeze_authority"] != "None":
            safety["warnings"].append("🟡 フリーズ権限あり（アカウント凍結リスク）")

        # ── リスクレベル判定 ──
        danger_count = sum(1 for w in safety["warnings"] if "🔴" in w)
        warn_count = sum(1 for w in safety["warnings"] if "🟡" in w)

        if danger_count >= 2:
            safety["risk_level"] = "danger"
            safety["is_safe"] = False
        elif danger_count >= 1:
            safety["risk_level"] = "warning"
        elif warn_count >= 2:
            safety["risk_level"] = "warning"
        else:
            safety["risk_level"] = "safe"

        return safety

    # ================================================================
    # RugCheck フルレポート処理
    # ================================================================
    def _process_rugcheck(self, data: dict, safety: dict):
        """RugCheckフルレポートのデータを安全性辞書に反映"""

        # スコア
        score = data.get("score", 0)
        normalized = data.get("score_normalised", None)
        safety["rugcheck_score"] = score
        safety["rugcheck_normalized"] = normalized

        # 権限（RugCheckから直接取得）
        mint_auth = data.get("mintAuthority")
        freeze_auth = data.get("freezeAuthority")
        if mint_auth is not None:
            safety["mint_authority"] = mint_auth if mint_auth else "None"
        if freeze_auth is not None:
            safety["freeze_authority"] = freeze_auth if freeze_auth else "None"

        # メタ情報
        safety["launchpad"] = data.get("launchpad")
        safety["deploy_platform"] = data.get("deployPlatform")
        safety["total_holders"] = data.get("totalHolders")
        safety["lp_providers"] = data.get("totalLPProviders")

        # Creator情報
        creator = data.get("creator")
        if creator:
            safety["creator"] = creator[:12] + "..." if len(creator) > 12 else creator
        creator_balance = data.get("creatorBalance", 0)
        if creator_balance and creator_balance > 0:
            safety["creator_balance_pct"] = creator_balance

        # ── LP Lock 情報（marketsから集計） ──
        markets = data.get("markets", [])
        if markets:
            total_lp_locked_usd = 0
            best_lock_pct = 0
            for m in markets:
                lp = m.get("lp", {})
                if isinstance(lp, dict):
                    lock_pct = lp.get("lpLockedPct", 0) or 0
                    lock_usd = lp.get("lpLockedUSD", 0) or 0
                    if lock_pct > best_lock_pct:
                        best_lock_pct = lock_pct
                    total_lp_locked_usd += lock_usd

            safety["lp_locked_pct"] = round(best_lock_pct, 1)
            safety["lp_locked_usd"] = round(total_lp_locked_usd, 2)
            safety["lp_locked"] = best_lock_pct > 0

            if best_lock_pct == 0:
                safety["warnings"].append("🔴 LP未ロック（ラグプルリスク）")
            elif best_lock_pct < 50:
                safety["warnings"].append(f"🟡 LP一部ロック（{best_lock_pct:.0f}%）")
        else:
            # marketsがない場合、summaryのlpLockedPctを使用
            lp_pct = data.get("lpLockedPct")
            if lp_pct is not None:
                safety["lp_locked_pct"] = round(lp_pct, 1)
                safety["lp_locked"] = lp_pct > 0

        # ── Top Holders 集中度 ──
        top_holders = data.get("topHolders", [])
        if top_holders:
            total_pct = sum(h.get("pct", 0) for h in top_holders[:10])
            safety["top_holders_pct"] = round(total_pct, 1)
            safety["top_holders_detail"] = [
                {
                    "address": h.get("address", "")[:8] + "...",
                    "pct": round(h.get("pct", 0), 2),
                    "isInsider": h.get("isInsider", False),
                }
                for h in top_holders[:10]
            ]

            # 集中度警告
            danger_pct = config.top_holders_danger_pct
            warn_pct = config.top_holders_warn_pct
            if total_pct > danger_pct:
                safety["warnings"].append(
                    f"🔴 上位10ホルダーが{total_pct:.0f}%保有（集中リスク）"
                )
            elif total_pct > warn_pct:
                safety["warnings"].append(
                    f"🟡 上位10ホルダーが{total_pct:.0f}%保有"
                )

            # インサイダー検出
            insider_count = sum(
                1 for h in top_holders[:10] if h.get("isInsider")
            )
            safety["insider_count"] = insider_count
            if insider_count >= config.insider_danger_count:
                safety["warnings"].append(
                    f"🔴 インサイダーウォレット{insider_count}件検出"
                )
            elif insider_count >= 1:
                safety["warnings"].append(
                    f"🟡 インサイダーウォレット{insider_count}件検出"
                )

        # ── インサイダーネットワーク ──
        insider_detected = data.get("graphInsidersDetected", False)
        if insider_detected:
            networks = data.get("insiderNetworks", [])
            net_count = len(networks) if networks else 0
            if net_count > 0:
                safety["warnings"].append(
                    f"🔴 インサイダーネットワーク{net_count}件検出"
                )

        # ── リスク項目 ──
        risks = data.get("risks", [])
        for risk in risks:
            name = risk.get("name", "")
            level = risk.get("level", "")
            desc = risk.get("description", "")

            # LP関連は上で処理済みなのでスキップ
            if "lp" in name.lower() and "lock" in name.lower():
                continue

            if level in ("danger", "critical"):
                safety["warnings"].append(f"🔴 {name}: {desc[:80]}")
            elif level == "warn":
                safety["warnings"].append(f"🟡 {name}: {desc[:80]}")

    # ================================================================
    # RugCheck API（フルレポート）
    # ================================================================
    async def _rugcheck_full(self, token_address: str) -> dict:
        """RugCheck.xyz API からフルレポートを取得"""
        try:
            url = f"{self.RUGCHECK_API}/tokens/{token_address}/report"
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(
                        f"  RugCheck Full: score={data.get('score', 'N/A')}, "
                        f"normalized={data.get('score_normalised', 'N/A')}, "
                        f"holders={data.get('totalHolders', 'N/A')}, "
                        f"markets={len(data.get('markets', []))}"
                    )
                    return data
                elif resp.status == 429:
                    # レート制限 → summaryにフォールバック
                    logger.warning("  RugCheck rate limited, falling back to summary")
                    return await self._rugcheck_summary(token_address)
                else:
                    logger.debug(f"  RugCheck Full: status={resp.status}")
                    return {}
        except Exception as e:
            logger.debug(f"  RugCheck Full error: {e}")
            return {}

    async def _rugcheck_summary(self, token_address: str) -> dict:
        """RugCheck summary（フォールバック用）"""
        try:
            url = f"{self.RUGCHECK_API}/tokens/{token_address}/report/summary"
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {}
        except Exception as e:
            logger.debug(f"  RugCheck Summary error: {e}")
            return {}

    # ================================================================
    # Solana RPC: ミント権限チェック（フォールバック）
    # ================================================================
    async def _check_mint_authority_rpc(self, token_address: str) -> dict:
        """Solana RPC getAccountInfo でミント権限を直接確認"""
        result: dict = {"mint_authority": None, "freeze_authority": None}
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [
                    token_address,
                    {"encoding": "jsonParsed"},
                ],
            }
            async with self.session.post(
                self.rpc_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return result
                data = await resp.json()

            account = data.get("result", {}).get("value")
            if not account:
                return result

            parsed = account.get("data", {}).get("parsed", {})
            info = parsed.get("info", {})

            mint_auth = info.get("mintAuthority")
            freeze_auth = info.get("freezeAuthority")

            result["mint_authority"] = mint_auth if mint_auth else "None"
            result["freeze_authority"] = freeze_auth if freeze_auth else "None"

            logger.info(
                f"  Mint権限: {result['mint_authority'][:12] if result['mint_authority'] != 'None' else 'なし'}"
                f" | Freeze: {result['freeze_authority'][:12] if result['freeze_authority'] != 'None' else 'なし'}"
            )

        except Exception as e:
            logger.debug(f"  Mint authority check error: {e}")

        return result

    # ================================================================
    # 安全性サマリー（通知用の簡潔な文字列）
    # ================================================================
    @staticmethod
    def format_safety_summary(safety: dict) -> str:
        """安全性データを1行サマリーに変換"""
        parts = []

        # リスクレベル
        level = safety.get("risk_level", "unknown")
        level_emoji = {"safe": "✅", "warning": "⚠️", "danger": "🔴"}.get(level, "❓")
        parts.append(level_emoji)

        # LP Lock
        lp_pct = safety.get("lp_locked_pct")
        if lp_pct is not None:
            if lp_pct >= 90:
                parts.append(f"LP🔒{lp_pct:.0f}%")
            elif lp_pct > 0:
                parts.append(f"LP⚠️{lp_pct:.0f}%")
            else:
                parts.append("LP❌")

        # Mint
        mint = safety.get("mint_authority")
        if mint == "None":
            parts.append("Mint✅")
        elif mint:
            parts.append("Mint❌")

        # Top Holders
        top_pct = safety.get("top_holders_pct")
        if top_pct is not None:
            if top_pct < 30:
                parts.append(f"分散✅{top_pct:.0f}%")
            elif top_pct < 50:
                parts.append(f"集中⚠️{top_pct:.0f}%")
            else:
                parts.append(f"集中❌{top_pct:.0f}%")

        return " | ".join(parts)

    # ================================================================
    # 一括チェック
    # ================================================================
    async def check_multiple(self, projects: list[SolanaProject]) -> dict[str, dict]:
        """複数プロジェクトを一括チェック（並列実行）"""
        async def _safe_check(p: SolanaProject) -> tuple[str, dict]:
            try:
                result = await self.check(p)
                return p.token_address, result
            except Exception as e:
                logger.warning(f"Safety check failed for {p.symbol}: {e}")
                return p.token_address, {
                    "is_safe": True,
                    "risk_level": "unknown",
                    "warnings": [],
                }

        tasks = [_safe_check(p) for p in projects]
        results_list = await asyncio.gather(*tasks)
        return dict(results_list)
