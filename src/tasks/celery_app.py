"""Celery app — uses Redis for broker + result backend.

Tasks run in the default prefork pool; async code is invoked via asyncio.run()
inside each task so we don't have to monkey-patch with gevent/eventlet.
"""

from celery import Celery

from src.config.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "akirs",
    broker=settings.broker_url,
    backend=settings.result_backend,
    include=[
        "tasks.phase1_scrape",
        "tasks.phase2_recon",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_default_queue="akirs",
    task_routes={
        "akirs.tasks.phase1_scrape.*": {"queue": "scrape"},
        "akirs.tasks.phase2_recon.*": {"queue": "recon"},
    },
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    broker_connection_retry_on_startup=True,
    result_expires=60 * 60 * 24,
)

# Conventional alias for Celery CLI/app discovery.
app = celery_app
