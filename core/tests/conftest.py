import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from picnicplace.api import create_app
from picnicplace.config import Config


def fast_config(**overrides: Any) -> Config:
    """A config with a fast simulator and short presence timers."""
    data: dict[str, Any] = {
        "machine": {"sim_time_scale": 100.0},
        "presence": {"grace_period_s": 0.2, "heartbeat_timeout_s": 5.0},
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return Config.model_validate(data)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(fast_config())) as c:
        yield c


def receive_until(
    ws: WebSocketTestSession, predicate: Callable[[dict[str, Any]], bool], limit: int = 500
) -> dict[str, Any]:
    """Read events until one matches ``predicate``."""
    for _ in range(limit):
        msg: dict[str, Any] = ws.receive_json()
        if predicate(msg):
            return msg
    raise AssertionError("expected event not received")


def operation_done(op_id: str) -> Callable[[dict[str, Any]], bool]:
    def check(msg: dict[str, Any]) -> bool:
        return (
            msg["type"] == "operation"
            and msg["operation"]["id"] == op_id
            and msg["operation"]["status"] != "running"
        )

    return check


def wait_for(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")
