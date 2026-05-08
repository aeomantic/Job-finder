"""
scraper_external.py – Scrape arbitrary company career pages.

Strategy:
  1. Load each URL with Playwright + stealth (handles SPAs / JS-heavy portals).
  2. Extract raw page text from the rendered DOM.
  3. Send the text to Groq (mixtral) with a structured extraction prompt.
  4. Return a flat list of standardised job dicts.

URL configuration (priority order):
  1. EXTERNAL_CAREER_URLS env var – comma-separated list of URLs
  2. company_urls.txt in the project root – one URL per line (# = comment)

If neither is configured, scrape() returns [] and logs a warning.
"""

import asyncio
import json
import logging
import os
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

SOURCE = "external"

logger = logging.getLogger(__name__)

GROQ_MODEL    = "llama-3.1-8b-instant"
MAX_PAGE_CHARS = 6_000   # keep well under mixtral's 32k context limit
MAX_JOBS_PER_PAGE = 20   # safety cap on extracted jobs per URL


# ---------------------------------------------------------------------------
# URL configuration
# ---------------------------------------------------------------------------

def load_target_urls() -> list[str]:
    """Return the list of career page URLs from env var or company_urls.txt."""
    env_val = os.getenv("EXTERNAL_CAREER_URLS", "").strip()
    if env_val:
        return [u.strip() for u in env_val.split(",") if u.strip()]

    url_file = os.path.join(os.path.dirname(__file__), "company_urls.txt")
    if not os.path.exists(url_file):
        return []

    urls: list[str] = []
    with open(url_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


# ---------------------------------------------------------------------------
# LLM extraction prompt
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = """\
You are a job-data extraction assistant.
Given the raw text scraped from a company career page, extract every visible job posting.
Return ONLY a valid JSON array — no markdown fences, no explanatory text outside the JSON.
If no jobs are visible return an empty array: []
"""

_EXTRACT_USER_TMPL = """\
Source URL: {url}

Raw page text (may be truncated to {max_chars} characters):
---
{page_text}
---

Extract all job postings. Return a JSON array where each element has EXACTLY these keys
(use null for any field that is not present on the page):

  "title"       – job title (string, required; skip entries without a title)
  "company"     – company name inferred from the page or domain (string)
  "location"    – office location or "Remote" if stated (string | null)
  "description" – brief role description or requirements, max 400 chars (string | null)
  "url"         – direct link to this posting; fall back to the source URL (string)
  "date_posted" – ISO-8601 date if visible, e.g. "2024-05-01" (string | null)
  "salary"      – salary or range if explicitly stated (string | null)

Return ONLY the JSON array.
"""


async def _extract_jobs_llm(url: str, page_text: str, groq_client) -> list[dict]:
    """Ask Groq to parse job listings out of raw page text."""
    user_msg = _EXTRACT_USER_TMPL.format(
        url=url,
        max_chars=MAX_PAGE_CHARS,
        page_text=page_text[:MAX_PAGE_CHARS],
    )
    try:
        resp = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=2_000,
        )
        raw = resp.choices[0].message.content.strip()
        # Strip accidental markdown fences
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) >= 3 else parts[-1].strip()
        jobs = json.loads(raw)
        if not isinstance(jobs, list):
            return []
        return jobs[:MAX_JOBS_PER_PAGE]
    except Exception as exc:
        logger.error("[scraper_external] LLM extraction failed for %s: %s", url, exc)
        return []


# ---------------------------------------------------------------------------
# Page scraping
# ---------------------------------------------------------------------------

def _make_absolute(href: str, base_url: str) -> str:
    """Turn a relative href into an absolute URL using base_url."""
    if not href or href.startswith("http"):
        return href or base_url
    parsed = urlparse(base_url)
    if href.startswith("/"):
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    return base_url  # can't safely resolve other relative forms


async def _scrape_page(url: str, groq_client, browser) -> list[dict]:
    """Load one career page with Playwright and extract job dicts via Groq."""
    try:
        from playwright_stealth import stealth_async
        page = await browser.new_page()
        await stealth_async(page)
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Give JS-heavy SPAs a moment to render listings
        await page.wait_for_timeout(2_500)
        page_text = await page.inner_text("body")
        await page.close()
    except Exception as exc:
        logger.error("[scraper_external] Playwright failed for %s: %s", url, exc)
        return []

    raw_jobs = await _extract_jobs_llm(url, page_text, groq_client)

    results: list[dict] = []
    for job in raw_jobs:
        title = (job.get("title") or "").strip()
        if not title:
            continue
        results.append({
            "title":       title,
            "company":     (job.get("company") or "").strip() or None,
            "location":    (job.get("location") or "").strip() or None,
            "description": (job.get("description") or "").strip() or None,
            "url":         _make_absolute((job.get("url") or "").strip(), url),
            "source":      SOURCE,
            "date_posted": job.get("date_posted") or None,
            "salary":      (job.get("salary") or "").strip() or None,
        })

    logger.info("[scraper_external] %s -> %d job(s) extracted", url, len(results))
    return results


# ---------------------------------------------------------------------------
# Async orchestration
# ---------------------------------------------------------------------------

async def _scrape_all(urls: list[str], groq_key: str) -> list[dict]:
    from groq import AsyncGroq
    from playwright.async_api import async_playwright

    groq_client = AsyncGroq(api_key=groq_key)
    all_jobs: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        for url in urls:
            logger.info("[scraper_external] Scraping: %s", url)
            jobs = await _scrape_page(url, groq_client, browser)
            all_jobs.extend(jobs)
            await asyncio.sleep(2)   # polite delay between sites
        await browser.close()

    return all_jobs


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape(urls: Optional[list[str]] = None) -> list[dict]:
    """
    Synchronous entry point used by orchestrator.py.
    If urls is None, reads from EXTERNAL_CAREER_URLS env var or company_urls.txt.
    Returns a flat list of standardised job dicts.
    """
    target_urls = urls or load_target_urls()
    if not target_urls:
        logger.info(
            "[scraper_external] No URLs configured. "
            "Add URLs to EXTERNAL_CAREER_URLS env var or company_urls.txt."
        )
        return []

    groq_key = os.getenv("GROQ_API_KEY", "")
    if not groq_key:
        logger.warning(
            "[scraper_external] GROQ_API_KEY not set – "
            "cannot extract jobs from external pages."
        )
        return []

    return asyncio.run(_scrape_all(target_urls, groq_key))
