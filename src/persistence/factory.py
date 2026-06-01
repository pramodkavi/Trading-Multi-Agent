"""Backend selection for the persistence layer.

``create_store`` is the single place that turns configuration into a live
``SignalStore``. The rest of the application (the scan runner, the future Lambda
handler) depends only on the ``SignalStore`` interface and never names a concrete
backend -- so switching between local asyncpg and cloud Data API is a config
change (``PERSISTENCE_BACKEND``), not a code change.

The factory is ``async`` because the asyncpg backend has to open a connection;
the Data API backend is constructed synchronously (its client is stateless) but
is awaited through the same call for a uniform caller experience.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.persistence.store import AsyncpgSignalStore, DataApiSignalStore, SignalStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.config import Settings


async def create_store(settings: Settings) -> SignalStore:
    """Build the configured persistence backend.

    Selection is driven by ``settings.persistence_backend``:

    - ``"asyncpg"`` -- opens an asyncpg connection to ``database_url`` (local
      Docker dev / integration tests).
    - ``"dataapi"`` -- a Data API client against the Aurora cluster identified
      by ``db_cluster_arn`` / ``db_secret_arn`` / ``db_name`` (cloud runtime).

    Settings validation already guarantees the backend-specific fields are
    present; the explicit checks here re-assert that contract at the boundary
    (and narrow the optional types for the type checker).

    Raises:
        ValueError: a required field for the selected backend is missing, or
            the backend name is unrecognised.
    """
    backend = settings.persistence_backend
    if backend == "asyncpg":
        if settings.database_url is None:
            raise ValueError("PERSISTENCE_BACKEND=asyncpg requires database_url to be set")
        return await AsyncpgSignalStore.connect(settings.database_url.get_secret_value())
    if backend == "dataapi":
        if settings.db_cluster_arn is None or settings.db_secret_arn is None:
            raise ValueError(
                "PERSISTENCE_BACKEND=dataapi requires db_cluster_arn and db_secret_arn"
            )
        return DataApiSignalStore.from_arns(
            cluster_arn=settings.db_cluster_arn,
            secret_arn=settings.db_secret_arn,
            database=settings.db_name,
        )
    raise ValueError(f"unknown persistence backend: {backend!r}")
