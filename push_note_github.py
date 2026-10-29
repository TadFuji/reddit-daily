"""Hand today's note manuscript and cover to GitHub so note.com posting needs no Mac (2026-10-12).

藤川さんの依頼で、Reddit の note 記事を Mac なしで公開するために足した。

1. VPS で作った `{日付}-Reddit-Top30-note.md` と `-cover.png` を、受け渡し専用の公開リポジトリ
   TadFuji/reddit-note-drop の直下へ置く（表紙が先、原稿が後）
2. TadFuji/Hacker-News の `.github/workflows/reddit-note.yml` を workflow_dispatch で起動する。
   あちらは受け渡しリポジトリを読み取り、Hacker News と同じログイン情報（Secret）で note.com へ公開する

トークンは 2 つで、役割を分けてある（独立レビューの指摘。VPS が乗っ取られても、投稿のプログラムや
note のログイン情報がある Hacker-News を書き換えられないようにするため）:
- 受け渡し用: `/root/reddit/.github-drop-token`（reddit-note-drop の Contents だけ書ける。藤川さんが作って置く）。
  `REDDIT_DROP_TOKEN_FILE` で別の場所を指せる
- 起動の合図: `/root/ai-news-trigger/.env` の GITHUB_TOKEN（Hacker-News の Actions だけ。もとから VPS にあるもの）。
  `REDDIT_GITHUB_ENV` で別のファイルを指せる
どちらの値もログに出さない。

何度呼んでもよい: 受け渡しリポジトリに同じ中身があれば書かない（git の blob の SHA で比べる）。起動の合図は毎回送る
（あちらは note.com の公開記事一覧を見て、公開済みなら何もしない）。一時的な失敗（409・5xx・通信断）は 3 回まで
試す。失敗しても例外を外へ出さず (成功したか, 説明) を返す。呼び出し側がログと健康チェックに残す。

    python3 push_note_github.py [YYYY-MM-DD]   # 手で送り直すとき（日付の既定は今日 JST）
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

DROP_REPO = "TadFuji/reddit-note-drop"
DROP_API = f"https://api.github.com/repos/{DROP_REPO}/contents/{{name}}"
DISPATCH_API = "https://api.github.com/repos/TadFuji/Hacker-News/actions/workflows/reddit-note.yml/dispatches"
BRANCH = "main"
NOTE_FILE_STEM = "Reddit-Top30"  # assemble_report_gemini.NOTE_FILE_STEM と同じ
DEFAULT_DROP_TOKEN_FILE = "/root/reddit/.github-drop-token"
DEFAULT_DISPATCH_ENV = "/root/ai-news-trigger/.env"
JST = dt.timezone(dt.timedelta(hours=9), name="JST")
TRIES = 3
RETRY_WAIT_S = 5.0


def blob_sha(data: bytes) -> str:
    """The SHA git (and the contents API) gives a file with these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def read_token_file(path: str) -> str:
    try:
        return pathlib.Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_env_token(env_path: str) -> str:
    try:
        for raw in pathlib.Path(env_path).read_text(encoding="utf-8").splitlines():
            line = raw.strip().removeprefix("export ").strip()
            if line.startswith("GITHUB_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _request(method: str, url: str, token: str, body: dict | None = None):
    """One HTTP call. Returns (status, json). status 0 means the network failed."""
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "reddit-note-push",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("message", "")
        except Exception:
            msg = ""
        return e.code, {"message": msg}
    except (urllib.error.URLError, OSError) as e:
        return 0, {"message": f"{type(e).__name__}: {e}"}


def _transient(status: int) -> bool:
    return status == 0 or status == 409 or status >= 500


def put_file(name: str, data: bytes, token: str, request=_request, wait_s: float = RETRY_WAIT_S) -> str:
    """Create or update <name> in the drop repo. Returns 'same', 'created' or 'updated'; raises on failure."""
    url = DROP_API.format(name=name)
    last = ""
    for attempt in range(1, TRIES + 1):
        status, got = request("GET", f"{url}?ref={BRANCH}", token)
        if status not in (200, 404):
            last = f"GitHub が {status} を返しました（確認）: {got.get('message', '')}"
            if _transient(status) and attempt < TRIES:
                time.sleep(wait_s * attempt)
                continue
            raise RuntimeError(last)
        remote_sha = got.get("sha") if status == 200 else None
        if remote_sha == blob_sha(data):
            return "same"
        body = {"message": f"Reddit note: {name}", "content": base64.b64encode(data).decode(), "branch": BRANCH}
        if remote_sha:
            body["sha"] = remote_sha
        status, got = request("PUT", url, token, body)
        if status in (200, 201):
            return "updated" if remote_sha else "created"
        last = f"GitHub が {status} を返しました（書き込み）: {got.get('message', '')}"
        if not (_transient(status) and attempt < TRIES):
            raise RuntimeError(last)
        time.sleep(wait_s * attempt)  # 409 = someone moved the branch: read the sha again
    raise RuntimeError(last)


def dispatch(date_str: str, token: str, request=_request, wait_s: float = RETRY_WAIT_S) -> None:
    """Start reddit-note.yml on Hacker-News for date_str. Raises on failure."""
    last = ""
    for attempt in range(1, TRIES + 1):
        status, got = request("POST", DISPATCH_API, token, {"ref": BRANCH, "inputs": {"date": date_str}})
        if status == 204:
            return
        last = f"公開処理の起動で GitHub が {status} を返しました: {got.get('message', '')}"
        # 起動は「受け付けた」だけでも 204。送り直しても公開済みの確認で止まるので、二重にはならない
        if not (_transient(status) and attempt < TRIES):
            break
        time.sleep(wait_s * attempt)
    raise RuntimeError(last)


def push_note(report_dir: pathlib.Path, date_str: str, drop_token_file: str | None = None,
              dispatch_env: str | None = None, request=_request, wait_s: float = RETRY_WAIT_S) -> tuple[bool, str]:
    """Send the day's cover then note to the drop repo, then start the posting workflow. Never raises."""
    try:
        note = report_dir / f"{date_str}-{NOTE_FILE_STEM}-note.md"
        cover = note.with_name(note.name.replace("-note.md", "-cover.png"))
        if not note.exists():
            return False, f"今日の note 原稿がありません: {note.name}"
        drop_token = read_token_file(drop_token_file or os.environ.get("REDDIT_DROP_TOKEN_FILE", DEFAULT_DROP_TOKEN_FILE))
        if not drop_token:
            return False, "受け渡し用の GitHub トークンがありません（/root/reddit/.github-drop-token）"
        # On GitHub Actions (2026-10-29) the news-clock Worker is the only one that
        # starts reddit-note.yml, so it can count its retries; skip the dispatch here.
        send_dispatch = os.environ.get("NOTE_DISPATCH", "1") != "0"
        go_token = ""
        if send_dispatch:
            go_token = read_env_token(dispatch_env or os.environ.get("REDDIT_GITHUB_ENV", DEFAULT_DISPATCH_ENV))
            if not go_token:
                return False, "起動の合図に使う GitHub トークンがありません（.env の GITHUB_TOKEN）"
        done = []
        # 表紙が先。公開処理は原稿があるときだけ動く
        if cover.exists():
            done.append(f"表紙 {put_file(cover.name, cover.read_bytes(), drop_token, request, wait_s)}")
        else:
            done.append("表紙なし")
        done.append(f"原稿 {put_file(note.name, note.read_bytes(), drop_token, request, wait_s)}")
        if send_dispatch:
            dispatch(date_str, go_token, request, wait_s)
            done.append("公開処理を起動")
        else:
            done.append("公開処理の起動は時刻係に任せる")
        return True, "、".join(done)
    except Exception as e:  # noqa: BLE001 - the report itself is already written
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    date_str = sys.argv[1] if len(sys.argv) > 1 else dt.datetime.now(JST).strftime("%Y-%m-%d")
    default_out = (pathlib.Path("/Volumes/ROCKET_XTRM/Obsidian") if sys.platform == "darwin"
         else pathlib.Path.home() / "Obsidian") / "📰_情報配信" / "Reddit"
    report_dir = pathlib.Path(os.environ.get("REDDIT_OUT_DIR", str(default_out)))
    ok, detail = push_note(report_dir, date_str)
    print(f"OK: note 原稿を GitHub へ渡しました（{detail}）" if ok else f"ERROR: {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
