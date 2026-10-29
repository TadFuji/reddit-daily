#!/usr/bin/env python
"""run_reddit_gemini.py — Reddit buzz report pipeline on Gemini.

Standalone replacement for `claude -p "/reddit all"`. Runs the same 4-step
pipeline as the /reddit and /redditq variants but with all LLM calls going to
the Gemini API in-process (no Claude Code orchestration, no Ollama):

  Step 1   fetch_data.py (unchanged) -> .cache/{variant}_data.json
  Step 1.5 dedupe against today's earlier *_gemini.md reports
  Step 2   one Gemini JSON call per post  -> .cache/gemini_{variant}_step2.json
  Step 3   one editorial Gemini JSON call -> .cache/gemini_{variant}_step3.json
  Step 3.5 (ai only) X article cover image  -> .cache/gemini_ai_cover.png
  Step 4   assemble_*_gemini.py -> Obsidian vault

Honest-comments policy (decided 2026-06-12, reworked 2026-09-11): the RSS
fetcher never returns comments -- fetch_data.py sets "comments": [] for every
post -- so the WITH_COMMENT_* rules below never actually fire and the NO_COMMENT_*
ones always do.

The prompts used to require the notice 「コメントデータ未取得」, which then opened
two of the five paragraphs in every post of every report (20 occurrences per
report, measured 2026-09-11). That tells the reader about our plumbing, not about
the story. The honesty it protected is now enforced the other way round: the
model may not write about comments or community reaction at all, so it cannot
imply it read them, and the two paragraphs carry analysis and advice instead.

Usage:
    python run_reddit_gemini.py [standard|ai|seminar|all] [--skip-fetch]
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import pathlib
import re
import subprocess
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from usage_meter import meter  # Gemini の使用量記録（2026-09-10 追加）
except ImportError:  # VPS への転送漏れで本番を止めない。計測だけ無効になる
    class _NullMeter:
        def __getattr__(self, _):
            return lambda *a, **k: None

        def summary_line(self):
            return "USAGE: usage_meter.py が無いため未計測"

    meter = _NullMeter()

BASE = pathlib.Path(__file__).resolve().parent
CACHE = BASE / ".cache"
# 文章規範。正本は x-morning-brief の同名ファイルで、あちらの
# skills/build_merged_skill.py が作る。ここは配った写しで、中身は同一（md5 で確認できる）。
# 更新したら rsync で VPS へ送ること（CLAUDE.md の反映手順に含めてある）。
WRITING_NORMS = BASE / "skills" / "japanese-tech-writing" / "SKILL.md"
WRITING_RULES = BASE / "skills" / "writing-rules.md"
ENV_FILE = BASE / ".env"


def _load_env_file(path: pathlib.Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            os.environ[key] = value


# Load .env before resolving REDDIT_OUT_DIR so a value set there is honored
# consistently by both the orchestrator and the assembler subprocesses.
_load_env_file(ENV_FILE)

_DEFAULT_OUT = str((pathlib.Path("/Volumes/ROCKET_XTRM/Obsidian") if sys.platform == "darwin"
         else pathlib.Path.home() / "Obsidian") / "📰_情報配信" / "Reddit")
REPORT_DIR = pathlib.Path(os.environ.get("REDDIT_OUT_DIR", _DEFAULT_OUT))
# The assemble_* scripts run as subprocesses and resolve the same default;
# pass the resolved dir down so orchestrator and assemblers always agree.
os.environ["REDDIT_OUT_DIR"] = str(REPORT_DIR)

JST = dt.timezone(dt.timedelta(hours=9), name="JST")

# 2026-07-28 藤川さんの指示で 3.6-flash へ変更（旧: gemini-3.1-flash-lite）。GEMINI_MODEL で上書き可。
# 2026-08-14 後継の 3.7-flash へ更新（API のモデル一覧で提供を確認済み）。
# 2026-09-08 後継の 3.8-flash へ更新（公式ドキュメントで GA 提供を確認）。旧 3.7 を先頭のフォールバックに残す。
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_FALLBACK_CANDIDATES = ["gemini-3.7-flash", "gemini-3.6-flash"]
MAX_CALL_RETRIES = 3

VARIANTS = {
    "standard": {
        "config": "config.json",
        "data": "standard_data.json",
        "dedup_glob": "{date}_*_gemini.md",
        "label": "gemini-std",
    },
    "ai": {
        "config": "config_ai.json",
        "data": "ai_data.json",
        "dedup_glob": "AI_{date}_*_gemini.md",
        "label": "gemini-ai",
    },
    "seminar": {
        "config": "config_seminar.json",
        "data": "seminar_data.json",
        "dedup_glob": "Seminar_{date}_*_gemini.md",
        "label": "gemini-sem",
    },
}


def log(label: str, msg: str) -> None:
    ts = dt.datetime.now(JST).strftime("%H:%M:%S")
    print(f"[{label} {ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Gemini plumbing
# ---------------------------------------------------------------------------

_CLIENT = None
_MODEL = None


def gemini_client():
    global _CLIENT
    if _CLIENT is None:
        from google import genai

        from google.genai import types
        _CLIENT = genai.Client(http_options=types.HttpOptions(timeout=180000))
    return _CLIENT


def resolve_model() -> str:
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    from google.genai import types

    client = gemini_client()
    for m in [GEMINI_MODEL, *GEMINI_FALLBACK_CANDIDATES]:
        try:
            probe = client.models.generate_content(
                model=m,
                contents="ping",
                config=types.GenerateContentConfig(max_output_tokens=1, temperature=0),
            )
            meter.record(m, probe, label="model-probe")
            _MODEL = m
            return m
        except Exception as e:
            log("gemini", f"model {m!r} unavailable: {type(e).__name__}: {str(e)[:150]}")
    raise RuntimeError("no usable Gemini model")


def gemini_json(prompt: str, label: str, cache_stem: str | None = None) -> dict | None:
    """One JSON-mode call with retries; prompt/response mirrored to .cache."""
    from google.genai import types

    if cache_stem:
        (CACHE / f"{cache_stem}.txt").write_text(prompt, encoding="utf-8")
    client = gemini_client()
    model = resolve_model()
    for attempt in range(1, MAX_CALL_RETRIES + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    response_mime_type="application/json",
                ),
            )
            meter.record(model, resp, label=label)
            text = (resp.text or "").strip()
            if not text:
                raise ValueError("empty response (safety block?)")
            obj = json.loads(text)
            if not isinstance(obj, dict):
                raise ValueError(f"expected JSON object, got {type(obj).__name__}")
            if cache_stem:
                (CACHE / f"{cache_stem}_out.json").write_text(text, encoding="utf-8")
            return obj
        except Exception as e:
            log(label, f"attempt {attempt}/{MAX_CALL_RETRIES} failed: {type(e).__name__}: {str(e)[:200]}")
            if attempt < MAX_CALL_RETRIES:
                time.sleep(4 * attempt)
    return None


# ---------------------------------------------------------------------------
# Step 2 prompts (ported from qwen_batch_process.py / qwen_batch_ai.py /
# seminar_select_and_process.py, with the honest-comments adaptation)
# ---------------------------------------------------------------------------

def writing_norms() -> str:
    """Load the writing norms verbatim, wrapped for this report's output shape.

    Applied to the AI report only (2026-09-11). The norms target book chapters
    and long articles, while what Gemini writes here is the value of a JSON
    field with a fixed five-paragraph shape, so the preamble names the rules
    that do not carry over and says which side wins on a conflict.

    A missing file is logged loudly rather than silently dropping the norms;
    otherwise the prose just quietly gets worse with no sign in the log.
    """
    try:
        text = WRITING_NORMS.read_text(encoding="utf-8")
    except OSError as e:
        log("norms", f"WARNING: 文章規範を読めません（{type(e).__name__}）: {WRITING_NORMS}")
        log("norms", "WARNING: 規範なしで生成します。文体が普段より落ちます")
        return ""
    if text.startswith("---"):  # drop the YAML frontmatter
        text = text.split("---", 2)[-1]
    return (
        "\n【文章作法: japanese-tech-writing 全文】\n"
        "以下は日本語の文章規範の全文です。日本語で書くすべての値（title_ja と summary）に、"
        "内容に適用できる規範をすべて適用してください。\n"
        "ただし、あなたが書くのは JSON の値になる平文です。次の項目は適用しません: "
        "一文ごとの改行、太字、箇条書き、脚注、コードブロック、引用ブロック、"
        "「見出しの付け方」の節。\n"
        "段落の数・各段落の役割・字数の指定は、この後の【出力形式】が決めます。"
        "規範と矛盾したら【出力形式】を優先してください。\n"
        + text.strip()
        + "\n"
    )


def writing_rules() -> str:
    """Load Fujikawa-san's short writing rules for the standard and seminar
    reports (2026-09-17).

    These two reports do not carry the full norms (the AI report does, and the
    rules are already merged into them), so they get the short list verbatim.
    The fixed five-paragraph shape stays; the preamble says it wins.
    """
    try:
        text = WRITING_RULES.read_text(encoding="utf-8")
    except OSError as e:
        log("rules", f"WARNING: 文章ルールを読めません（{type(e).__name__}）: {WRITING_RULES}")
        log("rules", "WARNING: ルールなしで生成します")
        return ""
    return (
        "\n【文章ルール】\n"
        "日本語で書くすべての値（title_ja と summary）に、次のルールを適用してください。"
        "見出しと箇条書きは出力しないので、それに関する項目は無視してください。"
        "段落の数・各段落の役割・字数は上の指定が決めます。ルールと矛盾したら上の指定を優先してください。\n"
        + text.strip()
        + "\n"
    )


NO_COMMENT_RULE = """\
IMPORTANT: you were not given the comment section for this post.
- NEVER write about comments, community reaction, or what commenters said, and never
  put 「」 quotes around anything you were not given. Do not invent user experiences.
- Do NOT tell the reader that comment data is missing. 「コメントデータ未取得」
  「コメントは取得していません」「議論の温度感は不明です」といった断り書きは書かない。
  こちらがコメント欄を見たかどうかは読者に関係がない。
- Paragraph 3 - 論点と考察: あなた自身の読みを書く。この投稿の主張のどこが争点になりうるか、
  どの前提がまだ確かめられていないか、技術的・実務的なトレードオフは何か。根拠は投稿本文だけに
  求める。断定できないことは「〜とみられます」「〜の可能性があります」と、自分の見立てだと
  分かる形で書く。
- Paragraph 4 - 注意点と確かめたいこと: 1〜2 文。この投稿だけでは分からない点は何か、
  読者が飛びつく前に何を見て判断すればよいか、どこに落とし穴がありそうか。具体的に書く。
  第 5 段落のポイントと同じことを繰り返さない。"""

WITH_COMMENT_RULE_STD = """\
Paragraph 3 - コメント欄のハイライト: Quote or closely paraphrase 3-5 specific notable comments using 「」 quotes with context. Be SPECIFIC.

Paragraph 4 - 議論の温度感: Exactly one sentence capturing the overall vibe of the comment section."""

STANDARD_PROMPT = """\
You are a Japanese news editor for a Reddit trend report. For the Reddit post provided, output a single JSON object.

The JSON object must have exactly these fields:
- "category": one of Tech, Politics, Gaming, Entertainment, Science, Sports, World, Business, Lifestyle, Humor, Other
- "title_ja": compelling Japanese headline in news style, 1 line
- "summary": exactly 5 paragraphs in Japanese, separated by a blank line (two newlines), total 500-700 characters
- "note_ok": true or false. This summary is published under a real person's name on note.com, a
  general-audience Japanese blog site. Set false if the post is sexual or NSFW, hateful or
  discriminatory, shows graphic violence, gore or death, involves self-harm, exposes or mocks a
  private individual (a non-public person who can be identified), promotes illegal acts, or is
  mainly partisan mockery or insults aimed at real people. Otherwise true. When unsure, false.
- "note_ng_reason": when note_ok is false, a short Japanese reason (under 30 characters). Otherwise "".

The 5 paragraphs MUST follow this structure:

Paragraph 1 - 投稿の中身: Describe what was ACTUALLY posted. Be concrete and specific. The reader should clearly understand WHAT was posted.

Paragraph 2 - なぜバズっているか: Why is this trending right now? Timing, social context, emotional hooks.

{comment_rule}

Paragraph 5 - ポイント: One-sentence takeaway for Japanese readers. Not a restatement of paragraphs 1-4: give the one thing the reader should take away that the body has not already said.

Exception for posts with no selftext (image, video or link posts): you cannot see the media.
Write only 2 paragraphs, total 150-300 characters: what the title says was posted, and why it
may be resonating. Never describe what the image or video shows beyond what the title states.

Rules:
- ALL text in natural Japanese (desu/masu style). No English except proper nouns.
- Do NOT start paragraphs with numbers.
- Be concrete and specific throughout. Generic filler is forbidden.
- Convey the gist of the selftext in your own words. Do not translate it sentence by sentence.

Output ONLY a valid JSON object. No markdown, no explanation, no preamble.
{writing_rules}
Here is the Reddit post data:
{post_data}
"""

WITH_COMMENT_RULE_AI = """\
Paragraph 3 - コメント欄のハイライト: Quote or closely paraphrase 3-5 specific notable comments using 「」 quotes with brief context. Prioritize: technical insights, practical experience, contrarian views, humor. Be SPECIFIC.

Paragraph 4 - 議論の温度感: Exactly one sentence capturing the overall vibe of the comment section."""

AI_PROMPT = """\
You are a Japanese tech editor specializing in AI news. For the Reddit post provided, output a single JSON object.

The JSON object must have exactly these fields:
- "category": one of LLM, ImageGen, Research, Robotics, Ethics, Business, Tools, Other
  (LLM=大規模言語モデル, ImageGen=画像生成AI, Research=研究・論文, Robotics=ロボティクス,
   Ethics=倫理・規制, Business=ビジネス, Tools=ツール・アプリ, Other=その他)
- "title_ja": compelling Japanese headline in tech news style, 1 line
- "summary": exactly 5 paragraphs in Japanese, separated by a blank line (two newlines), total 500-700 characters

The 5 paragraphs MUST follow this structure:

Paragraph 1 - 投稿の中身: Describe what was ACTUALLY posted. If it's a new tool/model, describe its capabilities. If it's a discussion post, summarize the core argument. If it's a demo/benchmark, describe the results. Be concrete.

Paragraph 2 - なぜバズっているか: Why is this trending in the AI community? Technical significance, timing relative to other announcements, competitive dynamics, community sentiment.

{comment_rule}

Paragraph 5 - ポイント: One-sentence takeaway for Japanese AI practitioners, developers, or the industry. Not a restatement of paragraphs 1-4: give the one thing the reader should take away that the body has not already said.

Rules:
- ALL text in natural Japanese (desu/masu style). English OK only for technical terms (LLM, GPU, API, model names, etc.) commonly used as-is in Japanese tech writing.
- Do NOT start paragraphs with numbers.
- Be concrete and specific throughout. Generic filler is forbidden.

Output ONLY a valid JSON object. No markdown, no explanation, no preamble.
{writing_norms}
【出力形式】
出力は JSON オブジェクトだけ。マークダウン記号は値の中に入れない。
summary は 5 段落、合計 500〜700 字。段落の区切りは空行（改行 2 つ）。
一文ごとの改行はしない。段落の数と各段落の役割は上の指定どおりにする。

Here is the Reddit post data:
{post_data}
"""

SEMINAR_SELECTION_PROMPT = """\
You are curating AI news for a seminar instructor. From the {count} posts listed below, select exactly 10 that are MOST USEFUL for teaching AI seminars.

KEEP posts about:
- New tools or services that can be demonstrated live
- Real business use cases or success stories
- New models, techniques, or benchmarks with practical implications
- Tutorials, workflows, or how-to content

REJECT posts that are:
- Memes, jokes, humor-only
- Pure complaints about AI services
- Speculation without substance
- Drama or gossip
- Posts with little informational content
- Duplicates: if two posts cover the same content (reposts, crossposts), select at most one of them

Output ONLY a valid JSON object with one field "selected" containing an array of exactly 10 integers (1-indexed post numbers).

Here are the {count} posts:
{posts_list}
"""

SEMINAR_NO_COMMENT_RULE = """\
IMPORTANT: you were not given the comment section for this post.
- NEVER write about comments, community reaction, or what users said, and never invent
  user experiences or quotes. Describe only what the post body itself establishes.
- Do NOT tell the reader that comment data is missing. 「コメントデータ未取得」
  「コミュニティの評価は不明です」といった断り書きは書かない。
- Paragraph 3 - 授業での活かし方: 投稿本文から分かる範囲で、セミナーでどう見せられるか、
  受講者のどんな疑問に答えられるかを書く。使い方の具体は投稿本文に書かれていることに限る。
- Paragraph 4 - 注意点: 1〜2 文。教える前に確かめておきたいこと、受講者が誤解しやすい点、
  デモで詰まりそうなところを具体的に書く。"""

SEMINAR_WITH_COMMENT_RULE = """\
Paragraph 3 - 実際の使い方・事例: How are people actually using this? Quote specific experiences from comments. Include setup tips, limitations, or gotchas. Be SPECIFIC.

Paragraph 4 - コミュニティの評価: What do practitioners think? Technical critiques, comparisons, concerns. One sentence on overall reception."""

SEMINAR_SUMMARY_PROMPT = """\
You are a Japanese tech editor writing for AI seminar instructors. For the Reddit post provided, output a single JSON object.

The JSON object must have exactly these fields:
- "rank": {rank} (the original position in the post list)
- "category": one of NewTool, BusinessCase, ModelUpdate, Tutorial, Research, Industry, Other
- "title_ja": compelling Japanese headline focused on practical value, 1 line
- "summary": exactly 5 paragraphs in Japanese, separated by a blank line (two newlines), total 500-700 characters

The 5 paragraphs MUST follow this structure:

Paragraph 1 - 何が新しいか: What is this tool/model/technique? Describe it concretely. What does it do? What problem does it solve?

Paragraph 2 - なぜ重要か: Why does this matter? Practical impact? How does it compare to existing solutions? Who benefits?

{comment_rule}

Paragraph 5 - セミナーでの活用ポイント: Concrete suggestion for how to use this in a seminar: demo idea, discussion topic, hands-on exercise, or talking point. Make this actionable, and do not restate paragraphs 1-4.

Rules:
- ALL text in natural Japanese (desu/masu style). Technical terms in English are fine (LLM, API, GPU, model names).
- Focus on PRACTICAL VALUE.
- Be concrete and specific throughout.

Output ONLY a valid JSON object. No markdown, no explanation, no preamble.
{writing_rules}
Here is the Reddit post data:
{post_data}
"""

FAIL_ENTRY = {"category": "Other", "title_ja": "(翻訳失敗)", "summary": "(要約生成失敗)"}


def post_to_text(post: dict, selftext_chars: int = 500) -> str:
    ups = post.get("ups")
    ncom = post.get("num_comments")
    metrics = (
        f"ups: {ups if ups is not None else '取得不可'}, "
        f"comments: {ncom if ncom is not None else '取得不可'}, "
        f"feed_rank_score: {int(post.get('buzz_score') or 0)}, "
        f"age: {post.get('age_hours')}h"
    )
    lines = [
        f"title: {post['title']}",
        f"subreddit: r/{post['subreddit']}",
        metrics,
    ]
    if post.get("selftext"):
        lines.append(f"selftext: {post['selftext'][:selftext_chars]}")
    if post.get("comments"):
        lines.append("notable_comments:")
        for c in post["comments"][:6]:
            lines.append(f"  - {c[:200]}".replace("\n", " "))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------


# On the VPS the whole pipeline runs on the project .venv interpreter
# (run_reddit.sh launches it), which has both defusedxml and google-genai.
# Prefer that interpreter for the fetch subprocess too; fall back to the
# current one so debug runs outside the .venv still work.
FETCH_PYTHON = BASE / ".venv" / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python"
)


def run_fetch(variant: str) -> pathlib.Path:
    spec = VARIANTS[variant]
    out = CACHE / spec["data"]
    fetch_py = str(FETCH_PYTHON) if FETCH_PYTHON.exists() else sys.executable
    cmd = [fetch_py, str(BASE / "fetch_data.py"), "--output", str(out)]
    if spec["config"]:
        cmd[2:2] = ["--config", spec["config"]]
    log(spec["label"], f"Step 1: fetch_data.py -> {out.name}")
    r = subprocess.run(cmd, cwd=str(BASE))
    if r.returncode != 0:
        raise RuntimeError(f"fetch_data.py failed (exit {r.returncode})")
    return out


def dedup_posts(variant: str, posts: list[dict]) -> list[dict]:
    """Step 1.5: drop posts already covered by today's earlier _gemini reports."""
    spec = VARIANTS[variant]
    date_str = dt.datetime.now(JST).strftime("%Y-%m-%d")
    pattern = str(REPORT_DIR / spec["dedup_glob"].format(date=date_str))
    seen_ids: set[str] = set()
    for f in glob.glob(pattern):
        try:
            text = pathlib.Path(f).read_text(encoding="utf-8")
        except Exception:
            continue
        seen_ids.update(re.findall(r"reddit\.com/r/[^/]+/comments/([a-z0-9]+)/", text))
    if not seen_ids:
        return posts
    kept = [p for p in posts if p.get("id") not in seen_ids]
    log(spec["label"], f"Step 1.5: dedup removed {len(posts) - len(kept)} of {len(posts)} posts")
    return kept


# note 記事（standard だけ。2026-10-12 追加）。r/popular・r/all の上位から、note に載せてよい
# 投稿がこの件数たまるまで順に要約する。候補の数は config.json の top_n（fetch_data.py が切る）。
NOTE_TARGET_DEFAULT = 30
# note 原稿と表紙のファイル名は {日付}-Reddit-Top30-note.md / -cover.png（assemble_report_gemini.py
# と同じ）。Hacker News の {日付}-HackerNews-Top30-note.md と同じ形にして、note への自動投稿
# （../Hacker-News/note_upload.py）が同じ探し方で見つけられるようにした。
NOTE_FILE_STEM = "Reddit-Top30"
# note のハッシュタグのうち毎日同じ 7 個。残り 3 個は Step 3 でその日の話題から作る。
# 組み立ては assemble_report_gemini.py（同じ文字列を持つ。テストで一致を確かめる）。
NOTE_FIXED_HASHTAGS = "#毎日note #Reddit #海外ニュース #海外の反応 #海外トレンド #ネットで話題 #雑学"
# 掲示板名だけで成人向けと分かるものは、要約を頼まずに外す（費用と安全フィルタの空振りを省く）。
# r/all は Reddit 側で成人向けを除かない一覧なので、ここが最初の関所になる。
NSFW_SUBREDDIT_RE = re.compile(
    r"nsfw|porn|gonewild|hentai|nude|naked|onlyfans|boob|tits|milf|sexy|r34|rule34|xxx",
    re.I,
)


def note_target(variant: str) -> int | None:
    """How many publishable posts the note needs, or None when this variant has no note."""
    if variant != "standard":
        return None
    try:
        cfg = json.loads((BASE / VARIANTS[variant]["config"]).read_text(encoding="utf-8"))
        return int(cfg.get("note_target", NOTE_TARGET_DEFAULT))
    except Exception:
        return NOTE_TARGET_DEFAULT


def is_note_ok(summary: dict) -> bool:
    """A summary may go into the note only when Gemini said so explicitly and it was generated.

    A failed summary counts as not OK: an empty response is usually the safety filter, and
    a post that trips it is exactly the kind that must not be published.
    """
    return summary.get("note_ok") is True and summary.get("title_ja") != FAIL_ENTRY["title_ja"]


def step2_summaries(variant: str, posts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Summarise posts in feed order. Returns (posts, summaries), aligned one to one.

    For standard, stops once note_target posts are judged publishable, so the returned
    post list can be shorter than the candidates.
    """
    spec = VARIANTS[variant]
    template = STANDARD_PROMPT if variant == "standard" else AI_PROMPT
    with_rule = WITH_COMMENT_RULE_STD if variant == "standard" else WITH_COMMENT_RULE_AI
    # 規範を載せるのは AI レポートだけ（2026-09-11）。10 投稿ぶん読み直さず 1 回で済ませる。
    norms = writing_norms() if variant == "ai" else ""
    # 標準レポートには藤川さんの短い文章ルールだけを載せる（2026-09-17）。AI は規範に統合済み。
    rules = writing_rules() if variant == "standard" else ""
    target = note_target(variant)
    used: list[dict] = []
    results = []
    for i, post in enumerate(posts):
        if target is not None and sum(1 for s in results if is_note_ok(s)) >= target:
            break
        used.append(post)
        if target is not None and NSFW_SUBREDDIT_RE.search(post.get("subreddit") or ""):
            log(spec["label"], f"Step 2: post {i + 1}/{len(posts)}: skipped (adult subreddit r/{post['subreddit']})")
            results.append({**FAIL_ENTRY, "note_ok": False, "note_ng_reason": "成人向けの掲示板",
                            "skipped": True})
            continue
        rule = with_rule if post.get("comments") else NO_COMMENT_RULE
        prompt = template.format(
            comment_rule=rule,
            post_data=post_to_text(post),
            writing_norms=norms,
            writing_rules=rules,
        )
        log(spec["label"], f"Step 2: post {i + 1}/{len(posts)}: {post['title'][:55]}")
        obj = gemini_json(prompt, spec["label"], cache_stem=f"gemini_{variant}_{i + 1}")
        if not obj or not obj.get("title_ja"):
            obj = dict(FAIL_ENTRY)
            if target is not None:
                obj.update(note_ok=False, note_ng_reason="要約を作れなかった")
        elif target is not None and not is_note_ok(obj):
            log(spec["label"], f"Step 2:   not for note: {obj.get('note_ng_reason') or '理由なし'}")
        results.append(obj)
    if target is not None:
        ok = sum(1 for s in results if is_note_ok(s))
        log(spec["label"], f"Step 2: {ok} of {len(results)} posts are publishable on note (target {target})")
    (CACHE / f"gemini_{variant}_step2.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return used, results


def step2_seminar(posts: list[dict]) -> list[dict]:
    """Selection (top 10 of up to 20) + per-post summaries, mirroring
    seminar_select_and_process.py."""
    label = VARIANTS["seminar"]["label"]

    posts_list_lines = []
    for i, p in enumerate(posts):
        line = f"{i + 1}. r/{p['subreddit']} | {p['title'][:80]}"
        if p.get("selftext"):
            line += f" | {p['selftext'][:80]}"
        posts_list_lines.append(line)

    log(label, f"Step 2a: selecting 10 of {len(posts)} posts")
    sel = gemini_json(
        SEMINAR_SELECTION_PROMPT.format(count=len(posts), posts_list="\n".join(posts_list_lines)),
        label,
        cache_stem="gemini_seminar_select",
    )
    raw_sel = sel.get("selected") if sel and isinstance(sel.get("selected"), list) else []
    valid: list[int] = []
    for n in raw_sel:
        if isinstance(n, int) and 1 <= n <= len(posts) and n not in valid:
            valid.append(n)
    if len(valid) >= 5:
        selected = valid[:10]
        log(label, f"Step 2a: selected {selected}")
    else:
        selected = list(range(1, min(11, len(posts) + 1)))
        log(label, f"Step 2a: selection failed, using default top {len(selected)}")

    rules = writing_rules()
    results = []
    for slot, rank in enumerate(selected):
        post = posts[rank - 1]
        rule = SEMINAR_WITH_COMMENT_RULE if post.get("comments") else SEMINAR_NO_COMMENT_RULE
        prompt = SEMINAR_SUMMARY_PROMPT.format(
            rank=rank,
            comment_rule=rule,
            post_data=post_to_text(post, selftext_chars=400),
            writing_rules=rules,
        )
        log(label, f"Step 2b: {slot + 1}/{len(selected)} (rank #{rank}): {post['title'][:50]}")
        obj = gemini_json(prompt, label, cache_stem=f"gemini_seminar_{slot + 1}")
        if not obj or not obj.get("title_ja") or obj.get("title_ja") in ("(翻訳失敗)", "?"):
            obj = {"rank": rank, **FAIL_ENTRY}
        obj["_post"] = post
        results.append(obj)

    clean = [{k: v for k, v in r.items() if k != "_post"} for r in results]
    (CACHE / "gemini_seminar_step2.json").write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    posts_map = {str(i + 1): results[i]["_post"] for i in range(len(results))}
    (CACHE / "gemini_seminar_posts_map.json").write_text(
        json.dumps(posts_map, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return clean


def step3_editorial(variant: str, summaries: list[dict], followup: bool) -> None:
    spec = VARIANTS[variant]
    titles_str = "\n".join(
        f"{i + 1}. [{d.get('category', 'Other')}] {d.get('title_ja', '?')}"
        for i, d in enumerate(summaries)
    )
    n = len(summaries)
    followup_note = (
        " Note: this is a follow-up run later the same day with fewer articles than usual;"
        " mention that briefly." if followup else ""
    )
    if variant == "seminar":
        prompt = (
            f"You are a senior AI educator reviewing today's AI seminar digest ({n} articles). "
            "Produce a single JSON object with exactly these 4 fields:\n\n"
            "1. \"editors_note\" (string, 200-300 chars): Japanese text starting with 「今日のAI動向から、」. "
            f"What themes emerged? What should instructors pay attention to? Be specific and actionable.{followup_note}\n\n"
            f"2. \"difficulty_tags\" (object): For each article key \"1\" through \"{n}\", assign one of: "
            "\"入門\" (anyone can understand), \"中級\" (needs some AI knowledge), \"上級\" (technical audience).\n\n"
            f"3. \"demo_feasibility\" (object): For each article key \"1\" through \"{n}\", assign one of: "
            "\"すぐにデモ可能\" (can show right now), \"準備すればデモ可能\" (needs setup), \"スライドで紹介\" (better as slides).\n\n"
            "4. \"weekly_theme\" (string, 1 sentence): If you notice a dominant theme across articles, state it in Japanese. "
            "Empty string if no clear theme.\n\n"
            "Output ONLY a valid JSON object with these 4 fields. No markdown, no explanation.\n\n"
            f"Here are the {n} articles:\n" + titles_str
        )
    else:
        # AI 版だけは X 記事のタイトルと宣伝文も作らせる（2026-10-11 追加。呼び出し回数は増やさない）
        x_fields = (
            "4. \"x_headline\" (string, at most 30 Japanese characters): the second half of today's"
            " X article title, naming the single most striking story of the day as a short Japanese"
            " news phrase. No date, no 「Reddit」, no emoji, no quotation marks.\n\n"
            "5. \"x_promotion\" (string, 80-120 Japanese characters, desu/masu): 2-3 sentences that"
            " introduce today's article to X readers and make them want to open it. Name one or two concrete"
            " stories. No URLs, no hashtags, no emoji, no line breaks.\n\n"
        ) if variant == "ai" else ""
        # standard だけは note 記事の枠も作らせる（2026-10-12 追加。呼び出し回数は増やさない）。
        # 渡す一覧は note に載せる投稿だけなので、star_picks / cross_refs の番号も note と同じ。
        if variant == "standard":
            x_fields = (
                "4. \"note_catch\" (string, 12-22 Japanese characters): the phrase that follows"
                " 「今日の目玉は」 in today's note.com title, naming the single most striking story"
                " concretely. Do not include 「今日の目玉は」 itself. No date, no emoji, no quotation marks.\n\n"
                "5. \"note_points\" (array of exactly 3 strings): today's overall trends and highlights,"
                " one Japanese sentence each (desu/masu), no markdown.\n\n"
                "6. \"note_extra_hashtags\" (array of exactly 3 strings): note.com hashtags specific to"
                " today's stories, concrete words people search for (proper nouns or topic names)."
                f" Do not repeat these fixed tags: {NOTE_FIXED_HASHTAGS}\n\n"
                "7. \"closing_comment\" (string, 1-2 Japanese sentences, desu/masu): a closing remark"
                " for the article that reflects today's mix of stories.\n\n"
            )
        field_count = {"ai": "5", "standard": "7"}.get(variant, "3")
        prompt = (
            f"You are a senior editor reviewing today's Reddit trend report ({n} articles with Japanese summaries)."
            f" Produce a single JSON object with exactly these {field_count} fields:\n\n"
            "1. \"editors_note\" (string): A Japanese text of 150-200 characters starting with"
            f" 「今日のRedditは」. Describe today's overall Reddit mood and what made it unique. Be specific, not generic.{followup_note}\n\n"
            "2. \"star_picks\" (array of up to 3 integers): The article numbers (1-indexed)"
            " that are most interesting, surprising, or socially significant.\n\n"
            "3. \"cross_refs\" (object or empty object): If any articles share a related topic,"
            " add an entry like {\"3\": \"→ #7 も参照\"}. Return {} if none are related.\n\n"
            + x_fields
            + f"Output ONLY a valid JSON object with these {field_count} fields. No markdown, no summaries array, no explanation.\n\n"
            f"Here are the {n} article titles for your reference:\n" + titles_str
        )
    log(spec["label"], "Step 3: editorial review")
    obj = gemini_json(prompt, spec["label"], cache_stem=f"gemini_{variant}_step3") or {}
    # 組み立て側が「同じ日の 2 回目」を知るため（standard はそのとき note を書かない）
    obj["_followup"] = followup
    (CACHE / f"gemini_{variant}_step3.json").write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def step3_x_cover(summaries: list[dict]) -> None:
    """Cover image for the AI X article (2026-10-11 追加).

    作り方は「僕のフォロワーさんたちが…」のバナーと同じ（x_cover.py）。
    失敗しても記事は画像なしで出す。前回の画像を使い回さないよう、先に消す。
    """
    out = CACHE / "gemini_ai_cover.png"
    out.unlink(missing_ok=True)
    try:
        import x_cover
    except ImportError as e:  # VPS への転送漏れで本番を止めない
        log("gemini-ai", f"WARNING: x_cover.py を読めないためカバー画像なし: {e}")
        return
    text = "\n\n".join(
        f"{d.get('title_ja', '')}\n{d.get('summary', '')}"
        for d in summaries if d.get("title_ja") != "(翻訳失敗)"
    )
    log("gemini-ai", "Step 3.5: X 記事のカバー画像")
    x_cover.make_cover(text, out)


def step3_note_cover(summaries: list[dict]) -> None:
    """Pixel-art cover for the standard note article (2026-10-12 追加).

    失敗しても記事は出す（note への自動投稿は表紙が無ければ事前検査で止まる）。
    前回の画像を使い回さないよう、先に消す。
    """
    out = CACHE / "gemini_standard_note_cover.png"
    out.unlink(missing_ok=True)
    try:
        import note_cover
    except ImportError as e:  # VPS への転送漏れで本番を止めない
        log("gemini-std", f"WARNING: note_cover.py を読めないため note の表紙なし: {e}")
        return
    text = "\n\n".join(f"{d.get('title_ja', '')}\n{d.get('summary', '')}" for d in summaries)
    log("gemini-std", "Step 3.5: note の表紙（ドット絵）")
    note_cover.make_cover(text, out)


def push_note_to_github(stats: dict) -> None:
    """Send today's note manuscript and cover to GitHub, which posts it to note.com (2026-10-12).

    Runs on every run that includes standard -- also when standard was skipped
    because the day's report already exists -- so a retry after a failed send
    still delivers. push_note_github is idempotent (same content = no commit).
    Failure never fails the run; it shows in the log and the health check.
    """
    if os.environ.get("NOTE_GITHUB_PUSH", "1") == "0":
        log("gemini-std", "NOTE_GITHUB_PUSH=0 のため note 原稿を GitHub へ送りません")
        return
    try:
        import push_note_github
    except ImportError as e:  # VPS への転送漏れで本番を止めない
        log("gemini-std", f"WARNING: push_note_github.py を読めません: {e}")
        stats.setdefault("standard", {})["note_pushed"] = False
        return
    date_str = dt.datetime.now(JST).strftime("%Y-%m-%d")
    ok, detail = push_note_github.push_note(REPORT_DIR, date_str)
    log("gemini-std", ("note 原稿を GitHub へ送りました: " if ok else "WARNING: note 原稿を GitHub へ送れませんでした: ") + detail)
    stats.setdefault("standard", {})["note_pushed"] = ok


def step4_assemble(variant: str) -> None:
    spec = VARIANTS[variant]
    script = {
        "standard": "assemble_report_gemini.py",
        "ai": "assemble_ai_report_gemini.py",
        "seminar": "assemble_seminar_report_gemini.py",
    }[variant]
    log(spec["label"], f"Step 4: {script}")
    r = subprocess.run([sys.executable, str(BASE / script)], cwd=str(BASE))
    if r.returncode != 0:
        raise RuntimeError(f"{script} failed (exit {r.returncode})")


def run_variant(variant: str, skip_fetch: bool, stats: dict | None = None) -> bool:
    spec = VARIANTS[variant]
    st = stats.setdefault(variant, {}) if stats is not None else {}
    # GitHub Actions (2026-10-29): a rerun restores today's reports first, so a
    # variant already made today is skipped before its ~10-25 min of fetching.
    if os.environ.get("REDDIT_SKIP_DONE") == "1":
        date_str = dt.datetime.now(JST).strftime("%Y-%m-%d")
        if glob.glob(str(REPORT_DIR / spec["dedup_glob"].format(date=date_str))):
            log(spec["label"], "today's report already exists — skipping (REDDIT_SKIP_DONE)")
            st["skipped"] = True
            return True
    try:
        data_file = CACHE / spec["data"]
        if not skip_fetch:
            run_fetch(variant)
        raw = json.loads(data_file.read_text(encoding="utf-8"))
        posts = raw["posts"]
        st["sources_total"] = raw.get("sources_total")
        st["sources_ok"] = raw.get("sources_ok")
        st["sources_rate_limited"] = raw.get("sources_rate_limited")
        st["posts_fetched"] = raw.get("total_fetched")

        posts = dedup_posts(variant, posts)
        if len(posts) < 3:
            log(spec["label"], "fewer than 3 new posts after dedup — skipping this variant")
            st["skipped"] = True
            return True
        followup = len(posts) < len(raw["posts"])
        st["followup"] = followup

        # Persist the deduped post list for the assembler (raw data keeps
        # the original order, but the assembler must zip with summaries).
        (CACHE / f"gemini_{variant}_posts.json").write_text(
            json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        if variant == "seminar":
            summaries = step2_seminar(posts)
        else:
            # 件数は各 config の top_n で決まる（fetch_data.py が切る）。AI 版は 30（2026-10-11）。
            # standard は note に載せてよい投稿が note_target 件たまったところで止まる（2026-10-12）
            posts, summaries = step2_summaries(variant, posts)
            (CACHE / f"gemini_{variant}_posts.json").write_text(
                json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        # 掲示板名で外した投稿は要約を頼んでいないので、失敗には数えない
        failed = sum(1 for s in summaries
                     if s.get("title_ja") == "(翻訳失敗)" and not s.get("skipped"))
        log(spec["label"], f"Step 2 done: {len(summaries)} summaries ({failed} failed)")
        st["summaries"] = len(summaries)
        st["summaries_failed"] = failed
        if failed == len(summaries):
            raise RuntimeError(f"all {failed} summaries failed — not publishing a broken report")

        if variant == "standard":
            # レポートと note には、note に載せてよい投稿だけを番号付きで並べる
            publishable = [s for s in summaries if is_note_ok(s)]
            st["note_articles"] = len(publishable)
            if not publishable:
                raise RuntimeError("no post is publishable on note — not publishing an empty report")
            step3_editorial(variant, publishable, followup)
            if not followup:
                step3_note_cover(publishable)
        else:
            step3_editorial(variant, summaries, followup)
        if variant == "ai":
            step3_x_cover(summaries)
        step4_assemble(variant)
        return True
    except Exception as e:
        log(spec["label"], f"ERROR: {type(e).__name__}: {e}")
        st["error"] = f"{type(e).__name__}: {e}"
        return False


# ---------------------------------------------------------------------------
# 健康チェック（2026-09-26 追加）
# ---------------------------------------------------------------------------

# 下限・上限は「これを割ったら人が見に来るべき」という値。2026-09-26 の実測
# （standard 2 本 25 件 / AI 12 本 216 件）と、429 で痩せていた頃の値（AI 3〜5 本
# 50〜75 件）の間に引いてある。過去との比較（ratio）は履歴が 5 回たまってから効く。
HEALTH_LIMITS = {
    # standard は 2026-10-12 に limit=25 → 100 へ増やした（note を 30 件にするため）。
    # 要約の失敗は r/all の成人向け投稿が安全フィルタで弾かれる分を見込んで 5 まで許す。
    # note の件数は目標 30。除外が多くて候補 40 件を使い切った朝に 25 を割ると警告になる。
    "standard": {"sources_floor": 2, "posts_floor": 60, "failed_ceiling": 5, "note_floor": 25},
    "ai": {"sources_floor": 10, "posts_floor": 90},
    "seminar": {"sources_floor": 8, "posts_floor": 70},
}
HEALTH_LABELS = {"standard": "標準", "ai": "AI", "seminar": "セミナー"}


def run_healthcheck(targets: list[str], stats: dict, ok: bool, elapsed: float) -> None:
    """1 回の実行の数値を記録し、痩せていれば警告ノートを書く。

    本番を止めないため、この関数自身の失敗は握り潰してログに残すだけにする。
    """
    try:
        from healthcheck import HealthCheck
    except ImportError:  # VPS への転送漏れで本番を止めない
        log("health", "WARNING: healthcheck.py が無いため健康チェックをしていません")
        return
    try:
        hc = HealthCheck("reddit", note_dir=REPORT_DIR, history_dir=BASE / "health")
        date_str = dt.datetime.now(JST).strftime("%Y-%m-%d")
        for v in targets:
            st = stats.get(v, {})
            name = HEALTH_LABELS.get(v, v)
            lim = HEALTH_LIMITS.get(v, {})
            if v == "standard" and st.get("note_pushed") is not None:
                # note.com への公開は GitHub 側で動く。届かなければその日の note は Mac の予備だけになる
                hc.metric("standard_note_pushed", 1 if st["note_pushed"] else 0, floor=1,
                          label=f"{name}: note 原稿を GitHub へ送れたか")
            if st.get("skipped"):
                hc.note(f"{name}レポートは、当日すでに同じ投稿を出していたため作っていません")
                continue
            if st.get("error"):
                hc.problem(f"{name}レポートの生成が途中で止まりました（{st['error']}）")
            total = st.get("sources_total")
            if st.get("sources_ok") is not None:
                hc.metric(
                    f"{v}_sources_ok", st["sources_ok"], of=total,
                    floor=lim.get("sources_floor"), label=f"{name}: 取得できた取得元",
                )
            if st.get("sources_rate_limited") is not None:
                hc.metric(
                    f"{v}_rate_limited", st["sources_rate_limited"], ceiling=2,
                    label=f"{name}: 回数制限で落ちた取得元",
                )
            if st.get("posts_fetched") is not None:
                hc.metric(
                    f"{v}_posts", st["posts_fetched"], floor=lim.get("posts_floor"),
                    ratio=0.6, label=f"{name}: 集めた投稿",
                )
            if st.get("summaries_failed") is not None:
                hc.metric(
                    f"{v}_summaries_failed", st["summaries_failed"],
                    ceiling=lim.get("failed_ceiling", 2),
                    label=f"{name}: 要約に失敗した件数",
                )
            # レポートが本当にファイルとして残ったかを見る。ここまでの工程が
            # 全部「成功」でも、書き出しに失敗していれば届いていない。
            pattern = str(REPORT_DIR / VARIANTS[v]["dedup_glob"].format(date=date_str))
            hc.metric(
                f"{v}_report_files", len(glob.glob(pattern)), floor=1,
                label=f"{name}: 今日のレポート",
            )
            if v == "standard" and not st.get("followup"):
                if st.get("note_articles") is not None:
                    hc.metric(
                        "standard_note_articles", st["note_articles"], floor=lim.get("note_floor"),
                        label=f"{name}: note に載せた投稿",
                    )
                for kind, suffix in (("note", "-note.md"), ("note_cover", "-cover.png")):
                    hc.metric(
                        f"standard_{kind}_files",
                        len(glob.glob(str(REPORT_DIR / f"{date_str}-{NOTE_FILE_STEM}{suffix}"))),
                        floor=1, label=f"{name}: 今日の note {'原稿' if kind == 'note' else '表紙'}",
                    )
        hc.metric("minutes", round(elapsed / 60, 1), ceiling=45, label="所要時間（分）")
        if not ok:
            hc.problem("3 種類のうち少なくとも 1 つが失敗しました（cron.log を参照）")
        log("health", hc.summary_line())
        note = hc.finish(extra={"targets": targets, "ok": ok})
        if note:
            log("health", f"警告ノートを書きました: {note.name}")
    except Exception as e:  # 健康チェックの不具合で配信を止めない
        log("health", f"WARNING: 健康チェックに失敗しました: {type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Reddit buzz reports via Gemini")
    ap.add_argument("target", nargs="?", default="all",
                    choices=["standard", "ai", "seminar", "all"])
    ap.add_argument("--skip-fetch", action="store_true",
                    help="reuse existing .cache/*_data.json (debug)")
    args = ap.parse_args()

    CACHE.mkdir(exist_ok=True)
    # Record the resolved model for the assemblers' generated_by field.
    model = resolve_model()
    (CACHE / "gemini_meta.json").write_text(
        json.dumps({"model": model}, ensure_ascii=False), encoding="utf-8"
    )
    log("gemini", f"using model: {model}")

    targets = ["standard", "ai", "seminar"] if args.target == "all" else [args.target]
    started = time.time()
    stats: dict[str, dict] = {}
    results = []
    for v in targets:
        results.append(run_variant(v, args.skip_fetch, stats))
        if v == "standard":
            # 先に送る: AI・セミナーの約 20 分を待たずに、6:10 ごろ GitHub が公開を始められる
            push_note_to_github(stats)
    ok = all(results)
    # On GitHub Actions a failed hand-over of the note manuscript must fail the
    # run, so the retry (or the clock) sends it again instead of nobody noticing.
    if os.environ.get("REDDIT_REQUIRE_PUSH") == "1" and stats.get("standard", {}).get("note_pushed") is False:
        log("gemini-std", "ERROR: note 原稿を GitHub へ渡せなかったので、この回を失敗にします")
        ok = False

    # Gemini の使用量を cron.log と usage/ の両方へ残す（2026-09-10 追加）
    log("usage", meter.summary_line())
    meter.flush(log_dir=BASE / "usage", run={"targets": targets, "ok": ok})

    # 「成功したが中身が痩せている」朝を見つける（2026-09-26 追加）
    run_healthcheck(targets, stats, ok, time.time() - started)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
