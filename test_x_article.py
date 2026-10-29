"""Offline checks for the Reddit AI X article (no network, no API key).

    python3 test_x_article.py
"""
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import assemble_ai_report_gemini as A  # noqa: E402

POSTS = [
    {"title": "a", "subreddit": "artificial", "url": "https://www.reddit.com/r/artificial/comments/1/a/",
     "buzz_score": 25.4, "ups": None, "num_comments": None, "age_hours": 5},
    {"title": "b", "subreddit": "LocalLLaMA", "url": "https://www.reddit.com/r/LocalLLaMA/comments/2/b/",
     "buzz_score": 12, "age_hours": None},
]
SUMMARIES = [
    {"category": "LLM", "title_ja": "見出し一", "summary": "一。\n\n二。"},
    {"category": "Tools", "title_ja": "見出し二", "summary": "三。\\n\\n四。"},
]
NOTE = "今日のRedditは、AIの安全性が話題でした。"


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        check.failed = True


check.failed = False


def run_assembler(editorial, with_cover):
    work = pathlib.Path(tempfile.mkdtemp())
    out = work / "out"
    shutil.copy(BASE / "assemble_ai_report_gemini.py", work)
    (work / ".cache").mkdir()
    (work / ".cache/ai_data.json").write_text(json.dumps(
        {"date": "2026-10-11", "generated_at": "2026-10-11T06:15:00+09:00", "posts": POSTS}))
    (work / ".cache/gemini_ai_posts.json").write_text(json.dumps(POSTS))
    (work / ".cache/gemini_ai_step2.json").write_text(json.dumps(SUMMARIES, ensure_ascii=False))
    (work / ".cache/gemini_ai_step3.json").write_text(json.dumps(editorial, ensure_ascii=False))
    if with_cover:
        (work / ".cache/gemini_ai_cover.png").write_bytes(b"\x89PNG fake")
    env = dict(os.environ, REDDIT_OUT_DIR=str(out))
    subprocess.run([sys.executable, str(work / "assemble_ai_report_gemini.py")], env=env,
                   check=True, capture_output=True)
    return out


# 1. full editorial + cover: one article, no comment-section file, cover copied and referenced
out = run_assembler({"editors_note": NOTE, "star_picks": [1], "cross_refs": {"2": "→ #1 も参照"},
                     "x_headline": "目玉の話題", "x_promotion": "宣伝の一文です。"}, with_cover=True)
names = sorted(p.name for p in out.iterdir())
check("no コメント欄 file", not any("コメント欄" in n for n in names))
check("one X article", sum(n.startswith("📍 X_AI_") and n.endswith(".md") for n in names) == 1)
check("normal AI report still written", "AI_2026-10-11_0615_gemini.md" in names)
check("cover copied", "📍 X_AI_2026-10-11_0615_cover.png" in names)
art = (out / "📍 X_AI_2026-10-11_0615.md").read_text(encoding="utf-8")
check("frontmatter type", art.startswith("---\ntype: x-article\n"))
check("cover in frontmatter", 'cover: "📍 X_AI_2026-10-11_0615_cover.png"' in art)
check("title", "\n# Reddit AIトレンド 2026/10/11｜目玉の話題\n" in art)
check("promotion section", "## 宣伝文\n\n宣伝の一文です。\n\n## 本文\n" in art)
check("links inline", art.count("🔗 [元の投稿を見る](https://www.reddit.com/") == 2)
check("literal \\n\\n split", "三。\n\n四。" in art)
check("no comment-section pointer", "コメント欄" not in art)
check("closing at the end", art.endswith("\n\n---\n\n" + A.X_CLOSING + "\n"))
check("order title<promotion<body<posts",
      art.index("# Reddit") < art.index("## 宣伝文") < art.index("## 本文") < art.index("## 1.") < art.index("## 2."))

# 2. editorial without the new fields and no cover: fallbacks
out = run_assembler({"editors_note": NOTE}, with_cover=False)
art = (out / "📍 X_AI_2026-10-11_0615.md").read_text(encoding="utf-8")
# 件数は実際に載せた本数（このテストでは 2 本。本番は config_ai.json の top_n = 30）
check("fallback title", "\n# Reddit AIトレンド Top 2｜2026/10/11\n" in art)
normal = (out / "AI_2026-10-11_0615_gemini.md").read_text(encoding="utf-8")
check("normal report heading count", "# 🤖 Reddit AI トレンド Top 2\n" in normal)
check("fallback promotion = editors note", f"## 宣伝文\n\n{NOTE}\n" in art)
check("no cover line without cover", "cover:" not in art)
check("no png without cover", not list(out.glob("*.png")))

# 3. promotion limit
long = "あ" * 100 + "。" + "い" * 100 + "。"
p = A.prepare_promotion(long, NOTE)
check("long promotion cut at sentence", p == "あ" * 100 + "。" and A.halfwidth_count(p) <= 256)
check("multi-line promotion becomes one line", "\n" not in A.prepare_promotion("一行目。\n二行目。", ""))

# 4. cover crop (needs Pillow; no API call)
try:
    from PIL import Image
    import x_cover
    buf = io.BytesIO()
    Image.new("RGB", (2100, 900), (240, 240, 240)).save(buf, "PNG")
    png = x_cover.crop_to_banner(buf.getvalue())
    check("cover is 2560x1024", Image.open(io.BytesIO(png)).size == (2560, 1024))
    os.environ.pop("GEMINI_IMAGE_API_KEY", None)
    tmp = pathlib.Path(tempfile.mkdtemp()) / "c.png"
    check("no key -> no cover, no exception", x_cover.make_cover("本文", tmp) is False and not tmp.exists())

    # 5. one generation only: the first image is used, retry only when no image came back
    from types import SimpleNamespace as NS
    from unittest import mock

    def reply(data):
        parts = [NS(inline_data=NS(data=data))] if data else []
        return NS(candidates=[NS(content=NS(parts=parts))])

    class FakeClient:
        def __init__(self, replies):
            self.replies, self.calls, self.models = list(replies), 0, self

        def generate_content(self, **kw):
            self.calls += 1
            r = self.replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    def run(replies):
        fake = FakeClient(replies)
        with mock.patch.dict(os.environ, {"GEMINI_IMAGE_API_KEY": "test"}), \
             mock.patch("google.genai.Client", return_value=fake), \
             mock.patch.object(x_cover, "BANNER_RETRY_BASE_WAIT", 0), \
             mock.patch.object(x_cover, "meter", mock.Mock()):
            return x_cover.gemini_generate_banner("p"), fake.calls

    out, calls = run([reply(buf.getvalue()), reply(buf.getvalue())])
    check("first image used without retry", calls == 1 and Image.open(io.BytesIO(out)).size == (2560, 1024))
    out, calls = run([reply(None), RuntimeError("503"), reply(buf.getvalue())])
    check("retry only when no image", calls == 3 and out is not None)
    check("prompt is retro poster", "レトロなシルクスクリーンのポスター" in x_cover.build_cover_prompt("本文"))
except ImportError:
    print("SKIP cover checks (Pillow not installed)")

sys.exit(1 if check.failed else 0)
