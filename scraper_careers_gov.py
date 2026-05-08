"""
scraper_careers_gov.py – jobs.careers.gov.sg scraper using Playwright.

Strategy:
  1. Navigate to the main jobs listing page.
  2. Apply the "Internship" filter (if available) or search "intern".
  3. Collect all job-detail URLs visible in the results table.
  4. Handle multi-page results by clicking Next or iterating pages.
  5. For each detail URL, open a new page and scrape full details.
  6. Exponential-backoff retry on timeout.

This is a Singapore Government public-sector job portal.
Jobs listed here are publicly accessible and intended for public viewing.

For personal, non-commercial use only.
Delay between requests: >= 5 seconds.
"""

import asyncio
import logging
import random
import re
from typing import Optional

logger = logging.getLogger(__name__)

SOURCE = "careers_gov"
BASE_URL = "https://jobs.careers.gov.sg"
LISTING_URL = f"{BASE_URL}/clp/home"

MIN_DELAY = 5.0
MAX_DELAY = 8.0
MAX_RETRIES = 3

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


async def _retry(coro_fn, retries: int = MAX_RETRIES, backoff: float = 5.0):
    """
    Run an async coroutine with exponential-backoff retries.
    coro_fn is a zero-argument callable that returns a coroutine.
    """
    for attempt in range(1, retries + 1):
        try:
            return await coro_fn()
        except Exception as exc:
            if attempt == retries:
                raise
            wait = backoff * (2 ** (attempt - 1)) + random.uniform(0, 2)
            logger.warning("Attempt %d/%d failed (%s). Retrying in %.1fs…", attempt, retries, exc, wait)
            await asyncio.sleep(wait)


async def _collect_listing_urls(page) -> list[str]:
    """
    Extract all job detail URLs from the current results page.
    Tries multiple selectors to handle potential site layout changes.
    """
    urls = set()

    # Common selectors for job listing links on careers.gov.sg
    selectors_to_try = [
        "a[href*='/clp/jobs/']",
        "a[href*='/jobs/']",
        "table tbody tr a",
        ".job-title a",
        "[class*='job'] a",
    ]

    for sel in selectors_to_try:
        links = await page.query_selector_all(sel)
        if links:
            for link in links:
                href = await link.get_attribute("href") or ""
                if href:
                    full = href if href.startswith("http") else BASE_URL + href
                    urls.add(full)
            if urls:
                logger.debug("[%s] Found %d URLs with selector: %s", SOURCE, len(urls), sel)
                break

    return list(urls)


async def _scrape_detail_page(page, url: str) -> Optional[dict]:
    """
    Open a job detail page and extract all relevant fields.
    Returns a job dict or None if extraction fails.
    """
    async def _navigate():
        await page.goto(url, wait_until="networkidle", timeout=35_000)
        await asyncio.sleep(random.uniform(1.5, 3.0))

    try:
        await _retry(_navigate)
    except Exception as exc:
        logger.error("[%s] Failed to load detail page %s: %s", SOURCE, url, exc)
        return None

    try:
        # Title
        title = ""
        for sel in ["h1", ".job-title", "[class*='title']", "h2"]:
            el = await page.query_selector(sel)
            if el:
                title = (await el.inner_text()).strip()
                if title:
                    break

        # Agency / company
        company = ""
        for sel in [".agency-name", "[class*='agency']", "[class*='ministry']", "[class*='company']"]:
            el = await page.query_selector(sel)
            if el:
                company = (await el.inner_text()).strip()
                if company:
                    break

        # Location – most gov jobs are in Singapore
        location = "Singapore"
        for sel in ["[class*='location']", "[class*='address']"]:
            el = await page.query_selector(sel)
            if el:
                loc_text = (await el.inner_text()).strip()
                if loc_text:
                    location = loc_text
                    break

        # Description – grab the largest text block
        description = ""
        for sel in [
            ".job-description",
            "[class*='description']",
            ".job-details",
            "article",
            "main",
        ]:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if len(text) > len(description):
                    description = text

        # Date posted
        date_posted = None
        for sel in ["[class*='posted']", "[class*='date']", "time"]:
            el = await page.query_selector(sel)
            if el:
                dt_str = (await el.inner_text()).strip()
                if dt_str:
                    date_posted = dt_str
                    break

        # Salary
        salary = ""
        for sel in ["[class*='salary']", "[class*='pay']"]:
            el = await page.query_selector(sel)
            if el:
                salary = (await el.inner_text()).strip()
                if salary:
                    break

        if not title:
            logger.debug("[%s] No title found for %s – skipping", SOURCE, url)
            return None

        return {
            "title":       title,
            "company":     company,
            "location":    location,
            "description": description,
            "url":         url,
            "source":      SOURCE,
            "date_posted": date_posted,
            "salary":      salary,
        }

    except Exception as exc:
        logger.error("[%s] Parse error for %s: %s", SOURCE, url, exc, exc_info=True)
        return None


_SEARCH_SELECTORS = [
    "input[type='search']",
    "input[placeholder*='Search']",
    "#search",
    ".search-input",
]

_NEXT_SELECTORS = [
    "button:has-text('Next')",
    "a:has-text('Next')",
    "[aria-label='Next']",
    ".pagination-next",
    "li.next a",
]


async def _collect_urls_for_keyword(
    list_page,
    keyword:   str,
    max_pages: int,
) -> list[str]:
    """
    Navigate to the listing page, submit a keyword search, then paginate
    through up to max_pages of results and return all collected job URLs.
    """
    logger.info("[%s] Loading listing page for keyword: '%s'", SOURCE, keyword)
    try:
        await list_page.goto(LISTING_URL, wait_until="networkidle", timeout=40_000)
        await asyncio.sleep(random.uniform(2, 3))
    except Exception as exc:
        logger.error("[%s] Failed to load listing page for '%s': %s", SOURCE, keyword, exc)
        return []

    # Submit the search
    for sel in _SEARCH_SELECTORS:
        el = await list_page.query_selector(sel)
        if el:
            logger.info("[%s] Submitting search: '%s'", SOURCE, keyword)
            await el.click()
            await el.fill(keyword)
            await asyncio.sleep(0.5)
            await list_page.keyboard.press("Enter")
            await asyncio.sleep(random.uniform(2, 4))
            break

    # Paginate and collect URLs
    collected: list[str] = []
    for page_num in range(1, max_pages + 1):
        logger.info("[%s] '%s' – collecting URLs from page %d", SOURCE, keyword, page_num)
        page_urls = await _collect_listing_urls(list_page)
        new_urls  = [u for u in page_urls if u not in collected]
        collected.extend(new_urls)
        logger.info(
            "[%s] Page %d: +%d new URLs (term total: %d)",
            SOURCE, page_num, len(new_urls), len(collected),
        )

        next_clicked = False
        for next_sel in _NEXT_SELECTORS:
            try:
                btn = await list_page.query_selector(next_sel)
                if btn:
                    is_disabled = await btn.get_attribute("disabled")
                    if not is_disabled:
                        await btn.click()
                        await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                        next_clicked = True
                        break
            except Exception:
                pass

        if not next_clicked:
            logger.info("[%s] No next page for '%s' – %d URLs collected", SOURCE, keyword, len(collected))
            break

    return collected


async def _scrape_async(
    max_pages:    int            = 5,
    search_terms: Optional[list[str]] = None,
) -> list[dict]:
    """
    Core async logic.

    For each search term, navigates to the listing page, submits the keyword,
    and collects job URLs across up to max_pages of results.  All collected
    URLs are then scraped as detail pages in a single pass.
    """
    from typing import Optional as _Opt  # avoid shadowing outer import
    try:
        from playwright.async_api import async_playwright
        from playwright_stealth import stealth_async
    except ImportError as exc:
        logger.error("[%s] Missing dependency: %s", SOURCE, exc)
        return []

    keywords   = search_terms or ["intern"]
    user_agent = random.choice(USER_AGENTS)
    all_urls:  list[str]  = []
    seen_urls: set[str]   = set()
    jobs:      list[dict] = []

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

        # ── Phase 1: collect job URLs for every search term ────────────────
        list_page = await context.new_page()
        await stealth_async(list_page)

        for keyword in keywords:
            urls = await _collect_urls_for_keyword(list_page, keyword, max_pages)
            for u in urls:
                if u not in seen_urls:
                    seen_urls.add(u)
                    all_urls.append(u)

        await list_page.close()

        if not all_urls:
            logger.warning("[%s] No listing URLs collected – check selectors or search terms", SOURCE)
            await browser.close()
            return []

        logger.info(
            "[%s] Collected %d unique URLs across %d term(s) – scraping detail pages…",
            SOURCE, len(all_urls), len(keywords),
        )

        # ── Phase 2: scrape each detail page ──────────────────────────────
        detail_page = await context.new_page()
        await stealth_async(detail_page)

        for i, url in enumerate(all_urls, 1):
            logger.info("[%s] Detail %d / %d: %s", SOURCE, i, len(all_urls), url)
            await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
            job = await _scrape_detail_page(detail_page, url)
            if job:
                jobs.append(job)

        await detail_page.close()
        await browser.close()

    return jobs


def scrape(
    max_pages:    int                  = 5,
    search_terms: Optional[list[str]] = None,
) -> list[dict]:
    """
    Public synchronous entry point.

    Args:
        max_pages:    Maximum listing pages to scan per search term.
        search_terms: Targeted query strings from the query generator.

    Returns:
        Deduplicated list of normalised job dicts.
    """
    terms = search_terms or ["intern"]
    logger.info(
        "[%s] Starting scrape (max_pages=%d per term, terms=%s)",
        SOURCE, max_pages, terms,
    )
    jobs = asyncio.run(_scrape_async(max_pages=max_pages, search_terms=terms))
    logger.info(
        "[%s] Scrape complete – %d jobs across %d term(s)",
        SOURCE, len(jobs), len(terms),
    )
    return jobs


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    results = scrape(max_pages=2, search_terms=["backend intern", "software engineer intern"])
    for j in results[:3]:
        print(j["title"], "|", j["company"], "|", j["url"])
    print(f"Total: {len(results)}")
