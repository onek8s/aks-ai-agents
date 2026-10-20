# Azure DevOps pipelines

Azure DevOps counterparts of the GitHub workflows (design section 6.1 leaves the Git platform open). The gate and
the agents behave the same; only the platform mechanisms differ.

| File | Lives in | Purpose |
|---|---|---|
| [`guardrails/azure-devops/pull-request.yml`](../../guardrails/azure-devops/pull-request.yml) | guardrails repository | Template every pull request pipeline extends: plan stages of the repository, the reviewer agent, the merge gate |
| [`guardrails/azure-devops/merge-gate-sweep.yml`](../../guardrails/azure-devops/merge-gate-sweep.yml) | guardrails repository | Re-evaluates open pull requests every 30 minutes (votes and change windows do not re-run a PR build) |
| [`iac-pull-request.yml`](iac-pull-request.yml) | IaC repository | `terraform plan` per environment → `plan-<env>` artifacts |
| [`fleet-pull-request.yml`](fleet-pull-request.yml) | fleet repository | Render base and head manifests → `rendered-manifests` artifact |
| [`design-pull-request.yml`](design-pull-request.yml) | design repository | No plan; gate and reviewer only |
| [`version-pipeline.yml`](version-pipeline.yml) | IaC repository | Daily version pipeline → starts the upgrade agent |
| [`../scripts/`](../scripts) | IaC / fleet repository | `terraform-plan.sh`, `render-fleet.sh`, `version-pipeline.sh` shared with the GitHub workflows |

## GitHub → Azure DevOps

| GitHub (design draft) | Azure DevOps |
|---|---|
| Organisation ruleset on `main` | Branch policies on `main`: minimum reviewers 1, **reset votes on new pushes**, **prohibit the most recent pusher from approving**, required reviewers per path from CODEOWNERS (`platform-engineers`, `security-engineers` groups), comment resolution, squash merge only |
| Required workflow (merge gate) defined in the guardrails repository | Pull request pipelines `extends: azure-devops/pull-request.yml@guardrails`, enforced by a **Required template** check on the `merge-gate` variable group and the `aks-agent-dispatcher` service connection |
| Required status check `merge-gate` | Branch **status policy** `aks-agents/merge-gate`, *authorised account* = the gate identity only, *reset on source update* |
| Required check `reviewer-agent` | Pull request status `aks-agents/reviewer-agent` posted by the reviewer agent; read by the gate |
| Stale approvals dismissed on push | "Reset votes when new changes are pushed" (the gate counts every remaining approval as one on the head commit) |
| Team membership | Required **group reviewers**; the gate reads who voted for the group (`votedFor`) |
| Auto-merge for tier low | Auto-complete set by the gate identity |
| Agent GitHub Apps | The agents' managed identities are Azure DevOps users (no secrets), matched in `tiers.yaml` `agentsAzureDevOps` (`id-agent-<agent>-*`); *Contribute* only on `agent/<name>/*` (branch security), never on `main` |
| Issues | Boards work items (`git.azureDevOps.workItemType`), tagged `repo:<key>` plus the labels |
| Runbooks (workflow dispatch) | A pipeline per runbook in the runbooks repository, queued with the inputs as template parameters |
| `incident-fix` label set by a human | PR tag `incident-fix` **and** a human's comment containing `incident-fix` (Azure DevOps does not record who added a tag) |
| `pull_request_review` / `check_run` events re-run the gate | `merge-gate-sweep.yml` every 30 minutes (optionally a service hook on "pull request updated" queueing it) |

## Setup checklist

1. Guardrails repository: import `guardrails/` (including `azure-devops/`), create the `merge-gate-sweep` pipeline.
2. Variable group `merge-gate`: `MERGE_GATE_TOKEN` (secret – PAT or token of the gate identity with *Contribute to
   pull requests* and *Read* on the repositories and builds), `AGENTS_PACKAGE` (the pinned `aks-ai-agents` package,
   e.g. from the internal feed). Add the **Required template** check: `azure-devops/pull-request.yml@aks-guardrails`.
3. Service connections: `aks-agent-dispatcher` (may only start the agents' Container Apps jobs; required template
   check too), `aks-iac-plan-<env>` (read-only plan identity per environment), `aks-version-pipeline`.
4. Variables used by the template: `AGENTS_RESOURCE_GROUP`, `REVIEWER_ENV`; by the fleet pipeline: `K8S_MINORS`
   (e.g. `1.36 1.37`), `CRD_SCHEMAS`.
5. Each platform repository: the pull request pipeline above as **build validation** (required, expires on source
   update) and the **status policy** `aks-agents/merge-gate` (required, authorised account = gate identity).
6. Agent service principals: branch security so each can create and push only `agent/<name>/*`.

The agents work on Azure DevOps when `platform.yaml` says `git.platform: azure-devops`: pull requests from one push on
a new `agent/<name>/*` branch, issues as work items, the reviewer's threads and status, runbooks as pipeline runs.
Identity and permission setup: [deploy/README.md](../../deploy/README.md#azure-devops).
