"""Job ledger endpoints: inspect, resume after an interruption, and replay."""

from __future__ import annotations

from portpower.console.http import BadRequest, Request, Response
from portpower.job.engine import JobAlreadyComplete
from portpower.job.store import JobNotFound


def job_list(runtime, request: Request) -> Response:
    jobs = runtime.jobs.jobs.list()
    return Response.json(
        200,
        {"count": len(jobs), "jobs": [job.as_dict() for job in jobs]},
    )


def job_detail(runtime, request: Request) -> Response:
    job = _require_job(runtime, request)
    return Response.json(200, job.as_dict())


def job_resume(runtime, request: Request) -> Response:
    """Continue an interrupted job from its first unfinished checkpoint."""

    payload = request.json_object()
    job_id = _job_id(payload, request)
    job = runtime.jobs.jobs.require(job_id)
    if job.state.value == "complete":
        raise JobAlreadyComplete(f"job {job_id} is already complete")
    runtime.jobs.jobs.resume(job_id)
    runtime.jobs.run(runtime, job_id)
    refreshed = runtime.jobs.jobs.require(job_id)
    body = refreshed.as_dict()
    body["conclusion"] = runtime.jobs.replay(job_id)
    return Response.json(200, body)


def job_replay(runtime, request: Request) -> Response:
    """Rebuild a completed job's read-side conclusion without side effects."""

    payload = request.json_object() if request.body else {}
    job_id = _job_id(payload, request)
    job = runtime.jobs.jobs.require(job_id)
    conclusion = runtime.jobs.replay(job_id)
    return Response.json(
        200,
        {
            "job_id": job_id,
            "kind": job.kind,
            "definition_version": job.definition_version,
            "replayed": True,
            "conclusion": conclusion,
            "steps": [record.as_dict() for record in job.steps],
        },
    )


def _require_job(runtime, request: Request):
    job_id = request.query_one("id")
    if not job_id:
        raise BadRequest("id is required")
    return runtime.jobs.jobs.require(job_id)


def _job_id(payload: dict, request: Request) -> str:
    job_id = payload.get("job_id") or request.query_one("id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise BadRequest("job_id is required")
    return job_id.strip()
