#!/usr/bin/env python3
"""
Fetch all missing Danish Prime Minister New Year speeches (Statsministerens nytårstaler)
from stm.dk using their DynamicListSearchApi endpoint.

Behavior:
- Calls STM DynamicListSearchApi search endpoint (no cookies).
- Recursively scans the returned JSON for URLs that:
  - are on stm.dk
  - look like speech pages
  - contain a year
- Collects unique (year -> url) candidates.
- For each candidate year:
  - if taler/<YEAR>.md does NOT exist -> fetch page + extract text -> write md
- Writes many files in one run.
- If nothing new is missing, does nothing.

Deps:
  pip install requests beautifulsoup4
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


API_URL = "https://stm.dk/umbraco/api/DynamicListSearchApi/search"
BASE_URL = "https://stm.dk"

# From your curl. These can change if STM changes their setup.
CURRENT_PAGE_ID = 19204
MODULE_ID = "f9e1f002-8433-4e61-bc59-d73b7469e5e0"

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b", re.IGNORECASE)

# URL/path hints for the type of pages we want
SPEECH_PATH_HINTS = (
    "/statsministeren/taler/",
    "/statsministeren/nytaarstaler-siden-1940/",
)
NYTAAR_HINTS = (
    "nytaarstale",
    "nytårstale",
    "nytår",
)

# How many API pages to try (increase if needed)
MAX_PAGES = 20

# Politeness: tiny delay between requests for individual pages
REQUEST_DELAY_SECONDS = 0.3


def post_json(page: int, search_term: str = "nytår") -> dict[str, Any]:
    payload = {
        "SearchTerm": search_term,
        "currentpageid": CURRENT_PAGE_ID,
        "ModuleId": MODULE_ID,
        "GlobalPageId": None,
        "Culture": "da",
        "DateFormat": "d",
        "Page": str(page),
    }

    r = requests.post(
        API_URL,
        json=payload,
        timeout=30,
        headers={
            "User-Agent": "statsministerens-nytaarstaler-bot/1.0",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": "https://stm.dk/statsministeren/taler/",
        },
    )
    r.raise_for_status()

    try:
        return r.json()
    except Exception:
        raise RuntimeError("API svarede ikke med JSON. Første 500 chars:\n" + r.text[:500])


def iter_strings(obj: Any) -> Iterable[str]:
    """Yield all string values found recursively in a JSON-like structure."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_strings(v)
    elif isinstance(obj, str):
        yield obj


def normalize_url(s: str) -> str | None:
    s = s.strip()
    if not s:
        return None

    if s.startswith("http://") or s.startswith("https://"):
        return s

    if s.startswith("/"):
        return urljoin(BASE_URL, s)

    return None


def is_stm_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return host == "stm.dk" or host.endswith(".stm.dk")
    except Exception:
        return False


def looks_like_new_year_speech_url(url: str) -> bool:
    u = url.lower()

    if not is_stm_url(url):
        return False

    # Must look like a speech-ish path
    if not any(h in u for h in SPEECH_PATH_HINTS):
        return False

    # Should include a new-year hint
    if not any(h in u for h in NYTAAR_HINTS):
        return False

    # Must contain a year
    if not YEAR_RE.search(u):
        return False

    return True


def collect_candidates_from_api() -> dict[int, str]:
    """
    Collect unique (year -> url) candidates by paging API results.
    We don’t assume any fixed JSON schema; we scan all strings.
    """
    candidates: dict[int, str] = {}

    empty_pages_in_a_row = 0

    for page in range(1, MAX_PAGES + 1):
        data = post_json(page=page, search_term="nytår")

        found_on_page = 0
        for s in iter_strings(data):
            url = normalize_url(s)
            if not url:
                continue

            if looks_like_new_year_speech_url(url):
                m = YEAR_RE.search(url)
                if not m:
                    continue
                year = int(m.group(0))

                # keep first seen (or overwrite—doesn't matter much)
                candidates.setdefault(year, url)
                found_on_page += 1

        # Heuristic stop: if we see nothing for a couple of pages, stop early
        if found_on_page == 0:
            empty_pages_in_a_row += 1
        else:
            empty_pages_in_a_row = 0

        if empty_pages_in_a_row >= 2 and page >= 3:
            break

    return candidates


def get_html(url: str) -> str:
    r = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": "statsministerens-nytaarstaler-bot/1.0"},
    )
    r.raise_for_status()
    return r.text


def extract_title_and_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else "Statsministerens nytårstale"

    main = soup.find("main") or soup
    paragraphs = [p.get_text(" ", strip=True) for p in main.find_all("p")]
    text = "\n\n".join([p for p in paragraphs if p])

    # sanity check
    if len(text) < 400:
        raise RuntimeError("Udtræk gav meget lidt tekst (markup kan have ændret sig).")

    return title, text


def write_markdown(year: int, title: str, source_url: str, text: str) -> Path:
    out_dir = Path("taler")
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"{year}.md"

    fetched = datetime.now().strftime("%Y-%m-%d")
    md = f"""# {title}

Kilde: {source_url}
Hentet: {fetched}

---

{text}
"""
    out_file.write_text(md, encoding="utf-8")
    return out_file


def main() -> int:
    candidates = collect_candidates_from_api()

    if not candidates:
        debug = post_json(page=1, search_term="nytår")
        Path("debug_api_response.json").write_text(
            json.dumps(debug, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise RuntimeError("Fandt ingen nytårstale-links via API. Skrev debug_api_response.json")

    out_dir = Path("taler")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Determine what's missing
    years_sorted = sorted(candidates.keys(), reverse=True)
    missing = [y for y in years_sorted if not (out_dir / f"{y}.md").exists()]

    print(f"Fandt {len(candidates)} kandidater: {min(years_sorted)}–{max(years_sorted)}")
    print(f"Mangler {len(missing)} filer i taler/: {missing[:15]}{'...' if len(missing) > 15 else ''}")

    if not missing:
        print("OK: Intet mangler. Ingen ændringer.")
        return 0

    # Fetch + write each missing year
    wrote = 0
    for year in sorted(missing):  # oldest -> newest (nicer history)
        url = candidates[year]
        try:
            print(f"Henter {year}: {url}")
            html = get_html(url)
            title, text = extract_title_and_text(html)
            path = write_markdown(year, title, url, text)
            print(f"Skrev: {path}")
            wrote += 1
            time.sleep(REQUEST_DELAY_SECONDS)
        except Exception as e:
            # Don't kill whole run; just report and continue
            print(f"WARNING: Kunne ikke hente {year} ({url}): {e}", file=sys.stderr)

    if wrote == 0:
        print("Fandt mangler, men kunne ikke skrive nogen filer (se warnings).")
        return 1

    print(f"Done. Skrev {wrote} filer.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.RequestException as e:
        print(f"HTTP ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)