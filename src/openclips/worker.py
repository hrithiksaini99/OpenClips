import logging
import time

from openclips.config import Settings

logger = logging.getLogger(__name__)


def run() -> None:
    settings = Settings()
    logger.info("OpenClips worker started with concurrency=%s", settings.worker_concurrency)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("OpenClips worker stopped")
