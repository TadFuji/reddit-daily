"""Probe whether Reddit's anonymous RSS answers from this machine.

Fetches a few feeds 62 s apart (Reddit allows one anonymous read per IP per
minute) with the same browser User-Agent as the daily fetcher, and prints the
HTTP status, the number of entries and the rate-limit headers. Reads only.
"""
import time
import urllib.error
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
URLS = [
    "https://www.reddit.com/r/popular/top.rss?t=day&limit=25",
    "https://www.reddit.com/r/all/top.rss?t=day&limit=25",
    "https://www.reddit.com/r/artificial/top.rss?t=day&limit=25",
]

ok = 0
for i, url in enumerate(URLS):
    if i:
        time.sleep(62)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            entries = body.count(b"<entry")
            print(f"{r.status} entries={entries} remaining={r.headers.get('x-ratelimit-remaining')} {url}")
            ok += entries > 0
    except urllib.error.HTTPError as e:
        print(f"{e.code} {e.reason} server={e.headers.get('server')} {url}")
    except Exception as e:  # noqa: BLE001
        print(f"ERROR {type(e).__name__}: {e} {url}")
print(f"RESULT {ok}/{len(URLS)} feeds readable")
