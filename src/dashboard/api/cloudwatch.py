"""CloudWatch reads for the dashboard (Step 4.3, design §4.2 P1 / P7, §8).

Two consumers: the overview's alarm strip and the P7 system page (alarms +
the scan Lambda's daily invocation / error / p95-duration metrics). Rules:

* **Read-only, three actions.** ``DescribeAlarms`` (limited by IAM to the
  ``CryptoSignals-Monitoring-*`` alarms), ``ListMetrics`` and ``GetMetricData``
  -- exactly what ``DashboardStack`` grants. The dashboard role has no
  ``lambda:*``, so the scan function's name is **discovered** from its
  ``AWS/Lambda`` ``Invocations`` metric (``FunctionName`` starting
  :data:`SCAN_FUNCTION_PREFIX`) rather than read from the function.
* **Bounded and off the event loop.** The boto3 client is built lazily, once
  per container (:func:`get_cloudwatch_client`, like ``deps.get_store``), with
  :data:`CLIENT_CONFIG` (3 s connect / 10 s read / one attempt) so a slow
  CloudWatch fails well inside the Lambda's 25 s budget; boto3 is synchronous,
  so every call runs in ``asyncio.to_thread``.
* **A CloudWatch hiccup never takes a page down.** The reader lets
  ``ClientError`` / ``BotoCoreError`` propagate; the routes catch them
  (:func:`read_alarms` and the metrics route) and answer **200** with the
  section ``null`` and ``note = "cloudwatch unavailable"``.

Routes depend on :data:`CloudWatchDep` (``get_cloudwatch_reader``), never on a
client, so tests override the dependency (or the cached ``_CLIENT``) and no
test ever constructs a boto3 client.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Final, Literal, Protocol

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ALARM_BUDGET_S",
    "ALARM_PREFIX",
    "CLIENT_CONFIG",
    "CLOUDWATCH_UNAVAILABLE",
    "SCAN_FUNCTION_PREFIX",
    "AlarmState",
    "Boto3CloudWatchReader",
    "CloudWatchDep",
    "CloudWatchReader",
    "MetricDay",
    "display_name",
    "get_cloudwatch_client",
    "get_cloudwatch_reader",
    "read_alarms",
    "reset_cloudwatch_client",
]

logger = logging.getLogger(__name__)

# MonitoringStack's alarms are CDK-named `<stack>-<logicalId><hash>-<suffix>`.
ALARM_PREFIX: Final = "CryptoSignals-Monitoring-"
# ComputeStack's scan function, CDK-named `CryptoSignals-Compute-ScanLambda<hash>-<suffix>`.
SCAN_FUNCTION_PREFIX: Final = "CryptoSignals-Compute-ScanLambda"
# The section note when CloudWatch refused or failed (AccessDenied, throttling, network).
CLOUDWATCH_UNAVAILABLE: Final = "cloudwatch unavailable"

# Overall budget for the overview's alarm-strip fetch. Routers read it at call time
# (`cloudwatch.ALARM_BUDGET_S`) so tests can shrink it, like `market.PRICE_BUDGET_S`.
ALARM_BUDGET_S: float = 5.0

# Bounded like the Data API client (deps.py): one attempt, short timeouts.
CLIENT_CONFIG: Final = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"max_attempts": 1},
)

# The CDK hash on a generated name: 8 hex chars of the logical-id hash, a dash and
# a 12-char random suffix (`ProviderErrorAlarm67BAF5FC-1casEA4Z9J63`).
_CDK_SUFFIX = re.compile(r"[0-9A-Fa-f]{8}-[A-Za-z0-9]{12}$")

_DAY_SECONDS: Final = 86400
# Defensive bound on NextToken loops (5 alarms / one function in practice).
_MAX_PAGES: Final = 20


class AlarmState(BaseModel):
    """One CloudWatch alarm as the health strip renders it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Display name: prefix and CDK hash suffix stripped.")
    state: Literal["OK", "ALARM", "INSUFFICIENT_DATA"]
    updated_at: datetime = Field(description="Last state transition (StateUpdatedTimestamp).")
    reason: str | None = Field(description="CloudWatch's StateReason for that transition.")


class MetricDay(BaseModel):
    """One UTC day of the scan Lambda's AWS/Lambda metrics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    invocations: int = Field(ge=0)
    errors: int = Field(ge=0)
    duration_p95_ms: float | None = Field(description="p95 Duration; null on a day with no data.")


def display_name(alarm_name: str) -> str:
    """Strip the Monitoring prefix and the CDK hash suffix from an alarm name.

    ``CryptoSignals-Monitoring-ProviderErrorAlarm67BAF5FC-1casEA4Z9J63`` ->
    ``ProviderErrorAlarm``; a name without the suffix keeps everything after the prefix.
    """
    name = alarm_name.removeprefix(ALARM_PREFIX)
    return _CDK_SUFFIX.sub("", name) or name


class CloudWatchReader(Protocol):
    """The dashboard's CloudWatch seam (default: :class:`Boto3CloudWatchReader`)."""

    async def describe_alarms(self, prefix: str) -> list[AlarmState]: ...

    async def scan_function_name(self) -> str | None: ...

    async def lambda_daily_metrics(
        self, function_name: str, *, days: int = 7, now: datetime
    ) -> list[MetricDay]: ...


def _day_start(now: datetime, days: int) -> datetime:
    day = (now.astimezone(UTC) - timedelta(days=days)).date()
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


class Boto3CloudWatchReader:
    """:class:`CloudWatchReader` over a boto3 ``cloudwatch`` client.

    Takes a client *factory* so the client is built (and any ``BotoCoreError``
    from building it, e.g. no region) inside the worker thread, on first use.
    """

    def __init__(self, client_factory: Callable[[], Any]) -> None:
        self._client_factory = client_factory

    # -- DescribeAlarms ---------------------------------------------------------

    async def describe_alarms(self, prefix: str) -> list[AlarmState]:
        """Every metric / composite alarm named ``prefix*``, sorted by display name."""
        return await asyncio.to_thread(self._describe_alarms, prefix)

    def _describe_alarms(self, prefix: str) -> list[AlarmState]:
        client = self._client_factory()
        alarms: list[AlarmState] = []
        token: str | None = None
        for _ in range(_MAX_PAGES):
            kwargs: dict[str, Any] = {
                "AlarmNamePrefix": prefix,
                "AlarmTypes": ["MetricAlarm", "CompositeAlarm"],
            }
            if token:
                kwargs["NextToken"] = token
            page = client.describe_alarms(**kwargs)
            for raw in [*page.get("MetricAlarms", []), *page.get("CompositeAlarms", [])]:
                alarms.append(
                    AlarmState(
                        name=display_name(raw["AlarmName"]),
                        state=raw["StateValue"],
                        updated_at=raw["StateUpdatedTimestamp"],
                        reason=raw.get("StateReason"),
                    )
                )
            token = page.get("NextToken")
            if not token:
                break
        return sorted(alarms, key=lambda a: a.name)

    # -- ListMetrics ------------------------------------------------------------

    async def scan_function_name(self) -> str | None:
        """The scan Lambda's name from its ``Invocations`` metric, or ``None``."""
        return await asyncio.to_thread(self._scan_function_name)

    def _scan_function_name(self) -> str | None:
        client = self._client_factory()
        names: set[str] = set()
        token: str | None = None
        for _ in range(_MAX_PAGES):
            kwargs: dict[str, Any] = {
                "Namespace": "AWS/Lambda",
                "MetricName": "Invocations",
                "Dimensions": [{"Name": "FunctionName"}],
            }
            if token:
                kwargs["NextToken"] = token
            page = client.list_metrics(**kwargs)
            for metric in page.get("Metrics", []):
                dims = metric.get("Dimensions", [])
                # Only the function-level series (a sole FunctionName dimension);
                # per-version / per-alias series carry a second dimension.
                if len(dims) == 1 and dims[0].get("Name") == "FunctionName":
                    value = str(dims[0].get("Value", ""))
                    if value.startswith(SCAN_FUNCTION_PREFIX):
                        names.add(value)
            token = page.get("NextToken")
            if not token:
                break
        if not names:
            return None
        ordered = sorted(names)
        if len(ordered) > 1:  # a replaced function's metrics linger for two weeks
            logger.warning("several scan functions have metrics, using %s: %s", ordered[0], ordered)
        return ordered[0]

    # -- GetMetricData ----------------------------------------------------------

    async def lambda_daily_metrics(
        self, function_name: str, *, days: int = 7, now: datetime
    ) -> list[MetricDay]:
        """Daily Invocations / Errors (Sum) and Duration (p95) from midnight ``days`` ago.

        One ``GetMetricData`` request (three queries, paged by ``NextToken``),
        period one day, ``StartTime`` = midnight UTC of ``now - days``,
        ``EndTime`` = ``now``. Every day through today is present; a day without
        data reads 0 / 0 / null.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1, got {days}")
        return await asyncio.to_thread(self._lambda_daily_metrics, function_name, days, now)

    def _lambda_daily_metrics(
        self, function_name: str, days: int, now: datetime
    ) -> list[MetricDay]:
        client = self._client_factory()
        start = _day_start(now, days)
        dimensions = [{"Name": "FunctionName", "Value": function_name}]

        def query(qid: str, metric: str, stat: str) -> dict[str, Any]:
            return {
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Lambda",
                        "MetricName": metric,
                        "Dimensions": dimensions,
                    },
                    "Period": _DAY_SECONDS,
                    "Stat": stat,
                },
                "ReturnData": True,
            }

        queries = [
            query("invocations", "Invocations", "Sum"),
            query("errors", "Errors", "Sum"),
            query("duration_p95", "Duration", "p95"),
        ]
        series: dict[str, dict[date, float]] = {q["Id"]: {} for q in queries}
        token: str | None = None
        for _ in range(_MAX_PAGES):
            kwargs: dict[str, Any] = {
                "MetricDataQueries": queries,
                "StartTime": start,
                "EndTime": now,
                "ScanBy": "TimestampAscending",
            }
            if token:
                kwargs["NextToken"] = token
            page = client.get_metric_data(**kwargs)
            for result in page.get("MetricDataResults", []):
                bucket = series.setdefault(result["Id"], {})
                for ts, value in zip(
                    result.get("Timestamps", []), result.get("Values", []), strict=False
                ):
                    bucket[ts.astimezone(UTC).date()] = float(value)
            token = page.get("NextToken")
            if not token:
                break

        out: list[MetricDay] = []
        day = start.date()
        last = now.astimezone(UTC).date()
        while day <= last:
            out.append(
                MetricDay(
                    date=day,
                    invocations=int(series["invocations"].get(day, 0.0)),
                    errors=int(series["errors"].get(day, 0.0)),
                    duration_p95_ms=series["duration_p95"].get(day),
                )
            )
            day += timedelta(days=1)
        return out


# ---------------------------------------------------------------------------
# Per-container client + the FastAPI dependency
# ---------------------------------------------------------------------------

_CLIENT: Any = None


def get_cloudwatch_client() -> Any:
    """Return the container's boto3 ``cloudwatch`` client, building it on first use.

    boto3 is imported here, not at module import, so the unauthenticated
    ``/api/health`` cold start pays nothing for it. The region comes from the
    Lambda runtime's ``AWS_REGION``.
    """
    global _CLIENT
    if _CLIENT is None:
        import boto3

        _CLIENT = boto3.client("cloudwatch", config=CLIENT_CONFIG)
    return _CLIENT


def reset_cloudwatch_client() -> None:
    """Drop the cached client so the next call builds a fresh one (tests)."""
    global _CLIENT
    _CLIENT = None


def get_cloudwatch_reader() -> CloudWatchReader:
    """FastAPI dependency: the default reader over the per-container client."""
    return Boto3CloudWatchReader(get_cloudwatch_client)


# Route parameter annotation: `async def route(cw: CloudWatchDep)`. Tests swap it with
# `app.dependency_overrides[get_cloudwatch_reader] = lambda: fake`.
CloudWatchDep = Annotated[CloudWatchReader, Depends(get_cloudwatch_reader)]


async def read_alarms(
    reader: CloudWatchReader, *, budget_s: float | None = None
) -> tuple[list[AlarmState] | None, str | None]:
    """``(alarms, None)``, or ``(None, "cloudwatch unavailable")`` on a CloudWatch error.

    With ``budget_s`` the call is bounded by ``asyncio.wait_for`` and a blown
    budget degrades the same way (the overview passes :data:`ALARM_BUDGET_S` so a
    slow CloudWatch cannot hold the landing page). The worker thread itself
    cannot be cancelled; it finishes within the client's own 3 s / 10 s bounds.
    """
    try:
        call = reader.describe_alarms(ALARM_PREFIX)
        alarms = await (call if budget_s is None else asyncio.wait_for(call, timeout=budget_s))
    except TimeoutError:
        logger.warning("CloudWatch DescribeAlarms exceeded the %.1f s budget", budget_s)
        return None, CLOUDWATCH_UNAVAILABLE
    except (ClientError, BotoCoreError) as exc:
        logger.warning("CloudWatch DescribeAlarms failed: %s", exc)
        return None, CLOUDWATCH_UNAVAILABLE
    return alarms, None
