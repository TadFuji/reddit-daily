"""Offline checks for push_note_github.py (no network, no token).

    python3 test_push_note_github.py
"""
import base64
import os
import pathlib
import subprocess
import sys
import tempfile

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import push_note_github as P  # noqa: E402


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        check.failed = True


check.failed = False

# blob_sha matches git's own answer
d = pathlib.Path(tempfile.mkdtemp())
(d / "x.bin").write_bytes(b"hello\n\x00\xff")
git_sha = subprocess.run(["git", "hash-object", str(d / "x.bin")], capture_output=True, text=True).stdout.strip()
check("blob_sha equals git hash-object", P.blob_sha(b"hello\n\x00\xff") == git_sha)

report = pathlib.Path(tempfile.mkdtemp())
note = report / "2026-10-13-Reddit-Top30-note.md"
cover = report / "2026-10-13-Reddit-Top30-cover.png"
note.write_text("note body", encoding="utf-8")
cover.write_bytes(b"\x89PNG fake")
drop_tok = report / ".github-drop-token"
drop_tok.write_text("drop-tok\n", encoding="utf-8")
env = report / ".env"
env.write_text('OTHER=1\nexport GITHUB_TOKEN="go-tok"\n', encoding="utf-8")


class Fake:
    """Answers like the GitHub API. remote is {name: bytes}; script injects failures in order."""

    def __init__(self, remote=None, put=None, dispatch=None):
        self.remote = dict(remote or {})
        self.put = list(put or [])  # statuses to return before succeeding
        self.dispatch = list(dispatch or [])
        self.calls = []

    def __call__(self, method, url, token, body=None):
        if url == P.DISPATCH_API:
            self.calls.append(("DISPATCH", None, token, body))
            return (self.dispatch.pop(0) if self.dispatch else 204), {"message": "x"}
        name = url.split("/contents/")[1].split("?")[0]
        self.calls.append((method, name, token, body))
        if method == "GET":
            if name in self.remote:
                return 200, {"sha": P.blob_sha(self.remote[name])}
            return 404, {"message": "Not Found"}
        if self.put:
            return self.put.pop(0), {"message": "injected"}
        self.remote[name] = base64.b64decode(body["content"])
        return 201, {}


def run(fake, day="2026-10-13", drop=None, go=None):
    return P.push_note(report, day, str(drop or drop_tok), str(go or env), request=fake, wait_s=0)


f = Fake()
ok, detail = run(f)
order = [(c[0], c[1]) for c in f.calls if c[0] in ("PUT", "DISPATCH")]
check("new day: cover, note, then dispatch", ok and order == [("PUT", cover.name), ("PUT", note.name), ("DISPATCH", None)])
check("drop repo written with the drop token only",
      all(c[2] == "drop-tok" for c in f.calls if c[0] in ("GET", "PUT")))
check("dispatch uses the Actions token and passes the date",
      [c for c in f.calls if c[0] == "DISPATCH"][0][2:] == ("go-tok", {"ref": "main", "inputs": {"date": "2026-10-13"}}))
check("content round-trips", f.remote[note.name] == b"note body")

f2 = Fake(remote=f.remote)
ok, detail = run(f2)
check("same content: nothing written, still dispatched",
      ok and not [c for c in f2.calls if c[0] == "PUT"] and [c for c in f2.calls if c[0] == "DISPATCH"])

note.write_text("note body v2", encoding="utf-8")
f3 = Fake(remote=f.remote)
ok, detail = run(f3)
put_note = [c for c in f3.calls if c[0] == "PUT"]
check("changed note: updated with the remote sha", ok and len(put_note) == 1 and put_note[0][3].get("sha"))

f4 = Fake(put=[409, 502])
ok, detail = run(f4, day="2026-10-13")
check("transient 409 then 502 on the cover: retried and succeeded",
      ok and len([c for c in f4.calls if c[0] == "PUT" and c[1] == cover.name]) == 3)

f5 = Fake(put=[403])
ok, detail = run(f5)
check("no write permission: reported at once, no dispatch",
      not ok and "403" in detail and not [c for c in f5.calls if c[0] == "DISPATCH"]
      and len([c for c in f5.calls if c[0] == "PUT"]) == 1)

f6 = Fake(put=[502, 502, 502])
ok, detail = run(f6)
check("three server errors: gives up and reports", not ok and "502" in detail)

f7 = Fake(dispatch=[500, 204])
ok, detail = run(f7)
check("dispatch retried after a 500", ok and len([c for c in f7.calls if c[0] == "DISPATCH"]) == 2)

f8 = Fake(dispatch=[403])
ok, detail = run(f8)
check("dispatch refused: reported", not ok and "起動" in detail)

ok, detail = run(Fake(), day="2026-10-14")
check("no manuscript: reported", not ok and "原稿がありません" in detail)
ok, detail = run(Fake(), drop=report / "missing")
check("no drop token: reported", not ok and "受け渡し用" in detail)
ok, detail = run(Fake(), go=report / "missing.env")
check("no Actions token: reported", not ok and "起動の合図" in detail)

cover.unlink()
f9 = Fake()
ok, detail = run(f9)
check("no cover: note still sent", ok and [c[1] for c in f9.calls if c[0] == "PUT"] == [note.name])

# 2026-10-29: on GitHub Actions the clock (news-clock) starts reddit-note.yml.
note.write_text("note body", encoding="utf-8")
os.environ["NOTE_DISPATCH"] = "0"
f10 = Fake()
ok, detail = run(f10, go=report / "missing.env")
check("NOTE_DISPATCH=0: sent without an Actions token and without dispatch",
      ok and [c[1] for c in f10.calls if c[0] == "PUT"] == [note.name]
      and not [c for c in f10.calls if c[0] == "DISPATCH"])
del os.environ["NOTE_DISPATCH"]

if check.failed:
    sys.exit(1)
print("all passed")
