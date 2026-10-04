"""Durable records for the job step ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

DONE = "done"
FAILED = "failed"
RUNNING = "running"


def timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class StepRecord:
    """The settled (or failed) outcome of one step in a job."""

    name: str
    status: str
    result: dict[str, object] = field(default_factory=dict)
    error: str | None = None
    attempt: int = 1
    at: str = ""

    @property
    def settled(self) -> bool:
        return self.status == DONE

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "result": self.result,
            "attempt": self.attempt,
            "at": self.at,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> "StepRecord | None":
        if not isinstance(payload, dict):
            return None
        name = payload.get("name")
        status = payload.get("status")
        if not isinstance(name, str) or not name:
            return None
        if status not in (DONE, FAILED):
            return None
        result = payload.get("result")
        error = payload.get("error")
        attempt = payload.get("attempt")
        at = payload.get("at")
        return cls(
            name=name,
            status=status,
            result=dict(result) if isinstance(result, dict) else {},
            error=error if isinstance(error, str) else None,
            attempt=attempt if isinstance(attempt, int) and attempt > 0 else 1,
            at=at if isinstance(at, str) else "",
        )


@dataclass(frozen=True)
class JobRecord:
    """One job: its request, run-stable context and every settled step."""

    job_id: str
    kind: str
    status: str
    request: dict[str, object] = field(default_factory=dict)
    context: dict[str, object] = field(default_factory=dict)
    steps: tuple[StepRecord, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    def step(self, name: str) -> StepRecord | None:
        for step in self.steps:
            if step.name == name:
                return step
        return None

    def settled(self, name: str) -> StepRecord | None:
        step = self.step(name)
        if step is not None and step.settled:
            return step
        return None

    @property
    def settled_count(self) -> int:
        return sum(1 for step in self.steps if step.settled)

    def summary(self) -> dict[str, object]:
        return {
            "job": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "steps": len(self.steps),
            "settled": self.settled_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "job": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "request": self.request,
            "context": self.context,
            "steps": [step.as_dict() for step in self.steps],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "JobRecord | None":
        if not isinstance(payload, dict):
            return None
        job_id = payload.get("job")
        kind = payload.get("kind")
        status = payload.get("status")
        if not all(isinstance(item, str) and item for item in (job_id, kind, status)):
            return None
        request = payload.get("request")
        context = payload.get("context")
        raw_steps = payload.get("steps")
        steps: list[StepRecord] = []
        if isinstance(raw_steps, list):
            for item in raw_steps:
                step = StepRecord.from_dict(item)
                if step is not None:
                    steps.append(step)
        created_at = payload.get("created_at")
        updated_at = payload.get("updated_at")
        return cls(
            job_id=job_id,
            kind=kind,
            status=status,
            request=dict(request) if isinstance(request, dict) else {},
            context=dict(context) if isinstance(context, dict) else {},
            steps=tuple(steps),
            created_at=created_at if isinstance(created_at, str) else "",
            updated_at=updated_at if isinstance(updated_at, str) else "",
        )
