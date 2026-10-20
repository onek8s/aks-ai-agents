"""Pull request metadata and the structure every agent pull request must have."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# A design citation: a page of the design repository's design/ or requirements/ folder, or a section number.
DESIGN_CITATION = re.compile(r"((?:design|requirements)/\d+(?:\.\d+)?-[\w-]+\.md|[Ss]ection \d+(?:\.\d+)?)")


@dataclass
class Review:
    user: str
    state: str  # APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED
    commit_id: str
    teams: list[str] = field(default_factory=list)  # gate team keys (platform, security) the user belongs to

    @property
    def is_bot(self) -> bool:
        return self.user.endswith("[bot]")


@dataclass
class Commit:
    sha: str
    message: str


@dataclass
class PullRequest:
    repository: str
    number: int
    author: str
    head_ref: str
    head_sha: str
    body: str = ""
    commits: list[Commit] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    reviewer_agent_conclusion: str | None = None  # conclusion of the reviewer agent check on the head commit
    reviewer_agent_flagged: bool = False  # the reviewer agent asked for a human look
    incident_fix_by_human: bool = False
    merges_today: dict[str, int] = field(default_factory=dict)  # per environment, by this pull request's agent
    open_pull_requests: int = 0  # open pull requests of this pull request's agent

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PullRequest:
        return cls(
            repository=data["repository"],
            number=int(data["number"]),
            author=data["author"],
            head_ref=data["head_ref"],
            head_sha=data["head_sha"],
            body=data.get("body") or "",
            commits=[Commit(**c) for c in data.get("commits", [])],
            reviews=[Review(**r) for r in data.get("reviews", [])],
            reviewer_agent_conclusion=data.get("reviewer_agent_conclusion"),
            reviewer_agent_flagged=bool(data.get("reviewer_agent_flagged", False)),
            incident_fix_by_human=bool(data.get("incident_fix_by_human", False)),
            merges_today=data.get("merges_today", {}),
            open_pull_requests=int(data.get("open_pull_requests", 0)),
        )


AGENT_BRANCH_RE = re.compile(r"^agent/(?P<agent>[a-z0-9-]+)/.+")


def branch_agent(head_ref: str) -> str | None:
    match = AGENT_BRANCH_RE.match(head_ref)
    return match.group("agent") if match else None


def parse_trailers(message: str) -> dict[str, list[str]]:
    """Git trailers of the last paragraph of a commit message."""
    paragraphs = [p for p in re.split(r"\n\s*\n", message.strip()) if p.strip()]
    if len(paragraphs) < 2:
        return {}
    trailers: dict[str, list[str]] = {}
    for line in paragraphs[-1].splitlines():
        match = re.match(r"^([A-Za-z0-9-]+):\s*(.+)$", line.strip())
        if not match:
            return {}
        trailers.setdefault(match.group(1), []).append(match.group(2).strip())
    return trailers


def body_sections(body: str) -> dict[str, str]:
    """Markdown sections of a pull request body, keyed by lower-case heading."""
    sections: dict[str, str] = {}
    current = None
    for line in body.splitlines():
        match = re.match(r"^#{1,4}\s+(.+?)\s*#*\s*$", line)
        if match:
            current = match.group(1).strip().lower()
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    return sections


def check_structure(
    pr: PullRequest, agent: str, required_sections: list[str], required_trailers: list[str]
) -> list[str]:
    """Problems with the structure of an agent pull request; empty when it is complete."""
    problems = []
    sections = body_sections(pr.body)
    for name in required_sections:
        content = sections.get(name.lower(), "").strip()
        if not content:
            problems.append(f"pull request body has no '{name}' section")
    design = sections.get("design section", "")
    if design.strip() and not DESIGN_CITATION.search(design):
        problems.append("'Design section' does not cite a design document section")
    if not pr.commits:
        problems.append("pull request has no commits")
    for commit in pr.commits:
        trailers = parse_trailers(commit.message)
        for name in required_trailers:
            if not trailers.get(name):
                problems.append(f"commit {commit.sha[:10]} has no '{name}' trailer")
        if trailers.get("Agent") and trailers["Agent"] != [agent]:
            problems.append(f"commit {commit.sha[:10]} is from agent {trailers['Agent']}, the branch is {agent}'s")
    return problems
