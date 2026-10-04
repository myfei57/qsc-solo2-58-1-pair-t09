"""Bringing a vessel alongside and closing onto the shore supply.

The chain is modelled as a durable step ledger (``portpower.job``): every row
checkpoints its own result, so a failed run resumes at the first unfinished
step instead of repeating the berth assignment, quota binding and audit line
that already landed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4

from portpower.cable.rating import classify
from portpower.console.http import BadRequest
from portpower.console.validation import (
    optional_number,
    require_number,
    require_text,
    valid_berth_code,
    valid_frequency,
    valid_load,
    valid_ship_id,
    valid_temperature,
)
from portpower.disconn.limit import LoadExceedsProfile
from portpower.disconn.parallel import parallel_close
from portpower.job.engine import JobDefinition, StepDef
from portpower.job.store import Job
from portpower.meter.binder import Binding
from portpower.meter.freeze import unfreeze
from portpower.meter.meter import Meter
from portpower.phase.parse import parse_sequence
from portpower.phase.verifier import Verifier
from portpower.plug.engage import engage
from portpower.plug.plug import Plug
from portpower.quota.quota import Quota
from portpower.ship.profile import Profile

JOB_KIND = "connect"
DEFINITION_VERSION = "connect.v1"
SYNC_TOLERANCE = 0.5


@dataclass(frozen=True)
class ConnectRequest:
    berth: str
    ship: str
    phase: str
    frequency: float
    load: float
    temperature: float
    job_id: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ConnectRequest":
        return cls(
            berth=require_text(payload, "berth"),
            ship=require_text(payload, "ship"),
            phase=require_text(payload, "phase"),
            frequency=require_number(payload, "frequency"),
            load=require_number(payload, "load"),
            temperature=optional_number(payload, "temperature", 25.0),
            job_id=optional_text_or_none(payload, "job_id"),
        )

    def as_ledger_request(self) -> dict[str, Any]:
        return {
            "berth": self.berth,
            "ship": self.ship,
            "phase": self.phase,
            "frequency": self.frequency,
            "load": self.load,
            "temperature": self.temperature,
        }


@dataclass(frozen=True)
class ConnectResult:
    connection_id: str
    job_id: str
    berth: str
    ship: str
    plug: dict[str, object]
    demand: float
    headroom: float
    breaker_state: str
    phase: str
    cable_rating: str
    temperature: float
    steps: list[dict[str, Any]]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def optional_text_or_none(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BadRequest(f"{key} must be a non-empty string")
    return value.strip()


def validate(runtime, request: ConnectRequest) -> None:
    if not valid_berth_code(request.berth) or not runtime.catalog.has(request.berth):
        raise BadRequest("invalid or unknown berth code")
    if not valid_ship_id(request.ship):
        raise BadRequest("invalid ship id")
    if not valid_frequency(request.frequency):
        raise BadRequest("invalid frequency")
    if not valid_load(request.load):
        raise BadRequest("invalid load")
    if not valid_temperature(request.temperature):
        raise BadRequest("invalid temperature")
    if parse_sequence(request.phase) is None:
        raise BadRequest(f"unknown phase sequence {request.phase!r}")


def run_connect(runtime, request: ConnectRequest) -> tuple[ConnectResult, Job]:
    """Open (or re-attach to) the connect job and advance its ledger."""

    validate(runtime, request)
    job_id = request.job_id or str(uuid4())

    job, _ = runtime.jobs.open_or_get(
        job_id, JOB_KIND, request.as_ledger_request()
    )
    runtime.jobs.run(runtime, job_id)
    return (
        project_connect(runtime, job_id),
        runtime.jobs.jobs.require(job_id),
    )


def definition() -> JobDefinition:
    """Step tailoring for the connect job kind."""

    return JobDefinition(
        kind=JOB_KIND,
        version=DEFINITION_VERSION,
        steps=STEPS,
        project=lambda job: _project(job).as_dict(),
    )


# ---------------------------------------------------------------------------
# Step actions. Each is idempotent: inspect current state before writing, so a
# re-run after a lost checkpoint never doubles up a binding or an audit line.
# ---------------------------------------------------------------------------


def _data(job: Job) -> dict[str, Any]:
    return job.request


def _assign_berth(runtime, job: Job) -> dict[str, Any]:
    data = _data(job)
    berth_code, ship = data["berth"], data["ship"]
    berth = runtime.berths.add(berth_code)
    if berth.ship_id is None:
        # After a restart the in-memory registry is empty; the durable mapping
        # (written by a later step) is evidence the assignment already landed.
        anchored = runtime.mapping.ship_for(berth_code)
        if anchored is not None:
            if anchored != ship:
                from portpower.berth.berth import BerthOccupied

                raise BerthOccupied(f"berth {berth_code} already holds {anchored}")
        else:
            berth.assign(ship)
    elif berth.ship_id != ship:
        from portpower.berth.berth import BerthOccupied

        raise BerthOccupied(f"berth {berth_code} already holds {berth.ship_id}")
    vessel = runtime.fleet.add(ship)
    if vessel.moored_at != berth_code:
        vessel.moor(berth_code)
    return {"berth": berth_code, "ship": ship}


def _save_profile(runtime, job: Job) -> dict[str, Any]:
    data = _data(job)
    ship = data["ship"]
    existing = runtime.profiles.load(ship)
    if existing is not None and not runtime.limiter.allow(ship, data["load"]):
        raise LoadExceedsProfile(
            f"load {data['load']} exceeds declared {existing.max_load}"
        )
    sequence = parse_sequence(data["phase"])
    profile = Profile(
        ship=ship,
        max_load=data["load"],
        phase_seq=sequence.value,
        frequency=data["frequency"],
    )
    if not profile.is_usable():
        raise BadRequest("invalid ship power profile")
    if existing != profile:
        runtime.profiles.save(profile)
        runtime.history.append(profile)
    return {
        "ship": ship,
        "max_load": profile.max_load,
        "phase_seq": profile.phase_seq,
        "frequency": profile.frequency,
        "demand": profile.max_load,
        "headroom": runtime.profiles.headroom(ship),
    }


def _bind_meter(runtime, job: Job) -> dict[str, Any]:
    data = _data(job)
    berth, ship = data["berth"], data["ship"]
    runtime.mapping.bind(berth, ship)
    binding = runtime.binder.load(berth)
    if binding is None or binding.ship != ship:
        runtime.binder.bind(Binding(berth=berth, ship=ship))
    return {"berth": berth, "ship": ship}


def _unfreeze_meter(runtime, job: Job) -> dict[str, Any]:
    berth = _data(job)["berth"]
    meter = Meter(f"meter-{berth}", berth, runtime.store)
    unfreeze(meter)
    return {"berth": berth, "frozen": False}


def _engage_plug(runtime, job: Job) -> dict[str, Any]:
    berth = _data(job)["berth"]
    connector = Plug(job.job_id)
    engage(connector, berth, runtime.binder)
    return connector.describe()


def _close_breaker(runtime, job: Job) -> dict[str, Any]:
    sequence = parse_sequence(_data(job)["phase"])
    verifier = Verifier(sequence)
    verifier.verify()
    runtime.shore.mark_closed()
    # Avoid appending a duplicate rotation if the checkpoint was lost after the
    # first pass.
    if runtime.phase.current is not sequence:
        runtime.phase.set(sequence)
    return {"breaker_state": runtime.shore.state.value, "phase": sequence.value}


def _parallel_close(runtime, job: Job) -> dict[str, Any]:
    target = _data(job)["frequency"]
    current = parallel_close(
        runtime.shore, runtime.syncer, target=target, tolerance=SYNC_TOLERANCE
    )
    return {"frequency": current, "breaker_state": runtime.shore.state.value}


def _measure_temperature(runtime, job: Job) -> dict[str, Any]:
    temperature = _data(job)["temperature"]
    runtime.sensor.set(temperature)
    runtime.monitor.observe(temperature)
    tripped = runtime.protector.verdict(runtime.sensor.read())
    return {
        "temperature": runtime.sensor.read(),
        "cable_rating": classify(
            runtime.sensor.read(), runtime.cable.rated_temperature
        ).value,
        "tripped": tripped,
    }


def _set_quota(runtime, job: Job) -> dict[str, Any]:
    data = _data(job)
    ship, load = data["ship"], data["load"]
    runtime.quotas.set(Quota(ship=ship, limit=load))
    return {"ship": ship, "limit": load}


def _audit(runtime, job: Job) -> dict[str, Any]:
    data = _data(job)
    detail = f"{runtime.harbor.qualified(data['berth'])} {data['ship']} {job.job_id}"
    entry = runtime.ledger.append_once("connect", detail)
    return entry.as_dict()


STEPS: tuple[StepDef, ...] = (
    StepDef("assign_berth", _assign_berth),
    StepDef("save_profile", _save_profile),
    StepDef("bind_meter", _bind_meter),
    StepDef("unfreeze_meter", _unfreeze_meter),
    StepDef("engage_plug", _engage_plug),
    StepDef("close_breaker", _close_breaker),
    StepDef("parallel_close", _parallel_close),
    StepDef("measure_temperature", _measure_temperature),
    StepDef("set_quota", _set_quota),
    StepDef("audit", _audit),
)


# ---------------------------------------------------------------------------
# Read-side projection: rebuilds the conclusion from checkpoint results only,
# which is also what a replay of a completed job returns.
# ---------------------------------------------------------------------------


def _project(job: Job) -> ConnectResult:
    data = job.request
    berth, ship = data["berth"], data["ship"]
    profile = job.result_of("save_profile") or {}
    plug = job.result_of("engage_plug") or {"id": job.job_id, "state": "disconnected"}
    breaker = job.result_of("parallel_close") or job.result_of("close_breaker") or {}
    measure = job.result_of("measure_temperature") or {}
    return ConnectResult(
        connection_id=job.job_id,
        job_id=job.job_id,
        berth=berth,
        ship=ship,
        plug=plug,
        demand=float(profile.get("demand", 0.0)),
        headroom=float(profile.get("headroom", 0.0)),
        breaker_state=str(breaker.get("breaker_state", "open")),
        phase=str(profile.get("phase_seq", data["phase"])),
        cable_rating=str(measure.get("cable_rating", "normal")),
        temperature=float(measure.get("temperature", data["temperature"])),
        steps=[record.as_dict() for record in job.steps],
    )


def project_connect(runtime, job_id: str) -> ConnectResult:
    return _project(runtime.jobs.jobs.require(job_id))
