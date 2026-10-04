"""Job ledger query and replay endpoints."""

from __future__ import annotations

from collections.abc import Callable

from portpower.console.flows.connect import replay_connect
from portpower.console.flows.disconnect import replay_disconnect
from portpower.console.http import BadRequest, Request, Response
from portpower.console.validation import require_text

# Replay rebuilds the read-side conclusion of a finished job; the builders
# only read the runtime and the settled ledger, never write to either.
REPLAYERS: dict[str, Callable] = {
    "connect": replay_connect,
    "disconnect": replay_disconnect,
}


def jobs(runtime, request: Request) -> Response:
    records = runtime.jobs.all()
    return Response.json(
        200,
        {
            "count": len(records),
            "jobs": [record.summary() for record in records],
        },
    )


def job_detail(runtime, request: Request) -> Response:
    job_id = request.query_one("id")
    if not job_id:
        raise BadRequest("id query parameter is required")
    record = runtime.jobs.require(job_id)
    return Response.json(200, record.as_dict())


def job_replay(runtime, request: Request) -> Response:
    payload = request.json_object()
    job_id = require_text(payload, "job")
    record = runtime.jobs.require(job_id)
    replayer = REPLAYERS.get(record.kind)
    if replayer is None:
        raise BadRequest(f"job kind {record.kind!r} cannot be replayed")
    result = replayer(runtime, record)
    return Response.json(
        200,
        {
            "job": record.job_id,
            "kind": record.kind,
            "replayed": True,
            "result": result.as_dict(),
        },
    )
