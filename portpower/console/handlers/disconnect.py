"""Disconnect endpoint."""

from __future__ import annotations

from portpower.console.flows.disconnect import DisconnectRequest, run_disconnect
from portpower.console.http import Request, Response


def disconnect(runtime, request: Request) -> Response:
    payload = request.json_object()
    result, job = run_disconnect(runtime, DisconnectRequest.from_payload(payload))
    body = result.as_dict()
    body["job_state"] = job.state.value
    return Response.json(200, body)
