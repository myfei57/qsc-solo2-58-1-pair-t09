"""Departure sequence: open, freeze, unbind and release.

Like the connect flow, departure runs as a ledger-backed job: every settled
step is recorded, an interrupted job resumes from its breakpoint under the
same job id, and replaying a finished job only rebuilds the read-side
conclusion without producing new side effects.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4

from portpower.console.validation import optional_number, optional_text, require_text
from portpower.disconn.batch import Connection, batch_disconnect
from portpower.disconn.close import close_after_arm
from portpower.job.errors import JobNotFinished
from portpower.job.record import DONE, JobRecord
from portpower.job.runner import JobRunner
from portpower.job.template import StepDefinition
from portpower.meter.binder import BindingMissing
from portpower.meter.freeze import freeze
from portpower.meter.meter import Meter
from portpower.plug.engage import engage
from portpower.plug.plug import Plug
from portpower.plug.release import release
from portpower.ship.arm import BreakerArm

KIND = "disconnect"


@dataclass(frozen=True)
class DisconnectRequest:
    berth: str
    ship: str
    final_value: float
    connection_id: str
    job: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DisconnectRequest":
        return cls(
            berth=require_text(payload, "berth"),
            ship=optional_text(payload, "ship"),
            final_value=optional_number(payload, "final_value", 0.0),
            connection_id=optional_text(payload, "connection", "disconnect"),
            job=optional_text(payload, "job"),
        )

    @classmethod
    def from_record(cls, payload: dict[str, object]) -> "DisconnectRequest":
        return cls(
            berth=str(payload["berth"]),
            ship=str(payload.get("ship") or ""),
            final_value=float(payload.get("final_value") or 0.0),  # type: ignore[arg-type]
            connection_id=str(payload.get("connection_id") or "disconnect"),
            job=str(payload.get("job") or ""),
        )

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DisconnectResult:
    job: str
    berth: str
    ship: str
    final_value: float | None
    released: bool
    breaker_state: str
    frozen: bool
    steps: int
    resumed: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def run_disconnect(runtime, request: DisconnectRequest) -> DisconnectResult:
    job_id = request.job or str(uuid4())
    record, created = runtime.jobs.ensure(
        KIND,
        job_id,
        request.as_payload(),
    )
    stored = DisconnectRequest.from_record(record.request)
    outcome = JobRunner(runtime.jobs).run(record, disconnect_steps(runtime, stored))
    return disconnect_result(runtime, outcome.record, resumed=not created)


def replay_disconnect(runtime, record: JobRecord) -> DisconnectResult:
    """Rebuild the read-side conclusion of a finished disconnect job."""

    if record.status != DONE:
        raise JobNotFinished(f"job {record.job_id} has not completed")
    return disconnect_result(runtime, record, resumed=False)


def disconnect_steps(runtime, request: DisconnectRequest) -> list[StepDefinition]:
    """The step template for a disconnect job; other kinds define their own."""

    def load_binding(ctx):
        binding = runtime.binder.load(request.berth)
        if binding is None:
            raise BindingMissing(f"no meter binding for berth {request.berth}")
        ship = request.ship or binding.ship
        return {"berth": binding.berth, "ship": ship}

    def arm_breaker(ctx):
        arm = BreakerArm()
        arm.arm()
        close_after_arm(runtime.shore, arm)
        return {"breaker": runtime.shore.state.value}

    def engage_plug(ctx):
        connector = Plug(request.connection_id)
        engage(connector, request.berth, runtime.binder)
        return connector.describe()

    def open_and_settle(ctx):
        steps = batch_disconnect(
            [
                Connection(
                    breaker=runtime.shore,
                    binder=runtime.binder,
                    berth=request.berth,
                )
            ],
            {request.berth: request.final_value},
        )
        return {"steps": len(steps), "final_value": request.final_value}

    def freeze_meter(ctx):
        meter = Meter(f"meter-{request.berth}", request.berth, runtime.store)
        return {"frozen": freeze(meter)}

    def release_berth(ctx):
        binding = ctx.settled("load-binding") or {}
        ship = str(binding.get("ship") or request.ship)
        released = False
        berth = runtime.berths.get(request.berth)
        if berth is not None and berth.ship_id == ship:
            released = runtime.berths.release(request.berth)
        runtime.fleet.undock(ship)
        runtime.mapping.unbind(request.berth)
        return {"released": released, "ship": ship}

    def release_plug(ctx):
        engaged = ctx.settled("engage-plug") or {}
        connector = Plug(request.connection_id)
        connector.mark_engaged(str(engaged.get("bound_ship") or ""))
        release(connector)
        runtime.protector.open_and_reset(runtime.shore)
        return {"released": not connector.engaged}

    def audit(ctx):
        binding = ctx.settled("load-binding") or {}
        ship = str(binding.get("ship") or request.ship)
        entry = runtime.ledger.append(
            "disconnect",
            f"{runtime.harbor.qualified(request.berth)} {ship}",
        )
        return entry.as_dict()

    return [
        StepDefinition("load-binding", load_binding),
        StepDefinition("arm-breaker", arm_breaker),
        StepDefinition("engage-plug", engage_plug),
        StepDefinition("open-and-settle", open_and_settle),
        StepDefinition("freeze-meter", freeze_meter),
        StepDefinition("release-berth", release_berth),
        StepDefinition("release-plug", release_plug),
        StepDefinition("audit", audit),
    ]


def disconnect_result(
    runtime,
    record: JobRecord,
    resumed: bool,
) -> DisconnectResult:
    """Read-side conclusion of a disconnect job; performs no side effects."""

    request = DisconnectRequest.from_record(record.request)
    binding = record.settled("load-binding")
    ship = str(binding.result.get("ship")) if binding is not None else request.ship
    settled = record.settled("open-and-settle")
    released_plug = record.settled("release-plug")
    meter = Meter(f"meter-{request.berth}", request.berth, runtime.store)
    return DisconnectResult(
        job=record.job_id,
        berth=request.berth,
        ship=ship,
        final_value=runtime.binder.final_value(request.berth),
        released=bool(released_plug.result.get("released"))
        if released_plug is not None
        else False,
        breaker_state=runtime.shore.state.value,
        frozen=meter.frozen,
        steps=int(settled.result.get("steps", 0)) if settled is not None else 0,  # type: ignore[arg-type]
        resumed=resumed,
    )
