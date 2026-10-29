#!/usr/bin/env python3
"""
Reddit Data Fetcher (RSS edition) — Collects top posts via public RSS feeds.

Reddit blocks anonymous .json API access (403 Blocked) and restricts the
official OAuth Data API to moderation use cases, so this fetcher uses the
public RSS/Atom feeds instead. RSS needs no auth, but exposes only title,
link, subreddit, author and timestamp — NOT upvotes, comment counts, or
comment bodies. Downstream report generation is the lightweight variant.

Usage:
    python fetch_data.py --output data.json
    python fetch_data.py --config config_ai.json --output ai_data.json
"""

import json
import time
import sys
import re
import argparse
import urllib.error
import urllib.request
import defusedxml.ElementTree as ET
from html import unescape
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Report dates must be Japan time regardless of the server's local timezone
# (the orchestrator's daily dedup also assumes JST).
JST = timezone(timedelta(hours=9), name="JST")


DEFAULT_SOURCES = [
    "https://www.reddit.com/r/popular/top.rss?t=day&limit=25",
    "https://www.reddit.com/r/all/top.rss?t=day&limit=25",
]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
ATOM = "{http://www.w3.org/2005/Atom}"

# Reddit rate-limits anonymous feed reads per IP: one request, then
# x-ratelimit-remaining is 0 with a reset ~46-60s away (measured on the VPS,
# 2026-09-26, same for a browser and a self-identifying User-Agent). Sources
# were polled 2s apart, so 17-18 of the 24 daily feeds returned 429 every
# morning since the first VPS run. Wait out the window instead.
REQUEST_INTERVAL = 62


def to_rss(url: str) -> str:
    """Convert a legacy .json source URL to its .rss equivalent."""
    return url.replace(".json", ".rss")


def fetch_rss(url: str) -> tuple[bytes | None, str]:
    """Fetch raw RSS/Atom bytes, retrying only errors a retry can fix.

    A 429 means the per-IP window is still closed, and it stays closed far
    longer than any retry we can afford here, so retrying it only burned ~2
    minutes per run. Give that source up and let the next one wait out the
    full interval.

    Returns (bytes or None, outcome) where outcome is "ok", "rate_limited" or
    "error". The caller counts the outcomes into the run's health record: a
    run that quietly loses most of its sources is the failure mode this whole
    pipeline was blind to for two months.
    """
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read(), "ok"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"  RATE LIMITED (no retry): {url}: {e}", file=sys.stderr)
                return None, "rate_limited"
            if attempt < 2:
                print(f"  Retry {attempt + 1}: {e}", file=sys.stderr)
                time.sleep(5)
            else:
                print(f"  FAILED: {url}: {e}", file=sys.stderr)
        except Exception as e:
            if attempt < 2:
                print(f"  Retry {attempt + 1}: {e}", file=sys.stderr)
                time.sleep(5)
            else:
                print(f"  FAILED: {url}: {e}", file=sys.stderr)
    return None, "error"


def _strip_html(html: str) -> str:
    """Strip tags/boilerplate from a feed entry's content to recover body text."""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"submitted by\s*/u/\S+", "", text, flags=re.IGNORECASE)
    text = text.replace("[link]", "").replace("[comments]", "")
    return text.strip()[:1500]


def parse_entries(xml_bytes: bytes) -> list[dict]:
    """Parse an Atom feed into a list of post dicts, preserving feed order."""
    root = ET.fromstring(xml_bytes)
    entries = []
    for e in root.findall(f"{ATOM}entry"):
        def txt(tag: str) -> str | None:
            el = e.find(f"{ATOM}{tag}")
            return el.text if el is not None else None

        raw_id = txt("id") or ""
        pid = raw_id.split("_", 1)[1] if "_" in raw_id else raw_id  # t3_abc -> abc
        link_el = e.find(f"{ATOM}link")
        href = link_el.get("href") if link_el is not None else ""
        cat_el = e.find(f"{ATOM}category")
        subreddit = cat_el.get("term") if cat_el is not None else ""
        auth_el = e.find(f"{ATOM}author/{ATOM}name")
        author = auth_el.text if auth_el is not None else ""
        content_el = e.find(f"{ATOM}content")
        content_html = (content_el.text or "") if content_el is not None else ""

        entries.append({
            "id": pid,
            "title": unescape(txt("title") or ""),
            "subreddit": subreddit,
            "url": href,
            "author": author,
            "published": txt("published") or txt("updated") or "",
            "selftext": _strip_html(content_html),
        })
    return entries


def age_hours(published: str, now_ts: float) -> float | None:
    """Compute post age in hours from an ISO8601 timestamp."""
    try:
        dt = datetime.fromisoformat(published)
        return max((now_ts - dt.timestamp()) / 3600, 0.5)
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Fetch Reddit trending data via RSS")
    parser.add_argument("--config", type=str, help="Config JSON file path")
    parser.add_argument("--output", type=str, required=True, help="Output JSON file path")
    args = parser.parse_args()

    sources = list(DEFAULT_SOURCES)
    top_n = 10
    blocked = set()
    if args.config:
        config_path = Path(args.config)
        if not config_path.is_absolute():
            config_path = Path(__file__).parent / config_path
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if "reddit_sources" in cfg:
            sources = [to_rss(u) for u in cfg["reddit_sources"]]
        top_n = cfg.get("top_n", 10)
        blocked = {s.lower() for s in cfg.get("blocked_subreddits", [])}

    print(f"Fetching from {len(sources)} RSS source(s)...")

    now = datetime.now(timezone.utc).timestamp()

    # Fetch every source, keeping each feed's ranking intact. The wait goes
    # before the request (not after) so a rate-limited or failed source still
    # pays it, and the last source does not delay the rest of the pipeline.
    per_source: list[list[dict]] = []
    outcomes = {"ok": 0, "rate_limited": 0, "error": 0, "parse_failed": 0}
    for i, url in enumerate(sources):
        if i:
            time.sleep(REQUEST_INTERVAL)
        rss_url = to_rss(url)
        label = rss_url.split("reddit.com")[1][:60] if "reddit.com" in rss_url else rss_url[:60]
        print(f"  Fetching: {label}...")
        xml, outcome = fetch_rss(rss_url)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if not xml:
            per_source.append([])
            continue
        try:
            entries = parse_entries(xml)
        except Exception as e:
            print(f"  PARSE FAILED: {label}: {e}", file=sys.stderr)
            outcomes["parse_failed"] += 1
            outcomes["ok"] -= 1
            entries = []
        per_source.append(entries)

    # Round-robin merge by within-feed rank so no single subreddit dominates
    # and each feed's #1 surfaces first. RSS has no cross-feed score.
    seen: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()
    posts: list[dict] = []
    max_len = max((len(s) for s in per_source), default=0)
    for rank in range(max_len):
        for feed in per_source:
            if rank >= len(feed):
                continue
            d = feed[rank]
            pid = d["id"]
            if not pid or pid in seen:
                continue
            if d["subreddit"].lower() in blocked:
                continue
            # Reposts/crossposts get fresh post ids, so also dedupe on
            # (author, normalized title); an empty title never matches.
            title_key = re.sub(r"[\W_]+", "", d["title"].lower())
            if title_key and (d["author"], title_key) in seen_titles:
                continue
            seen.add(pid)
            if title_key:
                seen_titles.add((d["author"], title_key))
            ah = age_hours(d["published"], now)
            posts.append({
                "id": pid,
                "title": d["title"],
                "subreddit": d["subreddit"],
                "author": d["author"],
                "url": d["url"],
                "selftext": d["selftext"],
                "age_hours": round(ah, 1) if ah is not None else None,
                # Engagement metrics are unavailable over RSS; kept as nulls
                # for downstream-schema compatibility (lightweight report
                # does not display these).
                "ups": None,
                "num_comments": None,
                "comments": [],
                # Rank proxy preserves feed ordering for any score-based sort.
                "buzz_score": max_len - rank,
            })

    print(
        f"  Total posts collected: {len(posts)} "
        f"(sources ok {outcomes['ok']}/{len(sources)}, "
        f"rate limited {outcomes['rate_limited']}, other errors {outcomes['error']})"
    )

    if len(posts) < 3:
        print(f"ERROR: Only {len(posts)} posts fetched. Aborting.", file=sys.stderr)
        sys.exit(1)

    top = posts[:top_n]
    for i, p in enumerate(top, 1):
        age = f"{p['age_hours']}h" if p["age_hours"] is not None else "?"
        print(f"  #{i}: r/{p['subreddit']} | {p['title'][:55]}... ({age})")

    result = {
        "date": datetime.now(JST).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "source": "rss",
        "total_fetched": len(posts),
        # Per-source outcome counts for the run's health record (2026-09-26).
        "sources_total": len(sources),
        "sources_ok": outcomes["ok"],
        "sources_rate_limited": outcomes["rate_limited"],
        "sources_error": outcomes["error"] + outcomes["parse_failed"],
        "posts": top,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\nDone: {len(top)} posts saved to {args.output}")


if __name__ == "__main__":
    main()
