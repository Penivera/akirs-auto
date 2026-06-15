"""Recon source: scrape advertiser websites with Playwright.

Visits each advertiser's website URLs (``SocialLink`` where
``platform="website"``), discovers Contact / About pages, follows them,
and extracts emails, phones, addresses, and business descriptions.

Uses a **shared browser** context across all website links for a single
advertiser to avoid the overhead of launching a new Chromium per link.
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Advertiser, SocialLink
from src.recon.base import ReconFindingData, ReconSource
from src.recon.extractors import extract_addresses, extract_emails, extract_phones
from src.scrapers.browser import launch_browser

logger = logging.getLogger(__name__)

# Link-text keywords that indicate a useful subpage.
_NAV_KEYWORDS = frozenset({
    "contact", "about", "about us", "get in touch",
    "our story", "reach us", "connect", "contact us",
})

# Maximum subpage links to follow per website.
_MAX_SUBPAGES = 3

# Playwright navigation timeout (ms).
_NAV_TIMEOUT = 15_000


class WebsiteRecon(ReconSource):
    """Tier-1 recon: lightweight browser scrape of advertiser websites."""

    name = "website"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _visit_page(page, url: str) -> tuple[str, list[str]]:
        """Navigate to *url*, return ``(visible_text, discovered_sublinks)``.

        Scrolls halfway to trigger lazy-loaded content in SPAs, then
        parses the rendered DOM with BeautifulSoup.
        """
        try:
            await page.goto(url, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await page.wait_for_timeout(800)

            html = await page.content()
        except PlaywrightError as exc:
            logger.warning("[website] Navigation failed for %s: %s", url, exc)
            return "", []

        soup = BeautifulSoup(html, "html.parser")

        # Strip non-visible tags before text extraction.
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)

        # Discover internal Contact / About links.
        base = "/".join(url.split("/")[:3])  # scheme + host
        domain = url.split("/")[2] if len(url.split("/")) > 2 else ""
        sublinks: list[str] = []

        for anchor in soup.find_all("a", href=True):
            href: str = anchor["href"]
            anchor_text = anchor.get_text(strip=True).lower()

            if not any(kw in anchor_text for kw in _NAV_KEYWORDS):
                continue

            if href.startswith("/"):
                sublinks.append(f"{base}{href}")
            elif href.startswith("http") and domain and domain in href:
                sublinks.append(href)

        # De-duplicate, preserving order.
        seen: set[str] = set()
        unique: list[str] = []
        for link in sublinks:
            if link not in seen:
                seen.add(link)
                unique.append(link)
        return text, unique

    # ------------------------------------------------------------------
    # ReconSource implementation
    # ------------------------------------------------------------------

    async def _enrich_impl(
        self, advertiser: Advertiser, session: AsyncSession
    ) -> list[ReconFindingData]:
        stmt = select(SocialLink).where(
            SocialLink.advertiser_id == advertiser.id,
            SocialLink.platform == "website",
        )
        result = await session.execute(stmt)
        links: list[SocialLink] = list(result.scalars().all())

        if not links:
            logger.debug("[website] No website links for advertiser=%s", advertiser.id)
            return []

        findings: list[ReconFindingData] = []

        # Share a single browser instance across all links for this advertiser.
        async with launch_browser(headless=True) as (_browser, _ctx, page):
            for link in links:
                try:
                    findings.extend(await self._scrape_site(page, link.url))
                except Exception:
                    logger.exception(
                        "[website] Unhandled error scraping %s", link.url
                    )

        if findings:
            logger.info(
                "[website] Extracted %d findings for advertiser=%s",
                len(findings),
                advertiser.id,
            )
        return findings

    async def _scrape_site(self, page, url: str) -> list[ReconFindingData]:
        """Run the full homepage → subpage flow for a single website URL."""
        logger.info("[website] Starting browser flow for %s", url)

        home_text, sublinks = await self._visit_page(page, url)
        all_text = home_text

        for sub_url in sublinks[:_MAX_SUBPAGES]:
            logger.info("[website] Following sublink %s", sub_url)
            sub_text, _ = await self._visit_page(page, sub_url)
            all_text += " " + sub_text

        raw_json = {"source_url": url}
        findings: list[ReconFindingData] = []

        # Emails
        for email in extract_emails(all_text):
            findings.append(
                ReconFindingData(
                    source=self.name, kind="email", value=email,
                    confidence=0.9, raw_json=raw_json,
                )
            )

        # Phones
        for phone in extract_phones(all_text, max_results=3):
            findings.append(
                ReconFindingData(
                    source=self.name, kind="phone", value=phone,
                    confidence=0.7, raw_json=raw_json,
                )
            )

        # Addresses
        for addr in extract_addresses(all_text):
            findings.append(
                ReconFindingData(
                    source=self.name, kind="address", value=addr,
                    confidence=0.6, raw_json=raw_json,
                )
            )

        # Business description — first 500 chars of homepage text.
        if home_text and len(home_text) >= 50:
            findings.append(
                ReconFindingData(
                    source=self.name, kind="description",
                    value=home_text[:500].strip(),
                    confidence=0.5, raw_json=raw_json,
                )
            )

        return findings
