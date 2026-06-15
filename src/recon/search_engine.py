"""Search-engine recon adapter — DuckDuckGo HTML via Playwright.

Queries DuckDuckGo with several templates (including contact-specific
ones), parses result URLs and snippets, and produces ``mention``,
``email``, and ``phone`` findings by running the shared extractors
against each snippet.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import get_settings
from src.db.models import Advertiser
from src.recon.base import ReconFindingData, ReconSource
from src.recon.extractors import extract_emails, extract_phones
from src.scrapers.browser import launch_browser

logger = logging.getLogger(__name__)

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_NAV_TIMEOUT = 15_000


class SearchEngineRecon(ReconSource):
    """Tier-2 recon: DuckDuckGo HTML search for advertiser mentions via Playwright."""

    name = "search"

    QUERY_TEMPLATES: tuple[str, ...] = (
        '"{name}" akwa ibom contact',
        '"{name}" akwa ibom email phone',
        '"{name}" business profile nigeria',
        # Contact-specific queries
        '"{name}" email address contact',
        '"{name}" phone number whatsapp',
    )

    async def _enrich_impl(
        self, advertiser: Advertiser, session: AsyncSession
    ) -> list[ReconFindingData]:
        if not advertiser.name:
            return []

        findings: list[ReconFindingData] = []
        settings = get_settings()

        async with launch_browser(headless=True) as (_browser, _ctx, page):
            for template in self.QUERY_TEMPLATES:
                query = template.format(name=advertiser.name)
                try:
                    results = await self._search(page, query)
                except Exception as exc:
                    logger.warning("[search] Query failed '%s': %s", query, exc)
                    results = []

                for r in results[:5]:
                    # Standard mention finding
                    findings.append(
                        ReconFindingData(
                            source=self.name,
                            kind="mention",
                            value=r["url"],
                            confidence=0.4,
                            raw_json={
                                "query": query,
                                "title": r.get("title"),
                                "snippet": r.get("snippet"),
                            },
                        )
                    )

                    # Extract emails and phones from the snippet text
                    snippet = r.get("snippet") or ""
                    title = r.get("title") or ""
                    combined = f"{title} {snippet}"

                    snippet_meta = {
                        "query": query,
                        "source_url": r["url"],
                        "snippet": snippet,
                    }

                    for email in extract_emails(combined):
                        findings.append(
                            ReconFindingData(
                                source=self.name,
                                kind="email",
                                value=email,
                                confidence=0.5,
                                raw_json=snippet_meta,
                            )
                        )

                    for phone in extract_phones(combined, max_results=2):
                        findings.append(
                            ReconFindingData(
                                source=self.name,
                                kind="phone",
                                value=phone,
                                confidence=0.4,
                                raw_json=snippet_meta,
                            )
                        )

                await asyncio.sleep(settings.recon_search_delay_seconds)

        if findings:
            logger.info(
                "[search] %d findings for advertiser=%s",
                len(findings), advertiser.id,
            )
        return findings

    # ------------------------------------------------------------------
    # DuckDuckGo HTML scraper
    # ------------------------------------------------------------------

    async def _search(
        self, page, query: str
    ) -> list[dict[str, Any]]:
        """Search DuckDuckGo HTML using Playwright and parse the results page."""
        url = f"{DDG_HTML_URL}?q={quote_plus(query)}"
        try:
            await page.goto(url, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
            # Wait for either results or a no-results message
            try:
                await page.wait_for_selector(".result, .no-results", timeout=5_000)
            except PlaywrightError:
                pass  # It's okay if they don't appear, we will parse whatever HTML is there
            html = await page.content()
        except PlaywrightError as exc:
            logger.warning("[search] Navigation failed for %s: %s", url, exc)
            return []

        soup = BeautifulSoup(html, "html.parser")

        results: list[dict[str, Any]] = []
        for result_div in soup.select("div.result"):
            link_el = result_div.select_one("a.result__a")
            snippet_el = result_div.select_one("a.result__snippet")
            if not link_el:
                continue
            href = link_el.get("href")
            title = link_el.get_text(strip=True)
            snippet = snippet_el.get_text(strip=True) if snippet_el else None
            if not href:
                continue
            results.append({"url": href, "title": title, "snippet": snippet})
        return results

