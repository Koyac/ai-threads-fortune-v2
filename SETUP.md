# セットアップ手順

## 1. 発信内容を確認する

発信内容(AI活用・業務効率化Tips)は設定済み。中身を調整したい場合だけ次を編集する:
- `content_prompt.md`(コンセプト・想定読者・発信の柱・フォーマット・トーン・禁止事項)
- `strategy.md`(固定ルール・A/Bパターン・投稿スロット)
- `lexicon.md`(語彙・語尾・禁止表現)
- `scripts/build_seeds.py` の `PILLARS` / `TASKS` / `PAINS`(投稿の種の3軸)
- `scripts/post.py` の `ACCOUNT_CONCEPT` / `PERSONA`(変える場合は `scripts/review.py` の
  `ACCOUNT_CONCEPT` もそろえる)

## 2. Threads APIのトークン・ユーザーIDを取得

1. [Meta for Developers](https://developers.facebook.com/) でアプリを作成
2. 「製品を追加」→ **Threads API** を追加
3. スコープを有効化: `threads_basic` / `threads_content_publish` / `threads_manage_insights`
4. 「Threadsテスターを追加」で投稿先アカウントを追加し、Threads側で招待を承認
5. 短期アクセストークンを取得(Graph API Explorer等)
6. 長期トークン(60日)に交換:
   ```
   GET https://graph.threads.net/access_token
     ?grant_type=th_exchange_token&client_secret={Secret}&access_token={短期トークン}
   ```
   → `access_token` が `THREADS_ACCESS_TOKEN`
7. ユーザーIDを取得:
   ```
   GET https://graph.threads.net/v1.0/me?fields=id,username&access_token={長期トークン}
   ```
   → `id` が `THREADS_USER_ID`

画面構成が変わっていたら[公式ドキュメント](https://developers.facebook.com/docs/threads)を参照。

## 3. Gemini APIキーを取得

[Google AI Studio](https://aistudio.google.com/) →「Get API key」→ `GEMINI_API_KEY`

## 4. 初期データを作成してpush

```bash
pip install -r requirements.txt
python scripts/build_seeds.py
```

生成された `seeds.jsonl` は commit & push しておく(リポジトリに無いと post.py が「seeds.jsonl が空です」で止まる)。

GitHubにpush済みなら、ローカルで実行せず「Actions」タブ → `Setup (one-time)` → 「Run workflow」でも同じことができる。

## 5. GitHub Secretsを登録

このフォルダをGitHubリポジトリにpushし、Settings → Secrets and variables → Actions で登録:
- `THREADS_ACCESS_TOKEN`
- `THREADS_USER_ID`
- `GEMINI_API_KEY`

さらに Settings → Actions → General → Workflow permissions を「Read and write permissions」に。

## 6. 動作確認

「Actions」タブ → `daily` → 「Run workflow」で手動実行 → Threadsに投稿されるか確認。

以降は `daily.yml` / `weekly.yml` のスケジュールで自動運用される。
