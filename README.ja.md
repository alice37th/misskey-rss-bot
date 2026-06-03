# Misskey RSS Bot

## 1. 概要

英語版ドキュメントは [`README.md`](README.md) を参照してください。

単一RSSフィードを定期確認し、新規記事を Misskey / Sharkey に投稿するPython製Botです。

- 対象RSS: `.env` の `FEED_URL` に設定
- Misskey / Sharkey ホスト: `.env` の `MISSKEY_HOST`
- Misskey APIトークン: `.env` の `MISSKEY_TOKEN`
- 通知済み記事管理: SQLite
- 定期実行: systemd timer

安全のため、初回は `python bot.py --init-seen` で既存RSS itemを既読登録してください。これにより、初回起動で過去記事を投稿しません。

## 2. 前提環境

- Ubuntu Server
- Python 3.10.12
- SQLite（Python標準ライブラリ `sqlite3` を利用）
- systemd
- Git

## 3. セットアップ手順

```bash
git clone <YOUR_REPOSITORY_URL> misskey-rss-bot
cd misskey-rss-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
vi .env
```

`.env` の主な項目:

```dotenv
MISSKEY_HOST=
MISSKEY_TOKEN=
FEED_URL=
VISIBILITY=public
DRY_RUN=true
MAX_POSTS_PER_RUN=3
POST_TEMPLATE=
DATABASE_PATH=data/bot.sqlite
```

> `MISSKEY_TOKEN` は秘密情報です。`.env` はGit管理しないでください。

## 4. 初回既読登録

初回起動で過去記事を投稿しないよう、最初に既存RSS itemを `seen` として登録します。

```bash
python bot.py --init-seen
```

このモードでは Misskey / Sharkey へは絶対に投稿しません。既に登録済みのitemはスキップされます。

## 5. dry-run

`.env` で `DRY_RUN=true` にした状態で通常実行します。

```bash
python bot.py
```

`DRY_RUN=true` の場合:

- Misskey APIを呼びません
- 投稿予定のタイトルと本文をログに出します
- DBには投稿済みとして登録しません

## 6. 本番投稿テスト

`.env` を編集し、`MISSKEY_TOKEN` を設定したうえで `DRY_RUN=false` にします。

```bash
python bot.py
```

`DRY_RUN=false` の場合のみ、`{MISSKEY_HOST}/api/notes/create` に投稿します。投稿成功後だけ `status=posted` としてSQLiteに保存します。投稿失敗時は `status=failed` と `error_message` を保存します。

## 7. systemd timer設定

配置先を `/opt/misskey-rss-bot`、venvを `/opt/misskey-rss-bot/.venv` とする想定です。

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo cp systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now misskey-rss-bot.timer
```

タイマーは15分ごとに `misskey-rss-bot.service` をoneshot実行します。

## 8. ログ確認

```bash
journalctl -u misskey-rss-bot.service -n 100
```

## 9. タイマー確認

```bash
systemctl list-timers | grep misskey-rss-bot
```

## 10. SQLite確認

```bash
sqlite3 data/bot.sqlite "select id, status, title, pub_date, posted_at from rss_items order by id desc limit 10;"
```

DBとテーブルは初回実行時に自動作成されます。

## 11. 投稿テンプレート変更方法

Bot本体に以下の既定テンプレートを持っているため、標準の投稿文でよければテンプレートファイルは不要です。

```text
📝 新着記事

{{ title }}

{{ link }}
```

`POST_TEMPLATE` が空、または既定パスのままの場合、Botは `templates/post.txt` を探します。ファイルが存在すればそれを使い、存在しなければBot内蔵の既定テンプレートを使います。

サーバ上で投稿文を変更したい場合は、`templates/post.txt` を作成してください。

```bash
mkdir -p templates
cat > templates/post.txt <<'EOF'
📝 新着記事

{{ title }}

{{ link }}
EOF
vi templates/post.txt
```

`templates/post.txt` はGit管理対象外にしているため、ローカル編集が将来の `git pull` と衝突しにくくなります。`.env` の `POST_TEMPLATE` で別ファイルを指定することもできます。独自の `POST_TEMPLATE` を指定した場合、そのファイルが存在しなければ明確なエラーを出して終了します。

使える変数:

- `{{ title }}`
- `{{ link }}`
- `{{ guid }}`
- `{{ pub_date }}`
- `{{ description }}`

## 12. トラブルシュート

### `.env` がない

`cp .env.example .env` を実行し、必要な値を編集してください。`.env` がない場合は環境変数と既定値を使いますが、実行には `FEED_URL` が必要で、本番投稿には `MISSKEY_HOST` と `MISSKEY_TOKEN` が必要です。

### `MISSKEY_TOKEN` が未設定

`DRY_RUN=false` で実行する場合、`.env` の `MISSKEY_TOKEN` を設定してください。未設定の場合は投稿せずエラー終了します。

### `FEED_URL` が未設定

`.env` の `FEED_URL` を設定してください。このBotには既定のRSS URLは含めていません。

### RSS取得失敗

`FEED_URL` が正しいか、サーバから外部HTTP/HTTPSアクセスできるかを確認してください。

### Misskey投稿失敗

- `MISSKEY_HOST` が正しいか確認してください
- `MISSKEY_TOKEN` の権限を確認してください
- `VISIBILITY` の値を確認してください
- SQLiteの `rss_items.error_message` に保存されたエラー内容を確認してください

### テンプレートファイルがない

`.env` で独自の `POST_TEMPLATE` を指定している場合、そのファイルが存在するか確認してください。`POST_TEMPLATE` が空の場合、`templates/post.txt` がなくてもBot内蔵の既定テンプレートを使うためエラーにはなりません。

## 重複判定仕様

同じRSS itemかどうかは以下の順で判定します。

1. `guid` が一致
2. `normalized_link` が一致
3. `title + pub_date` が一致

`guid` がない場合は `link` を代替として扱います。`normalized_link` はURL fragmentを削除し、末尾スラッシュの有無を同一扱いにします。query stringは維持します。

過去に `status=failed` で保存されたitemも登録済みとして扱い、自動再試行はしません。

## 受入チェック

- [ ] `python -m py_compile bot.py` が成功する
- [ ] `DRY_RUN=true` ではMisskeyに投稿されない
- [ ] `python bot.py --init-seen` で既存RSS item が `seen` 登録される
- [ ] `--init-seen` を2回実行しても二重登録されない
- [ ] 通常実行で同じRSS itemを二重投稿しない
- [ ] 投稿成功後だけ `status=posted` になる
- [ ] 投稿失敗時は `status=failed` と `error_message` が保存される
- [ ] `templates/post.txt` を作成または変更すると投稿文が変わる
- [ ] `.env`、`data/*.sqlite`、`templates/post.txt` がGit管理対象にならない

## 開発者向け検証

```bash
python -m py_compile bot.py
```
