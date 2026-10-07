"""Coordinate independent prepared executions and shared network concurrency."""

from __future__ import annotations

from dataclasses import dataclass

from ..llm.concurrent import ConcurrentCalls
from ..llm.managed import CallService
from ..runtime import get_execution
from .configuration import stored_configuration


@dataclass
class ExtractionTask:
    """One existing execution; its fixed configuration remains authoritative."""

    space: object
    execution_id: str
    service: object = None


def resume_extractions(tasks, *, concurrency=None, registry=None, on_report=None):
    """Advance papers as replies arrive; on_report may return same-space branches."""
    from .entry import _resume_extraction_once

    tasks = list(tasks)
    if not tasks:
        raise ValueError("provide at least one extraction task")
    identities = [(str(t.space.root), t.execution_id) for t in tasks]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate execution in extraction batch")
    limits = []
    for task in tasks:
        handle = get_execution(task.space, task.execution_id)
        if handle.input_snapshot.options.get("extraction_scope") not in {
            "roster",
            "full",
        }:
            raise ValueError("batch extraction requires fixed prepared PDF executions")
        config = stored_configuration(handle.input_snapshot)
        limits.append(min(config.parallelism, config.runtime.concurrency))
        if task.service is None:
            task.service = CallService(config.runtime)
        if (
            not isinstance(task.service, CallService)
            or task.service.dispatcher is not None
        ):
            raise ValueError(
                "provide independent model services not already in a batch"
            )
    limit = min(limits) if concurrency is None else concurrency
    if type(limit) is not int or limit < 1 or limit > min(limits):
        raise ValueError("batch concurrency exceeds a frozen execution limit")
    dispatcher = ConcurrentCalls(limit)
    locks = {}
    results = [None] * len(tasks)
    remaining = set(range(len(tasks)))
    ready = set(remaining)
    try:
        for root in sorted({str(t.space.root) for t in tasks}):
            task = next(t for t in tasks if str(t.space.root) == root)
            lock = task.space.lock()
            lock.acquire()
            locks[root] = lock
        for task in tasks:
            task.service.dispatcher = dispatcher
        while remaining:
            added = set()
            for index in sorted(ready & remaining):
                task = tasks[index]
                report = _resume_extraction_once(
                    task.space,
                    task.execution_id,
                    service=task.service,
                    registry=registry,
                )
                if report.outcome != "running":
                    results[index] = report
                    remaining.remove(index)
                    if on_report is not None:
                        for branch in on_report(index, report) or []:
                            identity = (str(branch.space.root), branch.execution_id)
                            if identity in identities or identity[0] not in locks:
                                raise ValueError(
                                    "branches require a new execution in an owned space"
                                )
                            handle = get_execution(branch.space, branch.execution_id)
                            config = stored_configuration(handle.input_snapshot)
                            if handle.input_snapshot.options.get(
                                "extraction_scope"
                            ) != "full" or limit > min(
                                config.parallelism, config.runtime.concurrency
                            ):
                                raise ValueError(
                                    "branch is incompatible with the fixed batch"
                                )
                            if branch.service is None:
                                branch.service = CallService(config.runtime)
                            if (
                                not isinstance(branch.service, CallService)
                                or branch.service.dispatcher is not None
                            ):
                                raise ValueError("branch service is already in use")
                            branch.service.dispatcher = dispatcher
                            added.add(len(tasks))
                            remaining.add(len(tasks))
                            tasks.append(branch)
                            identities.append(identity)
                            results.append(None)
            if not remaining:
                break
            completed = dispatcher.collect(wait_for_result=not added)
            ready = added | {
                i
                for i in remaining
                if any(key[:2] == identities[i] for key in completed)
            }
            for root in list(locks):
                if not any(identities[i][0] == root for i in remaining) and not any(
                    key[0] == root for key in dispatcher.jobs
                ):
                    locks.pop(root).release()
            if not dispatcher.jobs and not ready:
                raise RuntimeError(
                    "a running execution has no owned pending network call"
                )
        return results
    finally:
        try:
            dispatcher.close()
        finally:
            for task in tasks:
                task.service.dispatcher = None
            for lock in reversed(list(locks.values())):
                lock.release()
