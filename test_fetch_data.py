"""Offline checks for fetch_data.py (Gemini Google Search Grounding edition).

    python3 test_fetch_data.py
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import email.message
import io
import json
import os
import pathlib
import socket
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock
import urllib.error
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

# 2. resolve_redirect_url: grounding redirect, HTTPError redirect target, direct reddit, timeout=10, SSRF hardening
def _fake_global_getaddrinfo(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.190.14", port))]


def _make_response(url: str, code: int = 200, location: str | None = None):
    hdrs = email.message.Message()
    if location is not None:
        hdrs["Location"] = location
    resp = urllib.response.addinfourl(io.BytesIO(b""), hdrs, url, code=code)
    resp.msg = "Found" if 300 <= code < 400 else ("Forbidden" if code == 403 else "OK")
    return resp


seen_timeouts = []


def fake_https_open_ok(self, req):
    seen_timeouts.append(req.timeout)
    if "vertexaisearch.cloud.google.com" in req.full_url:
        return _make_response(
            req.full_url,
            code=302,
            location="https://old.reddit.com/r/LocalLLaMA/comments/abc123/llama_update/?utm_source=g",
        )
    return _make_response(req.full_url, code=200)


with mock.patch("socket.getaddrinfo", side_effect=_fake_global_getaddrinfo), \
     mock.patch.object(urllib.request.HTTPSHandler, "https_open", fake_https_open_ok):
    resolved = F.resolve_redirect_url(
        "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ123"
    )
check(
    "resolve_redirect_url resolves vertexaisearch redirect with timeout=10",
    resolved == "https://old.reddit.com/r/LocalLLaMA/comments/abc123/llama_update/?utm_source=g"
    and seen_timeouts == [10, 10],
)


closed_errors = []


class _TrackedHTTPError(urllib.error.HTTPError):
    def close(self):
        closed_errors.append(True)
        super().close()


class _Opener403:
    def open(self, req, timeout=None):
        raise _TrackedHTTPError(
            url="https://www.reddit.com/r/ClaudeAI/comments/xyz789/claude_post/",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=io.BytesIO(b""),
        )


with mock.patch("socket.getaddrinfo", side_effect=_fake_global_getaddrinfo), \
     mock.patch("urllib.request.build_opener", return_value=_Opener403()):
    resolved_403 = F.resolve_redirect_url(
        "https://vertexaisearch.cloud.google.com/grounding-api-redirect/XYZ"
    )
check(
    "resolve_redirect_url preserves redirected Reddit URL even on 403 and closes HTTPError",
    resolved_403 == "https://www.reddit.com/r/ClaudeAI/comments/xyz789/claude_post/"
    and closed_errors == [True],
)

with mock.patch("socket.getaddrinfo", side_effect=AssertionError("should not DNS-resolve reddit.com")), \
     mock.patch.object(urllib.request.HTTPSHandler, "https_open", side_effect=AssertionError("should not fetch reddit.com")):
    direct = F.resolve_redirect_url("https://www.reddit.com/r/OpenAI/comments/p1/test/")
check("resolve_redirect_url skips HTTP call for reddit.com URLs", direct == "https://www.reddit.com/r/OpenAI/comments/p1/test/")

with mock.patch("socket.getaddrinfo", side_effect=_fake_global_getaddrinfo), \
     mock.patch.object(urllib.request.HTTPSHandler, "https_open", side_effect=TimeoutError("timed out")):
    failed_url = F.resolve_redirect_url("https://vertexaisearch.cloud.google.com/grounding-api-redirect/TIMEOUT")
check("resolve_redirect_url returns empty string on network error", failed_url == "")

# 2b. SSRF hardening: disallowed schemes/hosts, non-global IPs, and unsafe redirect targets
disallowed_initial_urls = [
    "http://vertexaisearch.cloud.google.com/grounding-api-redirect/1",
    "http://www.reddit.com/r/OpenAI/comments/p1/test/",
    "file:///etc/passwd",
    "ftp://vertexaisearch.cloud.google.com/file",
    "javascript:alert(1)",
    "https://evil.example.com/path",
    "https://reddit.com.evil.example/r/OpenAI/comments/p1/test/",
    "https://.reddit.com/r/OpenAI/comments/p1/test/",
    "https://reddit.com./r/OpenAI/comments/p1/test/",
    "https://127.0.0.1/admin",
    "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/admin",
    "https://vertexaisearch.cloud.google.com@127.0.0.1/x",
    "https://vertexaisearch.cloud.google.com:8443/grounding-api-redirect/1",
]
with mock.patch("socket.getaddrinfo", side_effect=AssertionError("disallowed URL should not trigger DNS")), \
     mock.patch.object(urllib.request.HTTPSHandler, "https_open", side_effect=AssertionError("disallowed URL should not trigger HTTP")):
    blocked_initial = [F.resolve_redirect_url(u) for u in disallowed_initial_urls]
check(
    "resolve_redirect_url rejects disallowed schemes, hosts, IP literals, userinfo, and ports",
    all(res == "" for res in blocked_initial),
)

non_global_ips = [
    "127.0.0.1",
    "169.254.169.254",
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "100.64.0.1",
    "224.0.0.1",
    "0.0.0.0",
    "::1",
    "fe80::1",
    "fc00::1",
    "::ffff:127.0.0.1",
]
blocked_dns_results = []
for bad_ip in non_global_ips:
    def _bad_dns(host, port, _ip=bad_ip):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_ip, port))]

    with mock.patch("socket.getaddrinfo", side_effect=_bad_dns), \
         mock.patch.object(urllib.request.HTTPSHandler, "https_open", side_effect=AssertionError("non-global IP should not trigger HTTP")):
        blocked_dns_results.append(
            F.resolve_redirect_url("https://vertexaisearch.cloud.google.com/grounding-api-redirect/DNS")
        )

# Mixed global + loopback in same DNS response must also be rejected
with mock.patch(
    "socket.getaddrinfo",
    return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.190.14", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ],
), mock.patch.object(urllib.request.HTTPSHandler, "https_open", side_effect=AssertionError("mixed private IP should not trigger HTTP")):
    blocked_dns_results.append(
        F.resolve_redirect_url("https://vertexaisearch.cloud.google.com/grounding-api-redirect/MIXED")
    )
check(
    "resolve_redirect_url rejects non-global IPs from DNS resolution",
    all(res == "" for res in blocked_dns_results),
)

unsafe_redirect_targets = [
    "http://169.254.169.254/latest/meta-data/",
    "https://127.0.0.1/secret",
    "https://169.254.169.254/latest/meta-data/",
    "https://evil.example.com/phish",
    "http://www.reddit.com/r/OpenAI/comments/p1/test/",
    "file:///etc/passwd",
]
redirect_blocks_ok = True
for target in unsafe_redirect_targets:
    fetched_urls = []

    def _redirect_https_open(self, req, _target=target, _fetched=fetched_urls):
        _fetched.append(req.full_url)
        if "vertexaisearch.cloud.google.com" in req.full_url:
            return _make_response(req.full_url, code=302, location=_target)
        raise AssertionError(f"should not follow unsafe redirect to {req.full_url}")

    with mock.patch("socket.getaddrinfo", side_effect=_fake_global_getaddrinfo), \
         mock.patch.object(urllib.request.HTTPSHandler, "https_open", _redirect_https_open):
        res = F.resolve_redirect_url("https://vertexaisearch.cloud.google.com/grounding-api-redirect/SSRF")
    if res != "" or len(fetched_urls) != 1:
        redirect_blocks_ok = False
check(
    "resolve_redirect_url blocks redirects to private IPs, disallowed hosts, and non-https schemes",
    redirect_blocks_ok,
)

# Redirect to allowed reddit.com host whose DNS resolves to private/link-local IP must be blocked
for bad_ip in ("127.0.0.1", "169.254.169.254"):
    fetched_urls = []

    def _dns_per_host(host, port, _bad_ip=bad_ip):
        ip = _bad_ip if "reddit.com" in host else "142.250.190.14"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    def _redirect_to_reddit(self, req, _fetched=fetched_urls):
        _fetched.append(req.full_url)
        if "vertexaisearch.cloud.google.com" in req.full_url:
            return _make_response(
                req.full_url,
                code=302,
                location="https://www.reddit.com/r/OpenAI/comments/p1/test/",
            )
        raise AssertionError(f"should not connect to non-global IP for {req.full_url}")

    with mock.patch("socket.getaddrinfo", side_effect=_dns_per_host), \
         mock.patch.object(urllib.request.HTTPSHandler, "https_open", _redirect_to_reddit):
        res = F.resolve_redirect_url("https://vertexaisearch.cloud.google.com/grounding-api-redirect/REBIND")
    if res != "" or len(fetched_urls) != 1:
        redirect_blocks_ok = False
check(
    "resolve_redirect_url blocks redirect to reddit.com when DNS resolves to non-global IP",
    redirect_blocks_ok,
)

# 3. normalize_reddit_url
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
    "normalize_reddit_url rejects non-Reddit domains",
    F.normalize_reddit_url("https://reddit.com.evil.example/r/artificial/comments/123/x/") is None
    and F.normalize_reddit_url("https://arxiv.org/abs/2511.0001") is None,
)

# 4. Date filtering (JST today .. 2 days ago)
today = date(2026, 11, 18)
check("date filter keeps today, -1d, -2d in JST", not F.is_too_old("2026-11-18", today)
      and not F.is_too_old("2026-11-17T10:00:00+09:00", today)
      and not F.is_too_old("2026-11-16", today))
check("date filter rejects >2 days old in JST", F.is_too_old("2026-11-15", today)
      and F.is_too_old("2026-11-15T10:00:00Z", today))
check("date filter converts UTC across JST midnight boundary", not F.is_too_old("2026-11-15T20:00:00Z", today))
check("date filter keeps posts with missing/unknown published date", not F.is_too_old("", today)
      and not F.is_too_old("unknown", today))

now_ts = datetime(2026, 11, 18, 12, 0, tzinfo=F.JST).timestamp()
check(
    "age_hours computes hours for timestamps and None for date-only",
    F.age_hours("2026-11-18T09:00:00+09:00", now_ts) == 3.0
    and F.age_hours("2026-11-18", now_ts) is None,
)

# 5. search_subreddit with GoogleSearch tool + redirect + filtering + max_per_subreddit cap
captured_configs = []


class FakeModels:
    def __init__(self, responses):
        self._responses = dict(responses)

    def generate_content(self, *, model, contents, config):
        captured_configs.append((model, config))
        for sub, payload in self._responses.items():
            if f"r/{sub}" in contents:
                if isinstance(payload, Exception):
                    raise payload
                return SimpleNamespace(text=payload, usage_metadata=None)
        return SimpleNamespace(text="[]", usage_metadata=None)


class FakeClient:
    def __init__(self, responses):
        self.models = FakeModels(responses)


items_artificial = [
    {"title": "Fresh Redirect Post", "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/R1",
     "published": "2026-11-18T06:00:00+09:00", "author": "alice", "selftext": "Details A"},
    {"title": "Stale Old Post", "url": "https://www.reddit.com/r/artificial/comments/old1/stale/",
     "published": "2026-11-15", "author": "bob", "selftext": "Old"},
    {"title": "Not Comments Page", "url": "https://www.reddit.com/r/artificial/",
     "published": "2026-11-18", "author": "", "selftext": ""},
    {"title": "Non Reddit Link", "url": "https://example.com/news",
     "published": "2026-11-18", "author": "", "selftext": ""},
    {"title": "Fresh Direct Post", "url": "https://old.reddit.com/r/artificial/comments/art2/direct/",
     "published": "", "author": "", "selftext": "Details B"},
]
fake_client = FakeClient({
    "artificial": f"```json\n{json.dumps(items_artificial)}\n```",
})


def fake_resolve(url: str, timeout: int = 10) -> str:
    if url.endswith("/R1"):
        return "https://www.reddit.com/r/artificial/comments/art1/fresh_redirect/?utm_source=x"
    if "example.com" in url:
        return url
    return url


with mock.patch.object(F, "resolve_redirect_url", side_effect=fake_resolve):
    found, ok = F.search_subreddit(fake_client, "artificial", 5, today, "gemini-3.8-flash", now_ts, set())

check(
    "search_subreddit passes GoogleSearch tool in GenerateContentConfig",
    len(captured_configs) == 1
    and captured_configs[0][0] == "gemini-3.8-flash"
    and any(getattr(t, "google_search", None) is not None for t in (captured_configs[0][1].tools or [])),
)
check(
    "search_subreddit keeps valid fresh/undated posts and filters stale/non-comments/non-reddit",
    ok and [p["id"] for p in found] == ["art1", "art2"]
    and found[0]["url"] == "https://www.reddit.com/r/artificial/comments/art1/fresh_redirect/"
    and found[1]["url"] == "https://www.reddit.com/r/artificial/comments/art2/direct/",
)

# 6. Round-robin merge & deduplication (post_id and (subreddit.lower(), normalized_title))
per_source = [
    [
        {"id": "a1", "title": "First AI Post!", "subreddit": "artificial", "author": "u1",
         "url": "https://www.reddit.com/r/artificial/comments/a1/first/", "selftext": "", "age_hours": 1.0, "published": ""},
        {"id": "a2", "title": "First AI Post", "subreddit": "Artificial", "author": "u2",  # same subreddit + normalized title
         "url": "https://www.reddit.com/r/artificial/comments/a2/dup_title/", "selftext": "", "age_hours": 2.0, "published": ""},
        {"id": "a3", "title": "Second Artificial", "subreddit": "artificial", "author": "",
         "url": "https://www.reddit.com/r/artificial/comments/a3/second/", "selftext": "", "age_hours": 3.0, "published": ""},
    ],
    [
        {"id": "a1", "title": "Crosspost same ID", "subreddit": "OpenAI", "author": "",  # duplicate post_id
         "url": "https://www.reddit.com/r/OpenAI/comments/a1/cross/", "selftext": "", "age_hours": 1.0, "published": ""},
        {"id": "o1", "title": "First AI Post", "subreddit": "OpenAI", "author": "",  # different subreddit, diff id -> kept
         "url": "https://www.reddit.com/r/OpenAI/comments/o1/first/", "selftext": "", "age_hours": 1.5, "published": ""},
    ],
]
all_merged, top_merged, counts = F.merge_round_robin(["artificial", "OpenAI"], per_source, top_n=2)
check(
    "merge_round_robin merges round-robin, dedupes post_id & (subreddit.lower(), title), caps top_n",
    [p["id"] for p in all_merged] == ["a1", "o1", "a3"]
    and [p["id"] for p in top_merged] == ["a1", "o1"]
    and counts == {"artificial": 3, "OpenAI": 2},
)

# 7. main(): < 3 posts exits 1; >= 3 posts writes compatible ai_data.json
with tempfile.TemporaryDirectory() as tmp:
    tmp_dir = pathlib.Path(tmp)
    cfg_path = tmp_dir / "cfg.json"
    cfg_path.write_text(json.dumps({
        "subreddits": ["artificial", "OpenAI", "ClaudeAI"],
        "top_n": 2,
        "max_per_subreddit": 5,
        "blocked_subreddits": [],
    }), encoding="utf-8")
    out_too_few = tmp_dir / "few.json"
    few_client = FakeClient({
        "artificial": json.dumps([
            {"title": "Only 1", "url": "https://www.reddit.com/r/artificial/comments/p1/a/", "published": ""},
            {"title": "Only 2", "url": "https://www.reddit.com/r/artificial/comments/p2/b/", "published": ""},
        ]),
    })
    exit_code = None
    with mock.patch.object(F, "create_client", return_value=few_client), \
         mock.patch.object(sys, "argv", ["fetch_data.py", "--config", str(cfg_path), "--output", str(out_too_few)]):
        try:
            F.main()
        except SystemExit as e:
            exit_code = e.code
    check("main exits 1 when fewer than 3 posts collected", exit_code == 1 and not out_too_few.exists())

    out_ok = tmp_dir / "ai_data.json"
    ok_client = FakeClient({
        "artificial": json.dumps([
            {"title": "Art 1", "url": "https://www.reddit.com/r/artificial/comments/p1/a/", "published": ""},
            {"title": "Art 2", "url": "https://www.reddit.com/r/artificial/comments/p2/b/", "published": ""},
        ]),
        "OpenAI": json.dumps([
            {"title": "OAI 1", "url": "https://www.reddit.com/r/OpenAI/comments/p3/c/", "published": ""},
        ]),
        "ClaudeAI": RuntimeError("temporary error"),
    })
    with mock.patch.object(F, "create_client", return_value=ok_client), \
         mock.patch.object(F, "MAX_SEARCH_RETRIES", 1), \
         mock.patch.dict(os.environ, {"GEMINI_FETCH_MODEL": "gemini-3.8-flash"}), \
         mock.patch.object(sys, "argv", ["fetch_data.py", "--config", str(cfg_path), "--output", str(out_ok)]):
        F.main()
    data = json.loads(out_ok.read_text(encoding="utf-8"))
    required_post_keys = {
        "id", "title", "subreddit", "author", "url", "selftext",
        "age_hours", "published", "ups", "num_comments", "comments", "buzz_score",
    }
    check(
        "main writes ai_data.json with expected schema and fields",
        data["source"] == "google_search"
        and data["total_fetched"] == 3
        and data["sources_total"] == 3
        and data["sources_ok"] == 2
        and data["sources_error"] == 1
        and data["per_source_counts"] == {"artificial": 2, "OpenAI": 1, "ClaudeAI": 0}
        and len(data["posts"]) == 2
        and [p["id"] for p in data["posts"]] == ["p1", "p3"]
        and all(required_post_keys.issubset(p.keys()) for p in data["posts"])
        and all(p["ups"] is None and p["num_comments"] is None and p["comments"] == [] for p in data["posts"]),
    )

check("get_fetch_model defaults to gemini-3.8-flash",
      (lambda: (os.environ.pop("GEMINI_FETCH_MODEL", None), F.get_fetch_model() == "gemini-3.8-flash")[1])())

if check.failed:
    sys.exit(1)
print("all passed")
