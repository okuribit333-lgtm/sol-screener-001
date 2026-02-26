"""
X（Twitter）監視モジュール v5.6

@solana 公式アカウントの新規ツイートを定期的にチェックし、
新しいツイートがあればDiscordに通知する。

■ 仕組み:
  - Manus data_api の Twitter/get_user_tweets を使用
  - 最後に通知したツイートIDを記録し、それ以降の新規ツイートのみ通知
  - RTはフィルタ可能（デフォルトではRTも通知）
  - 5分間隔でチェック
"""
import logging
import sys
from typing import Optional

logger = logging.getLogger(__name__)

# Manus sandbox API
sys.path.append('/opt/.manus/.sandbox-runtime')

# 監視対象アカウント（rest_id: screen_name）
WATCH_ACCOUNTS = {
    "951329744804392960": "solana",  # @solana 公式
}


class XMonitor:
    """X（Twitter）アカウント監視"""

    def __init__(self):
        self._last_tweet_ids: dict[str, str] = {}  # user_id -> last_tweet_id
        self._initialized: dict[str, bool] = {}
        self._api_available = False
        self._client = None
        self._init_client()

    def _init_client(self):
        """API クライアントを初期化"""
        try:
            from data_api import ApiClient
            self._client = ApiClient()
            self._api_available = True
            logger.info("X Monitor: API クライアント初期化成功")
        except ImportError:
            logger.warning("X Monitor: data_api が利用できません（ローカル環境）")
            self._api_available = False
        except Exception as e:
            logger.warning(f"X Monitor: API初期化エラー: {e}")
            self._api_available = False

    def _fetch_tweets(self, user_id: str, count: int = 10) -> list[dict]:
        """指定ユーザーの最新ツイートを取得"""
        if not self._api_available or not self._client:
            return []

        try:
            result = self._client.call_api(
                'Twitter/get_user_tweets',
                query={'user': user_id, 'count': str(count)}
            )
        except Exception as e:
            logger.error(f"X Monitor: API呼び出しエラー (user={user_id}): {e}")
            return []

        tweets = []
        instructions = result.get('result', {}).get('timeline', {}).get('instructions', [])

        for inst in instructions:
            entries = []
            if inst.get('type') == 'TimelineAddEntries':
                entries = inst.get('entries', [])
            elif inst.get('type') == 'TimelinePinEntry':
                # ピン留めツイートはスキップ
                continue

            for entry in entries:
                eid = entry.get('entryId', '')
                if not eid.startswith('tweet-'):
                    continue

                content = entry.get('content', {})
                ic = content.get('itemContent', {})
                tr = ic.get('tweet_results', {}).get('result', {})

                if not tr:
                    continue

                user = tr.get('core', {}).get('user_results', {}).get('result', {})
                user_legacy = user.get('legacy', {})
                tweet_legacy = tr.get('legacy', {})

                tweet_id = tweet_legacy.get('id_str', '')
                text = tweet_legacy.get('full_text', '')
                created_at = tweet_legacy.get('created_at', '')

                if not tweet_id or not text:
                    continue

                screen_name = user_legacy.get('screen_name') or WATCH_ACCOUNTS.get(user_id, 'unknown')
                display_name = user_legacy.get('name') or screen_name
                profile_image = user_legacy.get('profile_image_url_https', '')

                # t.co URLをそのまま保持（Discordが展開してくれる）
                tweet_url = f"https://x.com/{screen_name}/status/{tweet_id}"

                tweets.append({
                    "tweet_id": tweet_id,
                    "username": screen_name,
                    "display_name": display_name,
                    "text": text,
                    "url": tweet_url,
                    "created_at": created_at,
                    "likes": tweet_legacy.get('favorite_count', 0),
                    "retweets": tweet_legacy.get('retweet_count', 0),
                    "replies": tweet_legacy.get('reply_count', 0),
                    "profile_image": profile_image,
                    "is_retweet": text.startswith('RT @'),
                })

        return tweets

    async def check_new_tweets(self, include_retweets: bool = True) -> list[dict]:
        """
        全監視アカウントの新規ツイートをチェック。
        初回実行時は最新ツイートIDを記録するだけで通知しない。

        Returns:
            新規ツイートのリスト（通知対象）
        """
        new_tweets = []

        for user_id, screen_name in WATCH_ACCOUNTS.items():
            tweets = self._fetch_tweets(user_id, count=10)

            if not tweets:
                logger.debug(f"X Monitor: @{screen_name} のツイート取得失敗またはゼロ件")
                continue

            # ツイートIDで降順ソート（最新が先頭）
            tweets.sort(key=lambda t: int(t['tweet_id']), reverse=True)

            # 初回実行: 最新IDを記録して終了（通知なし）
            if not self._initialized.get(user_id):
                self._last_tweet_ids[user_id] = tweets[0]['tweet_id']
                self._initialized[user_id] = True
                logger.info(
                    f"X Monitor: @{screen_name} 初期化完了 "
                    f"(最新ID: {tweets[0]['tweet_id']})"
                )
                continue

            last_id = self._last_tweet_ids.get(user_id, '0')

            for tweet in tweets:
                # 既に通知済みのツイートIDより新しいもののみ
                if int(tweet['tweet_id']) <= int(last_id):
                    break

                # RTフィルタ
                if not include_retweets and tweet['is_retweet']:
                    continue

                new_tweets.append(tweet)

            # 最新IDを更新
            if tweets:
                self._last_tweet_ids[user_id] = tweets[0]['tweet_id']

        if new_tweets:
            logger.info(f"X Monitor: {len(new_tweets)}件の新規ツイートを検出")

        return new_tweets

    @property
    def is_available(self) -> bool:
        return self._api_available

    def add_account(self, user_id: str, screen_name: str):
        """監視アカウントを追加"""
        WATCH_ACCOUNTS[user_id] = screen_name
        logger.info(f"X Monitor: @{screen_name} (ID: {user_id}) を監視対象に追加")

    def remove_account(self, user_id: str):
        """監視アカウントを削除"""
        name = WATCH_ACCOUNTS.pop(user_id, None)
        self._last_tweet_ids.pop(user_id, None)
        self._initialized.pop(user_id, None)
        if name:
            logger.info(f"X Monitor: @{name} を監視対象から削除")
