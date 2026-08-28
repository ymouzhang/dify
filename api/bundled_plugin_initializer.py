"""Idempotently install bundled plugins for tenants that already exist."""

import logging
import os
import time

from sqlalchemy import select

from app import app
from configs import dify_config
from extensions.ext_database import db
from models import Tenant
from services.plugin.bundled_plugins import install_bundled_plugins, wait_for_plugin_install

logger = logging.getLogger(__name__)


def main() -> None:
    plugin_ids = dify_config.NEW_USER_DEFAULT_PLUGIN_ID_LIST
    if not plugin_ids:
        logger.info("No default plugins configured")
        return

    retries = int(os.getenv("BUNDLED_PLUGIN_INIT_RETRIES", "30"))
    with app.app_context():
        tenant_ids = list(db.session.scalars(select(Tenant.id)))
        for tenant_id in tenant_ids:
            for attempt in range(1, retries + 1):
                try:
                    response = install_bundled_plugins(tenant_id, plugin_ids)
                    wait_for_plugin_install(tenant_id, response)
                    break
                except Exception:
                    if attempt == retries:
                        raise
                    logger.warning(
                        "Bundled plugin initialization attempt %d/%d failed for tenant %s",
                        attempt,
                        retries,
                        tenant_id,
                        exc_info=True,
                    )
                    time.sleep(5)
        logger.info("Bundled plugin initialization completed for %d tenant(s)", len(tenant_ids))


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    main()
