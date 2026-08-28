"""
投稿の「種」を全パターン作って seeds.jsonl に書き出すスクリプト。

企画が始まる前に、これ1回だけ実行する（毎日実行するスクリプトではない）。
post.py が日々の投稿を作るとき、この seeds.jsonl の先頭から1件ずつ取り出して消費していく。

実行方法:
    python scripts/build_seeds.py
"""

import itertools
import json
import random
from pathlib import Path

# このファイル (scripts/build_seeds.py) から見て1つ上の階層がリポジトリのルート。
# GitHub Actions からでもローカルからでも、どこから実行しても正しい場所に書き出せるようにしている。
ROOT = Path(__file__).resolve().parent.parent
SEEDS_PATH = ROOT / "seeds.jsonl"

# --- 3つの軸。ここを増減させるだけで全体の組み合わせ数が変わる ---

# 軸1: 発信の柱。content_prompt.md の「発信の柱」と1対1で対応させる。
# 週次集計(aggregate.py)はこの軸ごとに反応を比べるので、増やしすぎると1つあたりの
# サンプル数が減って比較しづらくなる。
PILLARS = [
    "即実践プロンプト例",
    "業務別時短Tips",
    "よくある失敗と改善策",
    "数字で示す効果",
    "あるある＋即Tips",
    "AIツール紹介",
    "AIの落とし穴と対処",
]

# 軸2: 対象の業務シーン。
# 「文章を書く」系だけに寄ると、どの投稿も同じような話になる。読む・調べる・数える・
# 整える・伝える・備えるといった、種類の違う仕事を意図的に混ぜている。
TASKS = [
    # --- 書く ---
    "メール返信",
    "議事録",
    "報告書・日報",
    "企画書・提案書",
    "資料作成（スライド）",
    "チャット/Slackの返信",
    "長文の下書き",
    # --- 読む・調べる ---
    "情報収集・記事の要約",
    "長い資料・PDFを読む",
    "調べ物の裏取り・事実確認",
    "競合・市場のリサーチ",
    # --- 数える・扱う ---
    "数字・データの整理",
    "Excel・スプレッドシートの関数",
    "グラフ・数値の読み解き",
    # --- 整える・確かめる ---
    "誤字脱字・文章のチェック",
    "抜け漏れの洗い出し",
    "文章の言い換え・トーン調整",
    # --- 考える ---
    "アイデア出し",
    "考えの整理・壁打ち",
    "判断・優先順位づけ",
    # --- 伝える・備える ---
    "会議の準備・想定問答",
    "説明・プレゼンの組み立て",
    "英語のメール・翻訳",
    "問い合わせ・クレーム対応",
    # --- 段取り ---
    "スケジュール調整",
    "タスク管理",
    "手順書・マニュアル作成",
    "学習・スキルアップ",
]

# 軸3: その業務で詰まっているポイント。
# 特定の作業(書くこと)に寄った表現を避け、どの業務シーンにも当てはまる形にしている。
PAINS = [
    "とにかく時間がかかる",
    "何から手をつけるか分からない",
    "抜け漏れ・ミスが出る",
    "つい後回しにする",
    "AIに頼んでも出力が微妙",
    "自分でやると品質がばらつく",
    "そもそもAIが使えると思っていない",
]


def build_seeds() -> list[dict]:
    """3軸の直積（全部の組み合わせ）を作り、順番をシャッフルして返す。

    5(発信の柱) × 12(業務シーン) × 6(詰まりポイント) = 360通り。
    1日3投稿なら約120日分にあたる。
    シャッフルするのは、似た柱・業務の投稿が連日続かないようにするため。
    """
    combinations = list(itertools.product(PILLARS, TASKS, PAINS))
    random.shuffle(combinations)

    seeds = []
    for i, (pillar, task, pain) in enumerate(combinations, start=1):
        seeds.append({
            "id": f"s{i:04d}",       # s0001, s0002, ... のような連番ID
            "pillar": pillar,
            "task": task,
            "pain": pain,
            "used": False,           # post.py がこの種を使ったら True にする
            # "skipped" は重複チェックで規定回数弾かれた種にだけ post.py が付け足すフィールド。
            # 最初は付けない（＝まだ一度も弾かれていない状態）。
        })
    return seeds


def main() -> None:
    if SEEDS_PATH.exists() and SEEDS_PATH.read_text(encoding="utf-8").strip():
        # 誤って2回実行すると、投稿済みかどうかの情報(used)ごとキューが
        # シャッフルし直されてしまう。運用中の事故を防ぐため、
        # 既に中身があるなら安全のために止める。
        print(f"[build_seeds] {SEEDS_PATH} は既に存在し、空ではありません。上書きを避けるため何もしませんでした。")
        print("[build_seeds] 本当に作り直したい場合は、先に既存の seeds.jsonl を削除してから実行してください。")
        return

    seeds = build_seeds()

    with SEEDS_PATH.open("w", encoding="utf-8") as f:
        for seed in seeds:
            # jsonl形式 = 1行に1つのJSONオブジェクト。あとから1行ずつ読み書きしやすい。
            f.write(json.dumps(seed, ensure_ascii=False) + "\n")

    print(f"[build_seeds] {len(seeds)} 件の種を {SEEDS_PATH} に書き出しました。")


if __name__ == "__main__":
    main()
