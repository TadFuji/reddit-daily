"""Offline checks for fetch_data.py (Arctic Shift API edition).

    python3 test_fetch_data.py
"""
from __future__ import annotations

from datetime import datetime
import email.message
import http.client
import io
import json
import os
import pathlib
import sys
import tempfile
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request
import urllib.response

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("USAGE_METER", "0")
import fetch_data as F  # noqa: E402


def check(name: str, cond: bool) -> None:
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        check.failed = True


check.failed = False


def _make_json_response(payload: dict, url: str = "https://arctic-shift.photon-reddit.com/api", code: int = 200):
    raw = json.dumps(payload).encode("utf-8")
    hdrs = email.message.Message()
    hdrs["Content-Type"] = "application/json"
    return urllib.response.addinfourl(io.BytesIO(raw), hdrs, url, code=code)


# 1. config_ai.json matches expected structure
cfg = json.loads((BASE / "config_ai.json").read_text(encoding="utf-8"))
expected_subs = [
    "artificial", "singularity", "ChatGPT", "OpenAI", "ClaudeAI", "GeminiAI",
    "MachineLearning", "deeplearning", "LocalLLaMA", "ollama", "LangChain",
    "StableDiffusion", "comfyui", "cursor",
]
check(
    "config_ai.json has 14 subreddits and expected settings",
    cfg.get("subreddits") == expected_subs
    and cfg.get("top_n") == 20
    and cfg.get("max_per_subreddit") == 5
    and cfg.get("blocked_subreddits") == []
    and cfg.get("last_reviewed") == "2026-11-18",
)

# 2. normalize_reddit_url & _build_reddit_url boundaries
check(
    "normalize_reddit_url normalizes old.reddit.com and strips query",
    F.normalize_reddit_url("http://old.reddit.com/r/LocalLLaMA/comments/abc123/llama_update/?foo=1#bar")
    == ("https://www.reddit.com/r/LocalLLaMA/comments/abc123/llama_update/", "LocalLLaMA", "abc123"),
)
check(
    "normalize_reddit_url handles URL without slug",
    F.normalize_reddit_url("https://np.reddit.com/r/OpenAI/comments/99zz")
    == ("https://www.reddit.com/r/OpenAI/comments/99zz/", "OpenAI", "99zz"),
)
check(
    "normalize_reddit_url rejects URL without /comments/",
    F.normalize_reddit_url("https://www.reddit.com/r/artificial/hot/") is None,
)
check(
    "normalize_reddit_url rejects non-Reddit domains, invalid post IDs, and Markdown-unsafe slugs",
    F.normalize_reddit_url("https://reddit.com.evil.example/r/artificial/comments/123/x/") is None
    and F.normalize_reddit_url("https://arxiv.org/abs/2511.0001") is None
    and F.normalize_reddit_url("https://www.reddit.com/r/artificial/comments/bad_id/x/") is None
    and F.normalize_reddit_url("https://www.reddit.com/r/artificial/comments/abc123/bad)slug/") is None
    and F.normalize_reddit_url("https://www.reddit.com/r/artificial/comments/abc123/bad slug/") is None,
)
check(
    "_build_reddit_url rejects protocol-relative or mismatched permalinks and falls back cleanly",
    F._build_reddit_url({"permalink": "//evil.com/r/artificial/comments/p1/x/"}, "artificial", "p1")
    == ("https://www.reddit.com/r/artificial/comments/p1/", "artificial", "p1")
    and F._build_reddit_url({"permalink": "/r/artificial/comments/other99/x/"}, "artificial", "p1")
    == ("https://www.reddit.com/r/artificial/comments/p1/", "artificial", "p1")
    and F._build_reddit_url({"permalink": "/r/artificial/comments/p1/bad)slug/"}, "artificial", "p1")
    == ("https://www.reddit.com/r/artificial/comments/p1/", "artificial", "p1"),
)

# 3. _safe_int, _parse_created_utc, _is_valid_post
now_ts = datetime(2026, 11, 24, 6, 0, tzinfo=F.JST).timestamp()
after_ts = int(now_ts - 48 * 3600)

check(
    "_safe_int and _parse_created_utc handle bool, NaN, Inf, and non-numeric inputs safely",
    F._safe_int(True, -1) == -1
    and F._safe_int("bad", 0) == 0
    and F._safe_int(float("nan"), 0) == 0
    and F._safe_int(42) == 42
    and F._parse_created_utc(float("nan")) is None
    and F._parse_created_utc(float("inf")) is None
    and F._parse_created_utc(True) is None
    and F._parse_created_utc(-5) is None
    and F._parse_created_utc(now_ts) == now_ts,
)

valid_post = {
    "id": "1y3s3m3",
    "title": "Valid AI Post",
    "selftext": "Useful technical post body",
    "created_utc": now_ts - 3600,
}
check("_is_valid_post keeps valid post within window", F._is_valid_post(valid_post, after_ts=after_ts))
check(
    "_is_valid_post rejects stickied, NSFW, removed, deleted, invalid id, and stale posts",
    not F._is_valid_post({**valid_post, "stickied": True}, after_ts=after_ts)
    and not F._is_valid_post({**valid_post, "over_18": True}, after_ts=after_ts)
    and not F._is_valid_post({**valid_post, "removed_by_category": "moderator"}, after_ts=after_ts)
    and not F._is_valid_post({**valid_post, "selftext": "[removed]"}, after_ts=after_ts)
    and not F._is_valid_post({**valid_post, "selftext": "[deleted]"}, after_ts=after_ts)
    and not F._is_valid_post({**valid_post, "title": "[ Removed by moderator ]"}, after_ts=after_ts)
    and not F._is_valid_post({**valid_post, "title": "[deleted]"}, after_ts=after_ts)
    and not F._is_valid_post({**valid_post, "id": "bad_id!"}, after_ts=after_ts)
    and not F._is_valid_post({**valid_post, "created_utc": after_ts - 10}, after_ts=after_ts)
    and not F._is_valid_post({**valid_post, "created_utc": float("nan")}, after_ts=after_ts),
)

# 4. _http_get_json safety: size cap, non-dict, JSONDecodeError, IncompleteRead retry, redirect block
class _FakeOpener:
    def __init__(self, handler):
        self._handler = handler

    def open(self, req, timeout=20):
        return self._handler(req, timeout)


def _run_http_case(handler, retries=2):
    with mock.patch("urllib.request.build_opener", return_value=_FakeOpener(handler)), \
         mock.patch("time.sleep", return_value=None):
        return F._http_get_json("https://arctic-shift.photon-reddit.com/api/posts/search?subreddit=test", retries=retries)


# 4a. IncompleteRead retries and recovers
inc_calls = {"n": 0}


class _IncompleteOnceResp:
    def __init__(self, fail: bool):
        self._fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n=-1):
        if self._fail:
            raise http.client.IncompleteRead(b'{"partial":')
        return b'{"data": []}'


def _incomplete_then_ok(req, timeout):
    inc_calls["n"] += 1
    return _IncompleteOnceResp(inc_calls["n"] == 1)


check(
    "_http_get_json retries http.client.IncompleteRead and succeeds",
    _run_http_case(_incomplete_then_ok, retries=2) == {"data": []} and inc_calls["n"] == 2,
)

# 4b. Oversized response fails fast
oversized_err = None
try:
    with mock.patch.object(F, "MAX_RESPONSE_BYTES", 10):
        _run_http_case(lambda req, t: _make_json_response({"data": [1, 2, 3, 4, 5]}), retries=2)
except ValueError as e:
    oversized_err = str(e)
check("_http_get_json rejects responses exceeding MAX_RESPONSE_BYTES", bool(oversized_err and "exceeded" in oversized_err))

# 4c. Non-dict JSON fails
non_dict_err = None
try:
    _run_http_case(
        lambda req, t: urllib.response.addinfourl(io.BytesIO(b"[1, 2]"), email.message.Message(), req.full_url, code=200),
        retries=1,
    )
except ValueError as e:
    non_dict_err = str(e)
check("_http_get_json rejects non-object JSON payload", bool(non_dict_err and "JSON object" in non_dict_err))

# 4d. Redirect handler refuses 302 redirect
redirect_blocked = False
try:
    F._NoRedirectHandler().redirect_request(
        urllib.request.Request("https://arctic-shift.photon-reddit.com/api"),
        None,
        302,
        "Found",
        {},
        "https://evil.example/",
    )
except urllib.error.URLError:
    redirect_blocked = True
check("_NoRedirectHandler refuses HTTP redirects", redirect_blocked)

# 5. fetch_subreddit_posts: UA header, query params, 422 retry, pagination, engagement rank, caps
seen_requests: list[tuple[str, str]] = []
attempt_counts: dict[str, int] = {}


def fake_posts_open(self, req, timeout=20):
    seen_requests.append((req.full_url, req.get_header("User-agent") or ""))
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
    sub = qs["subreddit"][0]
    attempt_counts[sub] = attempt_counts.get(sub, 0) + 1
    if sub == "artificial" and attempt_counts[sub] == 1:
        raise urllib.error.HTTPError(
            req.full_url,
            422,
            "Unprocessable Entity",
            {},
            io.BytesIO(b'{"data":null,"error":"Timeout. Maybe slow down a bit"}'),
        )
    if sub == "broken":
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"busy"))
    if sub == "busy":
        if "before" not in qs:
            # Return full page (2 items when ARCTIC_SHIFT_PAGE_LIMIT=2) to trigger page 2
            return _make_json_response({
                "data": [
                    {"id": "b1", "title": "Recent low", "subreddit": "busy", "permalink": "/r/busy/comments/b1/recent/",
                     "created_utc": now_ts - 1000, "score": 5, "num_comments": 1},
                    {"id": "b2", "title": "Recent mid", "subreddit": "busy", "permalink": "/r/busy/comments/b2/mid/",
                     "created_utc": now_ts - 2000, "score": 10, "num_comments": 2},
                ]
            })
        return _make_json_response({
            "data": [
                {"id": "b3", "title": "Older viral post", "subreddit": "busy", "permalink": "/r/busy/comments/b3/viral/",
                 "created_utc": now_ts - 10000, "score": 900, "num_comments": 100},
            ]
        })
    return _make_json_response({
        "data": [
            {
                "id": "p1",
                "title": "Low engagement",
                "subreddit": "artificial",
                "author": "u1",
                "permalink": "/r/artificial/comments/p1/low_engagement/?utm=1",
                "selftext": "body 1",
                "created_utc": now_ts - 7200,
                "score": 10,
                "num_comments": 2,
            },
            {
                "id": "p2",
                "title": "High engagement",
                "subreddit": "artificial",
                "author": "u2",
                "permalink": "/r/artificial/comments/p2/high_engagement/",
                "selftext": "body 2",
                "created_utc": now_ts - 3600,
                "score": 50,
                "num_comments": 30,
            },
            {
                "id": "p3",
                "title": "Medium engagement",
                "subreddit": "artificial",
                "author": "u3",
                "permalink": "/r/artificial/comments/p3/medium_engagement/",
                "selftext": "body 3",
                "created_utc": now_ts - 1800,
                "score": 25,
                "num_comments": 10,
            },
            {
                "id": "p4",
                "title": "Bad score string",
                "subreddit": "artificial",
                "author": "u4",
                "selftext": "still safe",
                "created_utc": now_ts - 1200,
                "score": "not-a-number",
                "num_comments": None,
            },
            {
                "id": "p5",
                "title": "Removed post",
                "subreddit": "artificial",
                "author": "u5",
                "selftext": "[removed]",
                "created_utc": now_ts - 1000,
                "score": 999,
                "num_comments": 999,
            },
        ]
    })


with mock.patch.object(urllib.request, "build_opener", return_value=_FakeOpener(lambda r, t: fake_posts_open(None, r, t))), \
     mock.patch("time.sleep", return_value=None):
    posts_art, ok_art = F.fetch_subreddit_posts("artificial", after_ts, now_ts, max_items=2)
    posts_bad, ok_bad = F.fetch_subreddit_posts("broken", after_ts, now_ts, max_items=5)
    posts_blk, ok_blk = F.fetch_subreddit_posts("artificial", after_ts, now_ts, max_items=5, blocked={"Artificial"})
    with mock.patch.object(F, "ARCTIC_SHIFT_PAGE_LIMIT", 2):
        posts_busy, ok_busy = F.fetch_subreddit_posts("busy", after_ts, now_ts, max_items=2)

first_qs = urllib.parse.parse_qs(urllib.parse.urlparse(seen_requests[0][0]).query)
check(
    "fetch_subreddit_posts sends User-Agent, limit=100, retries HTTP 422, ranks & caps",
    ok_art
    and attempt_counts["artificial"] == 2
    and seen_requests[0][1] == F.USER_AGENT
    and first_qs["subreddit"] == ["artificial"]
    and first_qs["limit"] == ["100"]
    and first_qs["sort"] == ["desc"]
    and [p["id"] for p in posts_art] == ["p2", "p3"]
    and posts_art[0]["buzz_score"] == 110
    and posts_art[0]["ups"] == 50
    and posts_art[0]["num_comments"] == 30
    and posts_art[0]["url"] == "https://www.reddit.com/r/artificial/comments/p2/high_engagement/",
)
check(
    "fetch_subreddit_posts paginates page 2 when page 1 hits ARCTIC_SHIFT_PAGE_LIMIT",
    ok_busy and [p["id"] for p in posts_busy] == ["b3", "b2"],
)
check(
    "fetch_subreddit_posts returns ([], False) on persistent errors or blocked subreddit",
    (not ok_bad and posts_bad == []) and (not ok_blk and posts_blk == []),
)

# 6. merge_round_robin: round-robin diversity, dedup, top_n cap, buzz_score==0 preserved, engagement sort
per_source = [
    [
        {"id": "a1", "title": "First AI Post!", "subreddit": "artificial", "author": "u1",
         "url": "https://www.reddit.com/r/artificial/comments/a1/first/", "selftext": "", "age_hours": 1.0,
         "published": "", "ups": 0, "num_comments": 0, "comments": [], "buzz_score": 0},
        {"id": "a2", "title": "First AI Post", "subreddit": "Artificial", "author": "u2",
         "url": "https://www.reddit.com/r/artificial/comments/a2/dup_title/", "selftext": "", "age_hours": 2.0,
         "published": "", "ups": 500, "num_comments": 100, "comments": [], "buzz_score": 700},
        {"id": "a3", "title": "Second Artificial", "subreddit": "artificial", "author": "",
         "url": "https://www.reddit.com/r/artificial/comments/a3/second/", "selftext": "", "age_hours": 3.0,
         "published": "", "ups": 10, "num_comments": 1, "comments": [], "buzz_score": 12},
    ],
    [
        {"id": "a1", "title": "Crosspost same ID", "subreddit": "OpenAI", "author": "",
         "url": "https://www.reddit.com/r/OpenAI/comments/a1/cross/", "selftext": "", "age_hours": 1.0,
         "published": "", "ups": 99, "num_comments": 10, "comments": [], "buzz_score": 119},
        {"id": "o1", "title": "First AI Post", "subreddit": "OpenAI", "author": "",
         "url": "https://www.reddit.com/r/OpenAI/comments/o1/first/", "selftext": "", "age_hours": 1.5,
         "published": "", "ups": 200, "num_comments": 50, "comments": [], "buzz_score": 300},
    ],
]
all_merged, top_merged, counts = F.merge_round_robin(["artificial", "OpenAI"], per_source, top_n=2)
check(
    "merge_round_robin deduplicates, preserves buzz_score=0, caps top_n, and sorts selected by buzz_score desc",
    [p["id"] for p in all_merged] == ["a1", "o1", "a3"]
    and all_merged[0]["buzz_score"] == 0
    and [p["id"] for p in top_merged] == ["o1", "a1"]
    and counts == {"artificial": 3, "OpenAI": 2},
)

# 7. fetch_top_comments: filtering, sorting, capping, and graceful fallback
seen_comment_urls: list[str] = []


def fake_comments_open(req, timeout=20):
    seen_comment_urls.append(req.full_url)
    if "t3_err1" in req.full_url:
        raise urllib.error.URLError("network down")
    return _make_json_response({
        "data": [
            {"author": "AutoModerator", "body": "Please follow subreddit rules carefully!", "score": 999, "created_utc": 100},
            {"author": "[deleted]", "body": "Deleted user comment long enough", "score": 500, "created_utc": 100},
            {"author": "mod1", "distinguished": "moderator", "body": "Moderator sticky notice long enough", "score": 400, "created_utc": 100},
            {"author": "u0", "stickied": True, "body": "Stickied comment long enough here", "score": 300, "created_utc": 100},
            {"author": "u1", "body": "[removed]", "score": 200, "created_utc": 100},
            {"author": "u2", "body": "Too short", "score": 200, "created_utc": 100},
            {"author": "u3", "body": "Lower score technical insight comment", "score": 12, "created_utc": 100},
            {"author": "u4", "body": "Highest score\nmultiline   insight comment " + ("x" * 260), "score": 88, "created_utc": 101},
            {"author": "u5", "body": "Second highest comment with good detail", "score": "45", "created_utc": 102},
        ]
    })


with mock.patch.object(urllib.request, "build_opener", return_value=_FakeOpener(fake_comments_open)), \
     mock.patch("time.sleep", return_value=None):
    top_comms = F.fetch_top_comments("abc123", max_comments=2)
    err_comms = F.fetch_top_comments("err1", max_comments=6)

comment_qs = urllib.parse.parse_qs(urllib.parse.urlparse(seen_comment_urls[0]).query)
check(
    "fetch_top_comments queries link_id=t3_<id> with limit=100, filters noise, sorts desc, caps & truncates",
    comment_qs["link_id"] == ["t3_abc123"]
    and comment_qs["limit"] == ["100"]
    and len(top_comms) == 2
    and top_comms[0].startswith("Highest score multiline insight comment ")
    and len(top_comms[0]) == 250
    and top_comms[1] == "Second highest comment with good detail"
    and err_comms == [],
)

# 8. End-to-end main() integration test mocking only HTTP transport (build_opener)
with tempfile.TemporaryDirectory() as tmp:
    tmp_dir = pathlib.Path(tmp)
    cfg_path = tmp_dir / "cfg.json"
    cfg_path.write_text(json.dumps({
        "subreddits": ["artificial", "OpenAI", "ClaudeAI"],
        "top_n": 2,
        "max_per_subreddit": 5,
        "blocked_subreddits": [],
    }), encoding="utf-8")

    # 8a. < 3 posts exits 1 without writing file
    out_too_few = tmp_dir / "few.json"

    def e2e_few_handler(req, timeout):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        if qs.get("subreddit") == ["artificial"]:
            now_epoch = datetime.now(F.JST).timestamp()
            return _make_json_response({
                "data": [
                    {"id": "p1", "title": "Only 1", "subreddit": "artificial", "created_utc": now_epoch - 3600, "score": 10, "num_comments": 2},
                    {"id": "p2", "title": "Only 2", "subreddit": "artificial", "created_utc": now_epoch - 7200, "score": 5, "num_comments": 1},
                ]
            })
        return _make_json_response({"data": []})

    exit_code = None
    with mock.patch.object(F, "REQUEST_INTERVAL", 0), \
         mock.patch.object(F, "COMMENT_INTERVAL", 0), \
         mock.patch.object(urllib.request, "build_opener", return_value=_FakeOpener(e2e_few_handler)), \
         mock.patch.object(sys, "argv", ["fetch_data.py", "--config", str(cfg_path), "--output", str(out_too_few)]):
        try:
            F.main()
        except SystemExit as e:
            exit_code = e.code
    check("main exits 1 when fewer than 3 posts collected", exit_code == 1 and not out_too_few.exists())

    # 8b. >= 3 posts full pipeline through posts/search -> merge -> comments/search -> atomic json
    out_ok = tmp_dir / "ai_data.json"

    def e2e_ok_handler(req, timeout):
        parsed = urllib.parse.urlparse(req.full_url)
        qs = urllib.parse.parse_qs(parsed.query)
        now_epoch = datetime.now(F.JST).timestamp()
        if parsed.path.endswith("/posts/search"):
            sub = qs["subreddit"][0]
            if sub == "artificial":
                return _make_json_response({
                    "data": [
                        {"id": "p1", "title": "Art 1", "subreddit": "artificial", "author": "a",
                         "permalink": "/r/artificial/comments/p1/art_1/", "selftext": "body1",
                         "created_utc": now_epoch - 3600, "score": 100, "num_comments": 20},
                        {"id": "p2", "title": "Art 2", "subreddit": "artificial", "author": "b",
                         "permalink": "/r/artificial/comments/p2/art_2/", "selftext": "body2",
                         "created_utc": now_epoch - 7200, "score": 20, "num_comments": 5},
                    ]
                })
            if sub == "OpenAI":
                return _make_json_response({
                    "data": [
                        {"id": "p3", "title": "OAI 1", "subreddit": "OpenAI", "author": "c",
                         "permalink": "/r/OpenAI/comments/p3/oai_1/", "selftext": "body3",
                         "created_utc": now_epoch - 5400, "score": 300, "num_comments": 50},
                    ]
                })
            raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(b"busy"))
        if parsed.path.endswith("/comments/search"):
            lid = qs["link_id"][0].removeprefix("t3_")
            return _make_json_response({
                "data": [
                    {"author": "dev1", "body": f"Useful technical comment on {lid} here!", "score": 42, "created_utc": now_epoch - 1000},
                ]
            })
        raise AssertionError(f"unexpected URL: {req.full_url}")

    with mock.patch.object(F, "REQUEST_INTERVAL", 0), \
         mock.patch.object(F, "COMMENT_INTERVAL", 0), \
         mock.patch.object(F, "MAX_POST_RETRIES", 1), \
         mock.patch.object(F, "MAX_COMMENT_RETRIES", 1), \
         mock.patch.object(urllib.request, "build_opener", return_value=_FakeOpener(e2e_ok_handler)), \
         mock.patch.object(sys, "argv", ["fetch_data.py", "--config", str(cfg_path), "--output", str(out_ok)]):
        F.main()

    data = json.loads(out_ok.read_text(encoding="utf-8"))
    required_post_keys = {
        "id", "title", "subreddit", "author", "url", "selftext",
        "age_hours", "published", "ups", "num_comments", "comments", "buzz_score",
    }
    check(
        "main e2e writes ai_data.json with JST published, age_hours, comments, no _created_utc, no google.genai",
        data["source"] == "arctic_shift"
        and data["total_fetched"] == 3
        and data["sources_total"] == 3
        and data["sources_ok"] == 2
        and data["sources_error"] == 1
        and data["posts_with_comments"] == 2
        and data["per_source_counts"] == {"artificial": 2, "OpenAI": 1, "ClaudeAI": 0}
        and len(data["posts"]) == 2
        and [p["id"] for p in data["posts"]] == ["p3", "p1"]
        and all(required_post_keys.issubset(p.keys()) for p in data["posts"])
        and all("_created_utc" not in p for p in data["posts"])
        and all(isinstance(p["ups"], int) and isinstance(p["num_comments"], int) for p in data["posts"])
        and all(str(p["published"]).endswith("+09:00") and isinstance(p["age_hours"], float) for p in data["posts"])
        and data["posts"][0]["comments"] == ["Useful technical comment on p3 here!"]
        and not any(m == "google.genai" or m.startswith("google.genai.") for m in sys.modules),
    )

if check.failed:
    sys.exit(1)
print("all passed")
