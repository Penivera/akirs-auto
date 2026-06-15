"""Recon source: scrape public social-media profiles with Playwright.

Visits Instagram, Facebook, and Twitter/X profile URLs stored in
``SocialLink``, extracts bio text, follower counts, and display names,
then parses any embedded contact information from the bio.

Findings are returned as ``ReconFindingData``; a ``SocialProfile`` row
is also persisted directly through the session for later reference.
"""

from __future__ import annotations

import logging
import re

from playwright.async_api import Error as PlaywrightError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Advertiser, SocialLink, SocialProfile
from src.recon.base import ReconFindingData, ReconSource
from src.recon.extractors import extract_contact_from_bio
from src.scrapers.browser import launch_browser

logger = logging.getLogger(__name__)

_SOCIAL_PLATFORMS = frozenset({"instagram", "facebook", "twitter", "x"})

_NAV_TIMEOUT = 20_000


# ------------------------------------------------------------------
# Per-platform extraction helpers
# ------------------------------------------------------------------

async def _extract_instagram(page) -> dict:
    """Best-effort extraction from a public Instagram profile page."""
    data: dict = {"bio": None, "handle": None, "follower_count": None, "display_name": None}
    try:
        # Handle is in the URL already but also in the header
        header = await page.query_selector("header section")
        if header:
            h_el = await header.query_selector("h2")
            if h_el:
                data["handle"] = (await h_el.inner_text()).strip()
            name_el = await header.query_selector("span[class*='x1lliihq']")
            if name_el:
                data["display_name"] = (await name_el.inner_text()).strip()

        # Bio text lives in a <div> just after the display name
        bio_el = await page.query_selector("div.-vDIg span, header section > div span")
        if bio_el:
            data["bio"] = (await bio_el.inner_text()).strip()

        # Follower count: look for the "followers" aria-label or text
        follower_text = await page.inner_text("header section") if await page.query_selector("header section") else ""
        match = re.search(r"([\d,.]+[KMkm]?)\s*followers", follower_text, re.IGNORECASE)
        if match:
            data["follower_count"] = _parse_follower_count(match.group(1))
    except PlaywrightError:
        logger.debug("[social] Instagram extraction error — partial data may exist")
    return data


async def _extract_facebook(page) -> dict:
    """Best-effort extraction from a public Facebook page."""
    data: dict = {"bio": None, "handle": None, "follower_count": None, "display_name": None}
    try:
        title_el = await page.query_selector("h1")
        if title_el:
            data["display_name"] = (await title_el.inner_text()).strip()

        # About / intro text
        intro_el = await page.query_selector("div[data-pagelet='ProfileTilesFeed_0'] span")
        if intro_el:
            data["bio"] = (await intro_el.inner_text()).strip()

        # Followers from the community section
        full_text = await page.inner_text("body")
        match = re.search(r"([\d,.]+[KMkm]?)\s*(?:followers|people follow)", full_text, re.IGNORECASE)
        if match:
            data["follower_count"] = _parse_follower_count(match.group(1))
    except PlaywrightError:
        logger.debug("[social] Facebook extraction error — partial data may exist")
    return data


async def _extract_twitter(page) -> dict:
    """Best-effort extraction from a public Twitter/X profile page."""
    data: dict = {"bio": None, "handle": None, "follower_count": None, "display_name": None}
    try:
        name_el = await page.query_selector("[data-testid='UserName'] span span")
        if name_el:
            data["display_name"] = (await name_el.inner_text()).strip()

        handle_el = await page.query_selector("[data-testid='UserName'] div span")
        if handle_el:
            raw = (await handle_el.inner_text()).strip()
            data["handle"] = raw if raw.startswith("@") else None

        bio_el = await page.query_selector("[data-testid='UserDescription']")
        if bio_el:
            data["bio"] = (await bio_el.inner_text()).strip()

        full_text = await page.inner_text("body")
        match = re.search(r"([\d,.]+[KMkm]?)\s*Followers", full_text)
        if match:
            data["follower_count"] = _parse_follower_count(match.group(1))
    except PlaywrightError:
        logger.debug("[social] Twitter extraction error — partial data may exist")
    return data


def _parse_follower_count(raw: str) -> int | None:
    """Convert ``'12.5K'`` / ``'1,200'`` into an int."""
    raw = raw.strip().replace(",", "")
    multiplier = 1
    if raw[-1:].upper() == "K":
        multiplier = 1_000
        raw = raw[:-1]
    elif raw[-1:].upper() == "M":
        multiplier = 1_000_000
        raw = raw[:-1]
    try:
        return int(float(raw) * multiplier)
    except ValueError:
        return None


_PLATFORM_EXTRACTORS = {
    "instagram": _extract_instagram,
    "facebook": _extract_facebook,
    "twitter": _extract_twitter,
    "x": _extract_twitter,
}


# ------------------------------------------------------------------
# ReconSource
# ------------------------------------------------------------------

class SocialProfileRecon(ReconSource):
    """Tier-2 recon: public social-media profile scraping."""

    name = "social"

    async def _enrich_impl(
        self, advertiser: Advertiser, session: AsyncSession
    ) -> list[ReconFindingData]:
        stmt = select(SocialLink).where(
            SocialLink.advertiser_id == advertiser.id,
            SocialLink.platform.in_(_SOCIAL_PLATFORMS),
        )
        result = await session.execute(stmt)
        links: list[SocialLink] = list(result.scalars().all())

        if not links:
            logger.debug("[social] No social links for advertiser=%s", advertiser.id)
            return []

        findings: list[ReconFindingData] = []

        async with launch_browser(headless=True) as (_browser, _ctx, page):
            for link in links:
                try:
                    findings.extend(
                        await self._scrape_profile(page, link, advertiser, session)
                    )
                except Exception:
                    logger.exception("[social] Error scraping %s", link.url)

        if findings:
            logger.info(
                "[social] Extracted %d findings for advertiser=%s",
                len(findings), advertiser.id,
            )
        return findings

    async def _scrape_profile(
        self,
        page,
        link: SocialLink,
        advertiser: Advertiser,
        session: AsyncSession,
    ) -> list[ReconFindingData]:
        platform = link.platform.lower()
        extractor = _PLATFORM_EXTRACTORS.get(platform)
        if extractor is None:
            return []

        logger.info("[social] Visiting %s profile: %s", platform, link.url)

        try:
            await page.goto(link.url, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)  # wait for SPA hydration
        except PlaywrightError as exc:
            logger.warning("[social] Could not load %s: %s", link.url, exc)
            return []

        profile_data = await extractor(page)

        # Persist the SocialProfile row.
        try:
            sp = SocialProfile(
                advertiser_id=advertiser.id,
                platform=platform,
                handle=profile_data.get("handle"),
                bio=profile_data.get("bio"),
                follower_count=profile_data.get("follower_count"),
                raw_json={"source_url": link.url, **profile_data},
            )
            session.add(sp)
            await session.flush()
        except Exception:
            logger.warning("[social] Could not persist SocialProfile for %s", link.url, exc_info=True)

        # Build ReconFindingData entries from the bio.
        findings: list[ReconFindingData] = []
        raw_json = {"source_url": link.url, "platform": platform}

        bio = profile_data.get("bio") or ""
        if bio:
            contact = extract_contact_from_bio(bio)
            for email in contact["emails"]:
                findings.append(ReconFindingData(
                    source=self.name, kind="email", value=email,
                    confidence=0.8, raw_json=raw_json,
                ))
            for phone in contact["phones"]:
                findings.append(ReconFindingData(
                    source=self.name, kind="phone", value=phone,
                    confidence=0.6, raw_json=raw_json,
                ))

        return findings
