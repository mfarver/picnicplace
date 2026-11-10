"""The local profile: what is true of this one machine, laid over its built-in profile.

A built-in profile describes a machine model's design. Each real machine differs a
little, and its owner puts things where suits them: the discard tray, the place the head
waits out of the way. The core keeps those in ``local-profile.json`` in its data
directory, as a partial profile merged over the built-in one (and over any
``[[machine.locations]]`` in the config) the same way ``extends`` merges profiles.

The Studio writes it: the discard and park locations, set by centering the top camera
on them, take effect at once; findings from the bring-up checklist (which way the axes
point, measured keep-outs) take effect when the core next starts, since the machine
service works from the profile it started with. The owner can also edit the file by
hand, with the core stopped, to override anything a profile holds.
"""

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from picnicplace.machine.profile import MachineProfile, merge_profile_data
from picnicplace.storage import write_atomic

LIVE_KEYS = frozenset({"locations"})
"""Parts of the local profile that take effect without restarting the core."""


class LocalProfileFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["picnicplace-local-profile"] = "picnicplace-local-profile"
    version: Literal[1] = 1
    profile: dict[str, Any] = Field(default_factory=dict[str, Any])


class LocalProfileStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def get(self) -> dict[str, Any]:
        if self.path is None or not self.path.is_file():
            return {}
        try:
            data = LocalProfileFile.model_validate_json(self.path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError(f"the local profile {self.path} is not valid: {e}") from e
        return data.profile

    def put(self, profile: dict[str, Any]) -> None:
        if self.path is None:
            return
        with self._lock:
            body = LocalProfileFile(profile=profile).model_dump(mode="json")
            write_atomic(self.path, json.dumps(body, indent=2) + "\n")


def without_location(profile: dict[str, Any], location_id: str) -> dict[str, Any]:
    """The local profile with one location taken out."""
    locations = [
        loc
        for loc in cast(list[dict[str, Any]], profile.get("locations", []))
        if loc.get("id") != location_id
    ]
    rest = {k: v for k, v in profile.items() if k != "locations"}
    return {**rest, "locations": locations} if locations else rest


LocationKind = Literal["discard", "park"]

LABELS: dict[LocationKind, str] = {"discard": "Discard", "park": "Park"}


class LocalProfileState(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    path: str | None = Field(description="Where the local profile is kept.")
    profile: dict[str, Any] = Field(
        description="What it holds: a partial machine profile, laid over the built-in one."
    )
    restart_needed: bool = Field(
        description="It has changes that take effect when the core next starts."
    )


class LocalProfile:
    """The local profile, and keeping the running machine's profile in step with it."""

    def __init__(
        self,
        store: LocalProfileStore,
        with_local: Callable[[dict[str, Any]], MachineProfile],
        live: MachineProfile,
        applied: dict[str, Any],
    ) -> None:
        self._store = store
        self._with_local = with_local
        self._live = live
        self._applied = applied

    def state(self) -> LocalProfileState:
        stored = self._store.get()
        return LocalProfileState(
            path=str(self._store.path) if self._store.path else None,
            profile=stored,
            restart_needed=_held(stored) != _held(self._applied),
        )

    def set_location(self, kind: LocationKind, x: float, y: float, label: str | None) -> None:
        """Put this machine's discard or park location here, from now on."""
        local = self._store.get()
        mine = [
            loc
            for loc in cast(list[dict[str, Any]], local.get("locations", []))
            if loc.get("kind") == kind
        ]
        # Take the place of the profile's (or config's) own, if it has one.
        theirs = next((loc for loc in self._live.locations if loc.kind == kind), None)
        location_id = mine[0]["id"] if mine else theirs.id if theirs else kind
        entry = {
            "id": location_id,
            "label": label or (theirs.label if theirs else LABELS[kind]),
            "kind": kind,
            "position": {"x": round(x, 3), "y": round(y, 3)},
        }
        updated = without_location(local, location_id)
        updated["locations"] = [*updated.get("locations", []), entry]
        self._save(updated)

    def reset_location(self, kind: LocationKind) -> None:
        """Forget this machine's own discard or park location: the profile's (or the
        config's), if any, applies again."""
        local = self._store.get()
        updated = local
        for loc in cast(list[dict[str, Any]], local.get("locations", [])):
            if loc.get("kind") == kind:
                updated = without_location(updated, str(loc["id"]))
        self._save(updated)

    def update(self, changes: dict[str, Any]) -> None:
        """Lay changes over the local profile (merged the way profiles merge). Anything
        but locations takes effect when the core next starts."""
        self._save(merge_profile_data(self._store.get(), changes))

    def _save(self, local: dict[str, Any]) -> None:
        profile = self._with_local(local)  # raises if the result is not a valid profile
        self._store.put(local)
        # Locations take effect at once; the rest waits for a restart.
        self._live.locations = profile.locations
        self._applied = {**_held(self._applied), "locations": local.get("locations", [])}


def _held(profile: dict[str, Any]) -> dict[str, Any]:
    """The parts of a local profile that wait for a restart."""
    return {k: v for k, v in profile.items() if k not in LIVE_KEYS}
