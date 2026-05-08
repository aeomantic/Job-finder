"""
llm_agents.py – Shared LLM utility agents powered by GitHub Models (Azure AI Inference).

Two agents:
  sanitise_metadata_with_ministral  – Cleans raw job listing text into structured
    metadata (date_posted, location, salary_range, company_name) using Ministral-3B.
    Fast, cheap, and deterministic (temperature=0).

  verify_company_with_cohere  – Determines whether an employer is a legitimate
    registered business by combining DuckDuckGo search snippets with Cohere
    Command R's grounded reasoning.

Environment variable required:
  GITHUB_TOKEN  – GitHub Personal Access Token with `models:read` scope.
                  Obtain one at: https://github.com/settings/tokens
                  This token is used as the API key for the Azure AI Inference
                  endpoint that backs GitHub Models.

Usage (both functions are async):
    from llm_agents import sanitise_metadata_with_ministral, verify_company_with_cohere

    cleaned = await sanitise_metadata_with_ministral(raw_page_text)
    verdict = await verify_company_with_cohere("Acme Corp", job_description, job_title)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GitHub Models / Azure AI Inference configuration
# ---------------------------------------------------------------------------

_GITHUB_ENDPOINT = "https://models.inference.ai.azure.com"
_MINISTRAL_MODEL = "Ministral-3B"
_COHERE_MODEL    = "Cohere-command-r-08-2024"

_DDGS_MAX_RESULTS = 3


def _get_github_client():
    """
    Return an AsyncOpenAI client pointed at the GitHub Models inference endpoint.
    Raises EnvironmentError if GITHUB_TOKEN is not set.
    """
    from openai import AsyncOpenAI
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise EnvironmentError(
            "GITHUB_TOKEN is not set. "
            "Create a PAT at https://github.com/settings/tokens and add it to .env"
        )
    return AsyncOpenAI(base_url=_GITHUB_ENDPOINT, api_key=token)


def _strip_fences(text: str) -> str:
    """
    Remove markdown code fences (```json ... ``` or ``` ... ```) from
    model output before passing it to json.loads().
    Handles both single-line and multi-line fence variants robustly.
    """
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Agent 1 – Metadata Sanitisation (Ministral-3B)
# ---------------------------------------------------------------------------

_SANITISE_SYSTEM = """\
You are a data sanitisation specialist for job listing metadata.
Extract exactly the four fields listed below from the raw job listing text provided.
Apply strict data normalisation rules.
Respond ONLY with a valid JSON object – no markdown fences, no commentary.\
"""

_SANITISE_USER_TMPL = """\
RAW JOB LISTING TEXT:
---
{raw_text}
---

Extract and normalise these four fields:

1. "date_posted"   – ISO-8601 date string (YYYY-MM-DD).
                     Convert relative terms: "today" or "just posted" → today's date.
                     Use null if absent or ambiguous.
2. "location"      – Full location string. Default to "Singapore" if no specific
                     location is mentioned. Strip noise like "Posted in:" prefixes.
3. "salary_range"  – Human-readable salary string (e.g. "SGD 1,500 – 2,000 / month",
                     "SGD 4,500 / month", "Unpaid"). Use null if not mentioned.
4. "company_name"  – Clean company name as it appears on official documents.
                     Use null if not clearly stated.

Output ONLY this JSON object (no other text):
{{"date_posted": "YYYY-MM-DD" | null, "location": "...", \
"salary_range": "..." | null, "company_name": "..." | null}}\
"""


async def sanitise_metadata_with_ministral(raw_text: str) -> dict:
    """
    Use Ministral-3B via GitHub Models to extract and normalise job metadata
    from the raw inner-text of a job listing page.

    Falls back to empty defaults on any error (network, API key, parse failure)
    so the caller is never blocked – raw scraped data will persist unchanged.

    Args:
        raw_text: Raw inner-text of the job listing page or description container.

    Returns:
        {
          "date_posted":  str | None,   # ISO-8601 (YYYY-MM-DD) or None
          "location":     str | None,
          "salary_range": str | None,
          "company_name": str | None,
        }
    """
    _FALLBACK: dict = {
        "date_posted":  None,
        "location":     None,
        "salary_range": None,
        "company_name": None,
    }

    if not raw_text or not raw_text.strip():
        return _FALLBACK

    try:
        client = _get_github_client()
    except EnvironmentError as exc:
        logger.warning("[llm_agents] Ministral skipped – %s", exc)
        return _FALLBACK

    user_msg = _SANITISE_USER_TMPL.format(raw_text=raw_text[:4_000])

    try:
        resp = await client.chat.completions.create(
            model    = _MINISTRAL_MODEL,
            messages = [
                {"role": "system", "content": _SANITISE_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            temperature = 0.0,
            max_tokens  = 200,
        )
        raw_output = resp.choices[0].message.content.strip()
        logger.debug("[llm_agents] Ministral raw output: %r", raw_output)

        parsed = json.loads(_strip_fences(raw_output))

        result = {
            "date_posted":  parsed.get("date_posted")  or None,
            "location":     parsed.get("location")     or None,
            "salary_range": parsed.get("salary_range") or None,
            "company_name": parsed.get("company_name") or None,
        }
        logger.debug("[llm_agents] Sanitised metadata: %s", result)
        return result

    except Exception as exc:
        logger.warning(
            "[llm_agents] sanitise_metadata_with_ministral failed (%s) – keeping raw metadata",
            exc,
        )
        return _FALLBACK


# ---------------------------------------------------------------------------
# Agent 2 – Company Verification (Cohere Command R)
# ---------------------------------------------------------------------------

_VERIFY_SYSTEM = """\
You are a due-diligence assistant screening job postings for scams and fraudulent employers.
Analyse the web search results and job details provided to determine whether the company
is a legitimate registered business (MNC, SME, or government agency).
Respond ONLY with a valid JSON object – no markdown fences, no text outside the JSON.\
"""

_VERIFY_USER_TMPL = """\
COMPANY NAME: {company}

JOB DETAILS:
Title:       {title}
Description: {description}

WEB SEARCH RESULTS ({n} snippets for "{company} company Singapore"):
{snippets}

Red flags that strongly indicate a scam or fraudulent listing:
  - Applicant required to pay upfront fees or deposits
  - Requests for personal banking or financial account details
  - Unrealistically high salary with no required experience
  - Vague or non-existent company details (no address, no registration number)
  - Zero credible online presence, or results contradict the company's claims

Output ONLY this JSON object (no other text):
{{"is_legitimate": true | false, \
"confidence": "High" | "Medium" | "Low", \
"verification_notes": "<1-2 sentence evidence summary>"}}\
"""


async def _search_company_ddg(company_name: str) -> str:
    """
    Return formatted DuckDuckGo search snippets for the given company.
    Runs the synchronous DDGS client inside a thread-pool executor so it
    does not block the asyncio event loop.
    """
    from duckduckgo_search import DDGS

    query = f"{company_name} company Singapore"

    def _sync_search() -> list[dict]:
        try:
            return list(DDGS().text(query, max_results=_DDGS_MAX_RESULTS))
        except Exception as exc:
            logger.warning("[llm_agents] DuckDuckGo search failed for '%s': %s", query, exc)
            return []

    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, _sync_search)

    if not results:
        return "No search results found."

    return "\n".join(
        f"{i}. {r.get('title', '')} – {r.get('body', '')}"
        for i, r in enumerate(results, 1)
    )


async def verify_company_with_cohere(
    company_name: str,
    job_description: str,
    job_title: str = "",
) -> dict:
    """
    Use Cohere Command R + DuckDuckGo to verify whether an employer is legitimate.

    Searches DuckDuckGo for the company name, then passes the top snippets to
    Cohere Command R for a grounded legitimacy judgement.  Cohere Command R is
    specifically designed for retrieval-augmented tasks, making it well-suited
    to reasoning over web search evidence.

    Defaults to is_legitimate=True on any error to avoid silently suppressing
    genuine job opportunities.

    Args:
        company_name:    The employer's name as scraped from the job listing.
        job_description: Full job description text (used for scam-signal analysis).
        job_title:       Optional job title for additional context.

    Returns:
        {
          "is_legitimate":      bool,
          "confidence":         str,   # "High" | "Medium" | "Low"
          "verification_notes": str,
        }
    """
    _SAFE_DEFAULT: dict = {
        "is_legitimate":      True,
        "confidence":         "Low",
        "verification_notes": "Verification could not be completed – defaulting to legitimate.",
    }

    company_clean = (company_name or "").strip()
    if not company_clean or company_clean.lower() in ("unknown", "n/a", ""):
        return {
            "is_legitimate":      True,
            "confidence":         "Low",
            "verification_notes": "Company name unavailable – verification skipped.",
        }

    try:
        client = _get_github_client()
    except EnvironmentError as exc:
        logger.warning("[llm_agents] Cohere verification skipped – %s", exc)
        return _SAFE_DEFAULT

    snippets = await _search_company_ddg(company_clean)

    user_msg = _VERIFY_USER_TMPL.format(
        company     = company_clean,
        title       = job_title or "N/A",
        description = job_description[:500],
        n           = _DDGS_MAX_RESULTS,
        snippets    = snippets,
    )

    try:
        resp = await client.chat.completions.create(
            model    = _COHERE_MODEL,
            messages = [
                {"role": "system", "content": _VERIFY_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            temperature = 0.1,
            max_tokens  = 250,
        )
        raw_output = resp.choices[0].message.content.strip()
        logger.debug("[llm_agents] Cohere verification raw output: %r", raw_output)

        parsed = json.loads(_strip_fences(raw_output))

        result = {
            "is_legitimate":      bool(parsed.get("is_legitimate", True)),
            "confidence":         parsed.get("confidence", "Low"),
            "verification_notes": parsed.get("verification_notes", ""),
        }
        logger.info(
            "[llm_agents] Cohere: '%s' -> legitimate=%s (confidence=%s)",
            company_clean, result["is_legitimate"], result["confidence"],
        )
        return result

    except Exception as exc:
        logger.error(
            "[llm_agents] verify_company_with_cohere failed for '%s': %s",
            company_clean, exc,
        )
        return _SAFE_DEFAULT
