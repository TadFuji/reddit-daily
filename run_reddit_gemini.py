#!/usr/bin/env python
"""run_reddit_gemini.py — Reddit AI buzz report pipeline on Gemini.

Runs the AI-only pipeline (Google Search Grounding -> Gemini summaries &
editorial -> X cover & note cover -> AI report, X article, and note manuscript):

  Step 1   fetch_data.py -> .cache/ai_data.json
  Step 1.5 dedupe against today's earlier AI_*_gemini.md reports
  Step 2   one Gemini JSON call per post  -> .cache/gemini_ai_step2.json
  Step 3   one editorial Gemini JSON call -> .cache/gemini_ai_step3.json
  Step 3.5 X article cover (.cache/gemini_ai_cover.png) and note cover
           (.cache/gemini_ai_note_cover.png, when not followup)
  Step 4   assemble_ai_report_gemini.py -> REPORT_DIR

Usage:
    python run_reddit_gemini.py [ai|all] [--skip-fetch]
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
except ImportError:
    class _NullMeter:
        def __getattr__(self, _):
            return lambda *a, **k: None

        def summary_line(self):
            return "USAGE: usage_meter.py が無いため未計測"

    meter = _NullMeter()

BASE = pathlib.Path(__file__).resolve().parent
CACHE = BASE / ".cache"
WRITING_NORMS = BASE / "skills" / "japanese-tech-writing" / "SKILL.md"
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
# consistently by both the orchestrator and the assembler subprocess.
_load_env_file(ENV_FILE)

_DEFAULT_OUT = str((pathlib.Path("/Volumes/ROCKET_XTRM/Obsidian") if sys.platform == "darwin"
         else pathlib.Path.home() / "Obsidian") / "📰_情報配信" / "Reddit")
REPORT_DIR = pathlib.Path(os.environ.get("REDDIT_OUT_DIR", _DEFAULT_OUT))
os.environ["REDDIT_OUT_DIR"] = str(REPORT_DIR)

JST = dt.timezone(dt.timedelta(hours=9), name="JST")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-4-hydrogen")
GEMINI_THINKING_LEVEL = os.environ.get("GEMINI_THINKING_LEVEL", "MEDIUM").strip()
GEMINI_SERVICE_TIER = os.environ.get("GEMINI_SERVICE_TIER", "flex").strip().lower()
FLEX_TIMEOUT_MS = int(os.environ.get("REDDIT_FLEX_TIMEOUT_MS", "35000"))
FLEX_MAX_STREAK_FAILURES = 2
FLEX_MAX_TOTAL_FAILURES = 4
FLEX_SLOW_THRESHOLD_SECONDS = float(os.environ.get("REDDIT_FLEX_SLOW_SECONDS", "25"))
FLEX_MAX_SLOW_STREAK = 3
FLEX_MAX_SLOW_TOTAL = 6
FLEX_DEADLINE_SECONDS = int(os.environ.get("REDDIT_FLEX_DEADLINE_SECONDS", "1500"))
GEMINI_FALLBACK_CANDIDATES = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]
MAX_CALL_RETRIES = 3

VARIANTS = {
    "ai": {
        "config": "config_ai.json",
        "data": "ai_data.json",
        "dedup_glob": "AI_{date}_*_gemini.md",
        "x_glob": "📍 X_AI_{date}_*.md",
        "label": "gemini-ai",
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

_FLEX_STATE: dict[str, float | int | bool] = {
    "started_monotonic": time.monotonic(),
    "streak_failures": 0,
    "total_failures": 0,
    "slow_streak": 0,
    "slow_total": 0,
    "disabled": False,
}


def reset_flex_state() -> None:
    """Reset the Flex circuit breaker timer and failure counters."""
    _FLEX_STATE["started_monotonic"] = time.monotonic()
    _FLEX_STATE["streak_failures"] = 0
    _FLEX_STATE["total_failures"] = 0
    _FLEX_STATE["slow_streak"] = 0
    _FLEX_STATE["slow_total"] = 0
    _FLEX_STATE["disabled"] = False


def _should_use_flex() -> bool:
    if GEMINI_SERVICE_TIER != "flex" or _FLEX_STATE["disabled"]:
        return False
    if (time.monotonic() - float(_FLEX_STATE["started_monotonic"])) >= FLEX_DEADLINE_SECONDS:
        _FLEX_STATE["disabled"] = True
        log("gemini", "Flex 推論の許容経過時間を超えたため、残りの呼び出しを通常モード（Standard）へ切り替えます")
        return False
    return True


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
        use_flex = attempt == 1 and _should_use_flex()
        cfg_kwargs: dict = {
            "temperature": 0.3,
            "response_mime_type": "application/json",
        }
        if GEMINI_THINKING_LEVEL:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=GEMINI_THINKING_LEVEL
            )
        if use_flex:
            cfg_kwargs["service_tier"] = "flex"
            cfg_kwargs["http_options"] = types.HttpOptions(timeout=FLEX_TIMEOUT_MS)

        api_ok = False
        call_started = time.monotonic()
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            call_elapsed = time.monotonic() - call_started
            api_ok = True
            meter.record(model, resp, label=label, service_tier="flex" if use_flex else None)
            if use_flex:
                _FLEX_STATE["streak_failures"] = 0
                if call_elapsed > FLEX_SLOW_THRESHOLD_SECONDS:
                    _FLEX_STATE["slow_streak"] = int(_FLEX_STATE["slow_streak"]) + 1
                    _FLEX_STATE["slow_total"] = int(_FLEX_STATE["slow_total"]) + 1
                    if (
                        int(_FLEX_STATE["slow_streak"]) >= FLEX_MAX_SLOW_STREAK
                        or int(_FLEX_STATE["slow_total"]) >= FLEX_MAX_SLOW_TOTAL
                    ):
                        _FLEX_STATE["disabled"] = True
                        log(
                            label,
                            f"Flex 推論の応答遅延（>{FLEX_SLOW_THRESHOLD_SECONDS:.0f}秒）が続いたため、"
                            "残りの呼び出しを通常モード（Standard）へ切り替えます",
                        )
                else:
                    _FLEX_STATE["slow_streak"] = 0
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
            if use_flex and not api_ok:
                _FLEX_STATE["streak_failures"] = int(_FLEX_STATE["streak_failures"]) + 1
                _FLEX_STATE["total_failures"] = int(_FLEX_STATE["total_failures"]) + 1
                if (
                    int(_FLEX_STATE["streak_failures"]) >= FLEX_MAX_STREAK_FAILURES
                    or int(_FLEX_STATE["total_failures"]) >= FLEX_MAX_TOTAL_FAILURES
                ):
                    _FLEX_STATE["disabled"] = True
                    log(label, "Flex 推論の失敗が閾値に達したため、残りの呼び出しを通常モード（Standard）へ切り替えます")
            if attempt < MAX_CALL_RETRIES:
                time.sleep(1 if use_flex else 4 * attempt)
    return None


# ---------------------------------------------------------------------------
# Step 2 prompt
# ---------------------------------------------------------------------------

def writing_norms() -> str:
    """Load the writing norms verbatim, wrapped for this report's output shape."""
    try:
        text = WRITING_NORMS.read_text(encoding="utf-8")
    except OSError as e:
        log("norms", f"WARNING: 文章規範を読めません（{type(e).__name__}）: {WRITING_NORMS}")
        log("norms", "WARNING: 規範なしで生成します。文体が普段より落ちます")
        return ""
    if text.startswith("---"):
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

FETCH_PYTHON = BASE / ".venv" / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python"
)


def run_fetch(variant: str = "ai") -> pathlib.Path:
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
    """Step 1.5: drop posts already covered by today's earlier AI_*_gemini reports."""
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


NOTE_FILE_STEM = "Reddit-Top30"
NOTE_FIXED_HASHTAGS = "#毎日note #Reddit #AI #生成AI #海外ニュース #海外トレンド #海外の反応"


def step2_summaries(variant: str, posts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Summarise AI posts in feed order. Returns (posts, summaries), aligned one to one."""
    spec = VARIANTS[variant]
    norms = writing_norms()
    results = []
    for i, post in enumerate(posts):
        rule = WITH_COMMENT_RULE_AI if post.get("comments") else NO_COMMENT_RULE
        prompt = AI_PROMPT.format(
            comment_rule=rule,
            post_data=post_to_text(post),
            writing_norms=norms,
        )
        log(spec["label"], f"Step 2: post {i + 1}/{len(posts)}: {post['title'][:55]}")
        obj = gemini_json(prompt, spec["label"], cache_stem=f"gemini_{variant}_{i + 1}")
        if not obj or not obj.get("title_ja"):
            obj = dict(FAIL_ENTRY)
        results.append(obj)
    (CACHE / f"gemini_{variant}_step2.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return posts, results


def step3_editorial(variant: str, summaries: list[dict], followup: bool) -> None:
    """Generate common editorial fields plus X and note fields in a single Gemini call."""
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
    prompt = (
        f"You are a senior AI tech editor reviewing today's Reddit AI report ({n} articles with Japanese summaries)."
        " Produce a single JSON object with exactly these 9 fields:\n\n"
        "1. \"editors_note\" (string): A Japanese text of 150-200 characters starting with"
        f" 「今日のRedditは」. Describe today's overall Reddit AI mood and what made it unique. Be specific, not generic.{followup_note}\n\n"
        "2. \"star_picks\" (array of up to 3 integers): The article numbers (1-indexed)"
        " that are most interesting, surprising, or impactful.\n\n"
        "3. \"cross_refs\" (object or empty object): If any articles share a related topic,"
        " add an entry like {\"3\": \"→ #7 も参照\"}. Return {} if none are related.\n\n"
        "4. \"x_headline\" (string, at most 30 Japanese characters): the second half of today's"
        " X article title, naming the single most striking story of the day as a short Japanese"
        " news phrase. No date, no 「Reddit」, no emoji, no quotation marks.\n\n"
        "5. \"x_promotion\" (string, 80-120 Japanese characters, desu/masu): 2-3 sentences that"
        " introduce today's article to X readers and make them want to open it. Name one or two concrete"
        " stories. No URLs, no hashtags, no emoji, no line breaks.\n\n"
        "6. \"note_catch\" (string, 12-22 Japanese characters): the short Japanese phrase that follows"
        " 「｜」 in today's note.com title, naming the single most striking story concretely."
        " No date, no 「Reddit」, no emoji, no quotation marks.\n\n"
        "7. \"note_points\" (array of exactly 3 strings): today's overall trends and highlights,"
        " one Japanese sentence each (desu/masu), no markdown.\n\n"
        "8. \"note_extra_hashtags\" (array of exactly 3 strings): note.com hashtags specific to"
        " today's stories, concrete words people search for (proper nouns or topic names)."
        f" Do not repeat these fixed tags: {NOTE_FIXED_HASHTAGS}\n\n"
        "9. \"closing_comment\" (string, 1-2 Japanese sentences, desu/masu): a closing remark"
        " for the note article that reflects today's mix of stories.\n\n"
        "Output ONLY a valid JSON object with these 9 fields. No markdown, no summaries array, no explanation.\n\n"
        f"Here are the {n} article titles for your reference:\n" + titles_str
    )
    log(spec["label"], "Step 3: editorial review")
    obj = gemini_json(prompt, spec["label"], cache_stem=f"gemini_{variant}_step3") or {}
    obj["_followup"] = followup
    (CACHE / f"gemini_{variant}_step3.json").write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _cover_digest_text(summaries: list[dict], *, skip_failed_title: bool = False) -> str:
    """Build concise cover prompt text (title + 1st paragraph <= 200 chars) per post."""
    items: list[str] = []
    for d in summaries:
        title = (d.get("title_ja") or "").strip()
        if skip_failed_title and title == "(翻訳失敗)":
            continue
        summary = (d.get("summary") or "").replace("\r\n", "\n").strip()
        first_para = summary.split("\n\n", 1)[0].strip()[:200]
        if title and first_para:
            items.append(f"{title}\n{first_para}")
        elif title:
            items.append(title)
        elif first_para:
            items.append(first_para)
    return "\n\n".join(items)


def step3_x_cover(summaries: list[dict]) -> None:
    """Cover image for the AI X article. Failure leaves the article without a cover."""
    out = CACHE / "gemini_ai_cover.png"
    out.unlink(missing_ok=True)
    try:
        import x_cover
    except ImportError as e:
        log("gemini-ai", f"WARNING: x_cover.py を読めないためカバー画像なし: {e}")
        return
    text = _cover_digest_text(summaries, skip_failed_title=True)
    log("gemini-ai", "Step 3.5: X 記事のカバー画像")
    x_cover.make_cover(text, out)


def step3_note_cover(summaries: list[dict]) -> None:
    """Retro poster cover for the AI note article. Failure leaves note without a cover."""
    out = CACHE / "gemini_ai_note_cover.png"
    out.unlink(missing_ok=True)
    try:
        import note_cover
    except ImportError as e:
        log("gemini-ai", f"WARNING: note_cover.py を読めないため note の表紙なし: {e}")
        return
    text = _cover_digest_text(summaries, skip_failed_title=True)
    log("gemini-ai", "Step 3.5: note の表紙")
    note_cover.make_cover(text, out)


def push_note_to_github(stats: dict) -> None:
    """Send today's note manuscript and cover to GitHub for note.com posting."""
    if os.environ.get("NOTE_GITHUB_PUSH", "1") == "0":
        log("gemini-ai", "NOTE_GITHUB_PUSH=0 のため note 原稿を GitHub へ送りません")
        return
    try:
        import push_note_github
    except ImportError as e:
        log("gemini-ai", f"WARNING: push_note_github.py を読めません: {e}")
        stats.setdefault("ai", {})["note_pushed"] = False
        return
    date_str = dt.datetime.now(JST).strftime("%Y-%m-%d")
    ok, detail = push_note_github.push_note(REPORT_DIR, date_str)
    log("gemini-ai", ("note 原稿を GitHub へ送りました: " if ok else "WARNING: note 原稿を GitHub へ送れませんでした: ") + detail)
    stats.setdefault("ai", {})["note_pushed"] = ok


def step4_assemble(variant: str = "ai") -> None:
    spec = VARIANTS[variant]
    script = "assemble_ai_report_gemini.py"
    log(spec["label"], f"Step 4: {script}")
    r = subprocess.run([sys.executable, str(BASE / script)], cwd=str(BASE))
    if r.returncode != 0:
        raise RuntimeError(f"{script} failed (exit {r.returncode})")


def run_variant(variant: str, skip_fetch: bool, stats: dict | None = None) -> bool:
    spec = VARIANTS[variant]
    st = stats.setdefault(variant, {}) if stats is not None else {}
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
        st["sources_error"] = raw.get("sources_error")
        st["posts_fetched"] = raw.get("total_fetched")

        posts = dedup_posts(variant, posts)
        if len(posts) < 3:
            log(spec["label"], "fewer than 3 new posts after dedup — skipping this variant")
            st["skipped"] = True
            return True
        followup = len(posts) < len(raw["posts"])
        st["followup"] = followup

        (CACHE / f"gemini_{variant}_posts.json").write_text(
            json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        posts, summaries = step2_summaries(variant, posts)
        (CACHE / f"gemini_{variant}_posts.json").write_text(
            json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        failed = sum(1 for s in summaries if s.get("title_ja") == FAIL_ENTRY["title_ja"])
        log(spec["label"], f"Step 2 done: {len(summaries)} summaries ({failed} failed)")
        st["summaries"] = len(summaries)
        st["summaries_failed"] = failed
        if failed == len(summaries):
            raise RuntimeError(f"all {failed} summaries failed — not publishing a broken report")

        step3_editorial(variant, summaries, followup)
        step3_x_cover(summaries)
        if not followup:
            step3_note_cover(summaries)
        step4_assemble(variant)
        return True
    except Exception as e:
        log(spec["label"], f"ERROR: {type(e).__name__}: {e}")
        st["error"] = f"{type(e).__name__}: {e}"
        return False


# ---------------------------------------------------------------------------
# 健康チェック
# ---------------------------------------------------------------------------

HEALTH_LIMITS = {
    "ai": {"sources_floor": 10, "posts_floor": 10, "failed_ceiling": 2},
}
HEALTH_LABELS = {"ai": "AI"}
HEALTH_MINUTES_CEILING = 20


def run_healthcheck(targets: list[str], stats: dict, ok: bool, elapsed: float) -> None:
    """1 回の実行の数値を記録し、痩せていれば警告ノートを書く。"""
    try:
        from healthcheck import HealthCheck
    except ImportError:
        log("health", "WARNING: healthcheck.py が無いため健康チェックをしていません")
        return
    try:
        hc = HealthCheck(
            "reddit",
            note_dir=REPORT_DIR,
            history_dir=BASE / "health",
            where=" GitHub Actions（`TadFuji/reddit-daily` の Actions 実行記録、履歴保管は `TadFuji/news-outbox` の `reddit/health/`）",
        )
        date_str = dt.datetime.now(JST).strftime("%Y-%m-%d")
        for v in targets:
            st = stats.get(v, {})
            name = HEALTH_LABELS.get(v, v)
            lim = HEALTH_LIMITS.get(v, {})
            if st.get("note_pushed") is not None:
                hc.metric(
                    f"{v}_note_pushed", 1 if st["note_pushed"] else 0, floor=1,
                    label=f"{name}: note 原稿を GitHub へ送れたか",
                )
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
            if st.get("sources_error") is not None:
                hc.metric(
                    f"{v}_sources_error", st["sources_error"], ceiling=4,
                    label=f"{name}: 取得に失敗した取得元",
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
            pattern = str(REPORT_DIR / VARIANTS[v]["dedup_glob"].format(date=date_str))
            hc.metric(
                f"{v}_report_files", len(glob.glob(pattern)), floor=1,
                label=f"{name}: 今日のレポート",
            )
            x_pattern = str(REPORT_DIR / VARIANTS[v]["x_glob"].format(date=date_str))
            hc.metric(
                f"{v}_x_files", len(glob.glob(x_pattern)), floor=1,
                label=f"{name}: 今日の X 記事",
            )
            if not st.get("followup"):
                for kind, suffix in (("note", "-note.md"), ("note_cover", "-cover.png")):
                    hc.metric(
                        f"{v}_{kind}_files",
                        len(glob.glob(str(REPORT_DIR / f"{date_str}-{NOTE_FILE_STEM}{suffix}"))),
                        floor=1, label=f"{name}: 今日の note {'原稿' if kind == 'note' else '表紙'}",
                    )
        hc.metric("minutes", round(elapsed / 60, 1), ceiling=HEALTH_MINUTES_CEILING, label="所要時間（分）")
        if not ok:
            hc.problem("AI レポートの生成または受け渡しが失敗しました（GitHub Actions の実行記録を参照）")
        log("health", hc.summary_line())
        note = hc.finish(extra={"targets": targets, "ok": ok})
        if note:
            log("health", f"警告ノートを書きました: {note.name}")
    except Exception as e:
        log("health", f"WARNING: 健康チェックに失敗しました: {type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Reddit AI buzz reports via Gemini")
    ap.add_argument("target", nargs="?", default="ai", choices=["ai", "all"])
    ap.add_argument("--skip-fetch", action="store_true",
                    help="reuse existing .cache/ai_data.json (debug)")
    args = ap.parse_args()

    CACHE.mkdir(exist_ok=True)
    reset_flex_state()
    model = resolve_model()
    (CACHE / "gemini_meta.json").write_text(
        json.dumps({"model": model}, ensure_ascii=False), encoding="utf-8"
    )
    log(
        "gemini",
        f"using model: {model} (thinking_level={GEMINI_THINKING_LEVEL or 'default'}, "
        f"service_tier={GEMINI_SERVICE_TIER or 'standard'})",
    )

    targets = ["ai"]
    started = time.time()
    stats: dict[str, dict] = {}
    ok = run_variant("ai", args.skip_fetch, stats)
    push_note_to_github(stats)

    if os.environ.get("REDDIT_REQUIRE_PUSH") == "1" and stats.get("ai", {}).get("note_pushed") is False:
        log("gemini-ai", "ERROR: note 原稿を GitHub へ渡せなかったので、この回を失敗にします")
        ok = False

    log("usage", meter.summary_line())
    meter.flush(log_dir=BASE / "usage", run={"targets": targets, "ok": ok})

    run_healthcheck(targets, stats, ok, time.time() - started)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
