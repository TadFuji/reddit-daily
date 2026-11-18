"""Assemble the AI Reddit trend report, note.com manuscript, and X article (Gemini series)."""
import datetime as dt
import json
import os
import re
import shutil
import sys
import unicodedata
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
    "LLM": "🧠", "ImageGen": "🎨", "Research": "📄", "Robotics": "🤖",
    "Ethics": "⚖️", "Business": "💼", "Tools": "🔧", "Other": "📌",
}


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
        return "gemini-4-hydrogen"


# ---------------------------------------------------------------------------
# note.com 記事
# ---------------------------------------------------------------------------
# 形は Hacker News の note 原稿（../Hacker-News/hn_daily.py の build_note）にそろえる。
# 先頭の「案1:」・ハッシュタグ行・貼り付けの区切りは、note への自動投稿
# （../Hacker-News/note_upload.py の parse_article）が読む目印なので崩さない。
# 本文の記法は ../Hacker-News/note-output-spec.md に従う（## と ### だけ、表・斜体なし）。

NOTE_FILE_STEM = "Reddit-Top30"  # 受け渡し互換のため維持（run_reddit_gemini.NOTE_FILE_STEM と同じ）
NOTE_TITLE_PREFIX = "Reddit日本語解説"
NOTE_FIXED_HASHTAGS = "#毎日note #Reddit #AI #生成AI #海外ニュース #海外トレンド #海外の反応"
NOTE_HASHTAG_MAX = 10
NOTE_PASTE_MARKER = "＝＝＝ ここから下を note 本文に全文貼り付け ＝＝＝"
PROFANE_RE = re.compile(r"fuck|shit|cunt|bitch|dick|piss|porn|nsfw|sex", re.I)
IMAGE_PLACEHOLDER_RE = re.compile(r"【[^】]{0,30}(画像|写真|スクリーンショット)[^】]{0,30}】")
NOTE_CLOSING_DEFAULT = "今日も海外のAIコミュニティでは、最新のツールから研究動向まで、さまざまな話題が注目を集めていました。"


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
        "世界最大級の掲示板「Reddit（レディット）」のAI関連コミュニティでは、毎日最新のツールや研究、活用事例が活発に議論されています。"
        f"この記事では、{date_ja}の注目記事{n}本をお届けします。"
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


# ---------------------------------------------------------------------------
# X 記事
# ---------------------------------------------------------------------------

PROMOTION_LIMIT = 256  # 半角換算。Hacker-News/x_article.py と同じ
X_CLOSING = (
    "ここまで読んでくださり、ありがとうございます。"
    "参考になった投稿があれば、いいねで教えていただけると嬉しいです。"
    "海外の AI の話題は毎朝まとめていますので、よろしければフォローして、また明日もご覧ください。"
)


def halfwidth_count(text: str) -> int:
    """ASCII/narrow/halfwidth=1, other characters (incl. emoji)=2."""
    return sum(1 if ord(c) < 128 or unicodedata.east_asian_width(c) in ("Na", "H") else 2 for c in text)


def _sentence_within_limit(text: str) -> str:
    used, chars = 0, []
    for char in text:
        used += halfwidth_count(char)
        if used > PROMOTION_LIMIT:
            break
        chars.append(char)
    prefix = "".join(chars)
    ends = [prefix.rfind(c) for c in "。！？!?"]
    return prefix[:max(ends) + 1] if max(ends) >= 0 else ""


def prepare_promotion(candidate: str, fallback: str) -> str:
    """One line within 256 halfwidth units, cut at a sentence end. Empty if nothing fits."""
    for value in (candidate, fallback):
        text = " ".join(str(value or "").split())
        if not text:
            continue
        if halfwidth_count(text) <= PROMOTION_LIMIT:
            return text
        short = _sentence_within_limit(text)
        if short:
            return short
    return ""


def x_article_title(date_str: str, headline: str, count: int) -> str:
    d = dt.date.fromisoformat(date_str)
    date_label = d.strftime("%Y/%m/%d")
    headline = " ".join(str(headline or "").split())
    if not headline:
        headline = f"注目記事{count}本"
    return f"Reddit AI 注目記事 {date_label}｜{headline}"


def build_x_article(date_str, pairs, editorial, cover_name: str | None) -> str:
    editors_note = editorial.get("editors_note", "")
    star_picks = set(editorial.get("star_picks", []))
    cross_refs = editorial.get("cross_refs", {})
    promotion = prepare_promotion(editorial.get("x_promotion", ""), editors_note)

    out = ["---", "type: x-article", "status: ready",
           f"created: {json.dumps(date_str)}", f"updated: {json.dumps(date_str)}",
           "tags: [reddit, x-article, ai]"]
    if cover_name:
        out.append(f"cover: {json.dumps(cover_name, ensure_ascii=False)}")
    out += ["---", "", f"# {x_article_title(date_str, editorial.get('x_headline', ''), len(pairs))}", ""]
    out += ["## 宣伝文", "", promotion, "", "## 本文", ""]
    out.append(
        "海外の掲示板「Reddit（レディット）」の AI 関連コミュニティで、最近"
        f"話題になった投稿から {len(pairs)} 本を選び、日本語で紹介します。"
    )
    out.append("")
    if editors_note:
        out += [editors_note, ""]

    for i, (post, summ) in enumerate(pairs):
        n = i + 1
        out.append(f"## {n}. {summ.get('title_ja', post['title'])}")
        out.append("")
        out.append(f"r/{post['subreddit']}")
        out.append("")
        for para in split_paragraphs(summ.get("summary", "（要約なし）")):
            out += [para.strip(), ""]
        out += [f"🔗 [元の投稿を見る]({post['url']})", ""]
        if str(n) in cross_refs:
            out += [f"📎 関連: {cross_refs[str(n)]}", ""]
    out += ["---", "", X_CLOSING]
    return "\n".join(out).rstrip() + "\n"


def main():
    raw = json.loads((BASE / ".cache/ai_data.json").read_text(encoding="utf-8"))
    posts = json.loads((BASE / ".cache/gemini_ai_posts.json").read_text(encoding="utf-8"))
    summaries = json.loads((BASE / ".cache/gemini_ai_step2.json").read_text(encoding="utf-8"))
    editorial = json.loads((BASE / ".cache/gemini_ai_step3.json").read_text(encoding="utf-8"))
    model = load_model()

    pairs = list(zip(posts, summaries))
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
    lines.append("tags: [reddit, trend, daily-report, ai]")
    lines.append(f"date: {date_str}")
    lines.append(f"generated_at: {generated_at}")
    lines.append(f"generated_by: Gemini ({model}) via run_reddit_gemini.py")
    if ups_vals:
        lines.append(f"total_upvotes: {sum(ups_vals)}")
    if com_vals:
        lines.append(f"total_comments: {sum(com_vals)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# Reddit AI 注目記事 {len(pairs)}本")
    lines.append("")
    lines.append(f"**{date_str[:4]}年{date_str[5:7]}月{date_str[8:]}日**")
    lines.append("")
    lines.append("## 📝 エディターズノート")
    lines.append(f"> {editors_note}")
    lines.append("")
    lines.append("---")

    for i, (post, summ) in enumerate(pairs):
        n = i + 1
        title_ja = summ.get("title_ja", post["title"])
        summary_text = summ.get("summary", "（要約なし）")

        lines.append("")
        lines.append(f"### {n}. {title_ja}")
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
    # note 原稿と表紙を先に書く。AI レポートの存在が「全部できた」の印で、
    # 再試行は当日のレポートがあると ai を丸ごと飛ばすため（run_reddit_gemini.run_variant）。
    # 同じ日の 2 回目（_followup）は朝の note を上書きしないよう書かない。
    if not editorial.get("_followup"):
        note_path = REPORT_DIR / f"{date_str}-{NOTE_FILE_STEM}-note.md"
        write_atomic(note_path, build_note(pairs, editorial, date_str))
        print(f"Saved: {note_path}")
        note_cover_src = BASE / ".cache/gemini_ai_note_cover.png"
        if note_cover_src.exists():
            note_cover_path = note_path.with_name(note_path.name.replace("-note.md", "-cover.png"))
            tmp = note_cover_path.with_name(note_cover_path.name + ".tmp")
            shutil.copyfile(note_cover_src, tmp)
            tmp.replace(note_cover_path)
            print(f"Saved: {note_cover_path}")
        else:
            print("WARNING: note の表紙がありません（生成に失敗したか NOTE_COVER=0）")

    output_path = REPORT_DIR / f"AI_{date_str}_{hhmm}_gemini.md"
    write_atomic(output_path, "\n".join(lines))
    print(f"Saved: {output_path}")

    # X 記事。カバー画像は run_reddit_gemini.py が .cache に作っておく（失敗時は無い）
    x_stem = f"📍 X_AI_{date_str}_{hhmm}"
    cover_name = None
    cover_src = BASE / ".cache/gemini_ai_cover.png"
    if cover_src.exists():
        cover_name = f"{x_stem}_cover.png"
        tmp = REPORT_DIR / (cover_name + ".tmp")
        shutil.copyfile(cover_src, tmp)
        tmp.replace(REPORT_DIR / cover_name)
        print(f"Saved: {REPORT_DIR / cover_name}")
    x_path = REPORT_DIR / f"{x_stem}.md"
    write_atomic(x_path, build_x_article(date_str, pairs, editorial, cover_name))
    print(f"Saved: {x_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
