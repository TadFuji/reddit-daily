"""Offline tests for tools/outbox.py (a fake GitHub; no network)."""
import base64
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import outbox as O  # noqa: E402

DAY = "2026-10-30"
failed = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failed.append(name)


class Fake:
    def __init__(self):
        self.files = {}  # path -> bytes
        self.assets = {}  # name -> bytes
        self.release = None
        self.calls = []

    def __call__(self, method, url, token, body=None, data=None, content_type=None, accept=None):
        self.calls.append((method, url))
        if "/releases/tags/" in url:
            return (200, json.dumps(self.release).encode()) if self.release else (404, b"")
        if url.endswith("/releases") and method == "POST":
            self.release = {"id": 1, "upload_url": "https://uploads.github.com/repos/x/releases/1/assets{?name,label}", "assets": []}
            return 201, json.dumps(self.release).encode()
        if url.startswith("https://uploads.github.com/"):
            name = url.split("name=")[1]
            self.assets[name] = data
            self.release["assets"].append({"name": name})
            return 201, b"{}"
        path = O.urllib.parse.unquote(url.split("/contents/")[1])
        if method == "DELETE":
            return (200, b"{}") if self.files.pop(path, None) is not None else (404, b"")
        if method == "PUT":
            if path in self.files and "sha" not in body:
                return 422, b""
            self.files[path] = base64.b64decode(body["content"])
            return 201, b"{}"
        if path in self.files:
            if accept == "application/vnd.github.raw":
                return 200, self.files[path]
            return 200, json.dumps({"sha": "s", "content": base64.b64encode(self.files[path]).decode()}).encode()
        if path == f"reddit/{DAY}":
            names = sorted({p.split("/")[-1] for p in self.files if p.startswith(f"reddit/{DAY}/")})
            return (200, json.dumps([{"name": n} for n in names]).encode()) if names else (404, b"")
        return 404, b""


with tempfile.TemporaryDirectory() as t:
    out = pathlib.Path(t) / "out"
    out.mkdir()
    (out / f"{DAY}_0605_gemini.md").write_text("std", encoding="utf-8")
    (out / f"📍 X_AI_{DAY}_0625.md").write_text("x", encoding="utf-8")
    (out / f"📍 X_AI_{DAY}_0625_cover.png").write_bytes(b"\x89PNGx")
    (out / f"{DAY}-Reddit-Top30-cover.png").write_bytes(b"\x89PNGc")
    (out / "2026-10-29_0742_gemini.md").write_text("old day", encoding="utf-8")
    f = Fake()
    done = O.upload(DAY, out, "tok", request=f)
    check("text of the day goes to reddit/DAY/", f"reddit/{DAY}/{DAY}_0605_gemini.md" in f.files
          and f"reddit/{DAY}/📍 X_AI_{DAY}_0625.md" in f.files)
    check("another day's file is not sent", not any("2026-10-29" in p for p in f.files))
    check("images go to the release, not git", len(f.assets) == 2 and not any(p.endswith(".png") for p in f.files))
    idx = json.loads(f.files[f"reddit/{DAY}/assets.json"])
    check("assets.json maps hashed names back to real names",
          sorted(idx.values()) == sorted([f"📍 X_AI_{DAY}_0625_cover.png", f"{DAY}-Reddit-Top30-cover.png"]))
    n_calls = len(f.calls)
    done2 = O.upload(DAY, out, "tok", request=f)
    check("second upload writes nothing new", all(d.endswith("same") for d in done2) and len(f.assets) == 2)

    back = pathlib.Path(t) / "back"
    got = O.restore(DAY, back, "tok", request=f)
    check("restore brings back the .md files only", sorted(got) == sorted([f"{DAY}_0605_gemini.md", f"📍 X_AI_{DAY}_0625.md"])
          and (back / f"{DAY}_0605_gemini.md").read_text(encoding="utf-8") == "std")
    check("restore of an empty day is empty", O.restore("2026-11-01", back, "tok", request=Fake()) == [])
    check("belongs_to ignores hidden files", not O.belongs_to(f".{DAY}.md", DAY))

with tempfile.TemporaryDirectory() as t:
    out = pathlib.Path(t) / "out"
    out.mkdir()
    f = Fake()
    failed_note = f"reddit-failed-{DAY}.md"
    f.files[f"reddit/{DAY}/{failed_note}"] = b"failed earlier"
    f.files[f"reddit/{DAY}/{DAY}_0605_gemini.md"] = b"std"
    got = O.restore(DAY, out, "tok", request=f)
    check("a failure note from an earlier attempt is not restored", got == [f"{DAY}_0605_gemini.md"])
    O.upload(DAY, out, "tok", request=f)
    check("a later success removes the stale failure note", f"reddit/{DAY}/{failed_note}" not in f.files)
    (out / failed_note).write_text("failed again", encoding="utf-8")
    O.upload(DAY, out, "tok", request=f)
    check("a current failure note is kept", f.files.get(f"reddit/{DAY}/{failed_note}") == "failed again".encode())

    h = pathlib.Path(t) / "health"
    check("no health history yet", O.health_restore(DAY, h, "tok", request=f) == [])
    h.mkdir()
    (h / "health-2026-10.jsonl").write_text('{"a":1}\n', encoding="utf-8")
    O.health_upload(DAY, h, "tok", request=f)
    h2 = pathlib.Path(t) / "health2"
    check("health history round-trips", O.health_restore(DAY, h2, "tok", request=f) == ["health-2026-10.jsonl"]
          and (h2 / "health-2026-10.jsonl").read_text(encoding="utf-8") == '{"a":1}\n')

if failed:
    sys.exit(1)
print("all passed")
