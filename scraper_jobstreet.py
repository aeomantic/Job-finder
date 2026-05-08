"""
scraper_jobstreet.py – JobStreet Singapore internship scraper.

Strategy: JobStreet SG is protected by Cloudflare's JS challenge, which
blocks plain httpx requests (HTTP 403).  We use Playwright + playwright-stealth
to pass the challenge transparently, then extract job listings from the page.

Two extraction layers (tried in order):
  1. Parse __NEXT_DATA__ JSON embedded in the HTML (fast, complete).
  2. Fall back to scraping the rendered DOM (slower, more resilient).

Accepts a list of targeted search terms from the query generator.
All terms are searched within a SINGLE browser session to minimise
Playwright startup overhead.  Results are deduplicated by URL.

For personal, non-commercial use only.
Respect rate limits and JobStreet's terms of service.
"""

import asyncio
import json
import logging
import random
from typing import Optional
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

SOURCE     = "jobstreet"
BASE_URL   = "https://sg.jobstreet.com"
SEARCH_URL = f"{BASE_URL}/jobs/in-Singapore"

MIN_DELAY = 3.0
MAX_DELAY = 6.0

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

_DEFAULT_TERM = "internship"


# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------

def _find_jobs_in_next_data(data) -> list[dict]:
    """Recursively search the __NEXT_DATA__ blob for the job array."""
    if isinstance(data, list):
        if data and isinstance(data[0], dict) and (
            "title" in data[0] or "id" in data[0] or "listingId" in data[0]
        ):
            return data
        for item in data:
            result = _find_jobs_in_next_data(item)
            if result:
                return result
    elif isinstance(data, dict):
        for key in ("jobs", "jobSummaries", "results", "data", "listings"):
            if key in data and isinstance(data[key], list) and data[key]:
                candidate = data[key]
                if isinstance(candidate[0], dict) and (
                    "title" in candidate[0] or "id" in candidate[0]
                ):
                    return candidate
        for value in data.values():
            if isinstance(value, (dict, list)):
                result = _find_jobs_in_next_data(value)
                if result:
                    return result
    return []


def _normalise(raw: dict) -> Optional[dict]:
    title = (raw.get("title") or "").strip()
    if not title:
        return None

    job_id  = raw.get("id") or raw.get("listingId") or raw.get("jobId") or ""
    job_url = raw.get("jobUrl") or raw.get("url") or ""
    if not job_url and job_id:
        job_url = f"{BASE_URL}/job/{job_id}"
    if job_url.startswith("/"):
        job_url = BASE_URL + job_url
    if not job_url:
        return None

    advertiser = raw.get("advertiser") or raw.get("company") or {}
    company    = advertiser if isinstance(advertiser, str) else (
        advertiser.get("description") or advertiser.get("name") or ""
    )

    location_obj = raw.get("location") or raw.get("locationLabel") or {}
    location     = location_obj if isinstance(location_obj, str) else (
        location_obj.get("label") or location_obj.get("description") or "Singapore"
    )

    date_posted = None
    for key in ("listingDate", "postedAt", "listedAt", "datePosted"):
        val = raw.get(key)
        if val:
            date_posted = str(val)[:10]
            break

    salary_obj = raw.get("salary") or {}
    if isinstance(salary_obj, str):
        salary = salary_obj
    elif isinstance(salary_obj, dict):
        label = salary_obj.get("label") or salary_obj.get("description") or ""
        if label:
            salary = label
        else:
            s_min  = salary_obj.get("minimum")
            s_max  = salary_obj.get("maximum")
            salary = f"SGD {s_min or ''} - {s_max or ''}".strip(" -") if (s_min or s_max) else ""
    else:
        salary = ""

    description = (
        raw.get("teaser") or raw.get("abstract") or raw.get("description") or ""
    ).strip()

    return {
        "title":       title,
        "company":     str(company).strip(),
        "location":    str(location).strip(),
        "description": description,
        "url":         job_url.strip(),
        "source":      SOURCE,
        "date_posted": date_posted,
        "salary":      salary,
    }


# ---------------------------------------------------------------------------
# DOM fallback parser
# ---------------------------------------------------------------------------

async def _extract_from_dom(page) -> list[dict]:
    """Fallback: extract job data directly from the rendered DOM."""
    jobs = []
    card_selectors = [
        "article[data-automation='normalJob']",
        "article[data-automation]",
        "[data-testid='job-card']",
        ".job-card",
        "article",
    ]

    cards = []
    for sel in card_selectors:
        cards = await page.query_selector_all(sel)
        if cards:
            logger.debug("[%s] DOM fallback: %d cards with selector '%s'", SOURCE, len(cards), sel)
            break

    for card in cards:
        try:
            title_el = await card.query_selector(
                "[data-automation='jobTitle'], h3, h2, .job-title"
            )
            title = (await title_el.inner_text()).strip() if title_el else ""
            if not title:
                continue

            link_el = await card.query_selector("a[href]")
            href    = await link_el.get_attribute("href") if link_el else ""
            url     = (BASE_URL + href) if href.startswith("/") else href
            if not url:
                continue

            co_el   = await card.query_selector(
                "[data-automation='jobCompany'], [data-automation='advertiser-name'], .company-name"
            )
            company = (await co_el.inner_text()).strip() if co_el else ""

            loc_el   = await card.query_selector(
                "[data-automation='jobLocation'], [data-automation='job-location'], .location"
            )
            location = (await loc_el.inner_text()).strip() if loc_el else "Singapore"

            sal_el = await card.query_selector("[data-automation='job-salary'], .salary")
            salary = (await sal_el.inner_text()).strip() if sal_el else ""

            jobs.append({
                "title":       title,
                "company":     company,
                "location":    location,
                "description": "",
                "url":         url,
                "source":      SOURCE,
                "date_posted": None,
                "salary":      salary,
            })
        except Exception as exc:
            logger.debug("[%s] DOM card error: %s", SOURCE, exc)

    return jobs


# ---------------------------------------------------------------------------
# Per-page scraper
# ---------------------------------------------------------------------------

async def _scrape_page(
    page,
    stealth_async,
    page_num:    int,
    search_term: str,
) -> list[dict]:
    """Navigate to one search-results page for a specific term and extract jobs."""
    encoded  = quote_plus(search_term)
    url      = f"{SEARCH_URL}?keywords={encoded}&sortmode=ListedDate&pg={page_num}"

    if page_num > 1:
        await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    logger.info("[%s] '%s' – page %d", SOURCE, search_term, page_num)
    try:
        await page.goto(url, wait_until="networkidle", timeout=40_000)
        await asyncio.sleep(random.uniform(1.5, 3.0))
    except Exception as exc:
        logger.warning("[%s] Navigation error ('%s' page %d): %s", SOURCE, search_term, page_num, exc)
        return []

    # Layer 1: __NEXT_DATA__
    next_data_raw = await page.evaluate(
        "() => { const el = document.getElementById('__NEXT_DATA__'); return el ? el.textContent : null; }"
    )
    if next_data_raw:
        try:
            data     = json.loads(next_data_raw)
            raw_jobs = _find_jobs_in_next_data(data)
            if raw_jobs:
                jobs = [j for j in (_normalise(r) for r in raw_jobs) if j]
                logger.debug(
                    "[%s] '%s' page %d: %d jobs from __NEXT_DATA__",
                    SOURCE, search_term, page_num, len(jobs),
                )
                return jobs
        except Exception as exc:
            logger.debug("[%s] __NEXT_DATA__ parse error: %s", SOURCE, exc)

    # Layer 2: DOM fallback
    logger.debug("[%s] '%s' page %d: falling back to DOM", SOURCE, search_term, page_num)
    jobs = await _extract_from_dom(page)
    logger.debug("[%s] '%s' page %d: %d jobs from DOM", SOURCE, search_term, page_num, len(jobs))
    return jobs


# ---------------------------------------------------------------------------
# Core async scraper
# ---------------------------------------------------------------------------

async def _scrape_async(
    max_pages:    int,
    search_terms: list[str],
) -> list[dict]:
    """
    Search for all terms within a single browser session.
    Deduplicates by URL across all terms.
    """
    try:
        from playwright.async_api import async_playwright
        from playwright_stealth import stealth_async
    except ImportError as exc:
        logger.error("[%s] Missing dependency: %s", SOURCE, exc)
        return []

    all_jobs:  list[dict] = []
    seen_urls: set[str]   = set()
    user_agent = random.choice(USER_AGENTS)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent  = user_agent,
            viewport    = {"width": 1366, "height": 768},
            locale      = "en-SG",
            timezone_id = "Asia/Singapore",
        )
        page = await context.new_page()
        await stealth_async(page)

        for term in search_terms:
            logger.info("[%s] Searching: '%s'", SOURCE, term)
            term_count = 0

            for page_num in range(1, max_pages + 1):
                page_jobs = await _scrape_page(page, stealth_async, page_num, term)

                if not page_jobs:
                    logger.info("[%s] No results for '%s' on page %d – next term", SOURCE, term, page_num)
                    break

                for job in page_jobs:
                    if job["url"] not in seen_urls:
                        seen_urls.add(job["url"])
                        all_jobs.append(job)
                        term_count += 1

                logger.info("[%s] Running total: %d unique jobs", SOURCE, len(all_jobs))

            logger.info("[%s] Term '%s' -> %d unique new jobs", SOURCE, term, term_count)

        await browser.close()

    return all_jobs


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape(
    results_wanted: int            = 100,
    search_terms:   Optional[list[str]] = None,
) -> list[dict]:
    """
    Public synchronous entry point.

    Args:
        results_wanted: Approximate max jobs per search term (rounded to pages).
        search_terms:   Targeted query strings from the query generator.

    Returns:
        Deduplicated list of normalised job dicts.
    """
    terms     = search_terms or [_DEFAULT_TERM]
    max_pages = max(1, (results_wanted + 29) // 30)
    logger.info(
        "[%s] Starting scrape (max_pages=%d per term, terms=%s)",
        SOURCE, max_pages, terms,
    )
    jobs = asyncio.run(_scrape_async(max_pages, terms))
    logger.info(
        "[%s] Scrape complete – %d unique jobs across %d term(s)",
        SOURCE, len(jobs), len(terms),
    )
    return jobs


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    results = scrape(results_wanted=30, search_terms=["backend intern", "java developer intern"])
    for j in results[:5]:
        print(j["title"], "|", j["company"], "|", j["url"])
    print(f"Total: {len(results)}")
