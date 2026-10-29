"""Hand the day's Reddit files to the private news-outbox repo (and back).

  python tools/outbox.py restore DAY DIR   # put the day's .md files back into DIR
  python tools/outbox.py upload  DAY DIR   # send the day's files from DIR

Text files go to news-outbox as reddit/DAY/<name> (git keeps them small).
Images go to a release "reddit-DAY" (keeping MBs of PNG out of git history);
GitHub rewrites unusual asset names, so each image gets a plain hashed name
and reddit/DAY/assets.json maps it back to the real file name for the Mac.

The token comes from OUTBOX_TOKEN (Contents read/write on news-outbox).
Restoring first lets run_reddit_gemini.py see what today already made and
skip it, so a second run on the same day does not write a second set.
"""
import base64
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
REPO = os.environ.get("OUTBOX_REPO", "TadFuji/news-outbox")
# "reddit" in production; a trial run writes under "reddit-trial" so it never mixes in.
PREFIX = os.environ.get("OUTBOX_PREFIX", "reddit")
TEXT_SUFFIXES = {".md", ".json", ".jsonl"}
IMAGE_SUFFIXES = {".png", ".jpg"}


def _request(method, url, token, body=None, data=None, content_type="application/json", accept="application/vnd.github+json"):
    """Return (status, bytes). Retries 5xx and network errors three times."""
    payload = data if data is not None else (json.dumps(body).encode() if body is not None else None)
    for attempt in range(3):
        req = urllib.request.Request(url, data=payload, method=method, headers={
            "Authorization": f"Bearer {token}", "Accept": accept, "User-Agent": "reddit-daily",
            "X-GitHub-Api-Version": "2022-11-28", **({"Content-Type": content_type} if payload is not None else {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                return e.code, e.read()
        except OSError:
            if attempt == 2:
                return 0, b""
        time.sleep(5 * (attempt + 1))
    return 0, b""


def _path(*parts):
    return "/".join(urllib.parse.quote(p, safe="") for p in parts)


def belongs_to(name: str, day: str) -> bool:
    """Files of the day carry the date (2026-10-29_0742_gemini.md, 📍 X_AI_2026-10-29_0800.md ...)."""
    return day in name and not name.startswith(".")


def asset_name(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:16] + pathlib.Path(name).suffix


def restore(day: str, out: pathlib.Path, token: str, request=_request) -> list[str]:
    status, raw = request("GET", f"{API}/repos/{REPO}/contents/{_path(PREFIX, day)}", token)
    if status == 404:
        return []
    if status != 200:
        raise RuntimeError(f"news-outbox を読めません（HTTP {status}）")
    got = []
    out.mkdir(parents=True, exist_ok=True)
    for entry in json.loads(raw):
        name = entry["name"]
        if pathlib.Path(name).suffix != ".md" or (out / name).exists():
            continue
        s, body = request("GET", f"{API}/repos/{REPO}/contents/{_path(PREFIX, day, name)}", token,
                          accept="application/vnd.github.raw")
        if s != 200:
            raise RuntimeError(f"{name} を読めません（HTTP {s}）")
        (out / name).write_bytes(body)
        got.append(name)
    return got


def _put_text(day, name, data, token, request):
    url = f"{API}/repos/{REPO}/contents/{_path(PREFIX, day, name)}"
    s, raw = request("GET", url, token)
    sha = None
    if s == 200:
        remote = json.loads(raw)
        if base64.b64decode(remote.get("content", "")) == data:
            return "same"
        sha = remote["sha"]
    elif s != 404:
        raise RuntimeError(f"{name}: news-outbox を読めません（HTTP {s}）")
    body = {"message": f"{PREFIX} {day}: {name}", "content": base64.b64encode(data).decode()}
    if sha:
        body["sha"] = sha
    s, _ = request("PUT", url, token, body=body)
    if s not in (200, 201):
        raise RuntimeError(f"{name}: 書き込めません（HTTP {s}）")
    return "updated" if sha else "created"


def _release(day, token, request):
    tag = f"{PREFIX}-{day}"
    s, raw = request("GET", f"{API}/repos/{REPO}/releases/tags/{tag}", token)
    if s == 200:
        return json.loads(raw)
    if s != 404:
        raise RuntimeError(f"release を読めません（HTTP {s}）")
    s, raw = request("POST", f"{API}/repos/{REPO}/releases", token,
                     body={"tag_name": tag, "name": tag, "body": f"Reddit images for {day}", "target_commitish": "main"})
    if s not in (200, 201):
        raise RuntimeError(f"release を作れません（HTTP {s}）")
    return json.loads(raw)


def upload(day: str, src: pathlib.Path, token: str, request=_request) -> list[str]:
    files = sorted(p for p in src.iterdir() if p.is_file() and belongs_to(p.name, day))
    done, index = [], {}
    images = [p for p in files if p.suffix in IMAGE_SUFFIXES]
    if images:
        rel = _release(day, token, request)
        have = {a["name"] for a in rel.get("assets", [])}
        base = rel["upload_url"].split("{")[0]
        for p in images:
            a = asset_name(p.name)
            index[a] = p.name
            if a in have:
                done.append(f"{p.name}: same")
                continue
            ctype = "image/png" if p.suffix == ".png" else "image/jpeg"
            s, _ = request("POST", f"{base}?name={a}", token, data=p.read_bytes(), content_type=ctype)
            if s not in (200, 201):
                raise RuntimeError(f"{p.name}: 画像を送れません（HTTP {s}）")
            done.append(f"{p.name}: uploaded")
        merged = dict(index)
        s, raw = request("GET", f"{API}/repos/{REPO}/contents/{_path(PREFIX, day, 'assets.json')}", token,
                         accept="application/vnd.github.raw")
        if s == 200:
            merged = {**json.loads(raw), **index}
        done.append(f"assets.json: {_put_text(day, 'assets.json', json.dumps(merged, ensure_ascii=False, indent=1).encode(), token, request)}")
    for p in files:
        if p.suffix in TEXT_SUFFIXES:
            done.append(f"{p.name}: {_put_text(day, p.name, p.read_bytes(), token, request)}")
    return done


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in ("restore", "upload"):
        print(__doc__)
        return 2
    cmd, day, d = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
    token = os.environ.get("OUTBOX_TOKEN", "")
    if not token:
        print("ERROR: OUTBOX_TOKEN がありません")
        return 1
    try:
        result = restore(day, d, token) if cmd == "restore" else upload(day, d, token)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 1
    print(f"{cmd}: " + ("、".join(result) if result else "なし"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
