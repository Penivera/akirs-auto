"""Recon source: LinkedIn enrichment via DuckDuckGo Playwright search.

Instead of scraping LinkedIn directly (which requires auth and blocks bots),
we search DuckDuckGo for `site:linkedin.com "Company Name"`. We then parse
the snippets to find employee names and job titles.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Advertiser
from src.recon.base import ReconFindingData, ReconSource
from src.scrapers.browser import launch_browser

logger = logging.getLogger(__name__)

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_NAV_TIMEOUT = 15_000


class LinkedInDDGRecon(ReconSource):
    """Tier-2 recon: LinkedIn enrichment via DDG Search."""

    name = "linkedin_ddg"

    async def _enrich_impl(
        self, advertiser: Advertiser, session: AsyncSession
    ) -> list[ReconFindingData]:
        if not advertiser.name:
            return []

        findings: list[ReconFindingData] = []
        
        query = f'site:linkedin.com "{advertiser.name}" Akwa Ibom'
        url = f"{DDG_HTML_URL}?q={quote_plus(query)}"

        async with launch_browser(headless=True) as (_browser, _ctx, page):
            logger.info("[linkedin_ddg] Searching DDG for %r", query)
            try:
                await page.goto(url, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
                try:
                    await page.wait_for_selector(".result, .no-results", timeout=5_000)
                except PlaywrightError:
                    pass
                html = await page.content()
            except PlaywrightError as exc:
                logger.warning("[linkedin_ddg] Navigation failed for %s: %s", url, exc)
                return []

        soup = BeautifulSoup(html, "html.parser")

        for result_div in soup.select("div.result"):
            link_el = result_div.select_one("a.result__a")
            snippet_el = result_div.select_one("a.result__snippet")
            
            if not link_el:
                continue
                
            href = link_el.get("href")
            title = link_el.get_text(strip=True)
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            
            if not href or "linkedin.com" not in href:
                continue

            # Standard LinkedIn profile titles on search engines often look like:
            # "John Doe - Manager - Company Name | LinkedIn"
            # Or "Jane Doe - Akwa Ibom, Nigeria - Professional Profile"
            
            # Let's extract a job title / person name from the title string
            clean_title = title.replace(" | LinkedIn", "").replace(" - LinkedIn", "")
            
            raw_meta = {
                "source_url": href,
                "query": query,
                "snippet": snippet
            }
            
            # We assume it's a person profile if it has " - " indicating Title/Company
            if " - " in clean_title:
                findings.append(ReconFindingData(
                    source=self.name, kind="job_title", value=clean_title,
                    confidence=0.6, raw_json=raw_meta
                ))
            else:
                findings.append(ReconFindingData(
                    source=self.name, kind="mention", value=clean_title,
                    confidence=0.4, raw_json=raw_meta
                ))

        if findings:
            logger.info(
                "[linkedin_ddg] %d findings for advertiser=%s",
                len(findings), advertiser.id,
            )
        return findings
