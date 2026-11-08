import tomllib
from importlib import resources
from typing import Any

import pytest
from pydantic import ValidationError

from picnicplace.machine.profile import (
    MachineProfile,
    builtin_profiles,
    load_profile,
    nozzle_height,
)


def lumenpnp_data() -> dict[str, Any]:
    text = (resources.files("picnicplace") / "profiles" / "lumenpnp-v4.toml").read_text()
    return tomllib.loads(text)


@pytest.mark.parametrize("profile_id", builtin_profiles())
def test_builtin_profiles_load(profile_id: str) -> None:
    profile = load_profile(profile_id)
    assert profile.id == profile_id


def test_reversed_range_threshold() -> None:
    # LumenPnP vacuum counts grow more negative with more vacuum.
    profile = load_profile("lumenpnp-v4")
    vac = next(i for i in profile.inputs if i.id == "vac_n1")
    assert not vac.past_threshold(-1_500_000)  # open nozzle
    assert vac.past_threshold(-2_000_000)  # part held


def test_lumenpnp_shared_z() -> None:
    profile = load_profile("lumenpnp-v4")
    n1, n2 = profile.nozzles
    assert n1.z_axis == n2.z_axis == "Z"
    # Lowering Z lowers N1 while N2 stays parked at the top, and the reverse.
    assert (nozzle_height(n1, -10), nozzle_height(n2, -10)) == (-10, 0)
    assert (nozzle_height(n1, 10), nozzle_height(n2, 10)) == (0, -10)


def test_references_are_checked() -> None:
    data = lumenpnp_data()
    data["nozzles"][0]["vacuum_input"] = "missing"
    with pytest.raises(ValidationError, match="unknown input 'missing'"):
        MachineProfile.model_validate(data)

    data = lumenpnp_data()
    data["nozzles"][1]["rotation_axis"] = "C"
    with pytest.raises(ValidationError, match="unknown axis 'C'"):
        MachineProfile.model_validate(data)


def test_duplicate_ids_rejected() -> None:
    data = lumenpnp_data()
    data["outputs"].append({"id": "vac_n1", "label": "Clash", "role": "other"})
    with pytest.raises(ValidationError, match="duplicate input or output id 'vac_n1'"):
        MachineProfile.model_validate(data)


def test_safe_z_input_must_be_digital() -> None:
    data = lumenpnp_data()
    data["safe_z_input"] = "vac_n1"
    with pytest.raises(ValidationError, match="safe_z_input"):
        MachineProfile.model_validate(data)


def test_placement_area_is_where_every_nozzle_reaches() -> None:
    profile = load_profile("lumenpnp-v4")
    reach = profile.nozzle_reach
    area = profile.placement_area
    assert area is not None
    # The nozzles are about 47 mm apart: each reaches farther on its own side, and boards
    # go only where both can.
    assert area.x_min == pytest.approx(max(r.x_min for r in reach.values()))
    assert area.x_max == pytest.approx(min(r.x_max for r in reach.values()))
    assert reach["N1"].x_min < area.x_min < reach["N2"].x_min + 0.001
    # Within the staging plates, and the keep-outs sit inside it.
    assert area.y_max == pytest.approx(213.071)
    for zone in profile.keep_outs:
        assert area.x_min < zone.area.x_min and zone.area.x_max < area.x_max
