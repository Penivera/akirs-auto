"""Application settings driven by environment variables."""

from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(default="sqlite+aiosqlite:///./akirs.db")
    database_url_sync: str = Field(default="sqlite:///./akirs.db")

    redis_url: str = Field(default="redis://localhost:6379/0")
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    fb_ads_base_url: str = Field(
        default=(
            "https://www.facebook.com/ads/library/?active_status=active"
            "&ad_type=all&country=NG&is_targeted_country=false&media_type=all"
            "&sort_data[mode]=total_impressions&sort_data[direction]=desc"
        )
    )
    fb_ads_country: str = Field(default="Nigeria")
    fb_ads_headless: bool = Field(default=True)
    fb_ads_user_data_dir: Path = Field(default=Path(".akirs-browser/facebook"))
    fb_ads_max_scrolls: int = Field(default=40)
    fb_ads_empty_scroll_threshold: int = Field(default=5)
    fb_ads_scroll_delay_ms: int = Field(default=1200)

    keyword_cap_default: int = Field(default=50)
    target_ads_per_keyword_default: int = Field(default=20)
    scraper_keywords: str = Field(default="restaurant, hotel, boutique, pharmacy")

    hunter_api_key: str | None = None
    apollo_api_key: str | None = None
    opencorporates_api_key: str | None = None
    tomtom_api_key: str | None = None
    pdl_api_key: str | None = None
    brave_search_api_key: str | None = None

    recon_search_concurrency: int = Field(default=2)
    recon_social_concurrency: int = Field(default=1)
    recon_enrichment_concurrency: int = Field(default=2)
    recon_registry_concurrency: int = Field(default=1)
    recon_warehouse_concurrency: int = Field(default=1)
    recon_places_concurrency: int = Field(default=2)
    recon_search_delay_seconds: float = Field(default=2.0)

    # Enrichment browser pool
    recon_browser_headless: bool = Field(default=True)
    recon_browser_pool_size: int = Field(default=2)

    # Website scraper
    recon_website_concurrency: int = Field(default=2)
    recon_website_timeout_ms: int = Field(default=15000)
    recon_website_max_subpages: int = Field(default=3)

    # Social profile scraper
    recon_social_timeout_ms: int = Field(default=10000)

    # Rate limiting
    recon_rate_limit_per_minute: int = Field(default=30)

    output_dir: Path = Field(default=Path("output"))

    # Taxation / LLM classification (Ollama phi4-mini via OpenAI-compatible API)
    ollama_base_url: str = Field(default="http://localhost:11434")
    tax_model: str = Field(default="phi4-mini")
    tax_concurrency: int = Field(default=2)

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
