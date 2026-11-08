"""Long-running commands run as operations (spec section 6.2).

A command returns an Operation right away. Progress and the final result are published
as ``operation`` events and can also be fetched by ID.
"""

import asyncio
import logging
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from picnicplace.events import EventBus
from picnicplace.machine.errors import MachineError
from picnicplace.schemas import Operation, OperationEvent, OperationStatus

log = logging.getLogger(__name__)


class OperationManager:
    def __init__(self, bus: EventBus, history: int = 200) -> None:
        self._bus = bus
        self._history = history
        self._operations: OrderedDict[str, Operation] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, kind: str, work: Callable[[], Awaitable[str | None]]) -> Operation:
        op = Operation(
            id=uuid.uuid4().hex, kind=kind, status="running", started_at=datetime.now(UTC)
        )
        self._store(op)
        task = asyncio.ensure_future(self._run(op.id, work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return op

    def get(self, op_id: str) -> Operation | None:
        return self._operations.get(op_id)

    async def shutdown(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(self, op_id: str, work: Callable[[], Awaitable[str | None]]) -> None:
        status: OperationStatus = "failed"
        error: str | None = None
        message: str | None = None
        try:
            message = await work()
            status = "succeeded"
        except asyncio.CancelledError:
            status, error = "cancelled", "cancelled"
            raise
        except MachineError as e:
            error = str(e)
        except Exception as e:
            log.exception("operation %s failed", op_id)
            error = f"internal error: {e}"
        finally:
            op = self._operations.get(op_id)
            if op is not None:
                self._store(
                    op.model_copy(
                        update={
                            "status": status,
                            "error": error,
                            "message": message,
                            "finished_at": datetime.now(UTC),
                        }
                    )
                )

    def _store(self, op: Operation) -> None:
        self._operations[op.id] = op
        self._operations.move_to_end(op.id)
        while len(self._operations) > self._history:
            oldest_id, oldest = next(iter(self._operations.items()))
            if oldest.status == "running":
                break
            del self._operations[oldest_id]
        self._bus.publish(OperationEvent(operation=op))
