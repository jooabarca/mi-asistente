"""Free fact lookups on Wikipedia (no key needed), so the fact checker can verify before correcting.

Uses the MediaWiki API: one request searches and returns the opening paragraph of the best matches.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

USER_AGENT = "MiAsistente/1.0 (personal voice assistant; local app)"  # Wikimedia asks for a descriptive agent
TIMEOUT_S = 6


def lookup(query: str, language: str = "es", limit: int = 3) -> list[dict]:
    """Return up to `limit` results as {"Title", "Summary", "Url"}. Raises OSError on network problems."""
    language = language if re.fullmatch(r"[a-z]{2,3}", language or "") else "es"
    params = urllib.parse.urlencode({
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "generator": "search",
        "gsrsearch": query,
        "gsrlimit": limit,
        "prop": "extracts|info",
        "exintro": 1,
        "explaintext": 1,
        "exchars": 900,
        "exlimit": limit,
        "inprop": "url",
        "redirects": 1,
    })
    request = urllib.request.Request(f"https://{language}.wikipedia.org/w/api.php?{params}",
                                     headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        data = json.loads(response.read().decode("utf-8"))
    pages = sorted((data.get("query") or {}).get("pages") or [], key=lambda p: p.get("index", 99))
    return [
        {"Title": page.get("title", ""), "Summary": (page.get("extract") or "").strip(), "Url": page.get("fullurl", "")}
        for page in pages
        if page.get("extract")
    ]
