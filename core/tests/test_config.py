from pathlib import Path

import pytest
from pydantic import ValidationError

from picnicplace.config import Config, load_config

EXAMPLE = Path(__file__).parents[1] / "picnicd.example.toml"


def test_defaults_are_loopback_without_token() -> None:
    config = Config()
    assert config.server.is_loopback
    assert config.server.token is None


def test_network_bind_requires_token() -> None:
    with pytest.raises(ValidationError, match=r"server\.token"):
        Config.model_validate({"server": {"host": "0.0.0.0"}})
    Config.model_validate({"server": {"host": "0.0.0.0", "token": "x" * 16}})


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"server": {"prot": 1}})


def test_axis_validation() -> None:
    with pytest.raises(ValidationError, match="min must be less than max"):
        Config.model_validate(
            {"machine": {"axes": [{"name": "X", "min": 5, "max": 0, "max_speed": 1}]}}
        )
    axis = {"name": "X", "min": 0, "max": 1, "max_speed": 1}
    with pytest.raises(ValidationError, match="unique"):
        Config.model_validate({"machine": {"axes": [axis, axis]}})


def test_example_config_loads() -> None:
    config = load_config(EXAMPLE)
    assert config.machine.driver == "simulator"
