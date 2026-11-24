#!/usr/bin/env python3
"""
Reddit Data Fetcher (Arctic Shift API edition).

Collects recent AI subreddit threads and top comments via the public Arctic
Shift API (https://arctic-shift.photon-reddit.com/api), filters removed/stale
entries, ranks by engagement (score + 2 * num_comments), merges round-robin for
subreddit diversity, and fetches top comments for the selected posts.

Usage:
    python fetch_data.py --config config_ai.json --output .cache/ai_data.json
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import http.client
import json
import math
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Report dates must be Japan time regardless of the server's local timezone.
JST = timezone(timedelta(hours=9), name="JST")

ARCTIC_SHIFT_BASE = "https://arctic-shift.photon-reddit.com/api"
ARCTIC_SHIFT_HOST = urllib.parse.urlparse(ARCTIC_SHIFT_BASE).hostname or ""
DEFAULT_SUBREDDITS = [
    "artificial",
    "singularity",
    "ChatGPT",
    "OpenAI",
    "ClaudeAI",
    "GeminiAI",
    "MachineLearning",
    "deeplearning",
    "LocalLLaMA",
    "ollama",
    "LangChain",
    "StableDiffusion",
    "comfyui",
    "cursor",
]
DEFAULT_TOP_N = 20
DEFAULT_MAX_PER_SUBREDDIT = 5

USER_AGENT = "reddit-daily/2.0 (personal-digest; respectful-rate)"
MAX_AGE_DAYS = 2
ARCTIC_SHIFT_PAGE_LIMIT = 100
MAX_POST_PAGES = 2
REQUEST_INTERVAL = 1.0
COMMENT_INTERVAL = 0.7
HTTP_TIMEOUT = 20
MAX_POST_RETRIES = 3
MAX_COMMENT_RETRIES = 3
MAX_COMMENTS_PER_POST = 6
MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB safety cap
FETCH_DEADLINE_SECONDS = 300  # Leave ample margin inside workflow 30m step timeout

SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]+$")
POST_ID_RE = re.compile(r"^[a-z0-9]+$", re.I)
_REDDIT_COMMENTS_RE = re.compile(
    r"^/r/([A-Za-z0-9_]+)/comments/([a-z0-9]+)(?:/(?:([A-Za-z0-9_%-]+)(?:/[A-Za-z0-9_/-]*)?)?)?$",
    re.I,
)
_RETRYABLE_HTTP_CODES = (422, 429, 500, 502, 503, 504)


def _is_reddit_host(host: str) -> bool:
    h = host.lower().split(":")[0]
    if not h or not all(h.split(".")):
        return False
    return h == "reddit.com" or h.endswith(".reddit.com")


def normalize_reddit_url(url: str) -> tuple[str, str, str] | None:
    """Normalize a Reddit thread URL to https://www.reddit.com/r/<sub>/comments/<id>/...

    Returns (normalized_url, subreddit, post_id) or None if not a valid thread URL.
    """
    raw = str(url or "").strip()
    if not raw:
        return None
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    if not _is_reddit_host(parsed.hostname or ""):
        return None
    m = _REDDIT_COMMENTS_RE.match(parsed.path or "")
    if not m:
        return None
    subreddit, post_id, slug = m.group(1), m.group(2), m.group(3)
    if slug:
        norm_url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/{slug}/"
    else:
        norm_url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/"
    return norm_url, subreddit, post_id


def _build_reddit_url(
    post: dict, subreddit: str, pid: str
) -> tuple[str, str, str] | None:
    """Build and validate a canonical Reddit thread URL for an Arctic Shift post."""
    permalink = str(post.get("permalink") or "").strip()
    if permalink.startswith("/") and not permalink.startswith("//"):
        norm = normalize_reddit_url(f"https://www.reddit.com{permalink}")
        if norm and norm[2].lower() == pid.lower():
            return norm
    return normalize_reddit_url(f"https://www.reddit.com/r/{subreddit}/comments/{pid}/")


def normalize_title(title: str) -> str:
    return re.sub(r"[\W_]+", "", str(title or "").lower())


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects so API calls never follow unexpected locations."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise urllib.error.URLError(f"HTTP redirect {code} disallowed")


def _validate_api_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != ARCTIC_SHIFT_HOST:
        raise ValueError(f"disallowed API URL: {url}")


def _http_get_json(
    url: str, *, retries: int = MAX_POST_RETRIES, timeout: int = HTTP_TIMEOUT
) -> dict:
    """Fetch JSON from Arctic Shift with no redirects, size cap, and polite retries."""
    _validate_api_url(url)
    opener = urllib.request.build_opener(_NoRedirectHandler())
    last_err: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw_bytes = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw_bytes) > MAX_RESPONSE_BYTES:
                raise ValueError(f"response exceeded {MAX_RESPONSE_BYTES} bytes")
            parsed = json.loads(raw_bytes.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError(f"expected JSON object, got {type(parsed).__name__}")
            return parsed
        except urllib.error.HTTPError as e:
            last_err = e
            body = ""
            try:
                body = e.read(512).decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            finally:
                e.close()
            if e.code in _RETRYABLE_HTTP_CODES and attempt < retries:
                wait = 2 * attempt
                print(
                    f"  retry {attempt}/{retries} after HTTP {e.code} ({body}): wait {wait}s",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue
            raise
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as e:
            last_err = e
            if attempt < retries:
                wait = 2 * attempt
                print(
                    f"  retry {attempt}/{retries} after {type(e).__name__}: wait {wait}s",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue
            raise
    assert last_err is not None
    raise last_err


def _safe_int(value, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _parse_created_utc(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ts) or ts <= 0:
        return None
    return ts


def _is_valid_post(post: dict, *, after_ts: float | None = None) -> bool:
    if not isinstance(post, dict):
        return False
    if post.get("stickied") is True or post.get("over_18") is True:
        return False
    if post.get("removed_by_category"):
        return False
    title = " ".join(str(post.get("title") or "").split())
    if not title or title.startswith("[ Removed") or title.startswith("[deleted]"):
        return False
    selftext = str(post.get("selftext") or "").strip()
    if selftext in ("[removed]", "[deleted]"):
        return False
    pid = str(post.get("id") or "").strip()
    if not pid or not POST_ID_RE.fullmatch(pid):
        return False
    if after_ts is not None:
        created_utc = _parse_created_utc(post.get("created_utc"))
        if created_utc is None or created_utc < after_ts:
            return False
    return True


def fetch_subreddit_posts(
    subreddit: str,
    after_ts: int,
    now_ts: float,
    max_items: int,
    blocked: set[str] | None = None,
) -> tuple[list[dict], bool]:
    """Fetch recent posts for one subreddit from Arctic Shift and rank by engagement."""
    blocked_lower = {s.lower() for s in (blocked or set())}
    sub_clean = str(subreddit or "").strip()
    if not SUBREDDIT_RE.fullmatch(sub_clean) or sub_clean.lower() in blocked_lower:
        print(f"  FAILED: invalid or blocked subreddit {subreddit!r}", file=sys.stderr)
        return [], False

    raw_posts: list[dict] = []
    before_ts: int | None = None
    for page in range(MAX_POST_PAGES):
        if page > 0 and REQUEST_INTERVAL > 0:
            time.sleep(REQUEST_INTERVAL)
        params: dict[str, str | int] = {
            "subreddit": sub_clean,
            "after": int(after_ts),
            "limit": ARCTIC_SHIFT_PAGE_LIMIT,
            "sort": "desc",
        }
        if before_ts is not None:
            params["before"] = before_ts
        url = f"{ARCTIC_SHIFT_BASE}/posts/search?{urllib.parse.urlencode(params)}"
        try:
            payload = _http_get_json(url, retries=MAX_POST_RETRIES, timeout=HTTP_TIMEOUT)
        except Exception as e:
            if page == 0:
                print(f"  FAILED: r/{sub_clean}: {type(e).__name__}: {e}", file=sys.stderr)
                return [], False
            print(
                f"  WARNING: r/{sub_clean} page {page + 1} failed ({type(e).__name__}); using page 1",
                file=sys.stderr,
            )
            break

        page_data = payload.get("data") or []
        if not isinstance(page_data, list):
            if page == 0:
                print(f"  FAILED: r/{sub_clean}: unexpected data type", file=sys.stderr)
                return [], False
            break
        raw_posts.extend(p for p in page_data if isinstance(p, dict))
        if len(page_data) < ARCTIC_SHIFT_PAGE_LIMIT:
            break

        page_timestamps = [
            ts for ts in (_parse_created_utc(p.get("created_utc")) for p in page_data if isinstance(p, dict))
            if ts is not None
        ]
        if not page_timestamps:
            break
        oldest_ts = int(min(page_timestamps))
        if oldest_ts <= int(after_ts) or (before_ts is not None and oldest_ts >= before_ts):
            break
        before_ts = oldest_ts

    cleaned: list[dict] = []
    seen_ids: set[str] = set()
    for p in raw_posts:
        if not _is_valid_post(p, after_ts=after_ts):
            continue
        pid = str(p["id"]).strip()
        sub = str(p.get("subreddit") or sub_clean).strip()
        norm = _build_reddit_url(p, sub, pid)
        if not norm:
            continue
        norm_url, url_sub, norm_pid = norm
        if norm_pid in seen_ids or url_sub.lower() in blocked_lower:
            continue
        seen_ids.add(norm_pid)

        created_utc = _parse_created_utc(p.get("created_utc")) or now_ts
        score = _safe_int(p.get("score"), 0)
        num_comments = _safe_int(p.get("num_comments"), 0)
        engagement = score + 2 * num_comments
        age_hours = round(max((now_ts - created_utc) / 3600.0, 0.5), 1)
        published = datetime.fromtimestamp(created_utc, tz=JST).isoformat(timespec="seconds")
        title = " ".join(str(p["title"]).split())
        selftext = str(p.get("selftext") or "").strip()[:1500]
        author = str(p.get("author") or "").strip()
        cleaned.append({
            "id": norm_pid,
            "title": title,
            "subreddit": url_sub,
            "author": author,
            "url": norm_url,
            "selftext": selftext,
            "age_hours": age_hours,
            "published": published,
            "ups": score,
            "num_comments": num_comments,
            "comments": [],
            "buzz_score": engagement,
            "_created_utc": created_utc,
        })

    cleaned.sort(
        key=lambda x: (x["buzz_score"], x["ups"], x["_created_utc"]),
        reverse=True,
    )
    return cleaned[:max_items], True


def merge_round_robin(
    subreddits: list[str],
    per_source: list[list[dict]],
    top_n: int,
    blocked: set[str] | None = None,
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Merge per-subreddit lists round-robin, deduplicate, cap top_n, then sort by engagement."""
    blocked_lower = {s.lower() for s in (blocked or set())}
    seen_ids: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()
    posts: list[dict] = []
    per_source_counts: dict[str, int] = {
        sub: len(items) for sub, items in zip(subreddits, per_source)
    }

    max_len = max((len(items) for items in per_source), default=0)
    for rank in range(max_len):
        for items in per_source:
            if rank >= len(items):
                continue
            d = items[rank]
            pid = str(d.get("id") or "")
            sub_lower = str(d.get("subreddit") or "").lower()
            if not pid or pid in seen_ids or sub_lower in blocked_lower:
                continue
            title_key = normalize_title(d.get("title", ""))
            if title_key and (sub_lower, title_key) in seen_titles:
                continue
            seen_ids.add(pid)
            if title_key:
                seen_titles.add((sub_lower, title_key))
            raw_buzz = d.get("buzz_score")
            buzz_score = _safe_int(raw_buzz) if raw_buzz is not None else (max_len - rank)
            posts.append({
                "id": pid,
                "title": d["title"],
                "subreddit": d["subreddit"],
                "author": d.get("author", ""),
                "url": d["url"],
                "selftext": d.get("selftext", ""),
                "age_hours": d.get("age_hours"),
                "published": d.get("published", ""),
                "ups": d.get("ups"),
                "num_comments": d.get("num_comments"),
                "comments": list(d.get("comments") or []),
                "buzz_score": buzz_score,
            })

    selected = posts[:top_n]
    selected.sort(
        key=lambda x: (_safe_int(x.get("buzz_score")), _safe_int(x.get("ups"))),
        reverse=True,
    )
    return posts, selected, per_source_counts


def fetch_top_comments(
    post_id: str, max_comments: int = MAX_COMMENTS_PER_POST
) -> list[str]:
    """Fetch and filter top comments for one Reddit post ID from Arctic Shift."""
    pid = str(post_id or "").strip()
    if not POST_ID_RE.fullmatch(pid):
        return []
    qs = urllib.parse.urlencode({
        "link_id": f"t3_{pid}",
        "limit": ARCTIC_SHIFT_PAGE_LIMIT,
        "sort": "desc",
    })
    url = f"{ARCTIC_SHIFT_BASE}/comments/search?{qs}"
    try:
        payload = _http_get_json(url, retries=MAX_COMMENT_RETRIES, timeout=HTTP_TIMEOUT)
    except Exception as e:
        print(
            f"  WARNING: comments failed for t3_{pid}: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return []

    raw_comments = payload.get("data") or []
    if not isinstance(raw_comments, list):
        return []

    candidates: list[tuple[int, float, str]] = []
    for c in raw_comments:
        if not isinstance(c, dict):
            continue
        if c.get("stickied") is True or c.get("distinguished") == "moderator":
            continue
        author = c.get("author")
        if author in ("AutoModerator", "[deleted]", None, ""):
            continue
        body = " ".join(str(c.get("body") or "").split())
        if not body or body in ("[removed]", "[deleted]") or len(body) < 15:
            continue
        score = _safe_int(c.get("score"), 0)
        created_utc = _parse_created_utc(c.get("created_utc")) or 0.0
        candidates.append((score, created_utc, body[:250]))

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [text for _, _, text in candidates[:max_comments]]


def load_config(config_arg: str | None) -> tuple[list[str], int, int, set[str]]:
    subreddits = list(DEFAULT_SUBREDDITS)
    top_n = DEFAULT_TOP_N
    max_per_subreddit = DEFAULT_MAX_PER_SUBREDDIT
    blocked: set[str] = set()

    if config_arg:
        config_path = Path(config_arg)
        if not config_path.is_absolute():
            config_path = Path(__file__).resolve().parent / config_path
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if "subreddits" in cfg:
            subreddits = [str(s).strip() for s in cfg["subreddits"] if str(s).strip()]
        top_n = int(cfg.get("top_n", DEFAULT_TOP_N))
        max_per_subreddit = int(cfg.get("max_per_subreddit", DEFAULT_MAX_PER_SUBREDDIT))
        blocked = {str(s).lower() for s in cfg.get("blocked_subreddits", [])}

    subreddits = [s for s in subreddits if s.lower() not in blocked]
    return subreddits, top_n, max_per_subreddit, blocked


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Reddit trending data via Arctic Shift API"
    )
    parser.add_argument("--config", type=str, help="Config JSON file path")
    parser.add_argument("--output", type=str, required=True, help="Output JSON file path")
    args = parser.parse_args()

    started = time.monotonic()
    subreddits, top_n, max_per_subreddit, blocked = load_config(args.config)
    now_jst = datetime.now(JST)
    now_ts = now_jst.timestamp()
    after_ts = int(now_ts - MAX_AGE_DAYS * 24 * 3600)

    print(f"Fetching {len(subreddits)} subreddit(s) via Arctic Shift (last {MAX_AGE_DAYS * 24}h)...")

    per_source: list[list[dict]] = []
    sources_ok = 0
    sources_error = 0

    for idx, sub in enumerate(subreddits):
        if idx > 0 and REQUEST_INTERVAL > 0:
            time.sleep(REQUEST_INTERVAL)
        items, ok = fetch_subreddit_posts(
            sub, after_ts, now_ts, max_per_subreddit, blocked
        )
        per_source.append(items)
        if ok:
            sources_ok += 1
        else:
            sources_error += 1
        print(f"  r/{sub}: {len(items)} post(s) ({'ok' if ok else 'error'})")

    posts, top, per_source_counts = merge_round_robin(
        subreddits, per_source, top_n, blocked
    )

    print(
        f"  Total posts collected: {len(posts)} "
        f"(sources ok {sources_ok}/{len(subreddits)}, errors {sources_error})"
    )

    if len(posts) < 3:
        print(f"ERROR: Only {len(posts)} posts fetched. Aborting.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching top comments for {len(top)} selected post(s)...")
    with_comments = 0
    for idx, p in enumerate(top):
        if (time.monotonic() - started) >= FETCH_DEADLINE_SECONDS:
            print(
                f"  WARNING: fetch deadline ({FETCH_DEADLINE_SECONDS}s) reached; "
                f"skipping comments for remaining {len(top) - idx} post(s)",
                file=sys.stderr,
            )
            break
        if idx > 0 and COMMENT_INTERVAL > 0:
            time.sleep(COMMENT_INTERVAL)
        try:
            comments = fetch_top_comments(p["id"], max_comments=MAX_COMMENTS_PER_POST)
        except Exception as e:
            print(
                f"  WARNING: unexpected comment error for {p['id']}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            comments = []
        p["comments"] = comments
        if comments:
            with_comments += 1
        age = f"{p['age_hours']}h" if p["age_hours"] is not None else "?"
        print(
            f"  #{idx + 1}: r/{p['subreddit']} | ⬆️{p['ups']} 💬{p['num_comments']} "
            f"(+{len(comments)} comments) | {p['title'][:50]}... ({age})"
        )

    result = {
        "date": now_jst.strftime("%Y-%m-%d"),
        "generated_at": now_jst.isoformat(timespec="seconds"),
        "source": "arctic_shift",
        "total_fetched": len(posts),
        "sources_total": len(subreddits),
        "sources_ok": sources_ok,
        "sources_error": sources_error,
        "posts_with_comments": with_comments,
        "per_source_counts": per_source_counts,
        "posts": top,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    tmp_path.replace(output_path)

    print(f"\nDone: {len(top)} posts ({with_comments} with comments) saved to {args.output}")


if __name__ == "__main__":
    main()
