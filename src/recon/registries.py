"""Recon source: public business registries (CAC Nigeria, OpenCorporates).

Searches the Corporate Affairs Commission (CAC) website for the
advertiser's name using Playwright (the site is JS-heavy), then falls
back to the OpenCorporates REST API when a key is configured.

Returns ``ReconFindingData`` with kinds ``registration``,
``legal_name``, and ``director``.
"""

from __future__ import annotations

import logging

import httpx
from playwright.async_api import Error as PlaywrightError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import get_settings
from src.db.models import Advertiser, RegistryRecord
from src.recon.base import ReconFindingData, ReconSource
from src.scrapers.browser import launch_browser

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15.0
_NAV_TIMEOUT = 25_000

# CAC public search endpoint (JS-heavy)
_CAC_SEARCH_URL = "https://search.cac.gov.ng/home"


class RegistryRecon(ReconSource):
    """Tier-3 recon: CAC Nigeria + OpenCorporates fallback."""

    name = "registry"

    def __init__(self, **kw) -> None:
        settings = get_settings()
        self._oc_key: str | None = settings.opencorporates_api_key
        super().__init__(**kw)

    # ------------------------------------------------------------------
    # ReconSource implementation
    # ------------------------------------------------------------------

    async def _enrich_impl(
        self, advertiser: Advertiser, session: AsyncSession
    ) -> list[ReconFindingData]:
        if not advertiser.name:
            logger.debug("[registry] Advertiser %s has no name — skipping", advertiser.id)
            return []

        findings: list[ReconFindingData] = []

        # Primary: CAC Nigeria browser scrape.
        cac_findings = await self._search_cac(advertiser.name)
        findings.extend(cac_findings)

        # Persist any CAC records.
        for f in cac_findings:
            try:
                rec = RegistryRecord(
                    advertiser_id=advertiser.id,
                    registry="cac_nigeria",
                    registration_number=f.raw_json.get("rc_number"),
                    status=f.raw_json.get("status"),
                    raw_json=f.raw_json,
                )
                session.add(rec)
            except Exception:
                logger.warning("[registry] Could not persist CAC record", exc_info=True)

        # Fallback: OpenCorporates API.
        if not cac_findings and self._oc_key:
            oc_findings = await self._search_opencorporates(advertiser.name)
            findings.extend(oc_findings)

            for f in oc_findings:
                try:
                    rec = RegistryRecord(
                        advertiser_id=advertiser.id,
                        registry="opencorporates",
                        registration_number=f.raw_json.get("company_number"),
                        status=f.raw_json.get("current_status"),
                        raw_json=f.raw_json,
                    )
                    session.add(rec)
                except Exception:
                    logger.warning("[registry] Could not persist OC record", exc_info=True)

        if findings:
            await session.flush()
            logger.info(
                "[registry] %d findings for advertiser=%s",
                len(findings), advertiser.id,
            )
        return findings

    # ------------------------------------------------------------------
    # CAC Nigeria (Playwright)
    # ------------------------------------------------------------------

    async def _search_cac(self, name: str) -> list[ReconFindingData]:
        """Drive a headless browser to search the CAC public portal."""
        findings: list[ReconFindingData] = []

        try:
            async with launch_browser(headless=True) as (_browser, _ctx, page):
                await page.goto(_CAC_SEARCH_URL, timeout=_NAV_TIMEOUT, wait_until="networkidle")

                # The search form uses a text input + button
                search_input = await page.query_selector("input[type='text'], input[name='searchterm'], input#searchterm")
                if not search_input:
                    logger.warning("[registry] CAC search input not found — page layout may have changed")
                    return findings

                await search_input.fill(name)
                await page.keyboard.press("Enter")

                # Wait for results to render (JS-driven)
                try:
                    await page.wait_for_selector(
                        "table tbody tr, .search-results, .result-item, .no-results",
                        timeout=10_000,
                    )
                except PlaywrightError:
                    logger.info("[registry] CAC search timed out waiting for results")
                    return findings

                await page.wait_for_timeout(1500)

                # Attempt to parse a results table
                rows = await page.query_selector_all("table tbody tr")
                for row in rows[:5]:  # cap at 5 results
                    cells = await row.query_selector_all("td")
                    if len(cells) < 2:
                        continue

                    cell_texts = []
                    for cell in cells:
                        cell_texts.append((await cell.inner_text()).strip())

                    # Common layout: [RC Number, Company Name, Status, ...]
                    rc_number = cell_texts[0] if cell_texts else None
                    legal_name = cell_texts[1] if len(cell_texts) > 1 else None
                    status = cell_texts[2] if len(cell_texts) > 2 else None

                    raw = {"rc_number": rc_number, "legal_name": legal_name, "status": status}

                    if legal_name:
                        findings.append(ReconFindingData(
                            source=self.name, kind="legal_name", value=legal_name,
                            confidence=0.8, raw_json=raw,
                        ))
                    if rc_number:
                        findings.append(ReconFindingData(
                            source=self.name, kind="registration", value=rc_number,
                            confidence=0.8, raw_json=raw,
                        ))

                # Look for director information if detail links exist
                detail_link = await page.query_selector("table tbody tr a")
                if detail_link and findings:
                    try:
                        await detail_link.click()
                        await page.wait_for_timeout(2000)
                        body_text = await page.inner_text("body")

                        # Extract director names from detail page
                        import re
                        director_pattern = re.compile(
                            r"(?:director|secretary)\s*[:\-–]\s*(.+?)(?:\n|$)",
                            re.IGNORECASE,
                        )
                        for m in director_pattern.finditer(body_text):
                            director = m.group(1).strip()
                            if 10 < len(director) < 100:
                                continue  # Skip if it looks like a sentence
                            if director:
                                findings.append(ReconFindingData(
                                    source=self.name, kind="director",
                                    value=director, confidence=0.7,
                                    raw_json={"source": "cac_detail"},
                                ))
                    except Exception:
                        logger.debug("[registry] Could not follow CAC detail link", exc_info=True)

        except PlaywrightError as exc:
            logger.warning("[registry] CAC browser error: %s", exc)
        except Exception:
            logger.exception("[registry] Unexpected error during CAC search")

        return findings

    # ------------------------------------------------------------------
    # OpenCorporates API (fallback)
    # ------------------------------------------------------------------

    async def _search_opencorporates(self, name: str) -> list[ReconFindingData]:
        """Query the OpenCorporates companies search API."""
        findings: list[ReconFindingData] = []
        url = "https://api.opencorporates.com/v0.4/companies/search"
        params = {
            "q": name,
            "jurisdiction_code": "ng",  # Nigeria
            "api_token": self._oc_key,
            "per_page": 5,
        }

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "[registry] OpenCorporates HTTP %s: %s",
                exc.response.status_code, exc,
            )
            return findings
        except Exception:
            logger.exception("[registry] OpenCorporates API error")
            return findings

        companies = (
            data.get("results", {}).get("companies", [])
        )
        for entry in companies:
            company = entry.get("company", {})
            legal_name = company.get("name")
            company_number = company.get("company_number")
            current_status = company.get("current_status")
            raw = {
                "company_number": company_number,
                "current_status": current_status,
                "jurisdiction": company.get("jurisdiction_code"),
                "opencorporates_url": company.get("opencorporates_url"),
            }

            if legal_name:
                findings.append(ReconFindingData(
                    source=self.name, kind="legal_name", value=legal_name,
                    confidence=0.7, raw_json=raw,
                ))
            if company_number:
                findings.append(ReconFindingData(
                    source=self.name, kind="registration",
                    value=company_number,
                    confidence=0.7, raw_json=raw,
                ))

            # Officer / director data
            officers_url = company.get("opencorporates_url")
            if officers_url:
                try:
                    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                        officer_resp = await client.get(
                            f"https://api.opencorporates.com/v0.4/companies/ng/{company_number}/officers",
                            params={"api_token": self._oc_key},
                        )
                        if officer_resp.status_code == 200:
                            officers_data = officer_resp.json()
                            for o_entry in officers_data.get("results", {}).get("officers", [])[:5]:
                                officer = o_entry.get("officer", {})
                                officer_name = officer.get("name")
                                if officer_name:
                                    findings.append(ReconFindingData(
                                        source=self.name, kind="director",
                                        value=officer_name, confidence=0.7,
                                        raw_json={"position": officer.get("position")},
                                    ))
                except Exception:
                    logger.debug("[registry] Could not fetch OC officers", exc_info=True)

        return findings
