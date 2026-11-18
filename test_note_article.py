"""Offline checks for the Reddit AI note article (no network, no API key).

    python3 test_note_article.py
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
import assemble_ai_report_gemini as A  # noqa: E402
import run_reddit_gemini as R  # noqa: E402


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        check.failed = True


check.failed = False


def post(i, sub="artificial", selftext=""):
    return {"id": f"p{i}", "title": f"title {i}", "subreddit": sub, "author": "a",
            "url": f"https://www.reddit.com/r/{sub}/comments/p{i}/t/", "selftext": selftext,
            "age_hours": 3.0, "ups": None, "num_comments": None, "comments": [], "buzz_score": 40 - i}


def summary(i):
    return {"category": "LLM", "title_ja": f"見出し{i}", "summary": f"第一段落{i}。\n\n第二段落{i}。"}


# ---------------------------------------------------------------------------
# 1. Step 2 summarizes all AI posts and step3_editorial includes X + note fields
# ---------------------------------------------------------------------------
calls = []
prompts = []


def fake_gemini(prompt, label, cache_stem=None):
    calls.append(cache_stem)
    prompts.append(prompt)
    if cache_stem and cache_stem.endswith("_step3"):
        return {
            "editors_note": "今日のRedditはAI話題が中心でした。",
            "star_picks": [1],
            "cross_refs": {},
            "x_headline": "目玉の話題",
            "x_promotion": "宣伝文です。",
            "note_catch": "Claudeの新機能",
            "note_points": ["一点目。", "二点目。", "三点目。"],
            "note_extra_hashtags": ["Claude", "OpenAI", "AI"],
            "closing_comment": "また明日。",
        }
    n = int(cache_stem.rsplit("_", 1)[1])
    if n == 2:
        return None
    return summary(n)


tmp_cache = pathlib.Path(tempfile.mkdtemp())
orig = (R.gemini_json, R.CACHE, R.writing_norms)
R.gemini_json, R.CACHE = fake_gemini, tmp_cache
R.writing_norms = lambda: ""
posts = [post(1), post(2), post(3)]
used, summ = R.step2_summaries("ai", posts)
check("step2: AI report summarizes every post", len(used) == len(summ) == 3 and len(calls) == 3)
check("step2: failed summary becomes FAIL_ENTRY", summ[1] == R.FAIL_ENTRY)

prompts.clear()
R.step3_editorial("ai", summ, followup=False)
step3_saved = json.loads((tmp_cache / "gemini_ai_step3.json").read_text(encoding="utf-8"))
check(
    "step3_editorial: requests common + X + note fields in one call and stores _followup",
    len(prompts) == 1
    and all(k in prompts[0] for k in ("editors_note", "x_headline", "x_promotion", "note_catch", "note_points", "note_extra_hashtags", "closing_comment"))
    and step3_saved.get("_followup") is False
    and step3_saved.get("note_catch") == "Claudeの新機能",
)
R.gemini_json, R.CACHE, R.writing_norms = orig

# 1b. run_variant("ai") calls both x_cover and note_cover (unless followup), and main() accepts ai/all + push
with tempfile.TemporaryDirectory() as rv_tmp:
    rv_dir = pathlib.Path(rv_tmp)
    rv_cache = rv_dir / ".cache"
    rv_out = rv_dir / "out"
    rv_cache.mkdir()
    rv_out.mkdir()
    (rv_cache / "ai_data.json").write_text(json.dumps({
        "date": "2026-10-12",
        "generated_at": "2026-10-12T06:00:00+09:00",
        "sources_total": 14,
        "sources_ok": 14,
        "sources_error": 0,
        "total_fetched": 3,
        "posts": [post(1), post(2), post(3)],
    }), encoding="utf-8")
    cover_calls = []
    orig_rv = (R.CACHE, R.REPORT_DIR, R.step2_summaries, R.step3_editorial,
               R.step3_x_cover, R.step3_note_cover, R.step4_assemble)
    R.CACHE = rv_cache
    R.REPORT_DIR = rv_out
    R.step2_summaries = lambda v, ps: (ps, [summary(1), summary(2), summary(3)])
    R.step3_editorial = lambda v, ss, fu: cover_calls.append(("editorial", fu))
    R.step3_x_cover = lambda ss: cover_calls.append(("x_cover",))
    R.step3_note_cover = lambda ss: cover_calls.append(("note_cover",))
    R.step4_assemble = lambda v: cover_calls.append(("assemble", v))
    st = {}
    ok_rv = R.run_variant("ai", skip_fetch=True, stats=st)
    check(
        "run_variant('ai') runs editorial, x_cover, note_cover, assemble when not followup",
        ok_rv and cover_calls == [("editorial", False), ("x_cover",), ("note_cover",), ("assemble", "ai")],
    )
    # Simulate follow-up run (earlier report already exists with p1)
    (rv_out / f"AI_{R.dt.datetime.now(R.JST).strftime('%Y-%m-%d')}_0600_gemini.md").write_text(
        "https://www.reddit.com/r/artificial/comments/p1/t/", encoding="utf-8"
    )
    (rv_cache / "ai_data.json").write_text(json.dumps({
        "date": "2026-10-12",
        "generated_at": "2026-10-12T12:00:00+09:00",
        "sources_total": 14,
        "sources_ok": 14,
        "sources_error": 0,
        "total_fetched": 4,
        "posts": [post(1), post(2), post(3), post(4)],
    }), encoding="utf-8")
    cover_calls.clear()
    ok_fu = R.run_variant("ai", skip_fetch=True, stats={})
    check(
        "run_variant('ai') skips note_cover on followup run",
        ok_fu and cover_calls == [("editorial", True), ("x_cover",), ("assemble", "ai")],
    )
    R.CACHE, R.REPORT_DIR, R.step2_summaries, R.step3_editorial, R.step3_x_cover, R.step3_note_cover, R.step4_assemble = orig_rv

    # main() accepts both 'ai' and 'all', pushes note after ai, and checks REDDIT_REQUIRE_PUSH
    for target in ("ai", "all"):
        main_calls = []
        orig_main = (R.CACHE, R.resolve_model, R.run_variant, R.push_note_to_github,
                     R.run_healthcheck, R.meter, sys.argv, os.environ.get("REDDIT_REQUIRE_PUSH"))
        R.CACHE = rv_cache
        R.resolve_model = lambda: "gemini-4-hydrogen"
        R.run_variant = lambda v, sf, s: (main_calls.append(("run", v)), True)[1]
        R.push_note_to_github = lambda s: (main_calls.append(("push",)), s.setdefault("ai", {}).__setitem__("note_pushed", False))
        R.run_healthcheck = lambda *a, **k: main_calls.append(("hc",))
        os.environ["REDDIT_REQUIRE_PUSH"] = "1"
        sys.argv = ["run_reddit_gemini.py", target]
        rc = R.main()
        R.CACHE, R.resolve_model, R.run_variant, R.push_note_to_github, R.run_healthcheck, R.meter, sys.argv, prev_req = orig_main
        if prev_req is None:
            os.environ.pop("REDDIT_REQUIRE_PUSH", None)
        else:
            os.environ["REDDIT_REQUIRE_PUSH"] = prev_req
        check(
            f"main('{target}') runs ai + push and fails when REDDIT_REQUIRE_PUSH=1 and note_pushed=False",
            rc == 1 and main_calls == [("run", "ai"), ("push",), ("hc",)],
        )

check("hashtags: fixed tags agree between generator and assembler",
      R.NOTE_FIXED_HASHTAGS == A.NOTE_FIXED_HASHTAGS
      and A.NOTE_FIXED_HASHTAGS == "#毎日note #Reddit #AI #生成AI #海外ニュース #海外トレンド #海外の反応")
check("file stem agrees between generator and assembler", R.NOTE_FILE_STEM == A.NOTE_FILE_STEM == "Reddit-Top30")
check("AI prompt formats", "title_ja" in R.AI_PROMPT.format(
    comment_rule="", post_data="x", writing_norms=""))


# ---------------------------------------------------------------------------
# 2. Assembler: note + cover, AI report, and X article
# ---------------------------------------------------------------------------
POSTS = [post(1), post(2, sub="nextfuckinglevel"), post(3, sub="LocalLLaMA")]
SUMMARIES = [summary(1), summary(2), summary(3)]
EDITORIAL = {
    "editors_note": "今日のRedditはAIエージェントの話題が多めでした。",
    "star_picks": [1], "cross_refs": {"2": "→ #1 も参照"},
    "x_headline": "目玉の話題",
    "x_promotion": "今日のAI話題をまとめました。",
    "note_catch": "Claudeの新機能が話題",
    "note_points": ["エージェントの話題。", "**太字**の混入。", "三つ目。"],
    "note_extra_hashtags": ["#Claude", "LocalLLM", "#毎日note"],
    "closing_comment": "明日もお楽しみに。",
    "_followup": False,
}


def run_assembler(editorial, with_note_cover=True, with_x_cover=True):
    work = pathlib.Path(tempfile.mkdtemp())
    out = work / "out"
    shutil.copy(BASE / "assemble_ai_report_gemini.py", work)
    (work / ".cache").mkdir()
    (work / ".cache/ai_data.json").write_text(json.dumps(
        {"date": "2026-10-12", "generated_at": "2026-10-12T06:01:00+09:00", "posts": POSTS}))
    (work / ".cache/gemini_ai_posts.json").write_text(json.dumps(POSTS))
    (work / ".cache/gemini_ai_step2.json").write_text(json.dumps(SUMMARIES, ensure_ascii=False))
    (work / ".cache/gemini_ai_step3.json").write_text(json.dumps(editorial, ensure_ascii=False))
    if with_note_cover:
        (work / ".cache/gemini_ai_note_cover.png").write_bytes(b"\x89PNG fake note")
    if with_x_cover:
        (work / ".cache/gemini_ai_cover.png").write_bytes(b"\x89PNG fake x")
    env = dict(os.environ, REDDIT_OUT_DIR=str(out))
    subprocess.run([sys.executable, str(work / "assemble_ai_report_gemini.py")], env=env,
                   check=True, capture_output=True)
    return out


out = run_assembler(EDITORIAL)
note = out / "2026-10-12-Reddit-Top30-note.md"
cover = out / "2026-10-12-Reddit-Top30-cover.png"
report = out / "AI_2026-10-12_0601_gemini.md"
x_art = out / "📍 X_AI_2026-10-12_0601.md"
check("assembler: note, note cover, AI report, and X article written",
      note.exists() and cover.exists() and report.exists() and x_art.exists())
check("assembler: note written before the AI report",
      note.stat().st_mtime_ns <= report.stat().st_mtime_ns)
text = note.read_text(encoding="utf-8")
check("note: title in the fixed format without '今日の目玉は'",
      "案1: Reddit日本語解説 2026/10/12｜Claudeの新機能が話題" in text)
tag_line = next(l for l in text.splitlines() if l.startswith("#"))
tags = tag_line.split()
check("note: 7 fixed + 2 new hashtags (third extra was a duplicate), cleaned",
      len(tags) == 9 and "#Claude" in tags and "#LocalLLM" in tags and tags.count("#毎日note") == 1)
check("note: paste marker present", A.NOTE_PASTE_MARKER in text)
check("note: passes note-output-spec checks", A.validate_note(text) == [])
body = text.split(A.NOTE_PASTE_MARKER, 1)[1]
check("note: AI-focused intro text present",
      "世界最大級の掲示板「Reddit（レディット）」のAI関連コミュニティでは、毎日最新のツールや研究、活用事例が活発に議論されています。"
      "この記事では、2026年10月12日の注目記事3本をお届けします。" in body)
check("note: one ## heading per post plus おわりに",
      re.findall(r"^## (.+)$", body, re.M) == ["1. 見出し1", "2. 見出し2", "3. 見出し3", "おわりに"])
check("note: profane subreddit name hidden but link kept",
      "nextfuckinglevel" not in body.replace(POSTS[1]["url"], "") and POSTS[1]["url"] in body)
check("note: clean subreddit name shown", "- **掲示板**: r/artificial" in body)
check("note: markdown stripped from 3-line summary", "- **太字**の混入。" not in body and "- 太字の混入。" in body)
check("note: summary paragraphs kept apart", "第一段落1。\n\n第二段落1。" in body)
check("note_clean: image placeholder line dropped",
      A.note_clean("前の段落。\n\n【ここに画像: 猫の写真】\n\n後の段落。") == "前の段落。\n\n後の段落。")
check("validate_note: image placeholder reported",
      A.validate_note(A.NOTE_PASTE_MARKER + "\n【画像: 猫】\n") != [])

titles = [m.group(1).strip() for m in re.finditer(r"^案[123][:：]\s*(.+)$", text, re.M)]
check("note: uploader finds exactly one title", len(titles) == 1)

rep = report.read_text(encoding="utf-8")
check("report: heading uses new AI format", "# Reddit AI 注目記事 3本" in rep)

out2 = run_assembler({**EDITORIAL, "_followup": True})
check("follow-up run: AI report and X article written, but no note or note cover",
      not list(out2.glob("*-note.md")) and not (out2 / "2026-10-12-Reddit-Top30-cover.png").exists()
      and list(out2.glob("AI_*_gemini.md")) and list(out2.glob("📍 X_AI_*.md")))

out3 = run_assembler({"editors_note": "x"}, with_note_cover=False, with_x_cover=False)
text3 = (out3 / "2026-10-12-Reddit-Top30-note.md").read_text(encoding="utf-8")
check("note: editorial failure falls back (title from first headline, fixed tags only)",
      "案1: Reddit日本語解説 2026/10/12｜見出し1" in text3
      and len(next(l for l in text3.splitlines() if l.startswith("#")).split()) == 7
      and A.validate_note(text3) == [])
check("note: no cover when generation failed", not list(out3.glob("*-cover.png")))


# ---------------------------------------------------------------------------
# 3. Cover crop size (needs Pillow; skipped otherwise)
# ---------------------------------------------------------------------------
try:
    from PIL import Image

    import note_cover

    buf = io.BytesIO()
    Image.new("RGB", (2752, 1536), "red").save(buf, "PNG")
    png = note_cover.crop_to_cover(buf.getvalue())
    check("cover: cropped to 1280x670", Image.open(io.BytesIO(png)).size == (1280, 670))
    im = Image.new("RGB", (2752, 1536), "white")
    im.paste((0, 0, 255), (0, 1526, 2752, 1536))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    out_img = Image.open(io.BytesIO(note_cover.crop_to_cover(buf.getvalue())))
    check("cover: bottom edge kept, top trimmed", out_img.getpixel((640, 669)) == (0, 0, 255))
    check("cover: prompt asks for a retro poster", "シルクスクリーンのポスター" in note_cover.build_prompt("x"))
except ImportError:
    print("SKIP cover crop (Pillow not installed)")

if check.failed:
    sys.exit(1)
print("all passed")
