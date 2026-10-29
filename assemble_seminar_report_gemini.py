"""Assemble the AI Seminar report (Gemini series).

Same fixes as assemble_report_gemini.py: gemini cache files, None-safe
metrics, robust paragraph split, _gemini.md filename, dynamic generated_by.
"""
import json
import os
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
    "NewTool": "🔧", "BusinessCase": "💼", "ModelUpdate": "🧠",
    "Tutorial": "📖", "Research": "📄", "Industry": "🏢", "Other": "📌",
}
DIFFICULTY_EMOJI = {"入門": "🟢", "中級": "🟡", "上級": "🔴"}
DEMO_EMOJI = {
    "すぐにデモ可能": "✅",
    "準備すればデモ可能": "⚙️",
    "スライドで紹介": "📊",
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


def main():
    raw = json.loads((BASE / ".cache/seminar_data.json").read_text(encoding="utf-8"))
    summaries = json.loads((BASE / ".cache/gemini_seminar_step2.json").read_text(encoding="utf-8"))
    editorial = json.loads((BASE / ".cache/gemini_seminar_step3.json").read_text(encoding="utf-8"))
    posts_map = json.loads((BASE / ".cache/gemini_seminar_posts_map.json").read_text(encoding="utf-8"))
    model = load_model()

    date_str = raw["date"]
    generated_at = raw["generated_at"]
    hhmm = generated_at[11:13] + generated_at[14:16]

    editors_note = editorial.get("editors_note", "")
    difficulty_tags = editorial.get("difficulty_tags", {})
    demo_feasibility = editorial.get("demo_feasibility", {})
    weekly_theme = editorial.get("weekly_theme", "")

    lines = []
    lines.append("---")
    lines.append("tags: [reddit, ai, seminar, daily-digest]")
    lines.append(f"date: {date_str}")
    lines.append(f"generated_at: {generated_at}")
    lines.append(f"generated_by: Gemini ({model}) via run_reddit_gemini.py")
    lines.append("---")
    lines.append("")
    lines.append("# 📚 AI セミナーネタ帳 Top 10")
    lines.append("")
    lines.append(f"**{date_str[:4]}年{date_str[5:7]}月{date_str[8:]}日**")
    lines.append("")
    lines.append("## 📝 今日のポイント")
    lines.append(f"> {editors_note}")
    lines.append("")

    if weekly_theme:
        lines.append(f"## 🎯 今週のテーマ: {weekly_theme}")
        lines.append("")

    lines.append("---")

    for i, summ in enumerate(summaries):
        n = i + 1
        post = posts_map.get(str(n), {})

        cat = summ.get("category", "Other")
        emoji = CATEGORY_EMOJI.get(cat, "📌")
        title_ja = summ.get("title_ja", "(タイトルなし)")
        summary_text = summ.get("summary", "（要約なし）")

        diff_tag = difficulty_tags.get(str(n), "中級")
        diff_emoji = DIFFICULTY_EMOJI.get(diff_tag, "🟡")
        demo_tag = demo_feasibility.get(str(n), "スライドで紹介")
        demo_emoji_str = DEMO_EMOJI.get(demo_tag, "📊")

        lines.append("")
        lines.append(f"### {n}. {emoji} {title_ja}")
        lines.append(f"**{diff_emoji} {diff_tag}** | **{demo_emoji_str} {demo_tag}**")
        lines.append("")
        lines.append(
            f"> **r/{post.get('subreddit', '?')}** · ⬆️ {fmt_opt(post.get('ups'))} · "
            f"💬 {fmt_opt(post.get('num_comments'))}"
        )
        lines.append("")

        paragraphs = split_paragraphs(summary_text)
        for j, para in enumerate(paragraphs):
            if j == len(paragraphs) - 1:
                # Last paragraph: セミナー活用ポイント
                lines.append(f"**💡 セミナー活用:** {para}")
            else:
                lines.append(para)
            lines.append("")

        url = post.get("url", "")
        if url:
            lines.append(f"🔗 [元の投稿を見る]({url})")
        lines.append("")
        lines.append("---")

    lines.append("")
    lines.append("*このレポートは Scheduled Task (run_reddit_gemini.py / Gemini) により自動生成されました*")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = REPORT_DIR / f"Seminar_{date_str}_{hhmm}_gemini.md"
    write_atomic(output_path, "\n".join(lines))
    print(f"Saved: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
