"""Departure sequence, modelled as a durable step ledger.

Like the connect chain, every step checkpoints its result; an interrupted
departure resumes at the first unfinished row rather than re-recording the
final meter value, the berth release or the audit line.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4

from portpower.console.validation import optional_number, optional_text, require_text
from portpower.disconn.close import close_after_arm
from portpower.job.engine import JobDefinition, StepDef
from portpower.job.store import Job
from portpower.meter.binder import BindingMissing
from portpower.meter.freeze import freeze
from portpower.meter.meter import Meter
from portpower.plug.engage import engage
from portpower.plug.plug import Plug
from portpower.plug.release import release
from portpower.ship.arm import BreakerArm

JOB_KIND = "disconnect"
DEFINITION_VERSION = "disconnect.v1"


@dataclass(frozen=True)
class DisconnectRequest:
    berth: str
    ship: str | None = None
    final_value: float = 0.0
    connection_id: str = "disconnect"
    job_id: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DisconnectRequest":
        return cls(
            berth=require_text(payload, "berth"),
            ship=optional_text(payload, "ship") or None,
            final_value=optional_number(payload, "final_value", 0.0),
            connection_id=optional_text(payload, "connection", "disconnect"),
            job_id=optional_text_or_none(payload, "job_id"),
        )

    def as_ledger_request(self) -> dict[str, Any]:
        return {
            "berth": self.berth,
            "ship": self.ship,
            "final_value": self.final_value,
            "connection": self.connection_id,
        }


@dataclass(frozen=True)
class DisconnectResult:
    berth: str
    ship: str
    job_id: str
    final_value: float | None
    released: bool
    breaker_state: str
    frozen: bool
    steps: int
    ledger: list[dict[str, Any]]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def optional_text_or_none(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        from portpower.console.http import BadRequest

        raise BadRequest(f"{key} must be a non-empty string")
    return value.strip()


def run_disconnect(runtime, request: DisconnectRequest) -> tuple[DisconnectResult, Job]:
    job_id = request.job_id or str(uuid4())
    existing = runtime.jobs.jobs.get(job_id) if request.job_id else None

    if existing is None:
        # The berth must currently be bound; this side-effect-free check lets
        # an unbound departure fail before a job document is opened.
        binding = runtime.binder.load(request.berth)
        if binding is None:
            raise BindingMissing(f"no meter binding for berth {request.berth}")
        ship = request.ship or binding.ship
    else:
        # Re-attaching to an existing job (possibly after a restart): the chain
        # may already have removed the binding, so trust the frozen request.
        ship = existing.request["ship"]

    ledger_request = request.as_ledger_request()
    ledger_request["ship"] = ship

    runtime.jobs.open_or_get(job_id, JOB_KIND, ledger_request)
    runtime.jobs.run(runtime, job_id)
    return (
        project_disconnect(runtime, job_id),
        runtime.jobs.jobs.require(job_id),
    )


# ---------------------------------------------------------------------------
# Step actions
# ---------------------------------------------------------------------------


def _data(job: Job) -> dict[str, Any]:
    return job.request


def _arm_and_close(runtime, job: Job) -> dict[str, Any]:
    arm = BreakerArm()
    arm.arm()
    close_after_arm(runtime.shore, arm)
    return {"breaker_state": runtime.shore.state.value}


def _engage_plug(runtime, job: Job) -> dict[str, Any]:
    berth = _data(job)["berth"]
    connector = Plug(job.job_id)
    engage(connector, berth, runtime.binder)
    return connector.describe()


def _disconnect_chain(runtime, job: Job) -> dict[str, Any]:
    data = _data(job)
    berth = data["berth"]
    # Each half of the chain is guarded by the state the previous half leaves
    # behind, so any crash window resumes cleanly:
    #   - breaker already open  -> do not open it again
    #   - binding already gone  -> the final value was already recorded
    if runtime.binder.load(berth) is not None:
        if runtime.shore.closed:
            runtime.shore.mark_open()
        final_value = runtime.binder.record_final(berth, data["final_value"])
        runtime.binder.unbind(berth)
    else:
        recorded = runtime.binder.final_value(berth)
        final_value = (
            recorded if recorded is not None else data["final_value"]
        )
        if runtime.shore.closed:
            runtime.shore.mark_open()
    return {
        "berth": berth,
        "opened": True,
        "final_value": float(final_value),
        "breaker_state": runtime.shore.state.value,
    }


def _freeze_meter(runtime, job: Job) -> dict[str, Any]:
    berth = _data(job)["berth"]
    meter = Meter(f"meter-{berth}", berth, runtime.store)
    frozen = freeze(meter)
    return {"berth": berth, "frozen": frozen}


def _release_berth(runtime, job: Job) -> dict[str, Any]:
    data = _data(job)
    berth, ship = data["berth"], data["ship"]
    current = runtime.berths.get(berth)
    released = False
    if current is not None and current.ship_id == ship:
        released = runtime.berths.release(berth)
    runtime.fleet.undock(ship)
    runtime.mapping.unbind(berth)
    return {"berth": berth, "ship": ship, "berth_released": bool(released)}


def _release_plug(runtime, job: Job) -> dict[str, Any]:
    connector = Plug(job.job_id)
    # Reconstruct the engaged state from the previous step's checkpoint: the
    # plug object itself is process-local and may have died with the process.
    engaged = job.result_of("engage_plug")
    if engaged is not None and not connector.engaged:
        connector.mark_engaged(engaged.get("bound_ship", _data(job)["ship"]))
    if connector.engaged:
        release(connector)
    return connector.describe()


def _reset_protection(runtime, job: Job) -> dict[str, Any]:
    runtime.protector.open_and_reset(runtime.shore)
    return {"breaker_state": runtime.shore.state.value, "tripped": False}


def _audit(runtime, job: Job) -> dict[str, Any]:
    data = _data(job)
    detail = f"{runtime.harbor.qualified(data['berth'])} {data['ship']}"
    entry = runtime.ledger.append_once("disconnect", detail)
    return entry.as_dict()


STEPS: tuple[StepDef, ...] = (
    StepDef("arm_and_close", _arm_and_close),
    StepDef("engage_plug", _engage_plug),
    StepDef("disconnect_chain", _disconnect_chain),
    StepDef("freeze_meter", _freeze_meter),
    StepDef("release_berth", _release_berth),
    StepDef("release_plug", _release_plug),
    StepDef("reset_protection", _reset_protection),
    StepDef("audit", _audit),
)


def _project(job: Job) -> DisconnectResult:
    data = job.request
    chain = job.result_of("disconnect_chain") or {}
    frozen_step = job.result_of("freeze_meter") or {}
    reset = job.result_of("reset_protection") or chain
    plug = job.result_of("release_plug") or {}
    return DisconnectResult(
        berth=data["berth"],
        ship=data["ship"],
        job_id=job.job_id,
        final_value=chain.get("final_value"),
        released=not plug.get("engaged", False),
        breaker_state=str(reset.get("breaker_state", "open")),
        frozen=bool(frozen_step.get("frozen", True)),
        steps=job.completed_steps,
        ledger=[record.as_dict() for record in job.steps],
    )


def project_disconnect(runtime, job_id: str) -> DisconnectResult:
    return _project(runtime.jobs.jobs.require(job_id))


def definition() -> JobDefinition:
    """Step tailoring for the disconnect job kind."""

    return JobDefinition(
        kind=JOB_KIND,
        version=DEFINITION_VERSION,
        steps=STEPS,
        project=lambda job: _project(job).as_dict(),
    )
