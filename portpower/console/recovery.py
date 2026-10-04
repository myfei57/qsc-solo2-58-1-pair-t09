"""Startup recovery of in-process collaborators from unfinished job ledgers.

Durable collaborators (bindings, quotas, mappings, audit) live in the record
store and need no recovery, but the berth registry, fleet, breaker, plug and
phase state are process-local. When a process restarts with an unfinished job
on disk, those collaborators must be rebuilt up to the last ``done``
checkpoint so the job can resume from its first unfinished step. Completed
steps are never re-executed; their recorded results are applied here instead.
"""

from __future__ import annotations

from typing import Any

from portpower.job.store import Job, JobState, StepState


def recover(runtime) -> None:
    for job in runtime.jobs.jobs.list():
        if job.state is JobState.COMPLETE:
            continue
        _RECOVERY.get(job.kind, lambda rt, job: None)(runtime, job)


def _recover_connect(runtime, job: Job) -> None:
    done = {r.key: r for r in job.steps if r.state is StepState.DONE}
    data = job.request
    berth_code, ship = data["berth"], data["ship"]

    if "assign_berth" in done:
        berth = runtime.berths.add(berth_code)
        if berth.ship_id is None:
            berth.assign(ship)
        runtime.fleet.add(ship).moor(berth_code)

    if "save_profile" in done:
        # Profile registry and history are durable; nothing in-process to fix.
        pass

    if "bind_meter" in done:
        binding = runtime.binder.load(berth_code)
        if binding is not None:
            # Prime the process-local current-binding cache.
            runtime.binder.bind(binding)

    if "close_breaker" in done:
        runtime.shore.mark_closed()
        from portpower.phase.parse import parse_sequence

        sequence = parse_sequence(done["close_breaker"].result.get("phase") or data["phase"])
        if sequence is not None and runtime.phase.current is not sequence:
            runtime.phase.set(sequence)

    if "parallel_close" in done and not runtime.shore.closed:
        runtime.shore.mark_closed()

    if "measure_temperature" in done:
        temperature = float(done["measure_temperature"].result.get("temperature", 0.0))
        runtime.sensor.set(temperature)

    # engage_plug's connector is local to the original request and only feeds
    # the plug description; the connect flow never releases it, so no recovery
    # is required there.


def _recover_disconnect(runtime, job: Job) -> None:
    done = {r.key for r in job.steps if r.state is StepState.DONE}

    if "arm_and_close" in done:
        runtime.shore.mark_closed()

    if "disconnect_chain" in done:
        # Chain already opened the breaker and removed the binding.
        if runtime.shore.closed:
            runtime.shore.mark_open()

    if "release_berth" in done:
        data = job.request
        berth_code, ship = data["berth"], data["ship"]
        berth = runtime.berths.get(berth_code)
        if berth is not None and berth.ship_id == ship:
            runtime.berths.release(berth_code)
        vessel = runtime.fleet.get(ship)
        if vessel is not None:
            vessel.depart()

    if "reset_protection" in done:
        runtime.protector.reset_filter()
        if runtime.shore.closed:
            runtime.shore.mark_open()


_RECOVERY: dict[str, Any] = {
    "connect": _recover_connect,
    "disconnect": _recover_disconnect,
}
