from neo4j import Driver, GraphDatabase

from app.core.config import settings

_driver: Driver | None = None


def get_driver() -> Driver:
    """Lazily-created process-wide sync driver — Celery forks workers, so this
    must not connect at import time (before the fork)."""
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )
    return _driver
