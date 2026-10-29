"""Offline checks for the Reddit daily-trend note article (no network, no API key).

    python3 test_note_article.py

Covers: which posts go into the note (Step 2 stopping rule and exclusions), the note
manuscript's shape (title, hashtags, paste marker, note-output-spec rules), the report
written next to it, the follow-up run that must not overwrite the morning note, and the
cover crop size (skipped when Pillow is missing).
"""
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import assemble_report_gemini as A  # noqa: E402
import run_reddit_gemini as R  # noqa: E402


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        check.failed = True


check.failed = False


def post(i, sub="pics", selftext=""):
    return {"id": f"p{i}", "title": f"title {i}", "subreddit": sub, "author": "a",
            "url": f"https://www.reddit.com/r/{sub}/comments/p{i}/t/", "selftext": selftext,
            "age_hours": 3.0, "ups": None, "num_comments": None, "comments": [], "buzz_score": 40 - i}


def summary(i, ok=True, reason=""):
    return {"category": "Humor", "title_ja": f"見出し{i}", "summary": f"第一段落{i}。\n\n第二段落{i}。",
            "note_ok": ok, "note_ng_reason": reason}


# ---------------------------------------------------------------------------
# 1. Step 2 stops once note_target posts are publishable, skips adult subreddits
#    without calling Gemini, and treats a failed summary as not publishable.
# ---------------------------------------------------------------------------
calls = []


def fake_gemini(prompt, label, cache_stem=None):
    calls.append(cache_stem)
    n = int(cache_stem.rsplit("_", 1)[1])
    if n == 2:
        return None  # e.g. the safety filter returned nothing
    if n == 4:
        return summary(n, ok=False, reason="性的な内容")
    if n == 5:
        return {**summary(n), "note_ok": "true"}  # a string is not an explicit yes
    return summary(n)


tmp_cache = pathlib.Path(tempfile.mkdtemp())
orig = (R.gemini_json, R.CACHE, R.note_target, R.writing_rules)
R.gemini_json, R.CACHE = fake_gemini, tmp_cache
R.note_target = lambda v: 3 if v == "standard" else None
R.writing_rules = lambda: ""
posts = [post(1), post(2), post(3, sub="NSFW_stuff"), post(4), post(5), post(6), post(7), post(8)]
used, summ = R.step2_summaries("standard", posts)
ok_ids = [p["id"] for p, s in zip(used, summ) if R.is_note_ok(s)]
check("step2: stops after 3 publishable posts", ok_ids == ["p1", "p6", "p7"])
check("step2: returned posts and summaries line up", len(used) == len(summ) == 7)
check("step2: adult subreddit skipped without a Gemini call", "gemini_standard_3" not in calls
      and summ[2].get("skipped") and summ[2]["note_ok"] is False)
check("step2: failed summary is not publishable", summ[1]["note_ok"] is False
      and summ[1]["title_ja"] == "(翻訳失敗)")
check("step2: string 'true' is not an explicit yes", not R.is_note_ok(summ[4]))
check("step2: post 8 never summarised", "gemini_standard_8" not in calls)

calls.clear()
used_ai, summ_ai = R.step2_summaries("ai", [post(1), post(2), post(3, sub="NSFW_stuff")])
check("step2: AI report keeps every post (no note filtering)", len(summ_ai) == 3 and len(calls) == 3)
R.gemini_json, R.CACHE, R.note_target, R.writing_rules = orig

check("hashtags: fixed tags agree between generator and assembler",
      R.NOTE_FIXED_HASHTAGS == A.NOTE_FIXED_HASHTAGS)
check("file stem agrees between generator and assembler", R.NOTE_FILE_STEM == A.NOTE_FILE_STEM)
check("standard prompt still formats", "note_ok" in R.STANDARD_PROMPT.format(
    comment_rule="", post_data="x", writing_norms="", writing_rules=""))


# ---------------------------------------------------------------------------
# 2. Assembler: note first, then the report; excluded posts stay out of the note.
# ---------------------------------------------------------------------------
POSTS = [post(1), post(2, sub="nextfuckinglevel"), post(3), post(4)]
SUMMARIES = [summary(1), summary(2), summary(3, ok=False, reason="個人を晒す内容"), summary(4)]
EDITORIAL = {
    "editors_note": "今日のRedditは猫の話題が多めでした。",
    "star_picks": [1], "cross_refs": {"2": "→ #1 も参照"},
    "note_catch": "今日の目玉は高齢の猫に毛が生えた話",
    "note_points": ["猫の話題。", "**太字**の混入。", "三つ目。"],
    "note_extra_hashtags": ["#猫", "スフィンクス", "#毎日note"],
    "closing_comment": "明日もお楽しみに。",
    "_followup": False,
}


def run_assembler(editorial, with_cover=True):
    work = pathlib.Path(tempfile.mkdtemp())
    out = work / "out"
    shutil.copy(BASE / "assemble_report_gemini.py", work)
    (work / ".cache").mkdir()
    (work / ".cache/standard_data.json").write_text(json.dumps(
        {"date": "2026-10-12", "generated_at": "2026-10-12T06:01:00+09:00", "posts": POSTS}))
    (work / ".cache/gemini_standard_posts.json").write_text(json.dumps(POSTS))
    (work / ".cache/gemini_standard_step2.json").write_text(json.dumps(SUMMARIES, ensure_ascii=False))
    (work / ".cache/gemini_standard_step3.json").write_text(json.dumps(editorial, ensure_ascii=False))
    if with_cover:
        (work / ".cache/gemini_standard_note_cover.png").write_bytes(b"\x89PNG fake")
    env = dict(os.environ, REDDIT_OUT_DIR=str(out))
    subprocess.run([sys.executable, str(work / "assemble_report_gemini.py")], env=env,
                   check=True, capture_output=True)
    return out


out = run_assembler(EDITORIAL)
note = out / "2026-10-12-Reddit-Top30-note.md"
cover = out / "2026-10-12-Reddit-Top30-cover.png"
report = out / "2026-10-12_0601_gemini.md"
check("assembler: note, cover and report written", note.exists() and cover.exists() and report.exists())
check("assembler: note written before the report (report is the completion mark)",
      note.stat().st_mtime_ns <= report.stat().st_mtime_ns)
text = note.read_text(encoding="utf-8")
check("note: title in the fixed format, catch not doubled",
      "案1: Reddit日本語解説 2026/10/12｜今日の目玉は高齢の猫に毛が生えた話" in text)
tag_line = next(l for l in text.splitlines() if l.startswith("#"))
tags = tag_line.split()
check("note: 7 fixed + 2 new hashtags (third extra was a duplicate), cleaned",
      len(tags) == 9 and "#猫" in tags and "#スフィンクス" in tags and tags.count("#毎日note") == 1)
check("note: paste marker present", A.NOTE_PASTE_MARKER in text)
check("note: passes note-output-spec checks", A.validate_note(text) == [])
body = text.split(A.NOTE_PASTE_MARKER, 1)[1]
check("note: one ## heading per publishable post plus おわりに",
      re.findall(r"^## (.+)$", body, re.M) == ["1. 見出し1", "2. 見出し2", "3. 見出し4", "おわりに"])
check("note: excluded post is absent", "見出し3" not in text and "p3/" not in text)
check("note: profane subreddit name hidden but link kept",
      "nextfuckinglevel" not in body.replace(POSTS[1]["url"], "") and POSTS[1]["url"] in body)
check("note: clean subreddit name shown", "- **掲示板**: r/pics" in body)
check("note: markdown stripped from 3-line summary", "- **太字**の混入。" not in body and "- 太字の混入。" in body)
check("note: summary paragraphs kept apart", "第一段落1。\n\n第二段落1。" in body)
check("note_clean: image placeholder line dropped",
      A.note_clean("前の段落。\n\n【ここに画像: 猫の写真】\n\n後の段落。") == "前の段落。\n\n後の段落。")
check("validate_note: image placeholder reported",
      A.validate_note(A.NOTE_PASTE_MARKER + "\n【画像: 猫】\n") != [])

# Same parser as the note.com uploader (../Hacker-News/note_upload.parse_article)
titles = [m.group(1).strip() for m in re.finditer(r"^案[123][:：]\s*(.+)$", text, re.M)]
check("note: uploader finds exactly one title", len(titles) == 1)

rep = report.read_text(encoding="utf-8")
check("report: heading counts the publishable posts", "# 🔥 Reddit デイリートレンド Top 3" in rep)
check("report: excluded post listed with reason, not as an article",
      "個人を晒す内容" in rep and len(re.findall(r"^### ", rep, re.M)) == 3)
check("report: excluded list is not a ### section (importance.py reads ###)",
      not re.search(r"^### .*note に載せなかった", rep, re.M))
check("report: excluded post's link kept so the day's dedup sees it", "comments/p3/" in rep)

out2 = run_assembler({**EDITORIAL, "_followup": True})
check("follow-up run: report written but no note or cover",
      not list(out2.glob("*-note.md")) and not list(out2.glob("*-cover.png"))
      and list(out2.glob("*_gemini.md")))

out3 = run_assembler({"editors_note": "x"}, with_cover=False)
text3 = (out3 / "2026-10-12-Reddit-Top30-note.md").read_text(encoding="utf-8")
check("note: editorial failure falls back (title from first headline, fixed tags only)",
      "今日の目玉は見出し1" in text3 and len(next(l for l in text3.splitlines() if l.startswith("#")).split()) == 7
      and A.validate_note(text3) == [])
check("note: no cover when generation failed", not list(out3.glob("*-cover.png")))


# ---------------------------------------------------------------------------
# 3. Cover crop size (needs Pillow; skipped otherwise)
# ---------------------------------------------------------------------------
try:
    from PIL import Image

    import note_cover

    buf = io.BytesIO()
    Image.new("RGB", (2752, 1536), "red").save(buf, "PNG")  # Gemini 16:9 2K
    png = note_cover.crop_to_cover(buf.getvalue())
    check("cover: cropped to 1280x670", Image.open(io.BytesIO(png)).size == (1280, 670))
    # 上端だけを削る（下の段のラベルを切らない）: 下端の 1 行が残ることを確かめる
    im = Image.new("RGB", (2752, 1536), "white")
    im.paste((0, 0, 255), (0, 1526, 2752, 1536))  # a 10-px band along the bottom edge
    buf = io.BytesIO(); im.save(buf, "PNG")
    out = Image.open(io.BytesIO(note_cover.crop_to_cover(buf.getvalue())))
    check("cover: bottom edge kept, top trimmed", out.getpixel((640, 669)) == (0, 0, 255))
    # The style is the retro silkscreen poster (decided 2026-10-29; it was pixel art before).
    check("cover: prompt asks for a retro poster", "シルクスクリーンのポスター" in note_cover.build_prompt("x"))
except ImportError:
    print("SKIP cover crop (Pillow not installed)")

if check.failed:
    sys.exit(1)
print("all passed")
