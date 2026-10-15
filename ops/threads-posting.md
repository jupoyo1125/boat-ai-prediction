# 展示後の平和島AI予想をThreadsへ投稿する

本機能は初期状態では無効です。予想保存後の送信先は **Buffer（無料プランのAPI利用枠あり）** またはThreads公式APIから選びます。ChatGPTのMetricool連携の認証情報は、Render上のアプリからは流用できません。新しい有料サービスやMetricool APIプランの契約は行いません。

## 対象と投稿内容

- 投稿先は `@ton123kyotei456`、対象場は平和島（04）です。投稿前にAPIでアカウントの一致を確認します。
- 対象の競走場は平和島だけです。`date` モードは指定した当日だけ、明示的に設定した `daily` モードは開始日から終了日まで日本時間の当日を対象にします。単日の既存設定を勝手に毎日に切り替えません。
- 今回承認された運用は **2026/10/16から、平和島の開催日の全12R** です。現在のAPIキーの期限に合わせ、運用期間の上限を2026/11/14に設定します。キーはその日の途中で期限になる場合があり、期限切れでは投稿を成功扱いにしません。更新する際はキーと運用期間を確認してください。
- 日付が変わると対象日を再計算し、1Rから12Rを対象にします。開始日前は待機し、終了日の翌日には停止します。平和島の保存済み自動予想がない日は投稿しません。
- 6艇の展示タイム・展示STが入った、サイトの保存済み自動予想だけを使用します。投稿のための再予想や結果を見た後の予想作成はしません。
- 1レースにつきテキスト1投稿。本命・相手軸・穴、展示データ、ガチガチ／ロマン砲／鬼しぼりの全買い目、保存時刻と締切を表示します。買い目を省略して的中実績のように見せません。
- 「独自作成のAI予想サイトでは、」の表現を使い、サイトURLやドメインは掲載しません。
- 予想保存で投稿担当を起こし、別スレッドから送信します。再起動後も保存済み予想を確認します。接続と相手サービスが正常なら保存後に送信を開始しますが、外部APIの応答時間まで即時性を保証するものではありません。
- 締切まで2分を切った予想は投稿しません。開始直前だけでなく、投稿準備後にも再確認します。

## Bufferで接続する（今回の運用）

Bufferのアカウントと、Instagramに紐づいた公開Threadsプロフィールが必要です。

1. Bufferに無料登録、または既存アカウントでログインします。Threadsチャンネルとして **`@ton123kyotei456`** を接続します。Instagram経由で対象のThreadsプロフィールを選び、実際の接続先を確認します。
2. メールアドレスの確認後、Bufferの **Settings → API** で、この本人の運用に使うAPIキーを作成します。権限は `account:read`・`posts:read`・`posts:write` の3つです。キーはチャットやGitHubへ送らず、Renderの秘密情報欄に直接入力します。
3. 対象のBufferチャンネルIDを確認し、Renderの既存サービス `boat-ai-prediction` の **Environment** に次を設定します。チャンネルIDとThreadsのネイティブユーザーIDは別物です。

| 変数 | 毎開催日の平和島の設定 |
|---|---|
| `BOAT_THREADS_ENABLED` | 接続完了まで `0`。接続・対象確認後に `1` |
| `BOAT_THREADS_PROVIDER` | `buffer` |
| `BOAT_THREADS_MODE` | `daily` |
| `BOAT_THREADS_START_DATE` | `20261016` |
| `BOAT_THREADS_UNTIL_DATE` | `20261114` |
| `BOAT_THREADS_FROM_RACE` | `1`（dailyモードでは全12Rが必須） |
| `BOAT_BUFFER_CHANNEL_ID` | 上記アカウントのBufferチャンネルID |
| `BUFFER_API_KEY` | Bufferで作成した本人のAPIキー（秘密情報） |

`BOAT_THREADS_DATE` はdailyモードでは使用しません。既存の単日用の値が残っていても、その日を再投稿しません。

4. この変更をデプロイし、`GET /api/threads_publishing` が `provider:buffer`・`mode:daily` を返すことを確認します。開始日前は `armed:true`・`enabled:false`・`phase:scheduled` です。開始日からは `enabled:true` になります。`enabled` は処理を有効にした意味で、実投稿の成功を保証しません。
5. `connection.state:connected` は実際のAPIで接続チャンネル・投稿先を読み取り確認できた意味です。起動時は開始日前でも読み取りだけを行い、接続が正常な場合は各ワーカーが監視時間中に最大1時間に1回、エラー後は最大15分に1回再確認します。公開直前にも必ず投稿先を再確認します。エラーの本文やキーは状態APIに出しません。
6. 最初の未投稿・締切前レースで実際の `published` とThreadsの投稿URLを確認します。接続テストのための架空予想や、締切後の過去予想は投稿しません。
7. dailyの開始日は、1Rをまだ投稿していない日を指定します。手動投稿と並行運用しません。途中で切り替える必要がある場合は無効化し、その日だけ `date` モードで最初の未投稿レースを指定します。

既存のGitHub Actionsが5分おきにサービスを起こし、自動予想の状態を読み取り確認します。このアクセスによって投稿処理の待機も維持します。Actionsの遅延や外部APIの障害まで即時性を保証しません。停止する場合は `BOAT_THREADS_ENABLED=0` を保存してデプロイします。

送信前にBufferのチャンネルID・サービスがThreadsであること・ユーザー名の一致、切断／ロック／一時停止がないことを確認します。別アカウントへの自動切り替えや、Threads公式APIへの自動フォールバックは行いません。

Bufferにはまず非公開の下書きを保存します。その内容と投稿先を確認し、締切まで2分以上あることを再確認してから、同じ下書きを `shareNow` で公開します。送信受付後は `submitted` として記録し、**Bufferが `sent` と実際の公開時刻を返すまでは `published` にしません**。公開状態の確認は複数ワーカーで共有し、同じ投稿について最大1分間に1回です。

締切まで2分になってもBuffer側で `scheduled` のままなら、待機中の投稿を下書きへ戻します。公開済みのThreads投稿は削除しません。Buffer側がすでに `sending` の場合、外部サービスの公開遅延を完全には制御できません。確認された公開時刻が締切後なら `published_late`、成否を確定できなければ `uncertain` と表示し、自動再投稿しません。Bufferの投稿制限・API利用枠・一時的な障害でも、成功した扱いにはしません。

## 単日の運用とThreads公式APIで接続する場合

単日だけ運用する場合は `BOAT_THREADS_MODE=date`、`BOAT_THREADS_DATE=YYYYMMDD`、`BOAT_THREADS_FROM_RACE=最初の未投稿レース` を指定します。Bufferでも同じ単日設定を使えます。当日終了後は自動停止します。

1. [MetaのThreads API設定](https://developers.facebook.com/docs/threads/get-started/get-access-tokens-and-permissions)で、投稿先のThreadsユーザー用アクセストークンを取得します。必要な権限は `threads_basic` と `threads_content_publish` です。トークンの有効期限と対象アカウントを確認してください。
2. Renderの既存 `boat-ai-prediction` サービスの **Environment** に、次を設定します。トークンはチャットやGitHub、原稿、画像に貼らず、Renderの秘密情報欄に直接入力します。

| 変数 | 単日の平和島の設定例 |
|---|---|
| `BOAT_THREADS_ENABLED` | `1` |
| `BOAT_THREADS_PROVIDER` | `threads` |
| `BOAT_THREADS_MODE` | `date` |
| `BOAT_THREADS_DATE` | `YYYYMMDD`（当日） |
| `BOAT_THREADS_FROM_RACE` | 最初の未投稿レース（1〜12） |
| `THREADS_ACCESS_TOKEN` | 上で取得した本人のアクセストークン |

3. この変更を本番へデプロイし、`GET /api/threads_publishing` の設定状態を確認します。設定値不足なら `enabled:false`、投稿失敗・成否不明は各投稿の状態に表示されます。`enabled:true` は起動条件がそろった意味で、投稿の成功やトークンの有効性を表すものではありません。初回の投稿で実際のAPI接続と `published` を確認します。
4. 準備中にほかのレースも手動で投稿した場合、最初の未投稿レースに `BOAT_THREADS_FROM_RACE` を進めてから有効にします。すでに手動で出したレースを再送しないでください。

単日モードは対象日が過ぎた設定では投稿しません。毎開催日の運用は上記の明示的なdaily設定を使用します。無効化するには `BOAT_THREADS_ENABLED=0` にしてデプロイします。

## 重複と障害時の扱い

投稿記録は、既存のバックエンド専用 `boat_automation_state` テーブルの **id=2** に保存します（予想処理の状態は従来どおりid=1）。既存の保存先移行にも含まれます。新しい公開テーブルや公開書き込みAPIは追加しません。

投稿権の確保は短いDBトランザクションで行い、外部通信中はDB行ロックを保持しません。複数ワーカーや再起動時も、同じ日・場・レースを重複送信しません。非公開の投稿準備中の通信失敗は最大3回まで次回確認時に再試行します。投稿記録には送信先も保存し、別の送信先で準備済みの投稿を流用しません。

**公開開始後のタイムアウトや、公開成功後のDB保存障害は成否不明として扱います。** この場合は自動再投稿せず、Threads側の実投稿を確認する必要があります。可視の投稿を二重に作るより、確認が必要な状態を残す設計です。APIの認証エラー・投稿上限・外部障害でも成功を装いません。

この機能は予想保存・結果取得・学習を別スレッドから参照し、投稿障害のために既存の予想処理を待たせません。接続トークンの値はログ・投稿記録・状態APIに出しません。

## 確認

```sh
BOAT_AUTO_ENABLED=0 BOAT_THREADS_ENABLED=0 python -m unittest discover -s tests -p 'test_threads_publisher.py'
```

このテストは架空の検証用データと偽の投稿先を使い、外部への投稿を実行しません。本番の読み取りによる接続確認は開始日前にも行います。実投稿の確認は、実際の未投稿レースだけを対象に行います。

公式API仕様：
- [Buffer APIの利用開始](https://developers.buffer.com/guides/getting-started.html)
- [Bufferのテキスト投稿](https://developers.buffer.com/examples/create-text-post.html)
- [Buffer APIリファレンス](https://developers.buffer.com/reference.html)
- [Meta公式Threads APIコレクション](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api)
- [Threadsの投稿API](https://developers.facebook.com/docs/threads/posts)
