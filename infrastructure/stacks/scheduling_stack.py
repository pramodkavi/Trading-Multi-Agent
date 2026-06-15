"""SchedulingStack: EventBridge Scheduler schedules that invoke the scan Lambda.

Implemented in Step 1.19 for the serverless architecture (SPEC §2.4 / §5):

- **EventBridge Scheduler** schedules (not legacy Events rules) that invoke the
  scan **Lambda** directly. Scheduler is the purpose-built cron service:
  timezone-aware, with a per-target retry policy and no always-on infrastructure.
- The four SPEC §5 scan windows -- London 08:03, NY 13:03, overlap 15:03,
  daily-wrap 22:03 -- each invoke the Lambda in its default (scan) mode.
- The Forecaster sweep (Step 2.10): ``cron(5 8,13,15,22 * * ? *)`` -- two minutes
  after each scan -- invokes the SAME Lambda with a ``{"mode": "forecaster"}``
  payload. CLAUDE.md keeps a single scan Lambda at this scale, so the "clean
  separation" the SPEC calls for is a dedicated schedule + event flag, not a
  second function. The weekly Critic (Sun 21:00) arrives in Slice 3.

The ``:03`` minute is deliberate (SPEC §5): it dodges the clock-jitter spike that
hits every cron at ``:00``. The L2 ``LambdaInvoke`` target provisions a dedicated
invoke role scoped to the one function, so the "appropriate invoke permission"
is created automatically.

The scan Lambda lives in the ComputeStack and is passed in, so the targets are
cross-stack references (CDK emits the export/import).
"""

from __future__ import annotations

from typing import Any

from aws_cdk import (
    CfnOutput,
    Stack,
    Tags,
    TimeZone,
)
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from cdk_nag import NagSuppressions
from constructs import Construct


class SchedulingStack(Stack):
    """EventBridge Scheduler schedule(s) that invoke the scan Lambda."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        scan_function: lambda_.IFunction,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # London open: 08:03 UTC daily -> cron(3 8 * * ? *). day-of-week is "?"
        # (AWS requires it whenever day-of-month is set); both default here.
        self.london_open_schedule = scheduler.Schedule(
            self,
            "LondonOpenScan",
            schedule=scheduler.ScheduleExpression.cron(
                minute="3",
                hour="8",
                time_zone=TimeZone.ETC_UTC,
            ),
            target=scheduler_targets.LambdaInvoke(scan_function),
            description=("London-open scan (Slice 1): invokes the scan Lambda daily at 08:03 UTC."),
        )

        CfnOutput(
            self,
            "LondonOpenScheduleName",
            value=self.london_open_schedule.schedule_name,
        )

        # Remaining SPEC §5 scan windows, mirroring the London-open template:
        # NY open 13:03, London-NY overlap 15:03, daily wrap 22:03. All invoke
        # the scan Lambda in its default (scan) mode.
        scan_windows = [
            ("NyOpenScan", "13", "NY-open scan: invokes the scan Lambda daily at 13:03 UTC."),
            (
                "OverlapScan",
                "15",
                "London-NY overlap scan: invokes the scan Lambda daily at 15:03 UTC.",
            ),
            ("DailyWrapScan", "22", "Daily-wrap scan: invokes the scan Lambda daily at 22:03 UTC."),
        ]
        for schedule_id, hour, description in scan_windows:
            scheduler.Schedule(
                self,
                schedule_id,
                schedule=scheduler.ScheduleExpression.cron(
                    minute="3",
                    hour=hour,
                    time_zone=TimeZone.ETC_UTC,
                ),
                target=scheduler_targets.LambdaInvoke(scan_function),
                description=description,
            )

        # Forecaster sweep (Step 2.10): two minutes after each scan window
        # (cron 5 8,13,15,22) re-evaluate open setups. Same Lambda, invoked with
        # a forecaster-mode flag in the event payload (see the module docstring).
        self.forecaster_schedule = scheduler.Schedule(
            self,
            "ForecasterSweep",
            schedule=scheduler.ScheduleExpression.cron(
                minute="5",
                hour="8,13,15,22",
                time_zone=TimeZone.ETC_UTC,
            ),
            target=scheduler_targets.LambdaInvoke(
                scan_function,
                input=scheduler.ScheduleTargetInput.from_object({"mode": "forecaster"}),
            ),
            description=(
                "Forecaster sweep: re-evaluates open setups ~2 min after each scan "
                "(08:05 / 13:05 / 15:05 / 22:05 UTC) via the scan Lambda in forecaster mode."
            ),
        )

        CfnOutput(
            self,
            "ForecasterScheduleName",
            value=self.forecaster_schedule.schedule_name,
        )

        Tags.of(self).add("project", "crypto-signals")
        Tags.of(self).add("layer", "scheduling")

        self._apply_nag_suppressions()

    def _apply_nag_suppressions(self) -> None:
        """Justified cdk-nag suppressions for the scheduler target role.

        The L2 ``LambdaInvoke`` target provisions its invoke role as a *stack*
        construct (``SchedulerRoleForTarget-...``), not a child of the Schedule,
        so the suppression is applied at the stack level. The ``appliesTo`` regex
        scopes it to exactly the ``<function.Arn>:*`` resource wildcard (the
        version/alias qualifier), so any unrelated future IAM5 finding still
        surfaces.
        """
        NagSuppressions.add_stack_suppressions(
            self,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The Scheduler target role's lambda:InvokeFunction is scoped to "
                        "the one scan function and its version/alias qualifiers (the ':*' "
                        "suffix the L2 LambdaInvoke target generates) -- no broader invoke "
                        "access."
                    ),
                    "appliesTo": [{"regex": r"/^Resource::<.*\.Arn>:\*$/g"}],
                },
            ],
        )
