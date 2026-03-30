"""Scheduled execution of the scraping pipeline."""

import logging
import time

import schedule

from config import AppConfig
from main import run_pipeline

logger = logging.getLogger(__name__)


def start_scheduler(config: AppConfig) -> None:
    """Start the scheduled scraping pipeline."""
    interval = config.schedule_interval_hours

    logger.info("Scheduling pipeline to run every %d hours", interval)
    schedule.every(interval).hours.do(lambda: run_pipeline(config))

    # Run immediately on start
    run_pipeline(config)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    start_scheduler(AppConfig())
