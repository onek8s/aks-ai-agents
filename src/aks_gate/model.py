"""Normalised view of what a pull request changes, independent of the tool that produced the plan."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Tier(enum.IntEnum):
    """Risk tiers of design section 6.1, ordered so that ``max()`` gives the strictest."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3
    NOT_ALLOWED = 4

    @classmethod
    def parse(cls, value: str | Tier) -> Tier:
        if isinstance(value, Tier):
            return value
        return cls[value.strip().upper().replace("-", "_").replace(" ", "_")]

    def __str__(self) -> str:
        return self.name.lower().replace("_", "-")


class Action(enum.StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    REPLACE = "replace"
    NOOP = "no-op"


_MISSING = object()


def dig(obj: Any, path: str) -> Any:
    """Return ``obj[a][b]...`` for ``path`` ``"a.b"``; list indexes are numbers; ``None`` when absent."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part, _MISSING)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is _MISSING:
            return None
    return cur


@dataclass
class ResourceChange:
    """One Azure resource in an IaC plan (Terraform plan or Bicep what-if)."""

    address: str  # Terraform address or Azure resource ID
    type: str  # Azure resource type, e.g. Microsoft.KeyVault/vaults (``terraform:<type>`` if unmapped)
    name: str
    action: Action
    environment: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    source: str = "terraform"

    def attr(self, *paths: str, side: str = "after") -> Any:
        """First non-``None`` value of the given attribute paths on one side of the change."""
        obj = self.after if side == "after" else self.before
        for path in paths:
            value = dig(obj or {}, path)
            if value is not None:
                return value
        return None

    @property
    def is_destructive(self) -> bool:
        return self.action in (Action.DELETE, Action.REPLACE)


@dataclass
class ManifestChange:
    """One Kubernetes object in the rendered manifests of one cluster."""

    environment: str
    cluster: str  # cell or cluster short name, e.g. sl-az1, sf
    api_version: str
    kind: str
    namespace: str
    name: str
    action: Action
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None

    @property
    def key(self) -> str:
        return f"{self.environment}/{self.cluster}/{self.kind}/{self.namespace or '-'}/{self.name}"


@dataclass
class PlanSet:
    """Everything the gate evaluates for one pull request."""

    repository: str = ""
    changed_files: list[str] = field(default_factory=list)
    resources: list[ResourceChange] = field(default_factory=list)
    manifests: list[ManifestChange] = field(default_factory=list)
    # Rendered manifests of other environments, used e.g. to check that a prd digest already runs in acc.
    reference_manifests: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def effective_resources(self) -> list[ResourceChange]:
        return [r for r in self.resources if r.action != Action.NOOP]

    @property
    def effective_manifests(self) -> list[ManifestChange]:
        return [m for m in self.manifests if m.action != Action.NOOP]
