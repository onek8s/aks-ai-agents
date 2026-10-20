# aks-ai-agents

Implementation of the AI agents that run day 2 operations of [OneK8s](https://onek8s.github.io/) – one
Kubernetes platform for every cloud, implemented first on AKS – described in
[onek8s.github.io](https://github.com/onek8s/onek8s.github.io) –
[section 6.1, AI-driven day 2 operations](https://github.com/onek8s/onek8s.github.io/blob/main/design/6.1-ai-driven-day-2-operations.md),
plus the agent parts of
[section 7.1, implementation project](https://github.com/onek8s/onek8s.github.io/blob/main/archive/7.1-implementation-project-and-acceptance-testing.md)
and the version pipeline of
[section 3.5](https://github.com/onek8s/onek8s.github.io/blob/main/design/3.5-zero-downtime-cluster-upgrades.md#update-policy).

> Status: **work in progress**, built for the build tenant (phase 2 of section 7.1). Not yet run against a real
> environment.

Agents may change every environment, prd included, but only the way a human platform engineer would: **with a pull
request**. They observe with read-only identities; a deterministic merge gate owned by humans decides what may be
merged and by whom.

```text
 alert / schedule / pipeline / PR event
            │
            ▼
 ┌────────────────────┐  read-only tools   ┌─────────────────────────────┐
 │ agent (Claude)     │ ─────────────────▶ │ Azure, clusters, Log        │
 │ src/aks_agents     │                    │ Analytics, design docs      │
 └─────────┬──────────┘                    └─────────────────────────────┘
           │ pull request (agent/<name>/*, signed commit, Agent / Agent-Run trailers)
           │ issue · comment · review · safe runbook dispatch
           ▼
 ┌────────────────────┐   ┌─────────────────────┐   ┌──────────────────────────────────┐
 │ CI attaches plan   │──▶│ reviewer agent      │──▶│ merge gate  src/aks_gate         │
 │ (terraform / what- │   │ (required check)    │   │ plan → tier → approvals → merge  │
 │ if / manifests)    │   └─────────────────────┘   └──────────────────────────────────┘
 └────────────────────┘                                          │ main
                                                                 ▼
                                   existing delivery: IaC pipelines, Flux, stateful pipeline
```

## What is here

| Path | Content |
|---|---|
| [`src/aks_agents/`](src/aks_agents) | Agent runtime: the Claude tool-use loop, tools with per-agent grants, audit trail, CLI |
| [`src/aks_agents/platforms/`](src/aks_agents/platforms) | Git platforms: **GitHub** (issues, checks, Actions) and **Azure DevOps** (Boards work items, PR statuses, Pipelines) |
| [`src/aks_agents/tools/`](src/aks_agents/tools) | Design and guardrails reading; repositories and pull requests; issues, reviews, runbooks; read-only `az`, Resource Graph, Log Analytics, `kubectl`, Flux; documentation sites |
| [`src/aks_agents/versions.py`](src/aks_agents/versions.py) | Version pipeline: due upgrade steps per the update policy (deterministic, no LLM) |
| [`src/aks_agents/permissions.py`](src/aks_agents/permissions.py) | Least-privilege roles derived from logged prototyping runs |
| [`src/aks_gate/`](src/aks_gate) | **Merge gate**: Terraform plan / what-if / rendered manifests → risk tier, violations, approvals, windows; VPA bounds and resource-only changes against `rightSizing` – no LLM |
| [`agents/`](agents) | The ten agent definitions and their instructions |
| [`guardrails/`](guardrails) | Content of the guardrails repository: `limits.yaml`, `tiers.yaml`, merge gate and reviewer workflows |
| [`config/platform.yaml`](config/platform.yaml) | Repositories, environments, clusters, model endpoint, documentation allow-list |
| [`deploy/`](deploy) | Container image entrypoint and Terraform for identities, roles and Container Apps jobs |
| [`pipelines/`](pipelines) | Pipeline examples for **GitHub Actions and Azure DevOps**: plans and rendered manifests for the gate, the version pipeline |

## The agents

| Agent | Triggered by | Output |
|---|---|---|
| Operations (on-call) | alerts, Flux notifications, failed pipelines, daily schedule, merged pull requests | incident and task issues, verification comments, safe runbook dispatch |
| Platform change | task issues | pull requests to IaC and fleet `infrastructure/` |
| Upgrade | version pipeline output | version pull requests with a compatibility report; halt + incident on failed health |
| Release | successful release in the previous environment | promotion pull request dev → acc → prd with release notes and risk summary |
| Capacity and cost | weekly schedule, quota and budget alerts | pull requests within `limits.yaml`, issues to application teams |
| Right-sizing | daily schedule, `right-sizing` task issues (OOMKills, throttling, stuck resizes) | pull requests to the VPA bounds and modes and the resource baselines – the VPA resizes pods in place, online |
| Security and compliance | daily schedule, Defender and Key Vault events | pull requests for platform findings, issues for application teams |
| Onboarding | onboarding request issue | pull requests creating namespace (tier low), identity (tier high), overlays |
| Reviewer | every pull request | review and the required `reviewer-agent` check – never an approval |
| Documentation (section 5.3) | merged firewall rules and application changes, weekly schedule | pull requests with the traffic flow documentation: a Mermaid picture and a page per application (who reaches it, what it reaches through the hub firewall, who asked for each rule) and per isolation zone (the union the firewall opens), generated from Git by `aks-agent docs traffic` – the model only explains the findings |
| Egress (D61) | hourly schedule, firewall deny alerts | pull requests to the landing zone's per-application firewall rules (`firewall-applications/<env>-<exposure>/<namespace>.yaml`), from the flows the hub firewall denied and the pods behind their addresses |

What keeps them inside their role, independently of the model:

- **Tool grants**: an agent only gets the tools its definition lists; repositories and writable paths are part of the
  grant; every tool input is validated against its schema; denied calls are logged.
- **Read-only by construction**: `az` and `kubectl` allow only read verbs in guarded mode (on top of read-only Azure
  roles); commands that reveal secrets or credentials, `Secret` objects and application logs are denied in every mode.
- **Git is the only write path**: one commit per pull request through the Git platform's API (GitHub or Azure
  DevOps, `git.platform` in `platform.yaml`) on `agent/<name>/*`,
  with `Agent:` / `Agent-Run:` trailers and a body with intent, evidence, design section and rollback.
- **The merge gate** computes the tier from the plan, never from a label: data deletion, identity, network, policy
  and pipeline changes need humans; agents cannot touch the guardrails, runbooks or their own identities at all;
  prd is one cell per pull request, inside change windows and merge rate limits; `agents.enabled: false` stops all.
- **Audit**: every model response, tool call and result is written to `runs/<run-id>/` (and the immutable storage
  account); commits, pull requests and issues carry the run ID.

## Using it

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check . && .venv/bin/pytest

# Agents
aks-agent validate                                   # check the agent definitions against the role rules
aks-agent list                                       # what each agent may write
AKS_AGENT_ENV=dev aks-agent run operations-agent --dry-run --task "Write the daily health report."
aks-agent run upgrade-agent --env dev --task "Open the due version pull requests." --trigger steps.json
aks-agent permissions --runs runs/                   # least-privilege report from prototyping runs

# Version pipeline (section 3.5)
aks-agent versions --catalog aks-releases.yaml --clusters current-versions.yaml

# Merge gate
aks-gate collect --guardrails guardrails --repository org/aks-iac --number 42 --output pr.json --changed-files files.txt
aks-gate evaluate --guardrails guardrails --pr pr.json --changed-files files.txt --artifacts ci-artifacts/ --markdown
# Azure DevOps: collect-azdo / publish-azdo instead of collect (see pipelines/azure-devops)
```

Runs need a Claude endpoint: the Claude API (`ANTHROPIC_API_KEY`, server-side refusal fallbacks) or Microsoft
Foundry (`model.provider: foundry` in `platform.yaml`, Entra ID token of the run's managed identity); the model
provider and region are still an open question of the design. `AKS_AGENT_MODE=prototype` (build tenant, phases 1–2
only) lets the tools write directly and logs it.

## The egress agent in the lab (D61)

`aks-terraform`'s **Egress agent** workflow runs [`egress-agent`](agents/egress-agent.yaml) against the build tenant
with [`config/onek8s-dev.yaml`](config/onek8s-dev.yaml): Claude Sonnet 5.5 on Microsoft Foundry (Entra ID, the dev
deployment identity), `kubectl` read verbs on the firewall-egress cell through the labfw VPN, and the denied flows
from Log Analytics with Reader: labfw's `LabfwFirewallLog_CL`, which the Azure Monitor Agent on its VM fills
(`firewall.table`; with Azure Firewall the tool reads `AZFWApplicationRule` / `AZFWNetworkRule`). Its only write is
one pull request per application namespace to `onek8s/azure-landing-zone`. It adds destinations with
`firewall_egress_add`, which keeps the file's existing entries line for line, and `create_pull_request` refuses any
change of an application's egress file that removes or rewrites an existing entry (azure-landing-zone#72 replaced
api.open-meteo.com with the new destination).

The model is the expensive part, so [`egress.py`](src/aks_agents/egress.py) does everything else in code and keeps a
state file between runs (`aks-agent egress new`, then `aks-agent egress propose`): only what Log Analytics ingested
after the last processed ingestion time (the checkpoint), but never more than 3 hours back (`--max-lookback-minutes`) -
a first run or one after a long pause starts there; only denials, unless `--include-allowed` (or
`firewall.includeAllowed`) adds the allowed lines for a firewall that still allows by default; no bare IP destinations;
sources already known to be nodes or platform pods, or to belong to a namespace with a pending proposal, are dropped
before any cluster access; then pods are attributed and every
(namespace, destination) already in the firewall file, in an open agent pull request, pending, merged in the last
hour or rejected in the last week is dropped. Nothing left: no VPN, no cluster access, no model call. What is left
goes into the model's task, so it neither re-queries the log nor re-maps the pods.

## The documentation agent (design section 5.3)

The hub firewall's rules name zones, not applications, so Azure cannot say which application uses a rule. The
egress agent's rule files can: each entry names the namespace, the reason and the evidence. [`trafficdocs.py`](src/aks_agents/trafficdocs.py)
joins those files with the application's own Flux configuration (its Kustomization in the team's repository, built
with `kustomize` when installed), the zone's allow-list (the fleet's `policy/egress-allow-lists.yaml`) and the cells
(`envs/*.tfvars` of aks-terraform) and writes Markdown pages with Mermaid pictures - GitHub renders them - one per
application namespace and one per isolation zone, into the landing zone's `docs/traffic-flows/<spoke>/`. Every
destination has a status: **allowed** and named in the configuration, **allowed but not named** by it (a hidden
dependency or a stale rule), **named but without a rule** (denied), **proposed** in an open pull request, or on the
**zone allow-list**. All of it is deterministic and reads Git only:

```bash
aks-agent docs traffic --spoke dev-int --landing-zone ../azure-landing-zone --fleet ../aks-fluxcd-platform \
    --iac ../aks-terraform --app-repo onek8s/aks-fluxcd-example=../aks-fluxcd-example        # writes the pages
aks-agent docs traffic ... --check                                                          # CI: fails when stale
```

`aks-terraform`'s **Traffic flow docs** workflow runs it after a firewall rule or an application change and, when a
page changed, starts [`documentation-agent`](agents/documentation-agent.yaml), whose tool `traffic_flow_docs` writes
the same pages into its workspace; the model only explains the findings and opens the pull request. The pages carry no
timestamp, so a run that changes nothing opens nothing.

### The lab examples

The design site has a section of its own for the lab's test applications, `examples/lab/` of onek8s.github.io -
outside the requirements, the design and the PDFs. [`labdocs.py`](src/aks_agents/labdocs.py) generates it from the
same collection (cells, rule files, the applications' built Flux configuration, allow-lists, statuses, findings and
pictures) plus the cells' settings (location, availability zones, Gateway implementation, the zones' classic node
pools), the fleet's zones and Gateway, and each application's source repository (README description, endpoints,
version), matched by the image's name: an index and one page per isolation zone, with a section per application -
image and digest, replicas, placement labels, Services, routes, environment, egress with its status and the pictures.
Deterministic, Git only, no timestamp; the command owns every `*.md` of the directory (a zone that is gone loses its
page):

```bash
aks-agent docs lab --spoke dev-int --landing-zone ../azure-landing-zone --fleet ../aks-fluxcd-platform \
    --iac ../aks-terraform --app-repo onek8s/aks-fluxcd-example=../aks-fluxcd-example \
    --app-source onek8s/aks-hello=../aks-hello --site ../onek8s.github.io     # writes <site>/examples/lab/
aks-agent docs lab ... --check                                               # fails when the pages are stale
```

`aks-terraform`'s **Lab examples** workflow runs it by hand and opens a pull request to the design site with the
regenerated pages; a human merges it.

## Assumptions and open points for the design

- **IaC repository layout**: the agents' writable paths assume `environments/<env>/{versions,capacity,applications}.auto.tfvars`,
  `modules/`, `policy/`, `monitoring/` ([agents/README.md](agents/README.md)). Adjust the grants if the real layout differs.
- **Security agent and clusters**: the design's role table does not give the security agent `Azure Kubernetes
  Service RBAC Reader`, but its stateful-cluster drift report needs it; the Terraform grants it.
- **Reviewer model**: the design asks for a different model than the authors where possible; set `model.name` in
  `agents/reviewer-agent.yaml` once the provider question is answered.
- **Event dispatch**: alerts and pipelines start the agents' Container Apps jobs with `az containerapp job start`;
  the dispatcher (action group → Logic App / Function) is not part of this repository yet.
- **Online right-sizing, on by default**: in-place pod resize is done by the AKS Vertical Pod Autoscaler add-on
  (`InPlaceOrRecreate`), not by an agent – an agent with write access to pods would break "Git is the only write
  path", and an LLM does not belong in a loop that must react in seconds. Every application gets a VPA in
  `InPlaceOrRecreate` at onboarding; teams opt out with the pod label `platform/rightsizing: off`. The right-sizing
  agent owns the VPA policy by pull request; the gate checks it against `rightSizing` in `limits.yaml`. Every cluster
  starts on Kubernetes 1.36 (the platform floor, `kubernetes.allowedMinors`), so container and pod-level in-place
  resize are available everywhere; pod-level budgets are set in Git until the VPA supports them. The cluster IaC,
  still to be written, must enable the VPA add-on (`--enable-vpa`).
