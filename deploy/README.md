# Deploying the agents

The agents run as **Container Apps jobs in the management VNet** (design section 6.1), one job per agent and
environment, each with its own user-assigned managed identity – and on GitHub its own GitHub App; on Azure DevOps the
managed identity itself is the agent's Azure DevOps user. Their only egress is the model endpoint, the Git platform
and the documentation allow-list, through the hub Firewall.

| Piece | Where | What |
|---|---|---|
| `Dockerfile` | this repository | Image with the agents, the merge gate, `az`, `kubectl`, `kubelogin` |
| `deploy/entrypoint.sh` | image | Managed identity login, Git platform token (GitHub App installation token or Azure DevOps Entra ID token), design repository, kubeconfig, then `aks-agent run` |
| `deploy/terraform/` | guardrails repository | Identities, role assignments (design table), audit and (GitHub) App key access, the jobs; `git_platform = "azure-devops"` for Azure DevOps |
| `guardrails/.github/workflows/merge-gate.yml` | guardrails repository | Required workflow: the merge gate |
| `guardrails/.github/workflows/reviewer-agent.yml` | guardrails repository | Required workflow: starts the reviewer agent for every pull request |

## Roles (guarded mode)

| Agent | Azure | Kubernetes |
|---|---|---|
| All | `Reader` + `Monitoring Reader` on the environment subscription; Log Analytics read on the platform tables only (custom role at table scope) | – |
| Operations, platform, upgrade, release | as all | `Azure Kubernetes Service RBAC Reader` (no Secrets) |
| Capacity and cost | + `Cost Management Reader` | – |
| Security and compliance | + `Security Reader`, `Key Vault Reader` on the zone and platform vaults (metadata only) | `Azure Kubernetes Service RBAC Reader` for the stateful cluster drift report |
| Right-sizing | as all | `Azure Kubernetes Service RBAC Reader` – VPA objects, pod resources and resize conditions (verify that the role covers `autoscaling.k8s.io`; otherwise add a custom role with `verticalpodautoscalers/read`) |
| Onboarding, reviewer | as all | – |

On GitHub, every identity can also read **its own** GitHub App key (secret-scoped `Key Vault Secrets User` on the agents' vault in
the management subscription – never the zone or platform vaults) and append runs to the immutable audit container.
No identity gets a write role, a data-plane role on PaaS or ACR, or `listClusterAdminCredential`.

With `mode = "prototype"` (build tenant, phases 1–2 of section 7.1 only) every identity also gets `Owner` on the
environment subscription and `Azure Kubernetes Service RBAC Cluster Admin`, and the tools allow writes. Every action
is logged; `aks-agent permissions` turns the logged runs into the least-privilege roles before phase 3.

## Azure DevOps

With `git.platform: azure-devops` in `platform.yaml` and `git_platform = "azure-devops"` in Terraform:

1. Add every agent identity (Terraform output `azure_devops_users`) to the organisation as a user with a **Basic**
   access level (Boards work items need it), in a group `aks-agents` – never in `platform-engineers` or
   `security-engineers`, so an agent vote can never count as a human one.
2. Repositories (IaC, fleet, design): `aks-agents` has *Read*; on the branch folder `agent/<name>` of each agent its
   own identity has *Create branch* and *Contribute*; nobody in `aks-agents` has *Contribute* on `main` or *Bypass
   policies*. Contribute to pull requests: allowed (agents open pull requests, the reviewer posts threads and its
   status).
3. Guardrails and runbooks repositories: *Read* only. Runbooks pipelines: *Queue builds* for the operations, upgrade
   and release agents only.
4. Boards: *Edit work items* in the area used for platform work items.
5. Branch policy status `aks-agents/reviewer-agent` is *not* required by itself – the merge gate reads it; the
   required status is `aks-agents/merge-gate`, authorised account = the gate identity.

## Runbooks

The runbooks repository holds one GitHub Actions workflow (`<runbook>.yml`, `workflow_dispatch`) or one Azure
Pipelines pipeline named after the runbook (`halt-release`, `drain-cell`, `rollback-cell`, `suspend-kustomization`).
Each must declare the inputs / parameters the agents send – `environment`, `cell`, `namespace`, `name`, `reason`,
`requested_by`, `agent_run` (unused ones as optional) – because both platforms reject undeclared ones, and must
enforce its own limits (never the last cell in traffic, only the previous tag, never `infra-*`) and page the on-call.

## Triggers

| Trigger | Starts | How |
|---|---|---|
| Schedule | operations (daily health report), security (daily), right-sizing (daily), capacity (weekly) | `cron-*` jobs |
| Azure Monitor alert, Flux notification, failed pipeline | operations agent | action group / notification → `az containerapp job start` of `job-operations-agent-<env>` with `AKS_AGENT_TRIGGER_JSON` |
| Version pipeline output | upgrade agent | the version pipeline starts `job-upgrade-agent-<env>` with its output as trigger |
| Successful release in an environment | release agent | the release pipeline starts `job-release-agent-<next env>` |
| Pull request | reviewer agent | required workflow `reviewer-agent.yml` (GitHub) or the reviewer stage of `guardrails/azure-devops/pull-request.yml` (Azure DevOps) |
| Onboarding request issue, task issue | onboarding / platform / right-sizing agent (`right-sizing` label) | issue workflow in the IaC or fleet repository starts the job |

Starting a job needs only a dispatcher identity with permission to start the agent jobs; the dispatcher cannot change
what a job runs except the task and trigger payload, which the agent treats as data.

## Kill switch

- suspend an agent's GitHub App installation, or disable its Azure DevOps user – its pull requests, issues and
  reviews stop;
- `agents.enabled: false` in `guardrails/limits.yaml` – the merge gate rejects every agent pull request;
- remove the identity's role assignments (or the job) – its Azure reads stop.

The platform keeps running; it is just changed by humans again.
