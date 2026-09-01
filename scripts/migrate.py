"""Bring the database up to head.

Runs as a release step / init container — never inside the API or a worker
process. Two API replicas racing `alembic upgrade`, or a worker booting against
an unmigrated database, are exactly the hazards this separation exists to
prevent (FIX.md item 3).

    python scripts/migrate.py
"""
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("migrate")


def main() -> int:
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        logger.error("DATABASE_URL is not set — nothing to migrate")
        return 1

    if db_url.startswith("sqlite"):
        # Alembic's history targets Postgres; sqlite is a local/dev convenience
        # and gets the schema straight off the ORM metadata instead.
        from app.db.engine import create_tables

        create_tables()
        logger.info("sqlite database — tables created from ORM metadata")
        return 0

    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(alembic_cfg, "head")
    logger.info("migrations applied to head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
