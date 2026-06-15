"""Recon source: Google Maps scraper via Playwright.

Searches Google Maps for the advertiser's name to find their physical
address, phone number, and verified business name, serving as a free
alternative to TomTom Places.
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

_NAV_TIMEOUT = 20_000


class GoogleMapsRecon(ReconSource):
    """Tier-2 recon: Free Google Maps lookup for address and phone."""

    name = "google_maps"

    async def _enrich_impl(
        self, advertiser: Advertiser, session: AsyncSession
    ) -> list[ReconFindingData]:
        if not advertiser.name:
            return []

        findings: list[ReconFindingData] = []
        
        # Bias the search slightly towards Akwa Ibom
        query = f"{advertiser.name} Akwa Ibom"
        url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}"

        async with launch_browser(headless=True) as (_browser, _ctx, page):
            logger.info("[google_maps] Searching for %r", query)
            try:
                await page.goto(url, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
                
                # Wait for the main panel to load
                await page.wait_for_timeout(3000)
                
                # Check if we landed on a specific place (title has the place name)
                # or a list of places. If it's a specific place, the aria-label for Address/Phone will be present.
                # Sometimes we need to click the first result if it's a list.
                # A simple heuristic: if there's a link with an aria-label containing the query, click it.
                # But to keep it simple and robust, let's just grab the whole body text and run our extractors,
                # OR look for specific aria-labels.
                
                # 1. Company Name (usually the h1)
                company_name = None
                try:
                    h1 = await page.query_selector("h1")
                    if h1:
                        company_name = (await h1.inner_text()).strip()
                except PlaywrightError:
                    pass

                # 2. Address (usually has a button with data-item-id="address" or aria-label="Address: ...")
                address = None
                try:
                    addr_el = await page.query_selector("button[data-item-id='address'], button[aria-label^='Address: ']")
                    if addr_el:
                        address_text = await addr_el.get_attribute("aria-label")
                        if address_text and address_text.startswith("Address: "):
                            address = address_text.replace("Address: ", "").strip()
                        else:
                            address = (await addr_el.inner_text()).strip()
                except PlaywrightError:
                    pass

                # 3. Phone (usually has a button with data-item-id^="phone" or aria-label="Phone: ...")
                phone = None
                try:
                    phone_el = await page.query_selector("button[data-item-id^='phone:tele:'], button[aria-label^='Phone: ']")
                    if phone_el:
                        phone_text = await phone_el.get_attribute("aria-label")
                        if phone_text and phone_text.startswith("Phone: "):
                            phone = phone_text.replace("Phone: ", "").strip()
                        else:
                            phone = (await phone_el.inner_text()).strip()
                except PlaywrightError:
                    pass

                raw_meta = {
                    "source_url": url,
                    "query": query
                }

                if company_name:
                    findings.append(ReconFindingData(
                        source=self.name, kind="company_name", value=company_name,
                        confidence=0.7, raw_json=raw_meta
                    ))
                if address:
                    findings.append(ReconFindingData(
                        source=self.name, kind="address", value=address,
                        confidence=0.8, raw_json=raw_meta
                    ))
                if phone:
                    findings.append(ReconFindingData(
                        source=self.name, kind="phone", value=phone,
                        confidence=0.8, raw_json=raw_meta
                    ))

            except PlaywrightError as exc:
                logger.warning("[google_maps] Navigation or extraction failed for %s: %s", url, exc)

        if findings:
            logger.info(
                "[google_maps] %d findings for advertiser=%s",
                len(findings), advertiser.id,
            )
        return findings
