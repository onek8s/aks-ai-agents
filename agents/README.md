# Agent definitions

One agent per responsibility (design [section 6.1](https://github.com/onek8s/onek8s.github.io/blob/main/design/6.1-ai-driven-day-2-operations.md#agents)),
each with its own identity, tool grants and instructions, so that a confused or manipulated agent can only do what
its role allows. In production these files live in the guardrails repository: agents can read them, only humans
change them (two approvals).

| Agent | Definition | Writes |
|---|---|---|
| Operations (on-call) | [operations-agent.yaml](operations-agent.yaml) | Issues, comments, safe runbook dispatch – never pull requests |
| Platform change | [platform-agent.yaml](platform-agent.yaml) | Pull requests to IaC modules / environments and fleet `infrastructure/` |
| Upgrade | [upgrade-agent.yaml](upgrade-agent.yaml) | Version pull requests (`environments/<env>/versions.auto.tfvars`) |
| Release | [release-agent.yaml](release-agent.yaml) | Promotion pull requests (`apps/*/{stateless,stateful}/{acc,prd}/`) |
| Capacity and cost | [capacity-agent.yaml](capacity-agent.yaml) | Pull requests within `limits.yaml`, issues to application teams |
| Security and compliance | [security-agent.yaml](security-agent.yaml) | Pull requests for platform findings, issues for application teams |
| Onboarding | [onboarding-agent.yaml](onboarding-agent.yaml) | Pull requests for new applications (new namespaces are tier low; identities in IaC stay tier high) |
| Right-sizing | [rightsizing-agent.yaml](rightsizing-agent.yaml) | Pull requests to `apps/*/stateless/*/{vpa,resources}.yaml` – VPA modes and bounds within `rightSizing` of `limits.yaml`; the VPA itself resizes pods in place |
| Reviewer | [reviewer-agent.yaml](reviewer-agent.yaml) | Reviews and the `reviewer-agent` check – never approvals |
| Documentation (section 5.3) | [documentation-agent.yaml](documentation-agent.yaml) | Pull requests with the generated traffic flow pages (`docs/traffic-flows/*` of the landing zone): a picture per application and per zone, from the rule files and the applications' Flux configuration – never a rule |
| Egress (D61) | [egress-agent.yaml](egress-agent.yaml) | Pull requests to the landing zone's per-application firewall rules (`firewall-applications/*/*.yaml`) – from denied flows mapped to their pods; never the zone allow-lists or the platform's rules |

Platform: the same definitions work on GitHub and Azure DevOps (`git.platform` in `config/platform.yaml`). On
Azure DevOps, issues are Boards work items and runbooks are pipelines; `publish_review.ownLogin` lists the reviewer's
login on both (`aks-reviewer-agent[bot]`, `id-agent-reviewer-agent-*`).

## Format

```yaml
name: upgrade-agent
description: one line
instructions: prompts/upgrade-agent.md      # the agent's own instructions; common rules are added by the runtime
triggers: [schedule, version-pipeline]      # documentation of what starts it (see deploy/)
model: { effort: high, maxTurns: 80 }       # optional name: overrides platform.yaml model.name
tools:
  - design                                  # a group: design_list, design_read, design_search
  - repo_read: { repositories: [iac, fleet] }
  - repo_write: { paths: { iac: ["environments/*/versions.auto.tfvars"] } }
  - create_pull_request: { repositories: [iac] }
```

Tool groups: `design` (list, read, search), `repo_read` (list, read, search), `repo_write` (write, replace, delete,
diff). Path patterns are `fnmatch` patterns where `*` also crosses `/`.

## Assumed repository layout

The write paths assume this IaC repository layout (the fleet layout is the one in design section 3.3, plus two
platform-maintained files per stateless application overlay: `vpa.yaml` and `resources.yaml`, written by the
onboarding and right-sizing agents); adjust the patterns, not the agents, if the real layout differs:

```text
iac/
├── modules/                       # environment, stateless cluster, stateful cluster, network modules
├── environments/<env>/            # one root module per environment
│   ├── versions.auto.tfvars       # target Kubernetes version and node image per cluster (upgrade agent)
│   ├── capacity.auto.tfvars       # system / stateful node pool sizes (capacity agent)
│   └── applications.auto.tfvars   # applications, identities, federated credentials (onboarding agent)
├── policy/                        # Azure Policy definitions and assignments
└── monitoring/                    # alert rules and dashboards
```
