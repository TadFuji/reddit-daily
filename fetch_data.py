#!/usr/bin/env python3
"""
Reddit Data Fetcher (Gemini Google Search Grounding edition).

Collects recent trending threads for each AI subreddit in config_ai.json using
Gemini with the Google Search tool, resolves grounding redirect URLs, normalizes
Reddit thread URLs, filters stale posts (>2 days in JST), and merges results
round-robin.

Usage:
    python fetch_data.py --config config_ai.json --output .cache/ai_data.json
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from usage_meter import meter
except ImportError:
    class _NullMeter:
        def __getattr__(self, _):
            return lambda *a, **k: None

    meter = _NullMeter()

# Report dates must be Japan time regardless of the server's local timezone.
JST = timezone(timedelta(hours=9), name="JST")

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
DEFAULT_FETCH_MODEL = "gemini-3.8-flash"
MAX_WORKERS = 4
REDIRECT_TIMEOUT = 10
MAX_AGE_DAYS = 2
MAX_SEARCH_RETRIES = 2

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_REDDIT_COMMENTS_RE = re.compile(
    r"^/r/([A-Za-z0-9_]+)/comments/([A-Za-z0-9]+)(?:/([^/?#]+))?"
)


def get_fetch_model() -> str:
    return os.environ.get("GEMINI_FETCH_MODEL", DEFAULT_FETCH_MODEL).strip() or DEFAULT_FETCH_MODEL


def create_client():
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Neither GEMINI_API_KEY nor GOOGLE_API_KEY is set")
    return genai.Client(api_key=api_key)


_ALLOWED_REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com",)


def _is_reddit_host(host: str) -> bool:
    h = host.lower().split(":")[0]
    if not h or not all(h.split(".")):
        return False
    return h == "reddit.com" or h.endswith(".reddit.com")


def _is_allowed_redirect_host(host: str) -> bool:
    h = host.lower().split(":")[0]
    return h in _ALLOWED_REDIRECT_HOSTS or _is_reddit_host(h)


def _is_global_ip(ip_str: str) -> bool:
    if not ip_str or "%" in ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def _validate_url_syntax(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"disallowed scheme: {parsed.scheme}")
    if parsed.username or parsed.password:
        raise ValueError("userinfo not allowed in URL")
    port = parsed.port
    if port not in (None, 443):
        raise ValueError(f"disallowed port: {port}")
    host = (parsed.hostname or "").strip().lower()
    if not host or not _is_allowed_redirect_host(host):
        raise ValueError(f"disallowed host: {host}")
    return host, port or 443


def _validate_redirect_url(url: str) -> None:
    host, port = _validate_url_syntax(url)
    infos = socket.getaddrinfo(host, port)
    if not infos:
        raise ValueError(f"no DNS records for host: {host}")
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0] if sockaddr else ""
        if not _is_global_ip(ip_str):
            raise ValueError(f"non-global IP {ip_str} for host {host}")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects to disallowed schemes, hosts, or non-global IPs."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            _validate_redirect_url(newurl)
        except Exception as exc:
            if fp is not None:
                fp.close()
            raise urllib.error.URLError(f"Blocked unsafe redirect: {exc}") from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def resolve_redirect_url(url: str, timeout: int = REDIRECT_TIMEOUT) -> str:
    """Resolve grounding/shortener redirect URLs via HTTP redirect target."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        host, _ = _validate_url_syntax(raw)
    except ValueError:
        return ""
    if _is_reddit_host(host):
        return raw

    try:
        _validate_redirect_url(raw)
    except Exception:
        return ""

    req = urllib.request.Request(raw, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(req, timeout=timeout) as resp:
            final_url = resp.geturl() or raw
            try:
                _validate_url_syntax(final_url)
            except ValueError:
                return ""
            return final_url
    except urllib.error.HTTPError as e:
        try:
            loc = e.headers.get("Location") if e.headers else None
            if loc and 300 <= e.code < 400:
                base = (e.geturl() if hasattr(e, "geturl") else getattr(e, "url", "")) or raw
                target = urllib.parse.urljoin(base, loc)
                try:
                    _validate_redirect_url(target)
                except Exception:
                    return ""
                return target
            final_url = e.geturl() if hasattr(e, "geturl") else getattr(e, "url", "")
            if not final_url:
                return ""
            try:
                _validate_url_syntax(final_url)
            except ValueError:
                return ""
            return final_url
        finally:
            e.close()
    except Exception:
        return ""


def normalize_reddit_url(url: str) -> tuple[str, str, str] | None:
    """Normalize a Reddit thread URL to https://www.reddit.com/r/<sub>/comments/<id>/...

    Returns (normalized_url, subreddit, post_id) or None if not a valid thread URL.
    """
    raw = str(url or "").strip()
    if not raw:
        return None
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https"):
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


def normalize_title(title: str) -> str:
    return re.sub(r"[\W_]+", "", str(title or "").lower())


def parse_published_jst(published: str) -> tuple[date | None, datetime | None]:
    """Parse an explicit published date/timestamp into (jst_date, jst_datetime_or_None)."""
    s = str(published or "").strip()
    if not s or s.lower() in ("unknown", "none", "null", "n/a", "?"):
        return None, None

    if re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", s):
        try:
            return date.fromisoformat(s.replace("/", "-")), None
        except ValueError:
            return None, None

    iso_candidate = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        else:
            dt = dt.astimezone(JST)
        return dt.date(), dt
    except ValueError:
        pass

    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if "%H" in fmt:
                dt = dt.replace(tzinfo=JST)
                return dt.date(), dt
            return dt.date(), None
        except ValueError:
            continue

    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        else:
            dt = dt.astimezone(JST)
        return dt.date(), dt
    except Exception:
        return None, None


def is_too_old(published: str, today_jst: date, max_days: int = MAX_AGE_DAYS) -> bool:
    """Return True only when an explicit published date is older than max_days in JST."""
    jst_date, _ = parse_published_jst(published)
    if jst_date is None:
        return False
    return jst_date < (today_jst - timedelta(days=max_days))


def age_hours(published: str, now_ts: float) -> float | None:
    """Compute post age in hours when a timestamp with time is available."""
    _, dt_jst = parse_published_jst(published)
    if dt_jst is None:
        return None
    return round(max((now_ts - dt_jst.timestamp()) / 3600, 0.5), 1)


def _extract_json_array(text: str) -> list[dict]:
    raw = (text or "").strip()
    if not raw:
        return []
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if fence:
        raw = fence.group(1).strip()

    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end > start:
            try:
                parsed = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return []

    if isinstance(parsed, dict):
        for key in ("posts", "items", "threads", "results"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _build_search_prompt(subreddit: str, max_items: int, today_jst: date) -> str:
    cutoff = today_jst - timedelta(days=MAX_AGE_DAYS)
    return (
        f"Use Google Search to find up to {max_items} notable recent discussion threads "
        f"on Reddit in r/{subreddit} (site:reddit.com/r/{subreddit}/comments) published "
        f"within the last 24-48 hours (between {cutoff.isoformat()} and {today_jst.isoformat()} JST).\n\n"
        "Return ONLY a JSON array (no markdown explanation) of up to "
        f"{max_items} objects, ordered from most notable/trending to least. "
        "Each object must have these fields:\n"
        '- "title": exact thread title (string)\n'
        '- "url": direct Reddit thread URL containing /comments/ (string)\n'
        '- "published": ISO-8601 timestamp or YYYY-MM-DD date if visible in search results, else ""\n'
        '- "author": Reddit username if visible, else ""\n'
        '- "selftext": concise summary or excerpt of the post body/topic in English (up to 800 chars)\n\n'
        "Only include actual Reddit thread URLs with /comments/. "
        "If no recent threads are found, return []."
    )


def search_subreddit(
    client,
    subreddit: str,
    max_items: int,
    today_jst: date,
    model: str,
    now_ts: float,
    blocked: set[str],
) -> tuple[list[dict], bool]:
    """Query Gemini with Google Search grounding for one subreddit."""
    from google.genai import types

    prompt = _build_search_prompt(subreddit, max_items, today_jst)
    raw_items: list[dict] | None = None
    last_err = ""

    for attempt in range(1, MAX_SEARCH_RETRIES + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.1,
                ),
            )
            meter.record(model, resp, label=f"fetch-{subreddit}")
            raw_items = _extract_json_array(getattr(resp, "text", "") or "")
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:150]}"
            if attempt < MAX_SEARCH_RETRIES:
                time.sleep(2 * attempt)

    if raw_items is None:
        print(f"  FAILED: r/{subreddit}: {last_err}", file=sys.stderr)
        return [], False

    cleaned: list[dict] = []
    for item in raw_items:
        title = " ".join(str(item.get("title") or "").split())
        raw_url = str(item.get("url") or "").strip()
        if not title or not raw_url:
            continue

        resolved_url = resolve_redirect_url(raw_url)
        norm = normalize_reddit_url(resolved_url)
        if not norm:
            continue
        norm_url, url_sub, post_id = norm
        if url_sub.lower() in blocked:
            continue

        published = str(item.get("published") or "").strip()
        if is_too_old(published, today_jst):
            continue

        selftext = str(item.get("selftext") or item.get("summary") or "").strip()[:1500]
        author = str(item.get("author") or "").strip()
        cleaned.append({
            "id": post_id,
            "title": title,
            "subreddit": url_sub,
            "author": author,
            "url": norm_url,
            "selftext": selftext,
            "age_hours": age_hours(published, now_ts),
            "published": published,
        })
        if len(cleaned) >= max_items:
            break

    return cleaned, True


def merge_round_robin(
    subreddits: list[str],
    per_source: list[list[dict]],
    top_n: int,
    blocked: set[str] | None = None,
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Merge per-subreddit lists round-robin, deduplicating by post_id and (subreddit.lower(), normalized_title)."""
    blocked_lower = {s.lower() for s in (blocked or set())}
    seen_ids: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()
    posts: list[dict] = []
    per_source_counts: dict[str, int] = {sub: len(items) for sub, items in zip(subreddits, per_source)}

    max_len = max((len(items) for items in per_source), default=0)
    for rank in range(max_len):
        for items in per_source:
            if rank >= len(items):
                continue
            d = items[rank]
            pid = d["id"]
            sub_lower = d["subreddit"].lower()
            if not pid or pid in seen_ids or sub_lower in blocked_lower:
                continue
            title_key = normalize_title(d["title"])
            if title_key and (sub_lower, title_key) in seen_titles:
                continue
            seen_ids.add(pid)
            if title_key:
                seen_titles.add((sub_lower, title_key))
            posts.append({
                "id": pid,
                "title": d["title"],
                "subreddit": d["subreddit"],
                "author": d.get("author", ""),
                "url": d["url"],
                "selftext": d.get("selftext", ""),
                "age_hours": d.get("age_hours"),
                "published": d.get("published", ""),
                "ups": None,
                "num_comments": None,
                "comments": [],
                "buzz_score": max_len - rank,
            })

    return posts, posts[:top_n], per_source_counts


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
    parser = argparse.ArgumentParser(description="Fetch Reddit trending data via Gemini Google Search Grounding")
    parser.add_argument("--config", type=str, help="Config JSON file path")
    parser.add_argument("--output", type=str, required=True, help="Output JSON file path")
    args = parser.parse_args()

    subreddits, top_n, max_per_subreddit, blocked = load_config(args.config)
    model = get_fetch_model()
    now_jst = datetime.now(JST)
    today_jst = now_jst.date()
    now_ts = now_jst.timestamp()

    print(f"Searching {len(subreddits)} subreddit(s) via Gemini ({model})...")
    client = create_client()

    per_source: list[list[dict]] = [[] for _ in subreddits]
    sources_ok = 0
    sources_error = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                search_subreddit,
                client,
                sub,
                max_per_subreddit,
                today_jst,
                model,
                now_ts,
                blocked,
            ): idx
            for idx, sub in enumerate(subreddits)
        }
        for future in as_completed(futures):
            idx = futures[future]
            sub = subreddits[idx]
            try:
                items, ok = future.result()
            except Exception as e:
                print(f"  FAILED: r/{sub}: {type(e).__name__}: {e}", file=sys.stderr)
                items, ok = [], False
            per_source[idx] = items
            if ok:
                sources_ok += 1
            else:
                sources_error += 1
            print(f"  r/{sub}: {len(items)} post(s) ({'ok' if ok else 'error'})")

    posts, top, per_source_counts = merge_round_robin(subreddits, per_source, top_n, blocked)

    print(
        f"  Total posts collected: {len(posts)} "
        f"(sources ok {sources_ok}/{len(subreddits)}, errors {sources_error})"
    )

    if len(posts) < 3:
        print(f"ERROR: Only {len(posts)} posts fetched. Aborting.", file=sys.stderr)
        sys.exit(1)

    for i, p in enumerate(top, 1):
        age = f"{p['age_hours']}h" if p["age_hours"] is not None else "?"
        print(f"  #{i}: r/{p['subreddit']} | {p['title'][:55]}... ({age})")

    result = {
        "date": now_jst.strftime("%Y-%m-%d"),
        "generated_at": now_jst.isoformat(timespec="seconds"),
        "source": "google_search",
        "total_fetched": len(posts),
        "sources_total": len(subreddits),
        "sources_ok": sources_ok,
        "sources_error": sources_error,
        "per_source_counts": per_source_counts,
        "posts": top,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    meter.flush(log_dir=Path(__file__).resolve().parent / "usage", run={"step": "fetch", "posts": len(top)})
    print(f"\nDone: {len(top)} posts saved to {args.output}")


if __name__ == "__main__":
    main()
