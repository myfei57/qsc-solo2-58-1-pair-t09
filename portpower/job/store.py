"""Durable ledger of jobs and their step checkpoints, backed by the record store."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from portpower.store.kvstore import RecordStore

PREFIX = "job."


def job_key(job_id: str) -> str:
    return PREFIX + job_id


class StepState(str, Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class JobState(str, Enum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True)
class StepRecord:
    """Ledger row for one step.

    ``result`` is the checkpoint payload written once the step's action has
    finished. Once recorded alongside ``state == done`` it is immutable and is
    the only data a replay projection is allowed to read.
    """

    key: str
    state: StepState = StepState.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    attempts: int = 0
    finished_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "state": self.state.value,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "StepRecord | None":
        if not isinstance(payload, dict):
            return None
        key = payload.get("key")
        if not isinstance(key, str) or not key:
            return None
        try:
            state = StepState(payload.get("state", StepState.PENDING.value))
        except ValueError:
            state = StepState.PENDING
        result = payload.get("result")
        if result is not None and not isinstance(result, dict):
            result = None
        error = payload.get("error")
        if error is not None and not isinstance(error, str):
            error = str(error)
        attempts = payload.get("attempts", 0)
        if not isinstance(attempts, int) or isinstance(attempts, bool):
            attempts = 0
        finished_at = payload.get("finished_at")
        if finished_at is not None and not isinstance(finished_at, str):
            finished_at = None
        return cls(
            key=key,
            state=state,
            result=result,
            error=error,
            attempts=max(0, attempts),
            finished_at=finished_at,
        )


@dataclass(frozen=True)
class Job:
    job_id: str
    kind: str
    definition_version: str
    request: dict[str, Any]
    steps: tuple[StepRecord, ...] = field(default_factory=tuple)
    state: JobState = JobState.RUNNING
    opened_at: str = ""
    finished_at: str | None = None
    error: str | None = None

    def step(self, key: str) -> StepRecord | None:
        for record in self.steps:
            if record.key == key:
                return record
        return None

    def result_of(self, key: str) -> dict[str, Any] | None:
        record = self.step(key)
        return record.result if record is not None else None

    def is_done(self, key: str) -> bool:
        record = self.step(key)
        return record is not None and record.state is StepState.DONE

    @property
    def completed_steps(self) -> int:
        return sum(1 for record in self.steps if record.state is StepState.DONE)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "definition_version": self.definition_version,
            "request": self.request,
            "state": self.state.value,
            "opened_at": self.opened_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "completed_steps": self.completed_steps,
            "total_steps": len(self.steps),
            "steps": [record.as_dict() for record in self.steps],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "Job | None":
        if not isinstance(payload, dict):
            return None
        job_id = payload.get("job_id")
        kind = payload.get("kind")
        if not isinstance(job_id, str) or not job_id:
            return None
        if not isinstance(kind, str) or not kind:
            return None
        raw_steps = payload.get("steps", [])
        steps: list[StepRecord] = []
        if isinstance(raw_steps, list):
            for item in raw_steps:
                record = StepRecord.from_dict(item)
                if record is not None:
                    steps.append(record)
        try:
            state = JobState(payload.get("state", JobState.RUNNING.value))
        except ValueError:
            state = JobState.RUNNING
        request = payload.get("request", {})
        if not isinstance(request, dict):
            request = {}
        version = payload.get("definition_version", "")
        if not isinstance(version, str):
            version = str(version)
        opened_at = payload.get("opened_at", "")
        if not isinstance(opened_at, str):
            opened_at = ""
        finished_at = payload.get("finished_at")
        if finished_at is not None and not isinstance(finished_at, str):
            finished_at = None
        error = payload.get("error")
        if error is not None and not isinstance(error, str):
            error = str(error)
        return cls(
            job_id=job_id,
            kind=kind,
            definition_version=version,
            request=request,
            steps=tuple(steps),
            state=state,
            opened_at=opened_at,
            finished_at=finished_at,
            error=error,
        )


def timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class JobStore:
    """Persists job documents and applies checkpoint transitions atomically."""

    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def open(
        self,
        job_id: str,
        kind: str,
        definition_version: str,
        request: dict[str, Any],
        step_keys: tuple[str, ...],
    ) -> Job:
        """Create the job document; fails if a job with that id already exists."""

        key = job_key(job_id)

        def mutate(document: Any) -> tuple[Any, Job]:
            existing = Job.from_dict(document)
            if existing is not None:
                raise JobAlreadyExists(f"job {job_id} already exists")
            job = Job(
                job_id=job_id,
                kind=kind,
                definition_version=definition_version,
                request=dict(request),
                steps=tuple(StepRecord(key=step_key) for step_key in step_keys),
                opened_at=timestamp(),
            )
            return job.as_dict(), job

        return self._store.update_json(key, mutate)

    def get(self, job_id: str) -> Job | None:
        return Job.from_dict(self._store.get_json(job_key(job_id)))

    def require(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise JobNotFound(f"no job {job_id}")
        return job

    def list(self) -> list[Job]:
        from portpower.store.query import keys_with_prefix

        jobs: list[Job] = []
        for key in keys_with_prefix(self._store, PREFIX):
            job = Job.from_dict(self._store.get_json(key))
            if job is not None:
                jobs.append(job)
        jobs.sort(key=lambda item: item.opened_at)
        return jobs

    def mark_step_done(self, job_id: str, step_key: str, result: dict[str, Any]) -> Job:
        """Checkpoint a finished step. A done step keeps its original result."""

        return self._mutate_steps(
            job_id,
            step_key,
            lambda record: StepRecord(
                key=record.key,
                state=StepState.DONE,
                result=dict(result),
                error=None,
                attempts=record.attempts + 1,
                finished_at=timestamp(),
            ),
        )

    def mark_step_failed(self, job_id: str, step_key: str, error: str) -> Job:
        return self._mutate_steps(
            job_id,
            step_key,
            lambda record: StepRecord(
                key=record.key,
                state=StepState.FAILED,
                result=record.result,
                error=error,
                attempts=record.attempts + 1,
                finished_at=record.finished_at,
            ),
            job_state=JobState.FAILED,
            job_error=error,
        )

    def complete(self, job_id: str) -> Job:
        def mutate(document: Any) -> tuple[Any, Job]:
            job = Job.from_dict(document)
            if job is None:
                raise JobNotFound(f"no job {job_id}")
            if all(record.state is StepState.DONE for record in job.steps):
                finished = replace(
                    job, state=JobState.COMPLETE, finished_at=timestamp(), error=None
                )
                return finished.as_dict(), finished
            return job.as_dict(), job

        return self._store.update_json(job_key(job_id), mutate)

    def resume(self, job_id: str) -> Job:
        """Move a failed job back to running so its unfinished step can retry."""

        def mutate(document: Any) -> tuple[Any, Job]:
            job = Job.from_dict(document)
            if job is None:
                raise JobNotFound(f"no job {job_id}")
            if job.state is JobState.COMPLETE:
                return None, job
            steps = tuple(
                StepRecord(
                    key=record.key,
                    state=(
                        StepState.PENDING
                        if record.state is StepState.FAILED
                        else record.state
                    ),
                    result=record.result,
                    error=None if record.state is StepState.FAILED else record.error,
                    attempts=record.attempts,
                    finished_at=record.finished_at,
                )
                for record in job.steps
            )
            resumed = replace(job, steps=steps, state=JobState.RUNNING, error=None)
            return resumed.as_dict(), resumed

        return self._store.update_json(job_key(job_id), mutate)

    def _mutate_steps(
        self,
        job_id: str,
        step_key: str,
        rewrite,
        job_state: JobState | None = None,
        job_error: str | None = None,
    ) -> Job:
        def mutate(document: Any) -> tuple[Any, Job]:
            job = Job.from_dict(document)
            if job is None:
                raise JobNotFound(f"no job {job_id}")
            steps: list[StepRecord] = []
            changed = False
            for record in job.steps:
                if record.key == step_key:
                    if record.state is StepState.DONE:
                        return None, job
                    steps.append(rewrite(record))
                    changed = True
                else:
                    steps.append(record)
            if not changed:
                raise UnknownStep(f"job {job_id} has no step {step_key!r}")
            values = dict(job.__dict__)
            values["steps"] = tuple(steps)
            if job_state is not None:
                values["state"] = job_state
                values["error"] = job_error
            updated = Job(**values)
            return updated.as_dict(), updated

        return self._store.update_json(job_key(job_id), mutate)


class JobNotFound(LookupError):
    """Raised when a job id is not present in the ledger."""


class JobAlreadyExists(RuntimeError):
    """Raised when opening a job whose ledger document already exists."""


class UnknownStep(RuntimeError):
    """Raised when a step key is missing from the frozen job definition."""
