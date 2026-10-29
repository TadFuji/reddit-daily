"""Assemble the AI Reddit trend report (Gemini series).

Same fixes as assemble_report_gemini.py: gemini cache files, None-safe
metrics, robust paragraph split, _gemini.md filename, dynamic generated_by.
"""
import json
import os
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
        return "gemini-3.8-flash"


# --- X 記事（2026-10-11 変更） -------------------------------------------------
# 以前は X の通常投稿向けに、本文（📍 X_AI_*.md）と URL だけのコメント欄用
# （📍 X_AI_*_コメント欄.md）の 2 本を書いていた。リンク入りの投稿は表示が
# 抑えられるため URL を返信へ逃がしていた。X の「記事」として出すことにしたので、
# Hacker News の for-X（Hacker-News/x_article.py）と同じ形の 1 本にまとめた:
# frontmatter → # タイトル → ## 宣伝文 → ## 本文。リンクは各投稿の中に置く。

PROMOTION_LIMIT = 256  # 半角換算。Hacker-News/x_article.py と同じ
# X 記事の末尾にだけ置く結び（2026-10-21 藤川さんの依頼）。いいねとフォローを売り込まずに頼む
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
    date_label = date_str.replace("-", "/")
    headline = " ".join(str(headline or "").split())
    if headline:
        return f"Reddit AIトレンド {date_label}｜{headline}"
    return f"Reddit AIトレンド Top {count}｜{date_label}"


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
        "海外の掲示板「Reddit（レディット）」の AI 関連コミュニティで、直近 24 時間に"
        f"話題になった投稿から {len(pairs)} 本を選び、日本語で紹介します。"
    )
    out.append("")
    if editors_note:
        out += [editors_note, ""]

    for i, (post, summ) in enumerate(pairs):
        n = i + 1
        emoji = CATEGORY_EMOJI.get(summ.get("category", "Other"), "📌")
        star = "⭐ " if n in star_picks else ""
        out.append(f"## {n}. {star}{emoji} {summ.get('title_ja', post['title'])}")
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
    lines.append(f"# 🤖 Reddit AI トレンド Top {len(pairs)}")
    lines.append("")
    lines.append(f"**{date_str[:4]}年{date_str[5:7]}月{date_str[8:]}日**")
    lines.append("")
    lines.append("## 📝 エディターズノート")
    lines.append(f"> {editors_note}")
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
