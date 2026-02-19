# Solana 高度なスクリーナーBot (sol-screener-001) v4 完成版

ご依頼いただいた「マニア向け」追加機能をすべて統合し、Railwayでの安定稼働を想定したアップグレード版の完成コードです。

## ✨ 主なアップグレード内容

### 1. Pump.fun 卒業のリアルタイム検知
- **Raydium上場を最速検知**: `scanner.py` と `pumpfun.py` を強化。DexScreener API の `search` エンドポイントを活用し、`dexId: "raydium"` かつ作成直後（直近30分以内）の新規ペアを常時監視。これにより、Pump.funからRaydiumへ移行した瞬間のトークンをリアルタイムで捕捉します。
- **RPCフォールバック**: 従来のRPC `getSignaturesForAddress` を使ったMigrationトランザクション解析も並行して実行し、検知の冗長性を確保しています。

### 2. ラグプル（詐欺）検知の強化
- **ミント権限の直接チェック**: `safety.py` を更新し、Solana RPC (`getAccountInfo`) を直接呼び出してトークンのミント権限が放棄されているかをチェックするロジックを追加しました。これにより、無限発行リスクをより正確に判定します。
- **統合的な安全性スコア**: RugCheck APIによるLPロック状況、Top 10ホルダー保有率、リスク分析結果と、ミント権限の有無を統合し、`safe`, `warning`, `danger` の3段階でリスクレベルを判定します。

### 3. スマートマネー（Smart Money）追跡
- **既知ウォレット追跡**: `mania.py` を新設。`.env` ファイルで指定した「利益を出している既知のウォレット」が、スキャン対象トークンのTopホルダーに含まれているかを自動で照合し、スコアにボーナスを加算します。
- **ホエール分析**: RugCheckのTopホルダー情報を利用し、2%以上を保有する「ホエール」の数や保有率の集中度を分析し、スコアリングに反映します。

### 4. Discord通知のUX改善
- **リッチなEmbed通知**: `notifier.py` を全面的に刷新。単なるテキストではなく、視認性の高いDiscord Embed形式で通知します。
- **ダイレクトリンクボタン**: 通知には、**[DexScreener] [RugCheck] [BirdEye] [Solscan]** への直リンクをMarkdown形式で埋め込み、ワンクリックで詳細分析ページに飛べるようにしました。（Discord Webhookの仕様上、ボタンではなくリンク形式での実装となります）
- **状況に応じたカラーリング**: スコアやリスクレベルに応じて、Embedのサイドカラーが緑（安全/高スコア）、黄（注意）、赤（危険）、紫（卒業）などに変化し、状況を直感的に把握できます。

## 📂 プロジェクト構成

```
/sol-screener-v4
├── main.py             # メインロジック、スケジューラ
├── src/                # ソースコード
│   ├── __init__.py
│   ├── scanner.py      # 4系統での新規ペア発見 + Raydium卒業検知
│   ├── scorer.py       # 多次元スコアリング
│   ├── safety.py       # 安全性チェック（ミント権限/LP/ホルダー）
│   ├── notifier.py     # Discord Embed通知（直リンク付き）
│   ├── pumpfun.py      # Pump.fun卒業検知ロジック
│   ├── mania.py        # スマートマネー追跡
│   ├── state.py        # 通知済み状態の管理
│   ├── config.py       # 環境変数からの設定読み込み
│   ├── expectation.py  # 期待値計算
│   ├── monitors.py     # ウォレット/流動性/SOL価格の監視
│   ├── market_events.py # TGE/NFT/Meme急騰の監視
│   ├── airdrop.py      # (既存機能)
│   ├── background.py   # (既存機能)
│   └── nft.py          # (既存機能)
├── data/               # (状態ファイル保存用、.gitignore対象)
├── logs/               # (ログファイル保存用、.gitignore対象)
├── .env.example        # 環境変数設定のサンプル
├── requirements.txt    # 必要なPythonパッケージ
├── railway.toml        # Railwayデプロイ設定
├── Procfile            # Railway Workerモード設定
└── .gitignore
```

## 🚀 デプロイ手順 (Railway)

1.  **GitHubにPush**: このプロジェクト一式を、ご自身の新しいGitHubリポジトリにpushしてください。
2.  **Railwayでプロジェクト作成**: Railwayダッシュボードで「New Project」から、作成したGitHubリポジトリを選択します。
3.  **環境変数の設定**: Railwayのプロジェクト設定画面の「Variables」タブで、`.env.example` を参考に以下の環境変数を設定します。
    - `DISCORD_WEBHOOK_URL`: **必須**。通知を送りたいDiscordチャンネルのWebhook URL。
    - `HELIUS_API_KEY`: （任意）HeliusのAPIキーを設定すると、RPCアクセスが高速・安定します。
    - `WATCH_WALLETS`: （任意）追跡したいスマートマネーウォレットを `アドレス1:ラベル1,アドレス2:ラベル2` の形式で設定します。
4.  **デプロイ**: 自動でデプロイが開始されます。`railway.toml` と `Procfile` により、自動的に**Worker**として認識され、Webサーバーではなくバックグラウンドプロセスとして起動します。

以上で、設定したスケジュールに従ってBotが自動的に動作を開始します。ログはRailwayの「Deploy Logs」画面で確認できます。
