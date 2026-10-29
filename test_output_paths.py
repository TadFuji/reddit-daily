"""Check every Reddit output entry point without loading real .env or writing the vault."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


BASE = Path(__file__).resolve().parent
ENTRYPOINTS = (
    "run_reddit_gemini.py", "assemble_report_gemini.py",
    "assemble_ai_report_gemini.py", "assemble_seminar_report_gemini.py",
    "push_note_github.py",
)


class OutputPathTests(unittest.TestCase):
    def test_platform_defaults_and_explicit_override(self):
        # Copies have no .env/cache. run_name avoids main(), all external APIs,
        # generation and report writes. The uploader's main has a fake push.
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            for name in (*ENTRYPOINTS, "usage_meter.py"):
                shutil.copy(BASE / name, work / name)
            for platform in ("darwin", "linux"):
                for override in (None, str(work / "test-vault" / "📰_情報配信" / "Reddit")):
                    expected = override or (
                        "/Volumes/ROCKET_XTRM/Obsidian/📰_情報配信/Reddit"
                        if platform == "darwin" else "/root/Obsidian/📰_情報配信/Reddit"
                    )
                    for name in ENTRYPOINTS:
                        with self.subTest(platform=platform, override=override, entrypoint=name):
                            env = dict(os.environ, USAGE_METER="0")
                            env.pop("REDDIT_OUT_DIR", None)
                            if override is not None:
                                env["REDDIT_OUT_DIR"] = override
                            script = """
import pathlib, runpy, sys
sys.platform = sys.argv[2]
if sys.platform == "linux":
    pathlib.Path.home = classmethod(lambda cls: pathlib.Path("/root"))
module = runpy.run_path(sys.argv[1], run_name="path_audit")
if "REPORT_DIR" in module:
    actual = str(module["REPORT_DIR"])
else:
    found = []
    def fake_push(report_dir, date_str):
        found.append(str(report_dir))
        return True, "offline stub"
    module["main"].__globals__["push_note"] = fake_push
    sys.argv = [sys.argv[1], "2026-10-13"]
    assert module["main"]() == 0
    actual = found[0]
print("PATH=" + actual)
"""
                            result = subprocess.run(
                                [sys.executable, "-c", script, str(work / name), platform],
                                cwd=work, env=env, text=True, capture_output=True,
                            )
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertIn("PATH=" + expected + "\n", result.stdout)
                            self.assertFalse((work / "test-vault").exists())


if __name__ == "__main__":
    unittest.main()
