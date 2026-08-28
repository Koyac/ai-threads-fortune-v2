"""
日次ジョブ本体。GitHub Actions から1日3回呼ばれ、そのたびに実行される。

1回の実行でやること:
    1. 取りこぼし判定    ... 投稿が遅れていないか確認する
    2. 種を1件取得       ... seeds.jsonl から次のお題(柱×業務×詰まりポイント)を取り出す
    3. A/Bパターンを決定 ... strategy.md に書かれたパターンを順番に割り当てる
    4. 生成             ... Gemini に投稿文を書かせる
    5. 重複チェック      ... 過去の投稿と似すぎていないか(本文・フック)を確認する
    6. 投稿             ... Threads APIに2段階で投稿する
    7. 記録             ... data/posts.jsonl に追記する
    8. commit & push    ... 変更をリポジトリに書き戻す

実行方法:
    python scripts/post.py
"""

import difflib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google import genai
from google.genai.errors import ClientError as GeminiClientError

from _secrets import redact as redact_secrets, run_safely

ROOT = Path(__file__).resolve().parent.parent
SEEDS_PATH = ROOT / "seeds.jsonl"
STRATEGY_PATH = ROOT / "strategy.md"
LEXICON_PATH = ROOT / "lexicon.md"
CONTENT_PROMPT_PATH = ROOT / "content_prompt.md"
POSTS_PATH = ROOT / "data" / "posts.jsonl"

JST = timezone(timedelta(hours=9))

# アカウントの立ち位置。プロンプトの冒頭に埋め込まれる。
# 発信内容の詳細(柱・フォーマット・トーン)は content_prompt.md 側で指定する。
ACCOUNT_CONCEPT = "忙しい会社員・フリーランス向けに、すぐ実践できるAI活用・業務効率化Tipsを毎日発信する"
PERSONA = "AIを使って自分の仕事を実際に減らしてきた、同じ立場の実務者"

# 1日の投稿スロット(JST)。label は集計用の名目スロット、due は実行してよい最短時刻。
# GitHub Actions 側で due(labelの15分前)に起動し、0〜20分のランダム待機をはさむので、
# 実投稿は label の15分前〜5分後あたりに散る。
# slotにはlabelを記録し、週次集計が細かい時刻で割れないようにする。
# 会社員・フリーランスが読む時間帯(通勤中・昼休み・帰宅後)に合わせている。
POSTING_WINDOWS = [
    {"label": "07:30", "due": "07:15"},
    {"label": "12:15", "due": "12:00"},
    {"label": "21:00", "due": "20:45"},
]
SLOT_TIMES = [window["label"] for window in POSTING_WINDOWS]

GENERATION_MODEL = "gemini-3.6-flash"

# 「似すぎ」判定は、意味的類似度(cosine similarity)ではなく、difflibによる文字列と
# しての類似度(0〜1、1に近いほど似ている)で行っている。1投稿ごとのAPI呼び出しを
# 生成の1回だけに抑えられる代わりに、言い回しが違うだけの類似投稿は拾いにくい。
# (Geminiには埋め込みモデルもあるので、精度を優先するなら embed_content に
#  差し替える手もある)
#
# Tips系の投稿は「フック / 本文 / 締め」の型が共通しているぶん、内容が別物でも
# 文字列としては近くなりやすい。そこで本文全体の閾値はやや厳しめにしたうえで、
# 直近のフック(1行目)と一致・酷似していないかを別途チェックしている。
DEDUP_THRESHOLD = 0.85
HOOK_DEDUP_THRESHOLD = 0.80     # 1行目(フック)同士の類似度。直近RECENT_HOOKS_WINDOW件と比較する
RECENT_HOOKS_WINDOW = 30
MAX_DEDUP_RETRIES = 3           # 1つの種につき、生成をやり直す回数の上限

# API利用コストを無限に膨らませないための安全弁。1回の実行で生成を呼んでよい回数の上限
# (種をまたいでも合計でこの回数まで)。値を上げると重複リトライの成功率は上がるが、
# その分コストも増える。
MAX_CATCHUP_POSTS_PER_RUN = 1
MAX_GENERATE_CALLS_PER_RUN = 3

# 発信の柱を巡回させる順番。build_seeds.py の PILLARS と content_prompt.md の
# 「発信の柱」に一致させること(ズレていると起動時に警告を出す)。
# 柱ごとに投稿の型が違うので、ここを巡回させることが投稿の単調さを防ぐ生命線になる。
#
# プロンプトを本文に載せる柱(即実践プロンプト例・よくある失敗と改善策)が連続しないよう、
# 載せない柱を間に挟む順番にしている。プロンプトが毎回並ぶと、内容が違っても
# 「プロンプト集」に見えてしまうため。
PILLAR_ROTATION = [
    "即実践プロンプト例",      # プロンプトを載せる
    "AIツール紹介",           # 載せない
    "あるある＋即Tips",        # 載せない
    "よくある失敗と改善策",      # プロンプトを載せる
    "AIの落とし穴と対処",       # 載せない
    "業務別時短Tips",         # 載せない
    "数字で示す効果",          # 載せない
]

# 直近この本数で使った業務シーンは、次の種選びで避ける。
RECENT_TASK_WINDOW = 4


# ============================================================
# 小さなユーティリティ(jsonlの読み書き・時刻の扱いなど)
# ============================================================

def load_jsonl(path: Path) -> list[dict]:
    """jsonl(1行1JSON)ファイルを読み込んで、辞書のリストにして返す。ファイルが空でもOK。"""
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def rewrite_jsonl(path: Path, records: list[dict]) -> None:
    """リスト全体で jsonl ファイルを丸ごと書き直す。seeds.jsonl の used/skipped 更新に使う。"""
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, record: dict) -> None:
    """1行だけ末尾に追記する。data/posts.jsonl への記録に使う。"""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def operational_date(dt_jst: datetime):
    """「運用日」を返す。07:00始まり・翌07:00終わりの1つの束として投稿日を扱うための工夫。

    深夜スロットを使う場合、カレンダー上の日付と運用日がズレることがあるため、
    07:00より前の時刻は「前日の続き」とみなすように7時間分シフトしてから日付を取る。
    """
    return (dt_jst - timedelta(hours=7)).date()


def slot_datetime(op_date, slot_str: str) -> datetime:
    """"11:45"のようなスロット文字列を、指定した運用日における実際の日時に変換する。"""
    hour, minute = map(int, slot_str.split(":"))
    day = op_date + timedelta(days=1) if hour < 7 else op_date
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=JST)


# ============================================================
# strategy.md の読み取り
# ============================================================

def parse_pattern_definitions(strategy_text: str) -> dict[str, str]:
    """strategy.md の「## A/Bパターン」セクションから、今使えるパターン一覧を取り出す。

    週次ジョブ(review.py)は、負けたパターンをこのセクションから削除することで
    「今後使わない」を実現する設計にしている。
    """
    patterns: dict[str, str] = {}
    in_section = False
    for raw_line in strategy_text.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            in_section = line[3:].strip().startswith("A/Bパターン")
            continue
        if in_section:
            m = re.match(r"-\s*([A-Za-z]+)\s*:\s*(.+)", line)
            if m:
                patterns[m.group(1)] = m.group(2).strip()
    if not patterns:
        raise ValueError("strategy.md の「## A/Bパターン」セクションからパターンを読み取れませんでした。")
    return patterns


def pick_pattern(patterns: dict[str, str], total_posts_so_far: int) -> tuple[str, str]:
    """A→B→C→D...と、ランダムではなく順番に均等に割り当てる。

    「これまでに投稿した本数」を使って割り当てを進めるので、自然と巡回する。
    """
    codes = list(patterns.keys())
    code = codes[total_posts_so_far % len(codes)]
    return code, patterns[code]


# ============================================================
# 直近投稿からの言い回し抽出(フック・語尾)
# ============================================================

def extract_hook(text: str) -> str:
    """投稿本文の1行目(=フック)を取り出す。空行しかなければ空文字列。"""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def extract_recent_hooks(posts: list[dict], n: int = RECENT_HOOKS_WINDOW) -> list[str]:
    """直近n件の投稿のフック(1行目)を集める。

    Tips系は「〜な人へ」のような呼びかけが便利なぶん、放っておくと同じフックが
    何度も出てくる。プロンプトで既出フックを見せ、生成後にも類似度で弾く。
    """
    return [hook for hook in (extract_hook(p.get("text", "")) for p in posts[-n:]) if hook]


def extract_recent_endings(posts: list[dict], n: int = 5) -> list[str]:
    """直近n件の投稿から、文末の言い回しをざっくり抜き出す(簡易ヒューリスティック)。

    本格的な形態素解析はせず、句点・感嘆符で区切った最後のかたまりの末尾数文字を
    「語尾」の代わりとして使う。厳密さより「同じ言い回しの連発を防ぐ」目的で十分。
    """
    endings = []
    for post in posts[-n:]:
        text = (post.get("text") or "").strip()
        sentences = [s for s in re.split(r"[。！\n]", text) if s.strip()]
        if sentences:
            endings.append(sentences[-1].strip()[-8:])
    return endings


# ============================================================
# Gemini呼び出し(生成) & 重複チェック(ローカル文字列類似度)
# ============================================================

def is_daily_quota_error(exc: GeminiClientError) -> bool:
    """429のうち「1日の上限(無料枠を使い切った)」かどうかを判定する。

    無料枠には「1日あたり(PerDay)」と「1分あたり(PerMinute)」の2種類の上限があり、
    どちらも429で返ってくる。1分あたりの方は少し待てば復活するので、
    ここで区別して、待って意味がある方だけリトライさせる。
    判定できない場合は安全側に倒して「1日の上限」とみなす(=無駄打ちしない)。
    """
    message = str(exc)
    if re.search(r"PerMinute|per minute|PerMinutePer", message):
        return False
    return True


def call_with_backoff(func, max_attempts: int = 5):
    """Gemini API呼び出し用の共通リトライ処理。指数バックオフで最大5回まで試す。

    503 UNAVAILABLE(「今このモデルが混んでいます」)は無料枠だと当たりやすいが、
    時間をおけば直る一時的な障害なので、回数と待ち時間を多めに取って粘る。
    429のうち「1分あたりの上限」も待てば直るので、60秒あけて最大2回まで待ち直す。
    逆に、待っても直らないもの(429=1日の無料枠切れ / 400=リクエスト不正 / 401・403=キー不正)は
    即座に呼び出し元へ投げ返す。
    """
    minute_quota_waits = 0
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except GeminiClientError as exc:
            # 4xx系は基本的に粘っても無駄なので、そのまま上へ返して呼び出し元に判断させる。
            if exc.code == 429 and not is_daily_quota_error(exc) and minute_quota_waits < 2:
                minute_quota_waits += 1
                print(f"[post] Geminiの1分あたりの上限に当たりました。60秒待って再試行します: {redact_secrets(exc)}")
                time.sleep(60)
                continue
            raise
        except Exception as exc:  # noqa: BLE001 - Gemini側の一時的な障害を包括的に受けてリトライしたい
            if attempt == max_attempts:
                raise
            wait_seconds = min(2 ** attempt, 30)
            print(f"[post] Gemini呼び出し失敗(試行{attempt}/{max_attempts}): {redact_secrets(exc)} -> {wait_seconds}秒後に再試行")
            time.sleep(wait_seconds)


def build_prompt(strategy_text, lexicon_text, content_prompt_text, seed, pattern_code, pattern_meaning,
                  recent_hooks, recent_endings) -> str:
    hooks_block = "\n".join(f"- {hook}" for hook in recent_hooks) if recent_hooks else "(まだ実績なし)"
    endings_block = "、".join(recent_endings) if recent_endings else "(まだ実績なし)"

    return f"""あなたはThreadsで「{ACCOUNT_CONCEPT}」アカウントを運用している、{PERSONA}です。
以下の情報だけを元に、Threadsに投稿する本文を1つ書いてください。

# アカウントの生成方針・フォーマット詳細 (content_prompt.md)
{content_prompt_text}

# 今週の投稿方針 (strategy.md)
{strategy_text}

# 使ってよい語彙・禁止表現 (lexicon.md)
{lexicon_text}

# 今回のお題(この3つの掛け合わせで書くこと。話を広げず、この1点に絞る)
- 軸1(発信の柱): {seed["pillar"]}
- 軸2(業務シーン): {seed["task"]}
- 軸3(読者が詰まっているポイント): {seed["pain"]}

# 今回のパターン: {pattern_code}（{pattern_meaning}）

# 直近{len(recent_hooks)}投稿のフック(1行目)。これらと同じ・似た書き出しは禁止
{hooks_block}

# 直近5投稿の文末表現(そのままの繰り返しは禁止)
{endings_block}

# 出力ルール
- strategy.md の「固定ルール」セクションを必ず守ること
- content_prompt.md のルールを必ず守ること
- 本文は150〜400文字。数えて超えていたら削ってから出力すること
- Tipsは1つだけ。2つ目を思いついても書かないこと
- content_prompt.md の「具体性の絶対条件」4つと「出力前の自己チェック」5項目を必ず通すこと
- 特に、どこに打ち込むかを書くこと・カギカッコの中だけでそのまま動くプロンプト全文を書くこと
- ツールは原則限定しない。特定ツール固有の機能を使うときだけ名指しすること
- 出力は投稿本文のみ。前置き・説明・引用符・見出しは一切つけないこと
"""


def generate_post_text(client: genai.Client, prompt: str) -> str:
    def _call():
        response = client.models.generate_content(model=GENERATION_MODEL, contents=prompt)
        text = (response.text or "").strip()
        if not text:
            # 安全性フィルタで止められた場合も text は空になる。原因が追えるよう
            # finish_reason をログに残しておく。
            reason = response.candidates[0].finish_reason if response.candidates else "不明"
            raise RuntimeError(f"Geminiから空の応答が返ってきました (finish_reason={reason})")
        return text

    return call_with_backoff(_call)


def text_similarity(a: str, b: str) -> float:
    """2つの文字列の類似度(0〜1)をdifflibで計算する。意味ではなく文字列としての近さ。"""
    return difflib.SequenceMatcher(None, a, b).ratio()


def is_too_similar(text: str, existing_texts: list[str], recent_hooks: list[str]) -> tuple[bool, str]:
    """本文全体・フックの2段構えで「似すぎ」を判定する。(判定, 理由)を返す。"""
    if any(text_similarity(text, existing) >= DEDUP_THRESHOLD for existing in existing_texts):
        return True, "本文が過去の投稿と似すぎています"

    hook = extract_hook(text)
    if hook and any(text_similarity(hook, past) >= HOOK_DEDUP_THRESHOLD for past in recent_hooks):
        return True, f"フック「{hook}」が直近の投稿と似すぎています"

    return False, ""


# ============================================================
# 種の管理(取得・消化済み更新)
# ============================================================

def find_next_seed_index(seeds: list[dict], posts: list[dict]) -> int | None:
    """次に使う種を選ぶ。柱を必ず巡回させ、業務シーンの連続も避ける。

    以前は「未使用のうち先頭」を取っていたが、それだと消費順が build_seeds.py の
    シャッフル結果そのままになり、短い期間で見ると柱が偏る。実際、最初の9投稿は
    6本が「即実践プロンプト例」に集中し、「あるある＋即Tips」と「数字で示す効果」は
    一度も使われなかった。柱ごとに投稿の型を変えている以上、この偏りは
    そのまま「毎回同じような投稿」に直結する。

    そこでA/Bパターンと同じように、投稿数から次の柱を機械的に決める。
    そのうえで、直近で使った業務シーンとは違うものを優先する。
    """
    unused = [i for i, s in enumerate(seeds) if not s.get("used")]
    if not unused:
        return None

    wanted_pillar = PILLAR_ROTATION[len(posts) % len(PILLAR_ROTATION)]
    candidates = [i for i in unused if seeds[i].get("pillar") == wanted_pillar]
    if not candidates:
        # その柱の種を使い切っている場合は、柱の制約を外して枯渇を避ける。
        candidates = unused

    recent_tasks = {p.get("task") for p in posts[-RECENT_TASK_WINDOW:]}
    fresh = [i for i in candidates if seeds[i].get("task") not in recent_tasks]
    return (fresh or candidates)[0]


class GenerationBudgetExhausted(RuntimeError):
    """1回の実行で使ってよい生成回数(MAX_GENERATE_CALLS_PER_RUN)を使い切ったことを表す。

    API利用コストを無限に膨らませないための安全弁。この例外は「今回はもう諦めて
    次の実行に任せる」という正常系の一部として扱い、呼び出し側はエラー終了せず
    ログを出して次のスロットに委ねる。
    """


def generate_unique_post(client, strategy_text, lexicon_text, content_prompt_text, pattern_code, pattern_meaning,
                          recent_hooks, recent_endings, seeds, existing_texts, posts):
    """種を1つずつ試しながら、重複しない投稿文ができるまで繰り返す。

    1つの種につき最大 MAX_DEDUP_RETRIES 回まで生成し直し、それでも似すぎている場合は
    その種を「投稿されないまま消化済み(skipped)」にして次の種に進む。
    種をまたいだ合計の生成回数が MAX_GENERATE_CALLS_PER_RUN に達したら、その時点で
    諦めて GenerationBudgetExhausted を送出する(今まさに試していた種はまだリトライ回数を
    使い切っていないので skipped にはしない = 次回また普通に候補になる)。
    """
    calls_made = 0
    while True:
        seed_index = find_next_seed_index(seeds, posts)
        if seed_index is None:
            raise RuntimeError("未使用の種(seed)がもうありません。build_seeds.py の再実行を検討してください。")
        seed = seeds[seed_index]

        exhausted_this_seed = False
        for attempt in range(1, MAX_DEDUP_RETRIES + 1):
            if calls_made >= MAX_GENERATE_CALLS_PER_RUN:
                raise GenerationBudgetExhausted(
                    f"この実行での生成回数の上限({MAX_GENERATE_CALLS_PER_RUN}回)に達しました。"
                )
            prompt = build_prompt(strategy_text, lexicon_text, content_prompt_text, seed,
                                   pattern_code, pattern_meaning, recent_hooks, recent_endings)
            text = generate_post_text(client, prompt)
            calls_made += 1
            too_similar, reason = is_too_similar(text, existing_texts, recent_hooks)
            if not too_similar:
                return seed_index, text
            print(f"[post] 種 {seed['id']}: {reason}(試行{attempt}/{MAX_DEDUP_RETRIES})")
            if attempt == MAX_DEDUP_RETRIES:
                exhausted_this_seed = True

        if exhausted_this_seed:
            # MAX_DEDUP_RETRIES 回とも似すぎていた場合だけ、この種は諦めて
            # 「消化済み・未投稿」として記録し、次の種へ移る。
            # ここでの変更は投稿が成功していなくても失わないよう、その場でcommitしておく。
            seeds[seed_index]["used"] = True
            seeds[seed_index]["skipped"] = True
            rewrite_jsonl(SEEDS_PATH, seeds)
            commit_and_push(f"post: 種 {seed['id']} は重複のためスキップ", paths=["seeds.jsonl"])
            print(f"[post] 種 {seed['id']} は重複が解消できずスキップしました")


# ============================================================
# Threads API
# ============================================================

class ThreadsRateLimited(Exception):
    """Threads APIがレート制限を返したことを表す例外。呼び出し側はリトライせず終了する。"""


class ThreadsApiError(Exception):
    """Threads APIがエラーを返したことを表す例外。認証情報は伏せた状態で詳細を持つ。"""


RATE_LIMIT_ERROR_CODES = {4, 17, 32, 613}

# 「コンテナがまだ見つからない」を表すエラー。作成直後の反映待ちで返ることがある。
MEDIA_NOT_FOUND_CODE = 24
MEDIA_NOT_FOUND_SUBCODE = 4279009

# コンテナが公開可能になるまで待つ上限と、確認の間隔(秒)。
CONTAINER_READY_TIMEOUT = 90
CONTAINER_POLL_INTERVAL = 3


def raise_for_status_without_token(response: requests.Response) -> None:
    """requests標準の raise_for_status とほぼ同じだが、メッセージから認証情報を伏せる。

    requests.HTTPError のメッセージにはリクエストURLがそのまま入るため、
    素で投げるとクエリに載った認証情報がログに残りうる。
    """
    try:
        response.raise_for_status()
    except requests.HTTPError:
        body = redact_secrets(response.text.strip())
        if len(body) > 500:
            body = body[:500] + "..."
        detail = f": {body}" if body else ""
        raise ThreadsApiError(f"HTTP {response.status_code}{detail}") from None


def _check_rate_limit(response: requests.Response) -> None:
    if response.status_code == 429:
        raise ThreadsRateLimited(f"HTTP 429: {redact_secrets(response.text)}")
    try:
        body = response.json()
    except ValueError:
        return
    error = body.get("error") if isinstance(body, dict) else None
    if error and error.get("code") in RATE_LIMIT_ERROR_CODES:
        raise ThreadsRateLimited(f"レート制限エラー: {redact_secrets(error)}")


def _error_body(response: requests.Response) -> dict:
    """レスポンスから error オブジェクトを取り出す。JSONでなければ空の辞書。"""
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _is_media_not_found(response: requests.Response) -> bool:
    error = _error_body(response)
    return (
        error.get("error_subcode") == MEDIA_NOT_FOUND_SUBCODE
        or error.get("code") == MEDIA_NOT_FOUND_CODE
    )


def wait_for_container_ready(creation_id: str, access_token: str) -> None:
    """下書きコンテナが公開可能になるまで待つ。

    Threads APIのコンテナ作成は非同期で、作成直後に公開しようとすると
    「Media Not Found」(code 24 / subcode 4279009)で弾かれることがある。
    エラーには is_transient: false と入っているが、実際にはタイミング依存の
    一時的な失敗なので、statusがFINISHEDになるのを待ってから公開する。

    反映前はこのstatus取得自体も同じMedia Not Foundを返すことがあるため、
    その場合はエラーにせず待ち続ける。
    """
    deadline = time.monotonic() + CONTAINER_READY_TIMEOUT
    last_status = "作成直後"

    while True:
        resp = requests.get(
            f"https://graph.threads.net/v1.0/{creation_id}",
            params={"fields": "status,error_message", "access_token": access_token},
            timeout=30,
        )
        _check_rate_limit(resp)

        if resp.ok:
            body = resp.json()
            status = body.get("status")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                detail = redact_secrets(str(body.get("error_message")))
                raise ThreadsApiError(f"コンテナが公開できない状態です (status={status}): {detail}")
            last_status = status or "不明"
        elif _is_media_not_found(resp):
            # まだ反映されていないだけ。待って確認し直す。
            last_status = "未反映"
        else:
            raise_for_status_without_token(resp)

        if time.monotonic() >= deadline:
            raise ThreadsApiError(
                f"コンテナが{CONTAINER_READY_TIMEOUT}秒以内に公開可能になりませんでした "
                f"(最後のstatus={last_status})"
            )
        time.sleep(CONTAINER_POLL_INTERVAL)


def post_to_threads(text: str, access_token: str, user_id: str) -> str:
    """Threads APIの2段階投稿(下書き作成 → 公開)を実行し、公開後のmedia_idを返す。"""
    base = f"https://graph.threads.net/v1.0/{user_id}"

    create_resp = requests.post(f"{base}/threads", data={
        "media_type": "TEXT",
        "text": text,
        "access_token": access_token,
    }, timeout=30)
    _check_rate_limit(create_resp)
    raise_for_status_without_token(create_resp)
    creation_id = create_resp.json()["id"]

    # 作成直後は公開できないことがあるので、準備できるまで待つ。
    wait_for_container_ready(creation_id, access_token)

    publish_resp = requests.post(f"{base}/threads_publish", data={
        "creation_id": creation_id,
        "access_token": access_token,
    }, timeout=30)
    _check_rate_limit(publish_resp)
    raise_for_status_without_token(publish_resp)
    return publish_resp.json()["id"]


# ============================================================
# git commit & push
# ============================================================

def commit_and_push(message: str, paths: list[str] | None = None, max_attempts: int = 10) -> None:
    """変更をコミットしてpushする。投稿は既に成功しているため、二重投稿を防ぐために
    pushが成功するまで(リモートの更新を取り込みながら)リトライし続ける。

    paths を指定しない場合は投稿成功時のデフォルト(seeds.jsonl + data/posts.jsonl)。
    種をスキップしただけでまだ投稿していない場合は paths=["seeds.jsonl"] のように
    絞って呼び出す。
    """
    if paths is None:
        paths = ["seeds.jsonl", "data/posts.jsonl"]

    subprocess.run(["git", "config", "user.name", "github-actions"], cwd=ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "actions@github.com"], cwd=ROOT, check=True)
    subprocess.run(["git", "add", *paths], cwd=ROOT, check=True)

    commit_result = subprocess.run(["git", "commit", "-m", message], cwd=ROOT, capture_output=True, text=True)
    if commit_result.returncode != 0 and "nothing to commit" not in commit_result.stdout:
        raise RuntimeError(f"git commit に失敗しました: {redact_secrets(commit_result.stderr)}")

    for attempt in range(1, max_attempts + 1):
        push_result = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True)
        if push_result.returncode == 0:
            return
        print(f"[post] git push 失敗(試行{attempt}/{max_attempts}): {redact_secrets(push_result.stderr.strip())}")
        # 他のジョブが先にpushしていた可能性があるので、取り込んでから再挑戦する。
        subprocess.run(["git", "pull", "--rebase"], cwd=ROOT, capture_output=True, text=True)
        time.sleep(min(2 ** attempt, 30))

    raise RuntimeError(
        "git push が繰り返し失敗しました。投稿自体は成功済みのため、"
        "二重投稿を避けるためにも手動でリポジトリの状態を確認してください。"
    )


# ============================================================
# メイン処理
# ============================================================

def main() -> None:
    access_token = os.environ["THREADS_ACCESS_TOKEN"]
    user_id = os.environ["THREADS_USER_ID"]
    api_key = os.environ["GEMINI_API_KEY"]

    client = genai.Client(api_key=api_key)

    now = datetime.now(JST)
    op_date = operational_date(now)

    posts = load_jsonl(POSTS_PATH)
    seeds = load_jsonl(SEEDS_PATH)
    if not seeds:
        print("[post] seeds.jsonl が空です。先に build_seeds.py を実行してください。", file=sys.stderr)
        sys.exit(1)

    # 柱名がズレていると巡回が空振りし、また同じ柱ばかりになる。早い段階で気づけるようにする。
    unknown = {s.get("pillar") for s in seeds} - set(PILLAR_ROTATION)
    if unknown:
        print(f"[post] 警告: PILLAR_ROTATION に無い柱が種にあります: {sorted(unknown)}", file=sys.stderr)

    # --- 1. 取りこぼし判定 ---
    due_slots = [
        window["label"]
        for window in POSTING_WINDOWS
        if slot_datetime(op_date, window["due"]) <= now
    ]
    posts_today = [
        p for p in posts
        if operational_date(datetime.fromisoformat(p["posted_at"]).astimezone(JST)) == op_date
    ]
    remaining_slots = due_slots[len(posts_today):]
    to_post = min(len(remaining_slots), MAX_CATCHUP_POSTS_PER_RUN)

    if to_post <= 0:
        print("[post] 現時点で投稿すべきスロットはありません。何もせず終了します。")
        return

    strategy_text = STRATEGY_PATH.read_text(encoding="utf-8")
    lexicon_text = LEXICON_PATH.read_text(encoding="utf-8")
    content_prompt_text = CONTENT_PROMPT_PATH.read_text(encoding="utf-8")
    existing_texts = [p["text"] for p in posts if p.get("text")]

    made_any = False
    for slot in remaining_slots[:to_post]:
        # --- 3. A/Bパターンを決定 ---
        patterns = parse_pattern_definitions(strategy_text)
        pattern_code, pattern_meaning = pick_pattern(patterns, len(posts))

        recent_hooks = extract_recent_hooks(posts)
        recent_endings = extract_recent_endings(posts)

        # --- 2・4・5. 種の取得〜生成〜重複チェック ---
        try:
            seed_index, text = generate_unique_post(
                client, strategy_text, lexicon_text, content_prompt_text,
                pattern_code, pattern_meaning, recent_hooks, recent_endings, seeds, existing_texts,
                posts,
            )
        except RuntimeError as exc:
            print(f"[post] {redact_secrets(exc)}")
            break
        except GeminiClientError as exc:
            # 429(レート制限・無料枠切れ)以外のクライアントエラーは設定ミスの可能性が
            # 高いので、握りつぶさずそのまま落としてActionsを赤くする。
            if exc.code != 429:
                raise
            print(f"[post] Gemini APIがレート制限中のため、この実行はここで終了します: {redact_secrets(exc)}")
            break
        seed = seeds[seed_index]

        # --- 6. 投稿 ---
        try:
            media_id = post_to_threads(text, access_token, user_id)
        except ThreadsRateLimited as exc:
            print(f"[post] Threads APIがレート制限中のため、この実行はここで終了します: {redact_secrets(exc)}")
            break

        # --- 7. 記録 ---
        # media_id は collect.py が後で `/{media-id}/insights` を叩くときに使う、
        # Threads側がこの投稿につけた本当のID。
        seeds[seed_index]["used"] = True
        record = {
            "id": f"p{len(posts) + 1:05d}",
            "media_id": media_id,
            "seed_id": seed["id"],
            "pattern": pattern_code,
            "pattern_meaning": pattern_meaning,
            "pillar": seed["pillar"],
            "task": seed["task"],
            "pain": seed["pain"],
            "slot": slot,
            "text": text,
            "posted_at": now.isoformat(),
        }
        posts.append(record)
        existing_texts.append(text)
        append_jsonl(POSTS_PATH, record)
        rewrite_jsonl(SEEDS_PATH, seeds)
        made_any = True

        # --- 8. commit & push ---
        commit_and_push(f"post: {slot} {seed['pillar']}/{seed['task']}/{seed['pain']} (pattern {pattern_code})")
        print(f"[post] {slot} 分の投稿が完了しました (seed={seed['id']}, pattern={pattern_code})")

    if not made_any:
        print("[post] 今回の実行では投稿できませんでした。")


if __name__ == "__main__":
    run_safely(main, "post")
