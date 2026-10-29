"""Assemble the standard Reddit trend report (Gemini series).

Differences from assemble_report.py (qwen series):
- reads .cache/gemini_* files and the deduped post list
- None-safe metrics (the RSS fetcher emits ups/num_comments = null)
- paragraph split tolerates both real blank lines and the literal "\\n\\n"
- output filename suffix _gemini.md / generated_by from gemini_meta.json
"""
import datetime as dt
import json
import os
import re
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPORT_DIR = Path(os.environ.get(
    "REDDIT_OUT_DIR", str((Path("/Volumes/ROCKET_XTRM/Obsidian") if sys.platform == "darwin"
         else Path.home() / "Obsidian") / "📰_情報配信" / "Reddit")
))


def write_atomic(path: Path, text: str) -> None:
    """Write via temp+rename so a crash never leaves a half-written report
    (the Mac import never overwrites existing files, so a torn file would
    stick)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)

CATEGORY_EMOJI = {
    "Tech": "💻", "Politics": "🏛️", "Gaming": "🎮", "Entertainment": "🎬",
    "Science": "🔬", "Sports": "⚽", "World": "🌍", "Business": "💼",
    "Lifestyle": "🏠", "Humor": "😂", "Other": "📌",
}


# ---------------------------------------------------------------------------
# note.com 記事（2026-10-12 追加）
# ---------------------------------------------------------------------------
# 形は Hacker News の note 原稿（../Hacker-News/hn_daily.py の build_note）にそろえる。
# 先頭の「案1:」・ハッシュタグ行・貼り付けの区切りは、note への自動投稿
# （../Hacker-News/note_upload.py の parse_article）が読む目印なので崩さない。
# 本文の記法は ../Hacker-News/note-output-spec.md に従う（## と ### だけ、表・斜体なし）。

NOTE_FILE_STEM = "Reddit-Top30"  # run_reddit_gemini.NOTE_FILE_STEM と同じ
NOTE_TITLE_PREFIX = "Reddit日本語解説"
NOTE_FIXED_HASHTAGS = "#毎日note #Reddit #海外ニュース #海外の反応 #海外トレンド #ネットで話題 #雑学"
NOTE_HASHTAG_MAX = 10
NOTE_PASTE_MARKER = "＝＝＝ ここから下を note 本文に全文貼り付け ＝＝＝"
# 掲示板名に汚い言葉が入っているものは、名前を本文に出さない（リンクは残す）
PROFANE_RE = re.compile(r"fuck|shit|cunt|bitch|dick|piss|porn|nsfw|sex", re.I)
# 「【ここに画像】」のような書き込み。note への自動投稿の事前検査（hn_daily.validate_note）が
# 止める対象なので、本文に混ざったら行ごと落とす（hn_daily.sanitize_note と同じ扱い）
IMAGE_PLACEHOLDER_RE = re.compile(r"【[^】]{0,30}(画像|写真|スクリーンショット)[^】]{0,30}】")
NOTE_CLOSING_DEFAULT = "今日も海外の掲示板では、真面目な話から肩の力が抜ける話まで、さまざまな投稿が注目を集めていました。"


def note_clean(text: str) -> str:
    """LLM free text -> note-safe plain paragraphs (blank line between).

    Mirrors hn_daily.sanitize_note: no markdown structure, no tables, no full-width spaces.
    """
    paras = []
    for line in split_paragraphs(str(text)):
        for sub in line.splitlines():
            # 太字の記号は先に外す（後の行頭 * の除去が ** の片方だけを消すため）
            sub = sub.replace("```", "").replace("**", "").replace("\u3000", " ").replace("|", "｜")
            sub = re.sub(r"^\s*#{1,6}\s+", "", sub)
            sub = re.sub(r"^\s*[*>|]\s*", "", sub)
            sub = re.sub(r"^\s*-\s+", "", sub)
            sub = re.sub(r"^\s*-{3,}\s*$", "", sub)
            sub = sub.strip()
            if IMAGE_PLACEHOLDER_RE.search(sub):
                continue
            if sub:
                paras.append(sub)
    return "\n\n".join(paras)


def one_line(text: str) -> str:
    return " ".join(note_clean(text).split())


def note_hashtags(extras) -> list[str]:
    """固定 7 個にその日の 3 個を足す（# を補い、記号と空白を外し、重複を除き、計 10 個まで）。"""
    tags = NOTE_FIXED_HASHTAGS.split()
    for t in extras or []:
        t = "#" + re.sub(r"[\s#\-・/、。,.!?！？「」()（）]+", "", str(t))
        if len(t) > 1 and t not in tags:
            tags.append(t)
        if len(tags) >= NOTE_HASHTAG_MAX:
            break
    return tags


def note_title(editorial: dict, pairs: list, date_str: str) -> str:
    d = dt.date.fromisoformat(date_str)
    catch = one_line(editorial.get("note_catch") or "").removeprefix("今日の目玉は").strip()
    if not catch and pairs:  # 生成に失敗したら 1 本目の見出しで補う
        catch = one_line(pairs[0][1].get("title_ja", ""))[:22]
    return f"{NOTE_TITLE_PREFIX} {d.strftime('%Y/%m/%d')}｜今日の目玉は{catch}"


def build_note(pairs: list, editorial: dict, date_str: str) -> str:
    d = dt.date.fromisoformat(date_str)
    date_ja = f"{d.year}年{d.month}月{d.day}日"
    n = len(pairs)

    lines = ["【note タイトル — 本文には含めません】"]
    lines += [f"案1: {note_title(editorial, pairs, date_str)}"]
    lines += ["", "＝＝＝ ハッシュタグ ＝＝＝", " ".join(note_hashtags(editorial.get("note_extra_hashtags")))]
    lines += ["", NOTE_PASTE_MARKER, ""]

    lines += [
        "世界最大級の掲示板「Reddit（レディット）」では、毎日さまざまな投稿が利用者の投票で順位づけされています。"
        f"この記事では、{date_ja}にRedditの総合ランキング（r/popular と r/all）で上位に入った投稿から{n}本を選び、"
        "日本語でご紹介します。"
    ]

    points = [one_line(p) for p in (editorial.get("note_points") or []) if one_line(p)][:3]
    if points:
        # 見出しにしない: note の目次は記事中の見出しをすべて拾う（Hacker News と同じ扱い）
        lines += ["", "**今日の3行まとめ**", ""]
        lines += [f"- {p}" for p in points]

    for i, (post, summ) in enumerate(pairs, 1):
        title = one_line(summ.get("title_ja") or post["title"])
        lines += ["", f"## {i}. {title}", ""]
        sub = post.get("subreddit") or ""
        if sub and not PROFANE_RE.search(sub):
            lines += [f"- **掲示板**: r/{sub}"]
        lines += [f"- [元の投稿を見る]({post['url']})", ""]
        body = note_clean(summ.get("summary") or "")
        if body:
            lines += [body]

    closing = note_clean(editorial.get("closing_comment") or "") or NOTE_CLOSING_DEFAULT
    lines += ["", "---", "", "## おわりに", "", closing, ""]
    lines += [
        "この記事は、海外の掲示板で毎日話題になっている投稿を、AIの力を借りて日本語で読み解いたものです。"
        "本文はAIが投稿の題名と本文から書いており、画像や動画の中身までは確かめていません。"
        "気になった投稿は、リンクから元の投稿もご覧ください。",
        "",
        f"**今日の{n}本で、あなたがいちばん気になったのはどれですか？よければコメントで教えてください。**",
    ]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def validate_note(text: str) -> list[str]:
    """note-output-spec.md violations in the paste body (empty = OK).

    Same rules as hn_daily.validate_note, which note_upload.py runs before publishing.
    """
    problems = []
    body = text.split(NOTE_PASTE_MARKER, 1)[-1]
    if re.search(r"\n{3,}", body):
        problems.append("consecutive blank lines")
    for n, line in enumerate(body.splitlines(), 1):
        if re.match(r"^#(?!#)|^#{4,}", line):
            problems.append(f"bad heading level at line {n}: {line[:40]!r}")
        if "|" in line:
            problems.append(f"table pipe at line {n}: {line[:40]!r}")
        if re.match(r"^\s*\*\s", line):
            problems.append(f"asterisk bullet at line {n}")
        if "![" in line or "[^" in line:
            problems.append(f"unsupported markup at line {n}: {line[:40]!r}")
        if "\u3000" in line:
            problems.append(f"full-width space at line {n}")
        if "藤川さん" in line:
            problems.append(f"personal address at line {n}")
        if IMAGE_PLACEHOLDER_RE.search(line):
            problems.append(f"image placeholder at line {n}: {line[:40]!r}")
    return problems


def fmt_opt(n) -> str:
    return f"{n:,}" if isinstance(n, int) else "—"


def split_paragraphs(text: str) -> list[str]:
    if "\n\n" in text:
        return text.split("\n\n")
    return text.split("\\n\\n")


def load_model() -> str:
    try:
        return json.loads((BASE / ".cache/gemini_meta.json").read_text(encoding="utf-8"))["model"]
    except Exception:
        return "gemini-3.8-flash"


def is_note_ok(summ: dict) -> bool:
    """Same rule as run_reddit_gemini.is_note_ok (kept here so this script stays standalone)."""
    return summ.get("note_ok") is True and summ.get("title_ja") != "(翻訳失敗)"


def main():
    raw = json.loads((BASE / ".cache/standard_data.json").read_text(encoding="utf-8"))
    posts = json.loads((BASE / ".cache/gemini_standard_posts.json").read_text(encoding="utf-8"))
    summaries = json.loads((BASE / ".cache/gemini_standard_step2.json").read_text(encoding="utf-8"))
    editorial = json.loads((BASE / ".cache/gemini_standard_step3.json").read_text(encoding="utf-8"))
    model = load_model()

    # 2026-10-12: note に載せてよい投稿だけを番号付きで並べ、外した投稿は理由だけを上に挙げる。
    # 番号は note 原稿と同じ（Step 3 の star_picks / cross_refs もこの並びの番号）。
    all_pairs = list(zip(posts, summaries))
    pairs = [(p, s) for p, s in all_pairs if is_note_ok(s)]
    excluded = [(p, s) for p, s in all_pairs if not is_note_ok(s)]
    date_str = raw["date"]
    generated_at = raw["generated_at"]
    hhmm = generated_at[11:13] + generated_at[14:16]

    ups_vals = [p["ups"] for p, _ in pairs if isinstance(p.get("ups"), int)]
    com_vals = [p["num_comments"] for p, _ in pairs if isinstance(p.get("num_comments"), int)]

    editors_note = editorial.get("editors_note", "")
    star_picks = set(editorial.get("star_picks", []))
    cross_refs = editorial.get("cross_refs", {})

    lines = []
    lines.append("---")
    lines.append("tags: [reddit, trend, daily-report]")
    lines.append(f"date: {date_str}")
    lines.append(f"generated_at: {generated_at}")
    lines.append(f"generated_by: Gemini ({model}) via run_reddit_gemini.py")
    if ups_vals:
        lines.append(f"total_upvotes: {sum(ups_vals)}")
    if com_vals:
        lines.append(f"total_comments: {sum(com_vals)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# 🔥 Reddit デイリートレンド Top {len(pairs)}")
    lines.append("")
    lines.append(f"**{date_str[:4]}年{date_str[5:7]}月{date_str[8:]}日**")
    lines.append("")
    lines.append("## 📝 エディターズノート")
    lines.append(f"> {editors_note}")
    lines.append("")
    if excluded:
        # 見出し（###）にしない: Obsidian の importance.py が ### を記事として拾うため
        lines.append(f"## 🚫 note に載せなかった投稿（{len(excluded)} 件）")
        lines.append("")
        for post, summ in excluded:
            reason = summ.get("note_ng_reason") or "理由なし"
            lines.append(f"- r/{post['subreddit']}: {reason}（[元の投稿]({post['url']})）")
        lines.append("")
    lines.append("---")

    for i, (post, summ) in enumerate(pairs):
        n = i + 1
        cat = summ.get("category", "Other")
        emoji = CATEGORY_EMOJI.get(cat, "📌")
        title_ja = summ.get("title_ja", post["title"])
        summary_text = summ.get("summary", "（要約なし）")
        star = "⭐ " if n in star_picks else ""

        lines.append("")
        lines.append(f"### {n}. {star}{emoji} {title_ja}")
        lines.append("")
        age_h = post.get("age_hours")
        lines.append(
            f"> **r/{post['subreddit']}** · ⬆️ {fmt_opt(post.get('ups'))} · "
            f"💬 {fmt_opt(post.get('num_comments'))} · "
            f"⏱️ {f'{age_h}h前' if age_h is not None else '—'}"
        )
        lines.append("")

        for para in split_paragraphs(summary_text):
            lines.append(para)
            lines.append("")

        lines.append(f"🔗 [元の投稿を見る]({post['url']})")

        ref_key = str(n)
        if ref_key in cross_refs:
            lines.append(f"📎 関連: {cross_refs[ref_key]}")

        lines.append("")
        lines.append("---")

    lines.append("")
    lines.append("*このレポートは Scheduled Task (run_reddit_gemini.py / Gemini) により自動生成されました*")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    # note 原稿と表紙を先に書く。通常レポートの存在が「全部できた」の印で、
    # 再試行は当日のレポートがあると standard を丸ごと飛ばすため（run_reddit_gemini.dedup_posts）。
    # 同じ日の 2 回目（_followup）は朝の note を上書きしないよう書かない。
    if not editorial.get("_followup"):
        note_path = REPORT_DIR / f"{date_str}-{NOTE_FILE_STEM}-note.md"
        write_atomic(note_path, build_note(pairs, editorial, date_str))
        print(f"Saved: {note_path}")
        cover_src = BASE / ".cache/gemini_standard_note_cover.png"
        if cover_src.exists():
            cover_path = note_path.with_name(note_path.name.replace("-note.md", "-cover.png"))
            tmp = cover_path.with_name(cover_path.name + ".tmp")
            shutil.copyfile(cover_src, tmp)
            tmp.replace(cover_path)
            print(f"Saved: {cover_path}")
        else:
            print("WARNING: note の表紙がありません（生成に失敗したか NOTE_COVER=0）")

    output_path = REPORT_DIR / f"{date_str}_{hhmm}_gemini.md"
    write_atomic(output_path, "\n".join(lines))
    print(f"Saved: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
