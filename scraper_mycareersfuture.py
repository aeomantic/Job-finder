"""
scraper_mycareersfuture.py – MyCareersFuture.gov.sg API scraper.

Strategy: call the official public REST API directly with httpx.
No browser needed.

Accepts a list of targeted search terms from the query generator.
All terms are fetched within a single httpx session; results are
deduplicated by URL before returning.

For personal, non-commercial use only.
Respect rate limits and MyCareersFuture terms of service.
"""

import asyncio
import logging
import random
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SOURCE    = "mycareersfuture"
BASE_URL  = "https://api.mycareersfuture.gov.sg/v2/jobs"
PAGE_SIZE = 100

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-SG,en;q=0.9",
    "Origin":          "https://www.mycareersfuture.gov.sg",
    "Referer":         "https://www.mycareersfuture.gov.sg/",
}

# Fallback used when no search_terms are supplied
_DEFAULT_TERM = "intern"


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _normalise(raw: dict) -> Optional[dict]:
    """Convert one raw API result object to our job dict schema."""
    uuid = raw.get("uuid", "")
    if not uuid:
        return None

    url = raw.get("externalLink") or f"https://www.mycareersfuture.gov.sg/job/{uuid}"

    date_posted = None
    metadata    = raw.get("metadata") or {}
    created_at  = metadata.get("createdAt")
    if created_at:
        date_posted = created_at[:10]

    salary_obj = raw.get("salary") or {}
    s_min      = salary_obj.get("minimum")
    s_max      = salary_obj.get("maximum")
    salary     = f"SGD {s_min or ''} – {s_max or ''}".strip(" –") if (s_min or s_max) else ""

    company_obj = raw.get("postedCompany") or {}
    company     = company_obj.get("name", "")

    location = (
        raw.get("addressFormatted", "")
        or (raw.get("address") or {}).get("location", "")
    )

    return {
        "title":       raw.get("title", "").strip(),
        "company":     company.strip(),
        "location":    location.strip(),
        "description": raw.get("description", "").strip(),
        "url":         url.strip(),
        "source":      SOURCE,
        "date_posted": date_posted,
        "salary":      salary,
    }


# ---------------------------------------------------------------------------
# Async implementation
# ---------------------------------------------------------------------------

async def _fetch_page(
    client: httpx.AsyncClient,
    page: int,
    search: str,
) -> list[dict]:
    """Fetch a single page of results for the given search term."""
    params = {
        "search": search,
        "limit":  PAGE_SIZE,
        "page":   page,
    }

    if page > 0:
        await asyncio.sleep(random.uniform(2, 4))

    try:
        resp = await client.get(BASE_URL, params=params, timeout=20)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "[%s] HTTP %s on page %d (term='%s'): %s",
            SOURCE, exc.response.status_code, page, search, exc,
        )
        return []
    except httpx.RequestError as exc:
        logger.error("[%s] Request error on page %d (term='%s'): %s", SOURCE, page, search, exc)
        return []

    raw_results = resp.json().get("results", [])
    logger.debug("[%s] term='%s' page=%d – raw results: %d", SOURCE, search, page, len(raw_results))

    return [j for j in (_normalise(r) for r in raw_results) if j and j["url"]]


async def _scrape_async(
    max_pages:    int,
    search_terms: list[str],
) -> list[dict]:
    """
    Fetch all pages for every search term within one httpx session.
    Deduplicates by URL across all terms.
    """
    all_jobs:  list[dict] = []
    seen_urls: set[str]   = set()

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        for term in search_terms:
            logger.info("[%s] Searching: '%s'", SOURCE, term)
            term_count = 0

            for page in range(max_pages):
                logger.info("[%s] '%s' – page %d / max %d", SOURCE, term, page, max_pages - 1)
                page_jobs = await _fetch_page(client, page, term)

                if not page_jobs:
                    logger.info("[%s] Empty page for '%s' – moving to next term", SOURCE, term)
                    break

                for job in page_jobs:
                    if job["url"] not in seen_urls:
                        seen_urls.add(job["url"])
                        all_jobs.append(job)
                        term_count += 1

            logger.info("[%s] Term '%s' -> %d unique jobs", SOURCE, term, term_count)

    return all_jobs


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape(
    max_pages:    int            = 10,
    search_terms: Optional[list[str]] = None,
) -> list[dict]:
    """
    Public synchronous entry point.

    Args:
        max_pages:    Maximum API pages to fetch per search term (100 results each).
        search_terms: Targeted query strings from the query generator.
                      Falls back to a generic 'intern' search if not provided.

    Returns:
        Deduplicated list of normalised job dicts.
    """
    terms = search_terms or [_DEFAULT_TERM]
    logger.info("[%s] Starting scrape (max_pages=%d, terms=%s)", SOURCE, max_pages, terms)
    jobs = asyncio.run(_scrape_async(max_pages, terms))
    logger.info("[%s] Scrape complete – %d unique jobs across %d term(s)", SOURCE, len(jobs), len(terms))
    return jobs


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    results = scrape(max_pages=2, search_terms=["backend intern", "AI intern"])
    for j in results[:3]:
        print(j["title"], "|", j["company"], "|", j["date_posted"])
    print(f"Total: {len(results)}")
