"""
安全性チェック v4 — ラグプル / ハニーポット / LP Lock / ミント権限 検知
RugCheck.xyz API + Solana RPC（ミント権限直接チェック）で動作

強化ポイント:
  - RugCheck API でリスクスコア・LP Lock・Top Holders を取得
  - Solana RPC getAccountInfo でミント権限を直接確認
  - danger レベルのトークンを自動除外するオプション
"""
import asyncio
import base64
import logging
import struct
from typing import Optional

import aiohttp

from .config import config
from .scanner import SolanaProject

logger = logging.getLogger(__name__)

# Solana SPL Token プログラム定数
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class SafetyChecker:
    """
    無料 API でトークンの安全性をチェック
    - RugCheck.xyz: ラグプルリスクスコア（無料、キー不要）
    - Solana RPC: ミント権限確認（無料）
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
            self._rugcheck(project.token_address),
            self._check_mint_authority(project.token_address),
            return_exceptions=True,
        )

        rugcheck = results[0] if not isinstance(results[0], Exception) else {}
        mint_info = results[1] if not isinstance(results[1], Exception) else {}

        safety: dict = {
            "is_safe": True,
            "risk_level": "unknown",
            "warnings": [],
            "rugcheck_score": None,
            "rugcheck_status": None,
            "mint_authority": None,
            "freeze_authority": None,
            "lp_locked": None,
            "top_holders_pct": None,
            "top_holders_detail": [],
        }

        # ── RugCheck 結果を反映 ──
        if rugcheck:
            score = rugcheck.get("score", 0)
            status = rugcheck.get("tokenMeta", {}).get("status", "")
            safety["rugcheck_score"] = score
            safety["rugcheck_status"] = status
            risks = rugcheck.get("risks", [])

            for risk in risks:
                name = risk.get("name", "")
                level = risk.get("level", "")
                desc = risk.get("description", "")

                if level in ("danger", "critical"):
                    safety["warnings"].append(f"🔴 {name}: {desc}")
                elif level == "warn":
                    safety["warnings"].append(f"🟡 {name}: {desc}")

            # LP Lock
            lp_locked = not any(
                "lp" in r.get("name", "").lower()
                and r.get("level") in ("danger", "critical")
                for r in risks
            )
            safety["lp_locked"] = lp_locked
            if not lp_locked:
                safety["warnings"].append("🔴 LP未ロック（ラグプルリスク）")

            # Top Holders 集中度
            top_holders = rugcheck.get("topHolders", [])
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
                if total_pct > 50:
                    safety["warnings"].append(
                        f"🔴 上位10ホルダーが{total_pct:.0f}%保有（集中リスク）"
                    )
                elif total_pct > 30:
                    safety["warnings"].append(
                        f"🟡 上位10ホルダーが{total_pct:.0f}%保有"
                    )

                # インサイダー検出
                insider_count = sum(
                    1 for h in top_holders[:10] if h.get("isInsider")
                )
                if insider_count >= 3:
                    safety["warnings"].append(
                        f"🔴 インサイダーウォレット{insider_count}件検出"
                    )

        # ── ミント権限（RPC 直接チェック） ──
        if mint_info:
            mint_auth = mint_info.get("mint_authority")
            freeze_auth = mint_info.get("freeze_authority")
            safety["mint_authority"] = mint_auth
            safety["freeze_authority"] = freeze_auth

            if mint_auth and mint_auth != "None":
                safety["warnings"].append("🔴 ミント権限が放棄されていない（無限発行リスク）")
            if freeze_auth and freeze_auth != "None":
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
    # RugCheck API
    # ================================================================
    async def _rugcheck(self, token_address: str) -> dict:
        """RugCheck.xyz API からトークンレポートを取得"""
        try:
            url = f"{self.RUGCHECK_API}/tokens/{token_address}/report/summary"
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(
                        f"  RugCheck: score={data.get('score', 'N/A')}, "
                        f"risks={len(data.get('risks', []))}"
                    )
                    return data
                else:
                    logger.debug(f"  RugCheck: status={resp.status}")
                    return {}
        except Exception as e:
            logger.debug(f"  RugCheck error: {e}")
            return {}

    # ================================================================
    # Solana RPC: ミント権限チェック
    # ================================================================
    async def _check_mint_authority(self, token_address: str) -> dict:
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
