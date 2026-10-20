"""The version pipeline of the update policy (design section 3.5): deterministic target versions per cluster.

The upgrade agent never decides *when* a version is due – this module does, from the AKS release catalog and the
clusters' current versions; the agent turns each due step into a pull request with a compatibility report.

Rules implemented (``design/3.5-zero-downtime-cluster-upgrades.md#update-policy``):

* fast speed (``sl-az1``): Kubernetes minor N-1 (second latest GA minor); a release reaches dev 1 week after AKS
  released it, acc 1 week after dev, prd 1 week after acc;
* slow speed (``sl-az2``, ``sl-az3``): the minor ``sl-az1`` left (N-2), with 1-month steps; the steps are shortened
  when the minor's end of support would otherwise come first, so that prd is upgraded in time;
* a step is promoted only when the previous environment of the same speed already runs it;
* patches are not applied one by one: the newest due patch replaces older ones;
* node images follow the same timetable as patches;
* stateful (``sf``): only within the last 6 months of the minor's LTS – otherwise held; patches only when needed,
  which is a human decision (not computed here);
* emergency fast track: waiting times skipped, the order dev → acc → prd still kept.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass
from typing import Any

ENVS = ["dev", "acc", "prd"]
STEP = {"fast": dt.timedelta(weeks=1), "slow": dt.timedelta(days=30)}
LTS_WINDOW = dt.timedelta(days=183)


def speed_of(cluster: str) -> str:
    if re.search(r"-sf$", cluster):
        return "stateful"
    return "fast" if re.search(r"sl-az1$", cluster) else "slow"


def cell_of(cluster: str) -> str:
    match = re.search(r"(sl-az\d+|sf)$", cluster)
    return match.group(1) if match else cluster


def minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def vkey(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version))


def _date(value: Any) -> Any:
    """ISO date strings become dates; anything else (dates, flags such as ``lts: true``) is kept."""
    return dt.date.fromisoformat(value) if isinstance(value, str) else value


@dataclass
class Step:
    environment: str
    cluster: str
    speed: str
    kind: str  # kubernetes | node-image
    current: str
    target: str
    action: str  # upgrade | hold | none
    reason: str
    due_since: str | None = None


class Catalog:
    """AKS releases in the platform's region.

    ``minors``: ``{"1.34": {"ga": date, "end_of_support": date, "lts_end": date|None}}``;
    ``patches``: ``{"1.34.2": released}``; ``node_images``: ``{"AzureLinux3-202510.01.0": released}``.
    """

    def __init__(self, data: dict[str, Any]):
        self.minors = {m: {k: _date(v) for k, v in info.items()} for m, info in (data.get("minors") or {}).items()}
        self.patches = {v: _date(d) for v, d in (data.get("patches") or {}).items()}
        self.node_images = {v: _date(d) for v, d in (data.get("node_images") or {}).items()}

    def ga_minors(self, today: dt.date) -> list[str]:
        return sorted((m for m, i in self.minors.items() if i.get("ga") and i["ga"] <= today), key=vkey)

    def target_minor(self, speed: str, today: dt.date) -> str | None:
        minors = self.ga_minors(today)
        offset = {"fast": 2, "slow": 3}[speed]
        return minors[-offset] if len(minors) >= offset else None

    def minor_since(self, speed: str, today: dt.date) -> dt.date | None:
        """Since when the speed's target minor has been its target: the GA date of the newest minor."""
        minors = self.ga_minors(today)
        return self.minors[minors[-1]]["ga"] if minors else None


def _step_length(speed: str, since: dt.date, end_of_support: dt.date | None) -> dt.timedelta:
    step = STEP[speed]
    if speed == "slow" and end_of_support and since + 3 * step > end_of_support:
        # Shorten the steps so that prd is upgraded before the end of support.
        return max(dt.timedelta(days=1), (end_of_support - since) / 3)
    return step


def _newest_due(releases: dict[str, dt.date], candidates: list[str], due_by: dt.date) -> str | None:
    due = [v for v in candidates if releases.get(v) and releases[v] <= due_by]
    return max(due, key=vkey) if due else None


def plan(
    catalog_data: dict[str, Any], clusters: list[dict[str, Any]], today: dt.date, emergency: bool = False
) -> list[Step]:
    """Due steps for every cluster. ``clusters``: ``[{"environment", "name", "kubernetes", "node_image"}]``."""
    catalog = Catalog(catalog_data)
    by_env_cell = {(c["environment"], cell_of(c["name"])): c for c in clusters}
    steps: list[Step] = []
    for c in sorted(
        clusters, key=lambda c: (ENVS.index(c["environment"]) if c["environment"] in ENVS else 9, c["name"])
    ):
        env, name = c["environment"], c["name"]
        speed = speed_of(name)
        position = ENVS.index(env) if env in ENVS else 0
        previous = None
        if position > 0:
            # The same speed in the previous environment: same cell name, or sl-az2 for sl-az3 (prd only).
            cell = cell_of(name)
            previous = by_env_cell.get((ENVS[position - 1], cell)) or by_env_cell.get((ENVS[position - 1], "sl-az2"))
        steps += _kubernetes_step(catalog, c, speed, position, previous, today, emergency)
        if speed != "stateful" and c.get("node_image"):
            steps += _node_image_step(catalog, c, speed, position, previous, today, emergency)
    return steps


def _kubernetes_step(
    catalog: Catalog,
    c: dict[str, Any],
    speed: str,
    position: int,
    previous: dict | None,
    today: dt.date,
    emergency: bool,
) -> list[Step]:
    env, name, current = c["environment"], c["name"], c["kubernetes"]
    base = {"environment": env, "cluster": name, "speed": speed, "kind": "kubernetes", "current": current}

    if speed == "stateful":
        info = catalog.minors.get(minor(current), {})
        lts_end = info.get("lts_end") or info.get("end_of_support")
        if not lts_end or today < lts_end - LTS_WINDOW:
            return [
                Step(
                    **base,
                    target=current,
                    action="hold",
                    reason=f"stateful cluster stays on {minor(current)} until 6 months before its LTS end ({lts_end})",
                )
            ]
        newer = [m for m in catalog.minors if vkey(m) > vkey(minor(current)) and catalog.minors[m].get("lts_end")]
        if not newer:
            return [Step(**base, target=current, action="hold", reason="LTS window open but no newer LTS minor")]
        target_minor = min(newer, key=vkey)
        target = _newest_due(catalog.patches, [v for v in catalog.patches if minor(v) == target_minor], today)
        return _gate(
            base,
            target,
            previous,
            position,
            f"LTS of {minor(current)} ends {lts_end}; within 6 months – move to the next LTS minor {target_minor}",
            None,
        )

    target_minor = catalog.target_minor(speed, today)
    if target_minor is None:
        return [Step(**base, target=current, action="none", reason="no target minor in the catalog")]
    since = catalog.minor_since(speed, today) or today
    eos = catalog.minors.get(minor(current), {}).get("end_of_support")
    step = dt.timedelta(0) if emergency else _step_length(speed, since, eos)
    candidates = [v for v in catalog.patches if minor(v) == target_minor]

    if minor(current) != target_minor and vkey(minor(current)) < vkey(target_minor):
        # Minor upgrade: counted from when the minor became the target, one step per environment.
        due_on = since + step * (position + 1)
        if today < due_on:
            return [
                Step(
                    **base,
                    target=current,
                    action="hold",
                    reason=f"minor {target_minor} due in {env} on {due_on}",
                    due_since=None,
                )
            ]
        # The newest patch that has waited its own steps, else the minor's first patch.
        due_by = today if emergency else today - STEP[speed] * (position + 1)
        released = [v for v in candidates if catalog.patches[v] and catalog.patches[v] <= today]
        target = _newest_due(catalog.patches, candidates, due_by) or (min(released, key=vkey) if released else None)
        return _gate(
            base,
            target,
            previous,
            position,
            f"{speed} speed: minor {target_minor} became the target on {since}",
            due_on,
        )

    if minor(current) != target_minor:
        return [
            Step(
                **base,
                target=current,
                action="hold",
                reason=f"{current} is newer than the {speed} target minor {target_minor}; check the cluster",
            )
        ]

    # Patch: the newest patch of the minor released at least (position+1) steps ago.
    due_by = today - STEP[speed] * (position + 1) if not emergency else today
    target = _newest_due(catalog.patches, candidates, due_by)
    if not target or vkey(target) <= vkey(current):
        return [Step(**base, target=current, action="none", reason="no newer patch due")]
    return _gate(
        base,
        target,
        previous,
        position,
        f"{speed} speed: patch {target} released {catalog.patches[target]}",
        catalog.patches[target] + STEP[speed] * (position + 1),
    )


def _gate(
    base: dict[str, Any],
    target: str | None,
    previous: dict | None,
    position: int,
    reason: str,
    due_on: dt.date | None,
    key: str = "kubernetes",
) -> list[Step]:
    current = base["current"]
    if not target or vkey(target) <= vkey(current):
        return [Step(**base, target=current, action="none", reason="no newer version due")]
    if position > 0:
        prev_version = (previous or {}).get(key)
        if not prev_version or vkey(prev_version) < vkey(target):
            same_line = key == "node_image" or minor(prev_version or "") == minor(target)
            if prev_version and vkey(prev_version) > vkey(current) and same_line:
                target, reason = (
                    prev_version,
                    reason + f"; capped to {prev_version}, which the previous environment runs",
                )
            else:
                return [
                    Step(
                        **base,
                        target=current,
                        action="hold",
                        reason=f"{target} is due, but the previous environment does not run it yet",
                    )
                ]
    return [
        Step(**base, target=target, action="upgrade", reason=reason, due_since=due_on.isoformat() if due_on else None)
    ]


def _node_image_step(
    catalog: Catalog,
    c: dict[str, Any],
    speed: str,
    position: int,
    previous: dict | None,
    today: dt.date,
    emergency: bool,
) -> list[Step]:
    current = c["node_image"]
    family = current.rsplit("-", 1)[0]
    candidates = [v for v in catalog.node_images if v.rsplit("-", 1)[0] == family]
    due_by = today - STEP[speed] * (position + 1) if not emergency else today
    target = _newest_due(catalog.node_images, candidates, due_by)
    base = {
        "environment": c["environment"],
        "cluster": c["name"],
        "speed": speed,
        "kind": "node-image",
        "current": current,
    }
    if not target or vkey(target) <= vkey(current):
        return [Step(**base, target=current, action="none", reason="no newer node image due")]
    return _gate(
        base,
        target,
        previous,
        position,
        f"{speed} speed: node image {target} released {catalog.node_images[target]}",
        catalog.node_images[target] + STEP[speed] * (position + 1),
        key="node_image",
    )


def to_dicts(steps: list[Step]) -> list[dict[str, Any]]:
    return [asdict(s) for s in steps]


def update_catalog(
    catalog: dict[str, Any], get_versions: dict[str, Any], node_images: list[str], today: dt.date
) -> dict[str, Any]:
    """Record when each version was first offered in the region.

    ``get_versions`` is ``az aks get-versions -l <region> -o json``; ``node_images`` the latest node image versions
    reported by ``az aks nodepool get-upgrades``. AKS does not publish release dates through the API, so the date a
    version is first seen in the region is its release date for the update policy. End-of-support and LTS end dates
    come from the AKS release calendar and are maintained by humans in the same file; they are never overwritten.
    """
    out = {
        "minors": {k: dict(v) for k, v in (catalog.get("minors") or {}).items()},
        "patches": dict(catalog.get("patches") or {}),
        "node_images": dict(catalog.get("node_images") or {}),
    }
    stamp = today.isoformat()
    for entry in get_versions.get("values", get_versions.get("orchestrators", [])) or []:
        if entry.get("isPreview"):
            continue
        version = entry.get("version") or entry.get("orchestratorVersion", "")
        if not version:
            continue
        info = out["minors"].setdefault(minor(version), {})
        info.setdefault("ga", stamp)
        if "AKSLongTermSupport" in ((entry.get("capabilities") or {}).get("supportPlan") or []):
            info["lts"] = True
        for patch in entry.get("patchVersions") or {}:
            out["patches"].setdefault(patch, stamp)
    for image in node_images:
        out["node_images"].setdefault(image, stamp)
    for section in ("minors", "patches", "node_images"):
        out[section] = dict(sorted(out[section].items(), key=lambda kv: vkey(kv[0])))
    return out
