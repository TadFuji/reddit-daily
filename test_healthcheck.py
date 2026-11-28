"""Offline checks for healthcheck thresholds, log wording, and thinking_level."""

import contextlib
import datetime as dt
import json
import os
import pathlib
import runpy
import sys
import tempfile
import types
from types import SimpleNamespace
from unittest import mock

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("USAGE_METER", "0")
import healthcheck as H  # noqa: E402
import run_reddit_gemini as R  # noqa: E402


def check(name: str, cond: bool) -> None:
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        check.failed = True


check.failed = False


def make_ok_stats() -> dict:
    return {
        "ai": {
            "note_pushed": True,
            "sources_total": 14,
            "sources_ok": 14,
            "sources_error": 0,
            "posts_fetched": 20,
            "summaries_failed": 0,
        },
    }


def populate_reports(report_dir: pathlib.Path, date_str: str) -> None:
    for name in (
        f"AI_{date_str}_0621_gemini.md",
        f"📍 X_AI_{date_str}_0621.md",
        f"{date_str}-Reddit-Top30-note.md",
    ):
        (report_dir / name).write_text("ok", encoding="utf-8")
    (report_dir / f"{date_str}-Reddit-Top30-cover.png").write_bytes(b"\x89PNG")


def run_hc_in_dir(
    root: pathlib.Path,
    minutes: float,
    ok: bool = True,
    stats: dict | None = None,
    skip_files: tuple[str, ...] = (),
) -> tuple[pathlib.Path | None, str, dict]:
    report_dir = root / "out"
    base_dir = root / "base"
    cache_dir = root / "cache"
    health_dir = base_dir / "health"
    report_dir.mkdir(parents=True, exist_ok=True)
    base_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    date_str = dt.datetime.now(R.JST).strftime("%Y-%m-%d")
    populate_reports(report_dir, date_str)
    for pattern in skip_files:
        for p in report_dir.glob(pattern.format(date=date_str)):
            p.unlink()
    env = {"HEALTHCHECK_DIR": str(health_dir), "HEALTHCHECK": "1", "HEALTHCHECK_NOTE": "1"}
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(R, "REPORT_DIR", report_dir), \
         mock.patch.object(R, "BASE", base_dir), \
         mock.patch.object(R, "CACHE", cache_dir):
        R.run_healthcheck(["ai"], stats if stats is not None else make_ok_stats(), ok, minutes * 60)
    note_path = report_dir / f"⚠️ 自動処理の異常-reddit-{date_str}.md"
    note_text = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
    jsonl_path = health_dir / f"health-{date_str[:7]}.jsonl"
    row = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[-1])
    return (note_path if note_path.exists() else None), note_text, row


def run_hc_case(
    minutes: float,
    ok: bool = True,
    stats: dict | None = None,
    skip_files: tuple[str, ...] = (),
) -> tuple[pathlib.Path | None, str, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        return run_hc_in_dir(pathlib.Path(tmp), minutes, ok=ok, stats=stats, skip_files=skip_files)


# 1. Regression & boundary: 12.0m, 18.5m, and exact boundary 25.0m pass under 25m ceiling
for mins in (12.0, 18.5, 25.0):
    note_p, _, row = run_hc_case(mins, ok=True)
    minutes_metric = next(m for m in row["metrics"] if m["name"] == "minutes")
    check(
        f"{mins}m produces no warning note and ceiling is 25",
        note_p is None and row["problems"] == [] and minutes_metric["ceiling"] == 25,
    )

# 2. Above ceiling (25.1m) triggers warning note with GitHub Actions & news-outbox wording,
#    and a subsequent healthy run clears the warning note via _clear_note().
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    note_p, note_text, row = run_hc_in_dir(root, 25.1, ok=True)
    check(
        "25.1m triggers warning note with ceiling 25 message",
        note_p is not None and "所要時間（分）が 25.1（上限 25 を超えました）" in note_text,
    )
    check(
        "warning note points to GitHub Actions and news-outbox (no VPS/cron.log)",
        "VPS" not in note_text
        and "/root/reddit/cron.log" not in note_text
        and "GitHub Actions" in note_text
        and "TadFuji/reddit-daily" in note_text
        and "TadFuji/news-outbox" in note_text,
    )
    cleared_p, _, cleared_row = run_hc_in_dir(root, 10.0, ok=True)
    check(
        "subsequent healthy run clears stale warning note",
        cleared_p is None and cleared_row["problems"] == [],
    )

# 3. AI-unified checks: X article, note, note cover, push failure, ok=False
_, fail_note_text, fail_row = run_hc_case(10.0, ok=False)
check(
    "ok=False problem mentions GitHub Actions and not cron.log or 3 variants",
    any("GitHub Actions" in p and "cron.log" not in p and "3 種類" not in p for p in fail_row["problems"])
    and "cron.log" not in fail_note_text,
)
missing_stats = make_ok_stats()
missing_stats["ai"]["note_pushed"] = False
_, _, missing_row = run_hc_case(
    10.0,
    ok=True,
    stats=missing_stats,
    skip_files=("📍 X_AI_{date}_*.md", "{date}-Reddit-Top30-note.md", "{date}-Reddit-Top30-cover.png"),
)
check(
    "healthcheck checks X article, note manuscript, note cover, and push status",
    any("X 記事" in p for p in missing_row["problems"])
    and any("note 原稿" in p for p in missing_row["problems"])
    and any("note 表紙" in p for p in missing_row["problems"])
    and any("GitHub へ送れたか" in p for p in missing_row["problems"]),
)
check(
    "HealthCheck default where remains unchanged when omitted",
    H.HealthCheck("reddit").where == "VPS の `/root/reddit/cron.log`",
)


# 4. gemini_json passes thinking_level="LOW" by default, and omits it when empty
@contextlib.contextmanager
def genai_types_context():
    try:
        from google.genai import types as real_types  # noqa: F401
        yield False
    except ImportError:
        fake_google = types.ModuleType("google")
        fake_genai = types.ModuleType("google.genai")
        fake_types = types.ModuleType("google.genai.types")

        class StubThinkingConfig:
            def __init__(self, thinking_level=None):
                self.thinking_level = thinking_level

        class StubHttpOptions:
            def __init__(self, timeout=None):
                self.timeout = timeout

        class StubGenerateContentConfig:
            def __init__(
                self,
                temperature=None,
                response_mime_type=None,
                thinking_config=None,
                service_tier=None,
                http_options=None,
            ):
                self.temperature = temperature
                self.response_mime_type = response_mime_type
                self.thinking_config = thinking_config
                self.service_tier = service_tier
                self.http_options = http_options

        fake_types.ThinkingConfig = StubThinkingConfig
        fake_types.HttpOptions = StubHttpOptions
        fake_types.GenerateContentConfig = StubGenerateContentConfig
        fake_genai.types = fake_types
        fake_google.genai = fake_genai
        with mock.patch.dict(
            sys.modules,
            {"google": fake_google, "google.genai": fake_genai, "google.genai.types": fake_types},
        ):
            yield True


class FakeModels:
    def __init__(self, fail_first_n: int = 0) -> None:
        self.configs = []
        self.fail_first_n = fail_first_n

    def generate_content(self, **kw):
        self.configs.append(kw.get("config"))
        if len(self.configs) <= self.fail_first_n:
            raise RuntimeError("503 Service Unavailable")
        return SimpleNamespace(
            text='{"ok": true}',
            usage_metadata=SimpleNamespace(
                prompt_token_count=1_000_000,
                cached_content_token_count=0,
                candidates_token_count=1_000_000,
                thoughts_token_count=0,
                tool_use_prompt_token_count=0,
                total_token_count=2_000_000,
            ),
        )


class FakeClient:
    def __init__(self, fail_first_n: int = 0) -> None:
        self.models = FakeModels(fail_first_n=fail_first_n)


with genai_types_context():
    import usage_meter as U

    R.reset_flex_state()
    fake_default = FakeClient()
    um_flex = U.UsageMeter()
    um_flex.enabled = True
    with mock.patch.object(R, "gemini_client", return_value=fake_default), \
         mock.patch.object(R, "resolve_model", return_value="gemini-4-hydrogen"), \
         mock.patch.object(R, "meter", um_flex):
        res = R.gemini_json("prompt", "test")
    cfg = fake_default.models.configs[0]
    lvl = getattr(getattr(cfg, "thinking_config", None), "thinking_level", None)
    lvl_val = getattr(lvl, "value", lvl)
    tier_val = getattr(getattr(cfg, "service_tier", None), "value", getattr(cfg, "service_tier", None))
    check("default GEMINI_THINKING_LEVEL is LOW", R.GEMINI_THINKING_LEVEL == "LOW")
    check("default GEMINI_EDITORIAL_THINKING_LEVEL is MEDIUM", R.GEMINI_EDITORIAL_THINKING_LEVEL == "MEDIUM")
    check("default GEMINI_SERVICE_TIER is flex", R.GEMINI_SERVICE_TIER == "flex")
    check(
        "gemini_json passes thinking_level=LOW, service_tier=flex, 150s http_options, and bills 50%",
        res == {"ok": True}
        and cfg.thinking_config is not None
        and lvl_val == "LOW"
        and tier_val == "flex"
        and R.FLEX_TIMEOUT_MS == 150000
        and R.FLEX_DEADLINE_SECONDS == 1500
        and getattr(cfg.http_options, "timeout", None) == R.FLEX_TIMEOUT_MS
        and cfg.response_mime_type == "application/json"
        and cfg.temperature == 0.3
        and abs(um_flex.totals()["usd"] - 4.5) < 1e-6,
    )

    R.reset_flex_state()
    fake_ed = FakeClient()
    with mock.patch.object(R, "gemini_client", return_value=fake_ed), \
         mock.patch.object(R, "resolve_model", return_value="gemini-4-hydrogen"), \
         mock.patch.object(R, "meter", mock.Mock()):
        res_ed = R.gemini_json("prompt", "test", thinking_level="MEDIUM", system_instruction="SYS")
    cfg_ed = fake_ed.models.configs[0]
    lvl_ed = getattr(getattr(cfg_ed, "thinking_config", None), "thinking_level", None)
    check(
        "gemini_json passes explicit thinking_level=MEDIUM and system_instruction",
        res_ed == {"ok": True}
        and getattr(lvl_ed, "value", lvl_ed) == "MEDIUM"
        and cfg_ed.system_instruction == "SYS",
    )

    R.reset_flex_state()
    fake_empty = FakeClient()
    with mock.patch.object(R, "GEMINI_THINKING_LEVEL", ""), \
         mock.patch.object(R, "gemini_client", return_value=fake_empty), \
         mock.patch.object(R, "resolve_model", return_value="gemini-4-hydrogen"), \
         mock.patch.object(R, "meter", mock.Mock()):
        res2 = R.gemini_json("prompt", "test")
    cfg2 = fake_empty.models.configs[0]
    check(
        "empty GEMINI_THINKING_LEVEL leaves thinking_config as None",
        res2 == {"ok": True}
        and cfg2.thinking_config is None
        and cfg2.response_mime_type == "application/json"
        and cfg2.temperature == 0.3,
    )

    # Flex 503 transient retry (2nd Flex try succeeds) + cooldown breaker & automatic recovery
    R.reset_flex_state()
    um_fb = U.UsageMeter()
    um_fb.enabled = True
    with mock.patch.object(R.time, "sleep") as mock_sleep, \
         mock.patch.object(R, "resolve_model", return_value="gemini-4-hydrogen"), \
         mock.patch.object(R, "FLEX_COOLDOWN_STREAK", 2), \
         mock.patch.object(R, "FLEX_COOLDOWN_CALLS", 1), \
         mock.patch.object(R, "meter", um_fb):
        # 1st attempt fails 503 -> 2nd Flex attempt succeeds (stays Flex, 50% billed)
        fake_tr = FakeClient(fail_first_n=1)
        with mock.patch.object(R, "gemini_client", return_value=fake_tr):
            res_tr = R.gemini_json("prompt", "test")
        # Two consecutive calls exhaust both Flex attempts -> Standard fallback & cooldown=1
        fake_ex1 = FakeClient(fail_first_n=2)
        with mock.patch.object(R, "gemini_client", return_value=fake_ex1):
            res_ex1 = R.gemini_json("prompt", "test")
        fake_ex2 = FakeClient(fail_first_n=2)
        with mock.patch.object(R, "gemini_client", return_value=fake_ex2):
            res_ex2 = R.gemini_json("prompt", "test")
        # Next call runs directly on Standard (consumes cooldown)
        fake_cd = FakeClient()
        with mock.patch.object(R, "gemini_client", return_value=fake_cd):
            res_cd = R.gemini_json("prompt", "test")
        # Following call automatically resumes Flex
        fake_rec = FakeClient()
        with mock.patch.object(R, "gemini_client", return_value=fake_rec):
            res_rec = R.gemini_json("prompt", "test")
    check(
        "Flex retries twice before Standard fallback, enters cooldown after streak, and recovers automatically",
        res_tr == {"ok": True}
        and res_ex1 == {"ok": True}
        and res_ex2 == {"ok": True}
        and res_cd == {"ok": True}
        and res_rec == {"ok": True}
        and getattr(fake_tr.models.configs[0], "service_tier", None) is not None
        and getattr(fake_tr.models.configs[1], "service_tier", None) is not None
        and getattr(fake_ex1.models.configs[0], "service_tier", None) is not None
        and getattr(fake_ex1.models.configs[1], "service_tier", None) is not None
        and getattr(fake_ex1.models.configs[2], "service_tier", None) is None
        and getattr(fake_ex1.models.configs[2], "http_options", None) is None
        and getattr(fake_cd.models.configs[0], "service_tier", None) is None
        and getattr(fake_rec.models.configs[0], "service_tier", None) is not None
        and R._FLEX_STATE["cooldown_remaining"] == 0
        and R._FLEX_STATE["disabled"] is False
        and len(mock_sleep.call_args_list) == 5
        and abs(um_fb.totals()["usd"] - 36.0) < 1e-6,
    )

    # Deadline circuit breaker
    R.reset_flex_state()
    R._FLEX_STATE["started_monotonic"] = R.time.monotonic() - (R.FLEX_DEADLINE_SECONDS + 5)
    fake_dl = FakeClient()
    with mock.patch.object(R, "gemini_client", return_value=fake_dl), \
         mock.patch.object(R, "resolve_model", return_value="gemini-4-hydrogen"), \
         mock.patch.object(R, "meter", mock.Mock()):
        res_dl = R.gemini_json("prompt", "test")
    check(
        "Deadline circuit breaker skips Flex and uses Standard",
        res_dl == {"ok": True}
        and R._FLEX_STATE["disabled"] is True
        and getattr(fake_dl.models.configs[0], "service_tier", None) is None,
    )

    # Default cooldown constants
    check(
        "Default FLEX_COOLDOWN_STREAK=3 and FLEX_COOLDOWN_CALLS=3",
        R.FLEX_COOLDOWN_STREAK == 3 and R.FLEX_COOLDOWN_CALLS == 3 and R.MAX_CALL_RETRIES == 4,
    )

    # GEMINI_SERVICE_TIER="standard" disables Flex from the start
    R.reset_flex_state()
    fake_std = FakeClient()
    with mock.patch.object(R, "GEMINI_SERVICE_TIER", "standard"), \
         mock.patch.object(R, "gemini_client", return_value=fake_std), \
         mock.patch.object(R, "resolve_model", return_value="gemini-4-hydrogen"), \
         mock.patch.object(R, "meter", mock.Mock()):
        res_std = R.gemini_json("prompt", "test")
    check(
        "GEMINI_SERVICE_TIER=standard calls Standard directly",
        res_std == {"ok": True} and getattr(fake_std.models.configs[0], "service_tier", None) is None,
    )

# 5. _cover_digest_text keeps title + first paragraph <= 200 chars and strips paragraphs 2..5
sample_summaries = [
    {
        "title_ja": "記事A",
        "summary": ("第一段落" * 60) + "\n\n第二段落（除去対象）\n\n第三段落\n\n第四段落\n\n第五段落",
    },
    {"title_ja": "(翻訳失敗)", "summary": "失敗要約"},
    {"title_ja": "記事B", "summary": "短い第一段落\n\n第二段落B"},
]
dig_ai = R._cover_digest_text(sample_summaries, skip_failed_title=True)
dig_note = R._cover_digest_text(sample_summaries, skip_failed_title=False)
check(
    "_cover_digest_text trims to title + <=200-char 1st paragraph and respects skip_failed_title",
    "第二段落" not in dig_ai
    and "第三段落" not in dig_ai
    and "(翻訳失敗)" not in dig_ai
    and "(翻訳失敗)" in dig_note
    and ("第一段落" * 50) in dig_ai
    and ("第一段落" * 51) not in dig_ai
    and "短い第一段落" in dig_ai,
)
many = [{"title_ja": f"見出し{i}", "summary": f"本文{i}"} for i in range(10)]
dig_capped = R._cover_digest_text(many)
check(
    "_cover_digest_text caps output at top 6 stories",
    "見出し5" in dig_capped and "見出し6" not in dig_capped,
)
um_1k = U.UsageMeter()
um_1k.enabled = True
um_1k.record_image("gemini-nano-banana-2.1", label="note-cover", image_size="1K")
check("UsageMeter bills 1K image as $0.0336", abs(um_1k.totals()["usd"] - 0.0336) < 1e-6)

# 6. Environment variable parsing of GEMINI_THINKING_LEVEL (unset / whitespace / blank)
with tempfile.TemporaryDirectory() as tmp:
    out_override = str(pathlib.Path(tmp) / "out")
    for label, env_val, expected in (
        ("unset -> LOW", None, "LOW"),
        ("padded MEDIUM -> MEDIUM", "  MEDIUM  ", "MEDIUM"),
        ("whitespace -> empty", "   ", ""),
    ):
        env_patch = {"USAGE_METER": "0", "REDDIT_OUT_DIR": out_override}
        clean_env = dict(os.environ, **env_patch)
        if env_val is None:
            clean_env.pop("GEMINI_THINKING_LEVEL", None)
        else:
            clean_env["GEMINI_THINKING_LEVEL"] = env_val
        with mock.patch.dict(os.environ, clean_env, clear=True):
            mod = runpy.run_path(str(BASE / "run_reddit_gemini.py"), run_name="hc_env_test")
        check(f"env GEMINI_THINKING_LEVEL ({label})", mod["GEMINI_THINKING_LEVEL"] == expected)

if not check.failed:
    print("all passed")
sys.exit(1 if check.failed else 0)
