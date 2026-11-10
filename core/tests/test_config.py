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


def test_unknown_profile_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown machine profile"):
        Config.model_validate({"machine": {"profile": "no-such-machine"}})


def test_profile_path_is_relative_to_config(tmp_path: Path) -> None:
    (tmp_path / "profiles").mkdir()
    builtin = Path(__file__).parents[1] / "src/picnicplace/profiles/lumenpnp-v4.1.toml"
    (tmp_path / "profiles/mine.toml").write_text(
        builtin.read_text().replace('name = "LumenPnP v4.1"', 'name = "My machine"')
    )
    (tmp_path / "picnicd.toml").write_text('[machine]\nprofile = "profiles/mine.toml"\n')
    config = load_config(tmp_path / "picnicd.toml")
    assert config.machine.machine_profile.name == "My machine"


def test_example_config_loads() -> None:
    config = load_config(EXAMPLE)
    assert config.machine.driver == "simulator"
    assert config.machine.machine_profile.id == "lumenpnp-v4.1"


def test_machine_locations_are_added_to_the_profile() -> None:
    config = Config.model_validate(
        {
            "machine": {
                "profile": "lumenpnp-v4.1",
                "locations": [
                    {
                        "id": "discard",
                        "label": "Discard",
                        "kind": "discard",
                        "position": {"x": 150, "y": 110},
                    },
                ],
            }
        }
    )
    profile = config.machine.machine_profile
    assert [loc.id for loc in profile.locations][-1] == "discard"
    assert profile.marlin is not None  # driver settings survive the merge
    with pytest.raises(ValidationError, match="duplicate location id"):
        Config.model_validate(
            {
                "machine": {
                    "profile": "lumenpnp-v4.1",
                    "locations": [
                        {
                            "id": "homing_fiducial",
                            "label": "X",
                            "kind": "discard",
                            "position": {"x": 0, "y": 0},
                        },
                    ],
                }
            }
        )


@pytest.mark.parametrize(
    ("argv", "warned"),
    [([], True), (["--driver", "simulator"], False), (["--driver", "fake-marlin"], False)],
)
def test_running_the_simulator_without_a_config_says_so(
    argv: list[str],
    warned: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Started without -c, picnicd quietly ran the simulator, and someone at a real
    machine took a while to notice. Now it says so (unless a driver was chosen)."""
    import picnicplace.cli as cli

    monkeypatch.setattr(cli, "create_app", lambda config: config)  # pyright: ignore[reportUnknownLambdaType]
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: None)  # pyright: ignore[reportUnknownLambdaType]
    cli.main(argv)
    err = capsys.readouterr().err
    assert ("SIMULATOR, not a real machine" in err) is warned
