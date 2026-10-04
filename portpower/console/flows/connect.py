"""Bringing a vessel alongside and closing onto the shore supply.

The flow runs as a ledger-backed job: each step is recorded as it settles,
an interrupted job resumes from its breakpoint when the same job id is
re-posted, and settled steps are never executed twice. Replaying a finished
job rebuilds the read-side result only — no step runs, nothing is written.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4

from portpower.cable.rating import classify
from portpower.console.http import BadRequest
from portpower.console.validation import (
    optional_number,
    optional_text,
    require_number,
    require_text,
    valid_berth_code,
    valid_frequency,
    valid_load,
    valid_ship_id,
    valid_temperature,
)
from portpower.disconn.close import close_after_phase
from portpower.disconn.limit import LoadExceedsProfile
from portpower.disconn.parallel import parallel_close
from portpower.job.record import DONE, JobRecord
from portpower.job.runner import JobRunner
from portpower.job.template import StepDefinition
from portpower.job.errors import JobNotFinished
from portpower.meter.binder import Binding
from portpower.meter.freeze import unfreeze
from portpower.meter.meter import Meter
from portpower.phase.parse import parse_sequence
from portpower.phase.verifier import Verifier
from portpower.plug.engage import engage
from portpower.plug.plug import Plug
from portpower.quota.quota import Quota
from portpower.ship.profile import Profile

SYNC_TOLERANCE = 0.5
KIND = "connect"


@dataclass(frozen=True)
class ConnectRequest:
    berth: str
    ship: str
    phase: str
    frequency: float
    load: float
    temperature: float
    job: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ConnectRequest":
        return cls(
            berth=require_text(payload, "berth"),
            ship=require_text(payload, "ship"),
            phase=require_text(payload, "phase"),
            frequency=require_number(payload, "frequency"),
            load=require_number(payload, "load"),
            temperature=optional_number(payload, "temperature", 25.0),
            job=optional_text(payload, "job"),
        )

    @classmethod
    def from_record(cls, payload: dict[str, object]) -> "ConnectRequest":
        return cls(
            berth=str(payload["berth"]),
            ship=str(payload["ship"]),
            phase=str(payload["phase"]),
            frequency=float(payload["frequency"]),  # type: ignore[arg-type]
            load=float(payload["load"]),  # type: ignore[arg-type]
            temperature=float(payload["temperature"]),  # type: ignore[arg-type]
            job=str(payload.get("job") or ""),
        )

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ConnectResult:
    job: str
    connection_id: str
    berth: str
    ship: str
    plug: dict[str, object]
    demand: float
    headroom: float
    breaker_state: str
    phase: str
    cable_rating: str
    temperature: float
    resumed: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def run_connect(runtime, request: ConnectRequest) -> ConnectResult:
    job_id = request.job or str(uuid4())
    record, created = runtime.jobs.ensure(
        KIND,
        job_id,
        request.as_payload(),
        context={"connection": str(uuid4())},
    )
    stored = ConnectRequest.from_record(record.request)
    outcome = JobRunner(runtime.jobs).run(record, connect_steps(runtime, stored))
    return connect_result(runtime, outcome.record, resumed=not created)


def replay_connect(runtime, record: JobRecord) -> ConnectResult:
    """Rebuild the read-side conclusion of a finished connect job."""

    if record.status != DONE:
        raise JobNotFinished(f"job {record.job_id} has not completed")
    return connect_result(runtime, record, resumed=False)


def connect_steps(runtime, request: ConnectRequest) -> list[StepDefinition]:
    """The step template for a connect job; other kinds define their own."""

    def validate(ctx):
        _validate(runtime, request)
        sequence = parse_sequence(request.phase)
        if sequence is None:
            raise BadRequest(f"unknown phase sequence {request.phase!r}")
        _enforce_existing_profile(runtime, request)
        profile = Profile(
            ship=request.ship,
            max_load=request.load,
            phase_seq=sequence.value,
            frequency=request.frequency,
        )
        if not profile.is_usable():
            raise BadRequest("invalid ship power profile")
        return {"phase": sequence.value}

    def assign_berth(ctx):
        berth = runtime.berths.add(request.berth)
        berth.assign(request.ship)
        vessel = runtime.fleet.add(request.ship)
        vessel.moor(request.berth)
        return {"berth": request.berth, "ship": request.ship}

    def save_profile(ctx):
        sequence = parse_sequence(request.phase)
        profile = Profile(
            ship=request.ship,
            max_load=request.load,
            phase_seq=sequence.value,
            frequency=request.frequency,
        )
        runtime.profiles.save(profile)
        record = runtime.history.append(profile)
        return {"version": record.version}

    def bind_meter(ctx):
        runtime.mapping.bind(request.berth, request.ship)
        binding = Binding(berth=request.berth, ship=request.ship)
        runtime.binder.bind(binding)
        return binding.as_dict()

    def unfreeze_meter(ctx):
        meter = Meter(f"meter-{request.berth}", request.berth, runtime.store)
        return {"unfrozen": unfreeze(meter)}

    def engage_plug(ctx):
        connector = Plug(_connection_id(ctx.record))
        engage(connector, request.berth, runtime.binder)
        return connector.describe()

    def close_breaker(ctx):
        sequence = parse_sequence(request.phase)
        close_after_phase(runtime.shore, Verifier(sequence))
        runtime.phase.set(sequence)
        return {
            "breaker": runtime.shore.state.value,
            "phase": runtime.phase.current.value,
        }

    def synchronize(ctx):
        current = parallel_close(
            runtime.shore,
            runtime.syncer,
            target=request.frequency,
            tolerance=SYNC_TOLERANCE,
        )
        return {"frequency": current}

    def observe_temperature(ctx):
        runtime.sensor.set(request.temperature)
        runtime.monitor.observe(request.temperature)
        tripped = runtime.protector.verdict(runtime.sensor.read())
        return {"temperature": runtime.sensor.read(), "tripped": tripped}

    def set_quota(ctx):
        quota = Quota(ship=request.ship, limit=request.load)
        runtime.quotas.set(quota)
        return quota.as_dict()

    def audit(ctx):
        entry = runtime.ledger.append(
            "connect",
            f"{runtime.harbor.qualified(request.berth)} {request.ship} "
            f"{_connection_id(ctx.record)}",
        )
        return entry.as_dict()

    return [
        StepDefinition("validate", validate),
        StepDefinition("assign-berth", assign_berth),
        StepDefinition("save-profile", save_profile),
        StepDefinition("bind-meter", bind_meter),
        StepDefinition("unfreeze-meter", unfreeze_meter),
        StepDefinition("engage-plug", engage_plug),
        StepDefinition("close-breaker", close_breaker),
        StepDefinition("synchronize", synchronize),
        StepDefinition("observe-temperature", observe_temperature),
        StepDefinition("set-quota", set_quota),
        StepDefinition("audit", audit),
    ]


def connect_result(
    runtime,
    record: JobRecord,
    resumed: bool,
) -> ConnectResult:
    """Read-side conclusion of a connect job; performs no side effects."""

    request = ConnectRequest.from_record(record.request)
    plug = record.settled("engage-plug")
    return ConnectResult(
        job=record.job_id,
        connection_id=_connection_id(record),
        berth=request.berth,
        ship=request.ship,
        plug=dict(plug.result) if plug is not None else {},
        demand=runtime.profiles.demand(request.ship),
        headroom=runtime.profiles.headroom(request.ship),
        breaker_state=runtime.shore.state.value,
        phase=runtime.phase.current.value,
        cable_rating=classify(
            runtime.sensor.read(), runtime.cable.rated_temperature
        ).value,
        temperature=runtime.sensor.read(),
        resumed=resumed,
    )


def _connection_id(record: JobRecord) -> str:
    return str(record.context.get("connection") or "")


def _validate(runtime, request: ConnectRequest) -> None:
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


def _enforce_existing_profile(runtime, request: ConnectRequest) -> None:
    existing = runtime.profiles.load(request.ship)
    if existing is None:
        return
    if not runtime.limiter.allow(request.ship, request.load):
        raise LoadExceedsProfile(
            f"load {request.load} exceeds declared {existing.max_load}"
        )
