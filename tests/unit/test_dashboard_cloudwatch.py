"""Slice 4 Step 4.3 Task 4 -- the dashboard's CloudWatch reader (`cloudwatch.py`).

No boto3 client is ever constructed here: the default reader is handed a fake
client factory, and the routes get a fake `CloudWatchReader` through the
`get_cloudwatch_reader` dependency override.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient

from src.dashboard.api import cloudwatch
from src.dashboard.api.app import create_app
from src.dashboard.api.cloudwatch import (
    ALARM_PREFIX,
    CLOUDWATCH_UNAVAILABLE,
    AlarmState,
    Boto3CloudWatchReader,
    MetricDay,
    display_name,
    get_cloudwatch_reader,
)

NOW = datetime(2026, 10, 21, 13, 10, tzinfo=UTC)


class FakeCwClient:
    """Records every call; serves canned (optionally paged) responses per operation."""

    def __init__(self, **responses: list[dict[str, Any]]) -> None:
        self.responses = {op: list(pages) for op, pages in responses.items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _serve(self, op: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, kwargs))
        pages = self.responses.get(op, [])
        if not pages:
            return {}
        page = pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    def describe_alarms(self, **kwargs: Any) -> dict[str, Any]:
        return self._serve("describe_alarms", kwargs)

    def list_metrics(self, **kwargs: Any) -> dict[str, Any]:
        return self._serve("list_metrics", kwargs)

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        return self._serve("get_metric_data", kwargs)


def _alarm(name: str, state: str = "OK", reason: str = "fine") -> dict[str, Any]:
    return {
        "AlarmName": name,
        "StateValue": state,
        "StateReason": reason,
        "StateUpdatedTimestamp": datetime(2026, 10, 20, 8, 0, tzinfo=UTC),
    }


def _client_error(code: str = "AccessDenied") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "nope"}}, "DescribeAlarms")


# ---------------------------------------------------------------------------
# Display names
# ---------------------------------------------------------------------------


def test_display_name_strips_prefix_and_cdk_hash() -> None:
    assert (
        display_name("CryptoSignals-Monitoring-ProviderErrorAlarm67BAF5FC-1casEA4Z9J63")
        == "ProviderErrorAlarm"
    )
    # No hash suffix -> only the prefix goes; a foreign name is left alone.
    assert display_name("CryptoSignals-Monitoring-ManualAlarm") == "ManualAlarm"
    assert display_name("SomethingElse") == "SomethingElse"


# ---------------------------------------------------------------------------
# Boto3CloudWatchReader
# ---------------------------------------------------------------------------


def test_describe_alarms_strips_hash_and_prefix_filter() -> None:
    client = FakeCwClient(
        describe_alarms=[
            {
                "MetricAlarms": [
                    _alarm("CryptoSignals-Monitoring-ScanFailureAlarmA1B2C3D4-AbCdEfGh1234")
                ],
                "NextToken": "page-2",
            },
            {
                "MetricAlarms": [
                    _alarm(
                        "CryptoSignals-Monitoring-ProviderErrorAlarm67BAF5FC-1casEA4Z9J63",
                        state="ALARM",
                        reason="Threshold Crossed",
                    )
                ],
                "CompositeAlarms": [],
            },
        ]
    )
    reader = Boto3CloudWatchReader(lambda: client)
    alarms = asyncio.run(reader.describe_alarms(ALARM_PREFIX))

    assert alarms == [
        AlarmState(
            name="ProviderErrorAlarm",
            state="ALARM",
            updated_at=datetime(2026, 10, 20, 8, 0, tzinfo=UTC),
            reason="Threshold Crossed",
        ),
        AlarmState(
            name="ScanFailureAlarm",
            state="OK",
            updated_at=datetime(2026, 10, 20, 8, 0, tzinfo=UTC),
            reason="fine",
        ),
    ]
    # Paged through NextToken; every call filters by the Monitoring prefix.
    assert [op for op, _ in client.calls] == ["describe_alarms", "describe_alarms"]
    first, second = (kwargs for _, kwargs in client.calls)
    assert first["AlarmNamePrefix"] == "CryptoSignals-Monitoring-"
    assert "NextToken" not in first
    assert second["NextToken"] == "page-2"
    assert second["AlarmNamePrefix"] == "CryptoSignals-Monitoring-"


def test_scan_function_discovered_by_prefix() -> None:
    def metric(fn: str) -> dict[str, Any]:
        return {
            "Namespace": "AWS/Lambda",
            "MetricName": "Invocations",
            "Dimensions": [{"Name": "FunctionName", "Value": fn}],
        }

    client = FakeCwClient(
        list_metrics=[
            {
                "Metrics": [
                    metric("CryptoSignals-Dashboard-DashboardApiABC-xyz"),
                    # A per-version metric carries a second dimension: not the function series.
                    {
                        "Namespace": "AWS/Lambda",
                        "MetricName": "Invocations",
                        "Dimensions": [
                            {
                                "Name": "FunctionName",
                                "Value": "CryptoSignals-Compute-ScanLambdaOld",
                            },
                            {"Name": "Resource", "Value": "x:1"},
                        ],
                    },
                ],
                "NextToken": "t2",
            },
            {"Metrics": [metric("CryptoSignals-Compute-ScanLambda5A4E3B2C-Q1w2E3r4T5y6")]},
        ]
    )
    reader = Boto3CloudWatchReader(lambda: client)
    assert asyncio.run(reader.scan_function_name()) == (
        "CryptoSignals-Compute-ScanLambda5A4E3B2C-Q1w2E3r4T5y6"
    )
    first, second = (kwargs for _, kwargs in client.calls)
    assert first["Namespace"] == "AWS/Lambda" and first["MetricName"] == "Invocations"
    assert first["Dimensions"] == [{"Name": "FunctionName"}]
    assert second["NextToken"] == "t2"


def test_scan_function_none_when_absent() -> None:
    client = FakeCwClient(list_metrics=[{"Metrics": []}])
    assert asyncio.run(Boto3CloudWatchReader(lambda: client).scan_function_name()) is None


def test_lambda_daily_metrics_query_shape() -> None:
    day = lambda d: datetime(2026, 10, d, tzinfo=UTC)  # noqa: E731
    client = FakeCwClient(
        get_metric_data=[
            {
                "MetricDataResults": [
                    {"Id": "invocations", "Timestamps": [day(14), day(20)], "Values": [4.0, 8.0]},
                    {"Id": "errors", "Timestamps": [day(20)], "Values": [1.0]},
                ],
                "NextToken": "more",
            },
            {
                "MetricDataResults": [
                    {"Id": "duration_p95", "Timestamps": [day(20)], "Values": [61234.5]},
                ]
            },
        ]
    )
    reader = Boto3CloudWatchReader(lambda: client)
    days = asyncio.run(reader.lambda_daily_metrics("ScanFn", days=7, now=NOW))

    # One GetMetricData request (paged), three queries, daily buckets from midnight.
    assert [op for op, _ in client.calls] == ["get_metric_data", "get_metric_data"]
    kwargs = client.calls[0][1]
    assert kwargs["StartTime"] == datetime(2026, 10, 14, tzinfo=UTC)
    assert kwargs["EndTime"] == NOW
    queries = {q["Id"]: q for q in kwargs["MetricDataQueries"]}
    assert set(queries) == {"invocations", "errors", "duration_p95"}
    for qid, metric, stat in (
        ("invocations", "Invocations", "Sum"),
        ("errors", "Errors", "Sum"),
        ("duration_p95", "Duration", "p95"),
    ):
        stat_block = queries[qid]["MetricStat"]
        assert stat_block["Metric"] == {
            "Namespace": "AWS/Lambda",
            "MetricName": metric,
            "Dimensions": [{"Name": "FunctionName", "Value": "ScanFn"}],
        }
        assert stat_block["Period"] == 86400 and stat_block["Stat"] == stat
    assert client.calls[1][1]["NextToken"] == "more"

    # Every day from the start date through today, gaps zero-filled.
    assert [d.date for d in days] == [date(2026, 10, n) for n in range(14, 22)]
    assert days[0] == MetricDay(
        date=date(2026, 10, 14), invocations=4, errors=0, duration_p95_ms=None
    )
    assert days[6] == MetricDay(
        date=date(2026, 10, 20), invocations=8, errors=1, duration_p95_ms=61234.5
    )
    assert days[7].invocations == 0


def test_reader_errors_propagate_from_the_thread() -> None:
    client = FakeCwClient(describe_alarms=[_client_error()])  # type: ignore[list-item]
    with pytest.raises(ClientError):
        asyncio.run(Boto3CloudWatchReader(lambda: client).describe_alarms(ALARM_PREFIX))


def test_client_config_is_bounded() -> None:
    config = cloudwatch.CLIENT_CONFIG
    assert config.connect_timeout == 3
    assert config.read_timeout == 10
    assert config.retries == {"max_attempts": 1}


def test_cloudwatch_client_is_cached_per_container(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict[str, Any]] = []

    class FakeBoto3:
        @staticmethod
        def client(service: str, **kwargs: Any) -> object:
            built.append({"service": service, **kwargs})
            return object()

    import sys

    monkeypatch.setitem(sys.modules, "boto3", FakeBoto3)
    cloudwatch.reset_cloudwatch_client()
    try:
        first = cloudwatch.get_cloudwatch_client()
        assert cloudwatch.get_cloudwatch_client() is first
        assert len(built) == 1
        assert built[0]["service"] == "cloudwatch"
        assert built[0]["config"] is cloudwatch.CLIENT_CONFIG
    finally:
        cloudwatch.reset_cloudwatch_client()


# ---------------------------------------------------------------------------
# Routes: /api/system/alarms, /api/system/metrics
# ---------------------------------------------------------------------------


class FakeReader:
    """`CloudWatchReader` stand-in; ``error`` is raised by every method when set."""

    def __init__(
        self,
        *,
        alarms: list[AlarmState] | None = None,
        function_name: str | None = "CryptoSignals-Compute-ScanLambdaX",
        metric_days: list[MetricDay] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.alarms = alarms or []
        self.function_name = function_name
        self.metric_days = metric_days or []
        self.error = error
        self.calls: list[tuple[str, Any]] = []

    async def describe_alarms(self, prefix: str) -> list[AlarmState]:
        self.calls.append(("describe_alarms", prefix))
        if self.error is not None:
            raise self.error
        return list(self.alarms)

    async def scan_function_name(self) -> str | None:
        self.calls.append(("scan_function_name", None))
        if self.error is not None:
            raise self.error
        return self.function_name

    async def lambda_daily_metrics(
        self, function_name: str, *, days: int = 7, now: datetime
    ) -> list[MetricDay]:
        self.calls.append(("lambda_daily_metrics", (function_name, days, now)))
        if self.error is not None:
            raise self.error
        return list(self.metric_days)


ALARM = AlarmState(
    name="ScanFailureAlarm",
    state="ALARM",
    updated_at=datetime(2026, 10, 21, 8, 30, tzinfo=UTC),
    reason="Threshold Crossed",
)


def _client(reader: FakeReader, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from src.dashboard.api import overview as assembly

    monkeypatch.setattr(assembly, "utcnow", lambda: NOW)
    app = create_app(version="t")
    app.dependency_overrides[get_cloudwatch_reader] = lambda: reader
    return TestClient(app)


def test_system_alarms_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader(alarms=[ALARM])
    resp = _client(reader, monkeypatch).get("/api/system/alarms")
    assert resp.status_code == 200
    body = resp.json()
    assert body["note"] is None
    assert body["alarms"] == [
        {
            "name": "ScanFailureAlarm",
            "state": "ALARM",
            "updated_at": "2026-10-21T08:30:00Z",
            "reason": "Threshold Crossed",
        }
    ]
    assert reader.calls == [("describe_alarms", "CryptoSignals-Monitoring-")]


@pytest.mark.parametrize(
    "error",
    [
        _client_error("AccessDenied"),
        _client_error("Throttling"),
        EndpointConnectionError(endpoint_url="https://monitoring.ap-south-1.amazonaws.com"),
    ],
)
def test_system_alarms_degrades_on_client_error(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = _client(FakeReader(error=error), monkeypatch).get("/api/system/alarms")
    assert resp.status_code == 200
    assert resp.json() == {"alarms": None, "note": CLOUDWATCH_UNAVAILABLE}


def test_system_metrics_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    metric_days = [
        MetricDay(date=date(2026, 10, 20), invocations=8, errors=1, duration_p95_ms=6100.0)
    ]
    reader = FakeReader(metric_days=metric_days)
    body = _client(reader, monkeypatch).get("/api/system/metrics?days=3").json()
    assert body["function_name"] == "CryptoSignals-Compute-ScanLambdaX"
    assert body["note"] is None
    assert body["days"] == [
        {"date": "2026-10-20", "invocations": 8, "errors": 1, "duration_p95_ms": 6100.0}
    ]
    assert reader.calls[-1] == (
        "lambda_daily_metrics",
        ("CryptoSignals-Compute-ScanLambdaX", 3, NOW),
    )


def test_system_metrics_function_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _client(FakeReader(function_name=None), monkeypatch).get("/api/system/metrics").json()
    assert body == {"function_name": None, "days": None, "note": "scan function not found"}


def test_system_metrics_degrades_on_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _client(FakeReader(error=_client_error()), monkeypatch).get("/api/system/metrics")
    assert resp.status_code == 200
    assert resp.json() == {"function_name": None, "days": None, "note": CLOUDWATCH_UNAVAILABLE}


@pytest.mark.parametrize("days", [0, 31])
def test_system_metrics_days_bounded(days: int, monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _client(FakeReader(), monkeypatch).get(f"/api/system/metrics?days={days}")
    assert resp.status_code == 422
