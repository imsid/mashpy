"""The policy document and its id."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mash.browser.sites import check_sites

DEFAULT_WAIT_MAX_SECONDS = 86400
MIN_WAIT_MAX_SECONDS = 60

_POLICY_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,255}$")


def validate_policy_id(policy_id: Any) -> str:
    if not isinstance(policy_id, str) or not _POLICY_ID_RE.match(policy_id):
        raise ValueError(
            "policy_id must be 1-255 characters of letters, digits, '.', '_', ':', '@' or '-'"
        )
    return policy_id


class PolicyNotFoundError(LookupError):
    """No policy has this id."""

    def __init__(self, policy_id: str) -> None:
        super().__init__(f"policy '{policy_id}' not found")
        self.policy_id = policy_id


class Policy(BaseModel):
    """What requests under a policy may use, and how long they may wait.

    ``sandbox``: false removes the sandbox tools from every agent.
    ``browser_sites``: None leaves each agent its own sites; a list narrows
    them to the sites both allow, and ``[]`` removes the browser tools.
    ``wait_max_seconds``: caps every interaction's deadline; None for no cap.
    """

    model_config = ConfigDict(extra="forbid")

    sandbox: bool = True
    browser_sites: Optional[list[str]] = None
    wait_max_seconds: Optional[int] = Field(
        default=DEFAULT_WAIT_MAX_SECONDS, ge=MIN_WAIT_MAX_SECONDS
    )

    @field_validator("browser_sites")
    @classmethod
    def _check_sites(cls, sites: Optional[list[str]]) -> Optional[list[str]]:
        return None if sites is None else check_sites(sites)


@dataclass(frozen=True)
class PolicyRecord:
    """A stored policy with its timestamps."""

    policy_id: str
    policy: Policy
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            **self.policy.model_dump(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
