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
        "standard": {
            "note_pushed": True,
            "sources_total": 1,
            "sources_ok": 1,
            "sources_rate_limited": 0,
            "posts_fetched": 100,
            "summaries_failed": 0,
            "note_articles": 30,
        },
        "ai": {
            "sources_total": 12,
            "sources_ok": 12,
            "sources_rate_limited": 0,
            "posts_fetched": 235,
            "summaries_failed": 0,
        },
        "seminar": {
            "sources_total": 10,
            "sources_ok": 10,
            "sources_rate_limited": 0,
            "posts_fetched": 134,
            "summaries_failed": 0,
        },
    }


def populate_reports(report_dir: pathlib.Path, date_str: str) -> None:
    for name in (
        f"{date_str}_0601_gemini.md",
        f"AI_{date_str}_0621_gemini.md",
        f"Seminar_{date_str}_0645_gemini.md",
        f"{date_str}-Reddit-Top30-note.md",
    ):
        (report_dir / name).write_text("ok", encoding="utf-8")
    (report_dir / f"{date_str}-Reddit-Top30-cover.png").write_bytes(b"\x89PNG")


def run_hc_in_dir(root: pathlib.Path, minutes: float, ok: bool = True) -> tuple[pathlib.Path | None, str, dict]:
    report_dir = root / "out"
    base_dir = root / "base"
    cache_dir = root / "cache"
    health_dir = base_dir / "health"
    report_dir.mkdir(parents=True, exist_ok=True)
    base_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    date_str = dt.datetime.now(R.JST).strftime("%Y-%m-%d")
    populate_reports(report_dir, date_str)
    env = {"HEALTHCHECK_DIR": str(health_dir), "HEALTHCHECK": "1", "HEALTHCHECK_NOTE": "1"}
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(R, "REPORT_DIR", report_dir), \
         mock.patch.object(R, "BASE", base_dir), \
         mock.patch.object(R, "CACHE", cache_dir):
        R.run_healthcheck(["standard", "ai", "seminar"], make_ok_stats(), ok, minutes * 60)
    note_path = report_dir / f"⚠️ 自動処理の異常-reddit-{date_str}.md"
    note_text = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
    jsonl_path = health_dir / f"health-{date_str[:7]}.jsonl"
    row = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[-1])
    return (note_path if note_path.exists() else None), note_text, row


def run_hc_case(minutes: float, ok: bool = True) -> tuple[pathlib.Path | None, str, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        return run_hc_in_dir(pathlib.Path(tmp), minutes, ok=ok)


# 1. Regression & boundary: 47.1m, 50.5m, and exact boundary 55.0m pass under 55m ceiling
for mins in (47.1, 50.5, 55.0):
    note_p, _, row = run_hc_case(mins, ok=True)
    minutes_metric = next(m for m in row["metrics"] if m["name"] == "minutes")
    check(
        f"{mins}m produces no warning note and ceiling is 55",
        note_p is None and row["problems"] == [] and minutes_metric["ceiling"] == 55,
    )

# 2. Above ceiling (55.1m) triggers warning note with GitHub Actions & news-outbox wording,
#    and a subsequent healthy run clears the warning note via _clear_note().
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    note_p, note_text, row = run_hc_in_dir(root, 55.1, ok=True)
    check(
        "55.1m triggers warning note with ceiling 55 message",
        note_p is not None and "所要時間（分）が 55.1（上限 55 を超えました）" in note_text,
    )
    check(
        "warning note points to GitHub Actions and news-outbox (no VPS/cron.log)",
        "VPS" not in note_text
        and "/root/reddit/cron.log" not in note_text
        and "GitHub Actions" in note_text
        and "TadFuji/reddit-daily" in note_text
        and "TadFuji/news-outbox" in note_text,
    )
    cleared_p, _, cleared_row = run_hc_in_dir(root, 45.0, ok=True)
    check(
        "subsequent healthy run clears stale warning note",
        cleared_p is None and cleared_row["problems"] == [],
    )

# 3. ok=False problem message references GitHub Actions instead of cron.log;
#    HealthCheck(where=None) keeps default behavior for sibling repos.
_, fail_note_text, fail_row = run_hc_case(40.0, ok=False)
check(
    "ok=False problem mentions GitHub Actions and not cron.log",
    any("GitHub Actions" in p and "cron.log" not in p for p in fail_row["problems"])
    and "cron.log" not in fail_note_text,
)
check(
    "HealthCheck default where remains unchanged when omitted",
    H.HealthCheck("reddit").where == "VPS の `/root/reddit/cron.log`",
)


# 4. gemini_json passes thinking_level="MEDIUM" by default, and omits it when empty
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

        class StubGenerateContentConfig:
            def __init__(self, temperature=None, response_mime_type=None, thinking_config=None):
                self.temperature = temperature
                self.response_mime_type = response_mime_type
                self.thinking_config = thinking_config

        fake_types.ThinkingConfig = StubThinkingConfig
        fake_types.GenerateContentConfig = StubGenerateContentConfig
        fake_genai.types = fake_types
        fake_google.genai = fake_genai
        with mock.patch.dict(
            sys.modules,
            {"google": fake_google, "google.genai": fake_genai, "google.genai.types": fake_types},
        ):
            yield True


class FakeModels:
    def __init__(self) -> None:
        self.configs = []

    def generate_content(self, **kw):
        self.configs.append(kw.get("config"))
        return SimpleNamespace(text='{"ok": true}')


class FakeClient:
    def __init__(self) -> None:
        self.models = FakeModels()


with genai_types_context():
    fake_default = FakeClient()
    with mock.patch.object(R, "gemini_client", return_value=fake_default), \
         mock.patch.object(R, "resolve_model", return_value="gemini-4-hydrogen"), \
         mock.patch.object(R, "meter", mock.Mock()):
        res = R.gemini_json("prompt", "test")
    cfg = fake_default.models.configs[0]
    lvl = getattr(getattr(cfg, "thinking_config", None), "thinking_level", None)
    lvl_val = getattr(lvl, "value", lvl)
    check("default GEMINI_THINKING_LEVEL is MEDIUM", R.GEMINI_THINKING_LEVEL == "MEDIUM")
    check(
        "gemini_json passes thinking_level=MEDIUM, response_mime_type, temperature",
        res == {"ok": True}
        and cfg.thinking_config is not None
        and lvl_val == "MEDIUM"
        and cfg.response_mime_type == "application/json"
        and cfg.temperature == 0.3,
    )

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

# 5. Environment variable parsing of GEMINI_THINKING_LEVEL (unset / whitespace / blank)
with tempfile.TemporaryDirectory() as tmp:
    out_override = str(pathlib.Path(tmp) / "out")
    for label, env_val, expected in (
        ("unset -> MEDIUM", None, "MEDIUM"),
        ("padded LOW -> LOW", "  LOW  ", "LOW"),
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
