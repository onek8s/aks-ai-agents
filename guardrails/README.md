# Guardrails repository content

These files are the content of the **guardrails repository** of design section 15. They are kept here next to the
gate code while the agents are built (phase 2 of section 7.1) and published to their own repository, where every
change needs two human approvals (platform + security) and agents have read access only.

| File | Content |
|---|---|
| `limits.yaml` | Numbers the gate checks: change windows, freezes, merge rate limits, node pool and CPU limits, allowed VM sizes and Kubernetes minors, IPAM allocation, forbidden actions, kill switch |
| `tiers.yaml` | Risk tier rules by resource type, Kubernetes kind and path; approvals per tier; teams; the agents' GitHub Apps; required pull request structure |
| `.github/workflows/merge-gate.yml` | The merge gate, attached to the IaC, fleet and design repositories as a required workflow |
| `.github/workflows/reviewer-agent.yml` | Starts the reviewer agent for every pull request (required workflow) |
| `azure-devops/pull-request.yml` | Azure DevOps: template every pull request pipeline extends (required template) – reviewer agent and merge gate |
| `azure-devops/merge-gate-sweep.yml` | Azure DevOps: re-evaluates open pull requests every 30 minutes |
| `agents/` | Agent definitions (instructions, tools, repositories, paths) – see the top-level `agents/` directory |

The gate itself is `aks-gate` from `src/aks_gate`. It computes the tier from the **plan**, not from the diff and never
from a label the author set; see `aks-gate evaluate --help`.
