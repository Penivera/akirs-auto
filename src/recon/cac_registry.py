"""Recon source: CAC Public Search via Playwright.

Searches the Nigerian Corporate Affairs Commission (CAC) public registry
for the advertiser's name, extracting the verified RC number and official
company name.
"""

from __future__ import annotations

import logging
from urllib.parse import quote_plus

from playwright.async_api import Error as PlaywrightError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Advertiser
from src.recon.base import ReconFindingData, ReconSource
from src.scrapers.browser import launch_browser

logger = logging.getLogger(__name__)

_NAV_TIMEOUT = 30_000


class CACRegistryRecon(ReconSource):
    """Tier-2 recon: CAC Public Search via Playwright."""

    name = "cac_registry"

    async def _enrich_impl(
        self, advertiser: Advertiser, session: AsyncSession
    ) -> list[ReconFindingData]:
        if not advertiser.name:
            return []

        findings: list[ReconFindingData] = []
        
        # We can search directly using the query parameter
        query = advertiser.name
        url = f"https://search.cac.gov.ng/list?searchTerm={quote_plus(query)}"

        async with launch_browser(headless=True) as (_browser, _ctx, page):
            logger.info("[cac_registry] Searching CAC for %r", query)
            try:
                # The CAC portal can be slow and relies heavily on JS.
                # We go to the search URL directly, and wait for the results table or "No record found".
                await page.goto(url, timeout=_NAV_TIMEOUT, wait_until="networkidle")
                
                # Wait for the results to populate (usually in a div or table)
                # If there are results, they usually appear in cards or table rows.
                try:
                    await page.wait_for_selector(".result-card, tr.result-row, div.company-name", timeout=10_000)
                except PlaywrightError:
                    pass

                # Extract the entire text to find RC numbers if specific selectors fail
                # But typically, RC number and company name are explicitly shown.
                # Since the exact HTML structure of search.cac.gov.ng can change,
                # we will grab the inner text of the body and use regex, OR grab specific elements.
                
                # A common pattern on CAC is `RC - 123456` or `BN - 123456`.
                # We can pull the full text and look for these.
                text = await page.inner_text("body")
                
                # Let's extract any RC number from the text as a generic fallback.
                import re
                rc_matches = re.findall(r"\b(?:RC|BN|IT)\s*[-:]?\s*\d+\b", text, re.IGNORECASE)
                
                raw_meta = {
                    "source_url": url,
                    "query": query
                }

                # Try to get the first company name result if available
                company_name = None
                try:
                    # Generic selector that might match standard result cards
                    name_el = await page.query_selector("h2, .company-name, td:nth-child(2)")
                    if name_el:
                        company_name = (await name_el.inner_text()).strip()
                except PlaywrightError:
                    pass

                if company_name and len(company_name) > 3:
                    findings.append(ReconFindingData(
                        source=self.name, kind="company_name", value=company_name,
                        confidence=0.7, raw_json=raw_meta
                    ))

                # Deduplicate RC numbers
                seen_rc = set()
                for rc in rc_matches[:3]:
                    rc_clean = rc.upper().strip()
                    if rc_clean not in seen_rc:
                        seen_rc.add(rc_clean)
                        findings.append(ReconFindingData(
                            source=self.name, kind="registration_number", value=rc_clean,
                            confidence=0.8, raw_json=raw_meta
                        ))

            except PlaywrightError as exc:
                logger.warning("[cac_registry] Navigation or extraction failed for %s: %s", url, exc)

        if findings:
            logger.info(
                "[cac_registry] %d findings for advertiser=%s",
                len(findings), advertiser.id,
            )
        return findings
