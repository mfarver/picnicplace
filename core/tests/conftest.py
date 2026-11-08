import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from picnicplace.api import create_app
from picnicplace.config import Config

TEST_PROFILE = Path(__file__).parent / "profiles" / "test-machine.toml"


def fast_config(**overrides: Any) -> Config:
    """A config for the test machine, with a fast simulator and short presence timers."""
    data: dict[str, Any] = {
        "machine": {"sim_time_scale": 100.0, "profile": str(TEST_PROFILE)},
        "presence": {"grace_period_s": 0.2, "heartbeat_timeout_s": 5.0},
        "data": {"dir": tempfile.mkdtemp(prefix="picnicplace-test-")},
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


@dataclass
class Session:
    """A connected test client: an open event socket plus REST calls that carry its ID."""

    client: TestClient
    ws: WebSocketTestSession
    client_id: str
    hello: dict[str, Any]

    @property
    def headers(self) -> dict[str, str]:
        return {"X-PicnicPlace-Client": self.client_id}

    def post(self, url: str, json: Any = None) -> httpx.Response:
        return self.client.post(url, json=json, headers=self.headers)

    def put(self, url: str, json: Any = None) -> httpx.Response:
        return self.client.put(url, json=json, headers=self.headers)

    def delete(self, url: str) -> httpx.Response:
        return self.client.delete(url, headers=self.headers)

    def state(self) -> dict[str, Any]:
        return self.client.get("/api/v1/machine").json()

    def wait(self, response: httpx.Response) -> dict[str, Any]:
        """Wait for the operation a 202 response started, and return it finished."""
        assert response.status_code == 202, response.json()
        return receive_until(self.ws, operation_done(response.json()["id"]))["operation"]

    def home(self) -> None:
        assert self.wait(self.post("/api/v1/machine/home"))["status"] == "succeeded"

    def move(self, **position: float) -> dict[str, Any]:
        return self.wait(self.post("/api/v1/machine/move", {"position": position}))


@contextmanager
def connect(client: TestClient, name: str = "test") -> Iterator[Session]:
    with client.websocket_connect(f"/api/v1/events?name={name}") as ws:
        hello = ws.receive_json()
        yield Session(client, ws, hello["client_id"], hello)
