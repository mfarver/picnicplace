"""Authentication and permissions (spec section 6.3).

1.0 uses one shared secret. Requests still resolve to a Principal with a permission set,
and every endpoint declares the permission it needs, so local-only motion and user roles
can be added later without changing the API.
"""

import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Permission(StrEnum):
    VIEW = "view"
    MOTION = "motion"
    JOB = "job"
    CONFIG = "config"


ALL_PERMISSIONS = frozenset(Permission)


@dataclass(frozen=True)
class Principal:
    id: str
    kind: Literal["shared-secret", "open"]
    permissions: frozenset[Permission]


class Authenticator:
    def __init__(self, token: str | None) -> None:
        self._token = token

    @property
    def required(self) -> bool:
        return self._token is not None

    def authenticate(self, presented: str | None) -> Principal | None:
        """Return the principal for a presented token, or None if it is not accepted."""
        if self._token is None:
            # No secret configured: only possible when bound to loopback (see Config).
            return Principal(id="local", kind="open", permissions=ALL_PERMISSIONS)
        if presented is not None and secrets.compare_digest(
            presented.encode(), self._token.encode()
        ):
            return Principal(id="shared", kind="shared-secret", permissions=ALL_PERMISSIONS)
        return None
