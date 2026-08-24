# ai-threads-tips

Threads の**AI活用・業務効率化Tips**アカウントを、GitHub Actions と Gemini API だけで完全自動運用するシステム。

忙しい会社員・フリーランス向けに、1〜2分で試せるAI活用Tipsを1日3回投稿する。
テキストのみ・画像なし・一方的な有益投稿（返信や絡みは想定しない）。

サーバー・データベースは使わない。実行のたびにスクリプト自身がこのリポジトリへ commit / push し、
投稿ログや週次の方針もすべてリポジトリ内のファイルとして残る。

## 必要なもの

- Python 3.12
- GitHub Secrets に登録する値
  - `THREADS_ACCESS_TOKEN`
  - `THREADS_USER_ID`
  - `GEMINI_API_KEY`

## ディレクトリ構成

```
seeds.jsonl                  # 投稿の種(消費キュー)。build_seeds.py で生成
strategy.md                  # 週次で書き換わる投稿方針(唯一の可変状態)
lexicon.md                   # 語彙・語尾・禁止表現
content_prompt.md            # 生成方針・フォーマット詳細のプロンプト本体
data/posts.jsonl             # 投稿ログ
data/metrics.jsonl           # 日次メトリクスのスナップショット
scripts/
  build_seeds.py             # 【初回のみ】投稿の種を生成(発信の柱 × 業務シーン × 詰まりポイント)
  post.py                    # 【日次】投稿する
  collect.py                 # 【日次・1回】メトリクスを収集する
  aggregate.py               # 【週次】LLMを使わずに集計する
  review.py                  # 【週次】集計結果をもとにAIが方針を更新する
.github/workflows/
  setup.yml                  # 【初回のみ・手動】seeds.jsonl を生成してcommit
  daily.yml                  # 日次スケジュール(post.py / collect.py)
  weekly.yml                 # 週次スケジュール(aggregate.py -> review.py)
```

## 発信内容の設計

投稿の中身は、コードではなく次の3ファイルで決まる。方針を変えたいときはここを直す。

| ファイル | 決めていること |
| --- | --- |
| `content_prompt.md` | コンセプト・想定読者・発信の柱・投稿フォーマット・トーン・禁止事項 |
| `strategy.md` | 固定ルール、A/Bパターン、投稿スロット（週次ジョブが「固定ルール」以外を書き換える） |
| `lexicon.md` | 使う語彙・語尾、禁止表現（週次ジョブが禁止欄に追記する） |

### 発信の柱（5つをローテーション）

1. 即実践プロンプト例
2. 業務別時短Tips
3. よくある失敗と改善策
4. 小さな習慣化Tips
5. 数字で示す効果

### 投稿の種（seeds.jsonl）

`発信の柱(5) × 業務シーン(12) × 詰まりポイント(6) = 360通り` をシャッフルしてキューにしている。
1日3投稿なので約120日分。使い切ったら `build_seeds.py` を作り直すか、軸を足す。

### 投稿スロット

`07:30 / 12:15 / 21:00`（JST、前後20分に散る）。通勤中・昼休み・帰宅後を狙っている。
変えるときは **`scripts/post.py` の `POSTING_WINDOWS`・`scripts/aggregate.py` の `SLOT_ORDER`・
`.github/workflows/daily.yml` の cron の3か所** をそろえて直す。

## 公開リポジトリとして運用する上での注意

このリポジトリはpublicで運用している。**GitHub Actionsの実行ログもWeb上で誰でも読める**ため、
認証情報がログに出ないようコード側で対策してある。

### 秘密情報の扱い

- 認証情報は**必ずGitHub Secretsに入れる**。コードやワークフローに直接書かない
- ログに出す文字列は `scripts/_secrets.py` の `redact()` を必ず通す。
  例外メッセージ・APIレスポンス・gitコマンドの出力は、外部由来なので何が混ざるか分からない
- `main()` は `run_safely()` で包む。未捕捉の例外が出たとき、トレースバックには
  リクエストURLがそのまま載る（Threads APIはクエリに `access_token` を乗せる）ため、
  素で出すとトークンが公開ログに残ってしまう
- GitHub側でもSecretsは自動マスクされるが、URLエンコードされるなどして
  登録値と完全一致しない形で出るとすり抜ける。`redact()` はその穴を埋めるためのもの

**新しくログ出力を足すときは、外部由来の文字列を `{変数}` のまま埋め込まないこと。**

### 公開されるもの・されないもの

| 公開される | 公開されない |
| --- | --- |
| ソースコード一式 | GitHub Secretsの中身 |
| `data/posts.jsonl`（投稿本文・media_id） | — |
| `data/metrics.jsonl`（views/いいね/フォロワー数） | — |
| `strategy.md` の変更履歴（AIの判断理由） | — |
| Actionsの実行ログ | — |

投稿本文とmedia_idはThreads上で元々公開されている情報なので問題ない。
一方で**フォロワー数や反応率の推移が誰でも追える**状態になる点は認識しておくこと。
これを見せたくない場合はリポジトリをprivateにする。

### トークンの失効

Threadsの長期アクセストークンは**60日で失効する**。自動更新は実装していないので、
期限を控えて手動で取り直すこと。切れると投稿が止まる。

なお、**トークンが漏れた疑いがあるときは、まずMeta側でトークンを無効化して再発行する。**
リポジトリからログを消しても、フォークやキャッシュに残っている可能性があるため、
「消す」より「失効させる」ほうが確実。

## セットアップ

投稿できるようになるまでの手順は [SETUP.md](SETUP.md) を参照。

## 各スクリプトを単体で試す

すべてリポジトリのルートから実行する想定。

```bash
python scripts/build_seeds.py
python scripts/post.py
python scripts/collect.py
python scripts/aggregate.py | python scripts/review.py
```
