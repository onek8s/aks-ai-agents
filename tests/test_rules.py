from conftest import manifest, resource

from aks_gate.model import Action, PlanSet, Tier
from aks_gate.rules import evaluate

CLUSTER = "Microsoft.ContainerService/managedClusters"


def run(config, resources=(), manifests=(), files=(), agent="platform-agent", repo="org/iac", reference=None):
    plan = PlanSet(
        repository=repo,
        changed_files=list(files),
        resources=list(resources),
        manifests=list(manifests),
        reference_manifests=reference or {},
    )
    findings = evaluate(plan, config, agent)
    tier = max((f.tier for f in findings if f.tier), default=Tier.LOW)
    return tier, [f.rule for f in findings if f.violation], findings


def cluster(before_version, after_version, name="aks-prd-sl-az1", env="prd", extra_after=None):
    before = {
        "name": name,
        "kubernetes_version": before_version,
        "default_node_pool": [
            {"orchestrator_version": before_version, "vm_size": "Standard_D4ads_v6", "node_count": 3}
        ],
    }
    after = {
        "name": name,
        "kubernetes_version": after_version,
        "default_node_pool": [{"orchestrator_version": after_version, "vm_size": "Standard_D4ads_v6", "node_count": 3}],
    }
    after.update(extra_after or {})
    return resource(
        CLUSTER,
        before=before,
        after=after,
        name=name,
        env=env,
        address=f"module.{name}.azurerm_kubernetes_cluster.this",
    )


def test_patch_upgrade_is_low(config):
    tier, violations, _ = run(config, [cluster("1.36.1", "1.36.3")])
    assert (tier, violations) == (Tier.LOW, [])


def test_minor_upgrade_is_medium(config):
    tier, violations, _ = run(config, [cluster("1.35.5", "1.36.3")])
    assert (tier, violations) == (Tier.MEDIUM, [])


def test_disallowed_minor_is_rejected(config):
    _, violations, _ = run(config, [cluster("1.36.3", "1.37.0")])
    assert violations == ["kubernetes-allowed-minors"]


def test_stateful_version_change_is_high(config):
    tier, _, _ = run(config, [cluster("1.36.1", "1.36.3", name="aks-prd-sf")])
    assert tier == Tier.HIGH


def test_version_plus_other_change_gets_default(config):
    tier, _, _ = run(config, [cluster("1.36.1", "1.36.3", extra_after={"sku_tier": "Premium"})])
    assert tier == Tier.MEDIUM


def test_two_prd_cells_rejected(config):
    _, violations, _ = run(config, [cluster("1.36.1", "1.36.3"), cluster("1.36.2", "1.36.4", name="aks-prd-sl-az2")])
    assert violations == ["cells-per-pull-request"]


def test_two_dev_cells_allowed(config):
    _, violations, _ = run(
        config,
        [
            cluster("1.36.1", "1.36.3", name="aks-dev-sl-az1", env="dev"),
            cluster("1.36.2", "1.36.4", name="aks-dev-sl-az2", env="dev"),
        ],
    )
    assert violations == []


def test_fleet_cell_entry_points_count_as_cells(config):
    _, violations, _ = run(config, files=["clusters/prd/sl-az1/apps.yaml", "clusters/prd/sl-az2/apps.yaml"])
    assert violations == ["cells-per-pull-request"]


def test_key_vault_delete_rejected_for_agents_high_for_humans(config):
    kv = resource("Microsoft.KeyVault/vaults", Action.DELETE, before={"name": "kv"})
    _, violations, _ = run(config, [kv])
    assert violations == ["forbidden-delete"]
    tier, violations, _ = run(config, [kv], agent=None)
    assert (tier, violations) == (Tier.HIGH, [])


def test_storage_replace_counts_as_delete(config):
    st = resource("Microsoft.Storage/storageAccounts", Action.REPLACE, before={}, after={"name": "st"})
    _, violations, _ = run(config, [st])
    assert violations == ["forbidden-delete"]


def test_owner_role_and_subscription_scope(config):
    ra = resource(
        "Microsoft.Authorization/roleAssignments",
        Action.CREATE,
        after={"role_definition_name": "Owner", "scope": "/subscriptions/abc"},
    )
    _, violations, _ = run(config, [ra])
    assert sorted(violations) == ["role-assignment-scope", "role-not-allowed"]


def test_only_whitelisted_roles(config):
    def ra(**after):
        return resource(
            "Microsoft.Authorization/roleAssignments",
            Action.CREATE,
            after={"scope": "/subscriptions/s/resourceGroups/rg-extfi-shop", **after},
        )

    tier, violations, _ = run(config, [ra(role_definition_name="Storage Blob Data Contributor")])
    assert (tier, violations) == (Tier.HIGH, [])
    assert run(config, [ra(role_definition_name="Contributor")])[1] == ["role-not-allowed"]
    custom = "/subscriptions/s/providers/Microsoft.Authorization/roleDefinitions/11111111-2222-3333-4444-555555555555"
    assert run(config, [ra(role_definition_id=custom)])[1] == ["role-not-allowed"]
    assert run(config, [ra()])[1] == ["role-not-allowed"]  # role unknown in the plan
    # humans: not rejected, but tier high
    assert run(config, [ra(role_definition_name="Contributor")], agent=None)[:2] == (Tier.HIGH, [])


def test_role_assignment_by_guid_in_resource_group_is_high(config):
    ra = resource(
        "Microsoft.Authorization/roleAssignments",
        Action.CREATE,
        source="bicep",
        address="/subscriptions/s/resourceGroups/rg-extfi/providers/Microsoft.Authorization/roleAssignments/g",
        after={
            "properties": {
                "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/4633458b-17de-408a-b874-0445c86b69e6"
            }
        },
    )
    tier, violations, _ = run(config, [ra])
    assert (tier, violations) == (Tier.HIGH, [])


def test_policy_audit_to_deny_is_medium_and_disable_is_restricted(config):
    t = "Microsoft.Authorization/policyAssignments"
    tier, violations, _ = run(config, [resource(t, before={"enforce": False}, after={"enforce": True})])
    assert (tier, violations) == (Tier.MEDIUM, [])
    _, violations, _ = run(config, [resource(t, before={"enforce": True}, after={"enforce": False})])
    assert violations == ["policy-disable"]
    _, violations, _ = run(config, [resource(t, Action.DELETE, before={"enforce": True})])
    assert violations == ["policy-disable"]


def test_agent_identity_is_restricted(config):
    mi = resource(
        "Microsoft.ManagedIdentity/userAssignedIdentities",
        after={"name": "id-agent-upgrade-prd"},
        name="id-agent-upgrade-prd",
    )
    _, violations, _ = run(config, [mi])
    assert violations == ["agent-identity"]


def test_address_outside_ipam_rejected(config):
    good = resource(
        "Microsoft.Network/virtualNetworks/subnets", Action.CREATE, after={"address_prefixes": ["10.64.1.0/24"]}
    )
    bad = resource(
        "Microsoft.Network/virtualNetworks/subnets", Action.CREATE, after={"address_prefixes": ["192.168.0.0/24"]}
    )
    tier, violations, _ = run(config, [good])
    assert (tier, violations) == (Tier.HIGH, [])
    _, violations, _ = run(config, [bad])
    assert violations == ["address-plan"]


def test_firewall_any_destination_rejected(config):
    rcg = resource(
        "Microsoft.Network/firewallPolicies/ruleCollectionGroups",
        after={"network_rule_collection": [{"rule": [{"name": "all", "destination_addresses": ["*"]}]}]},
    )
    _, violations, _ = run(config, [rcg])
    assert violations == ["firewall-any"]


def test_nsg_inbound_from_internet_rejected(config):
    nsg = resource(
        "Microsoft.Network/networkSecurityGroups",
        source="bicep",
        after={
            "properties": {
                "securityRules": [
                    {
                        "name": "in",
                        "properties": {"access": "Allow", "direction": "Inbound", "sourceAddressPrefix": "Internet"},
                    }
                ]
            }
        },
    )
    _, violations, _ = run(config, [nsg])
    assert violations == ["nsg-any-inbound"]


def test_node_pool_size_and_count(config):
    np = "Microsoft.ContainerService/managedClusters/agentPools"
    tier, violations, _ = run(
        config,
        [
            resource(
                np,
                before={"vm_size": "Standard_D4ads_v6", "node_count": 3},
                after={"vm_size": "Standard_D4ads_v6", "node_count": 40},
            )
        ],
    )
    assert (tier, violations) == (Tier.MEDIUM, [])
    _, violations, _ = run(config, [resource(np, after={"vm_size": "Standard_M128", "node_count": 3})])
    assert violations == ["vm-size"]
    _, violations, _ = run(config, [resource(np, after={"vm_size": "Standard_D4ads_v6", "max_count": 99})])
    assert violations == ["node-count"]


def test_monitoring_is_low(config):
    tier, _, _ = run(config, [resource("Microsoft.Insights/metricAlerts", Action.CREATE, after={})])
    assert tier == Tier.LOW


def test_unknown_resource_is_medium(config):
    tier, _, _ = run(config, [resource("terraform:azurerm_something", after={})])
    assert tier == Tier.MEDIUM


def test_guardrails_repository_not_allowed_for_agents(config):
    _, violations, _ = run(config, files=["limits.yaml"], repo="org/guardrails")
    assert violations == ["repository"]


def test_pipeline_files_high_docs_low(config):
    assert run(config, files=[".github/workflows/deploy.yml"])[0] == Tier.HIGH
    assert run(config, files=["README.md", "docs/x.md"])[0] == Tier.LOW


def test_blast_radius(config):
    many = [resource("Microsoft.Insights/metricAlerts", Action.CREATE, after={}, name=f"a{i}") for i in range(30)]
    assert run(config, many)[0] == Tier.MEDIUM


def deployment(image, replicas=2):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "api"},
        "spec": {"replicas": replicas, "template": {"spec": {"containers": [{"name": "api", "image": image}]}}},
    }


def test_image_promotion_low_only_when_running_in_previous_env(config):
    old, new = "acr.io/api@sha256:aaa", "acr.io/api@sha256:bbb"
    change = manifest("Deployment", before=deployment(old), after=deployment(new), env="prd")
    tier, _, _ = run(config, manifests=[change], reference={"acc": [deployment(new)]})
    assert tier == Tier.LOW
    tier, _, _ = run(config, manifests=[change], reference={"acc": [deployment(old)]})
    assert tier == Tier.MEDIUM


def test_image_and_replica_change_is_default(config):
    change = manifest("Deployment", before=deployment("a@sha256:1"), after=deployment("a@sha256:2", replicas=3))
    assert run(config, manifests=[change])[0] == Tier.MEDIUM


def test_rbac_manifest_high(config):
    assert run(config, manifests=[manifest("ClusterRoleBinding", Action.CREATE, after={})])[0] == Tier.HIGH


def test_bicep_cluster_minor_upgrade(config):
    rid = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.ContainerService/managedClusters/aks-acc-sl-az2"
    before = {
        "properties": {
            "kubernetesVersion": "1.35.4",
            "agentPoolProfiles": [
                {"name": "system", "vmSize": "Standard_D4ads_v6", "count": 3, "orchestratorVersion": "1.35.4"}
            ],
        }
    }
    after = {
        "properties": {
            "kubernetesVersion": "1.36.2",
            "agentPoolProfiles": [
                {"name": "system", "vmSize": "Standard_D4ads_v6", "count": 3, "orchestratorVersion": "1.36.2"}
            ],
        }
    }
    rc = resource(CLUSTER, before=before, after=after, name="aks-acc-sl-az2", env="acc", address=rid, source="bicep")
    tier, violations, findings = run(config, [rc])
    assert (tier, violations) == (Tier.MEDIUM, [])
    assert [f.rule for f in findings] == ["kubernetes-version"]


def test_new_namespace_is_low_but_deletion_high(config):
    assert (
        run(config, manifests=[manifest("Namespace", Action.CREATE, after={}, namespace="", name="extfi-shop")])[0]
        == Tier.LOW
    )
    assert (
        run(config, manifests=[manifest("Namespace", Action.DELETE, before={}, namespace="", name="extfi-shop")])[0]
        == Tier.HIGH
    )


def vpa(mode="InPlaceOrRecreate", values="RequestsOnly", cpu="2", memory="4Gi", resources=None):
    policy = {"containerName": "*", "controlledValues": values, "maxAllowed": {"cpu": cpu, "memory": memory}}
    if resources:
        policy["controlledResources"] = resources
    return {
        "apiVersion": "autoscaling.k8s.io/v1",
        "kind": "VerticalPodAutoscaler",
        "spec": {
            "targetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "api"},
            "updatePolicy": {"updateMode": mode},
            "resourcePolicy": {"containerPolicies": [policy]},
        },
    }


def test_vpa_in_place_within_limits_is_low(config):
    change = manifest("VerticalPodAutoscaler", before=vpa(mode="Off"), after=vpa(), env="prd")
    assert run(config, manifests=[change], agent="rightsizing-agent")[:2] == (Tier.LOW, [])
    # YAML 1.1 reads an unquoted Off as false
    assert run(config, manifests=[manifest("VerticalPodAutoscaler", Action.CREATE, after=vpa(mode=False))])[0] == (
        Tier.LOW
    )


def test_vpa_bounds_modes_and_values(config):
    def tier(after, cluster="sl-az1"):
        return run(config, manifests=[manifest("VerticalPodAutoscaler", after=after, cluster=cluster)])[:2]

    assert tier(vpa(cpu="8")) == (Tier.MEDIUM, [])  # over the soft limit
    assert tier(vpa(memory="128Gi"))[1] == ["right-sizing-max"]  # over the hard limit
    assert tier(vpa(mode="Recreate")) == (Tier.MEDIUM, [])
    assert tier(vpa(values="RequestsAndLimits")) == (Tier.MEDIUM, [])
    unbounded = vpa()
    del unbounded["spec"]["resourcePolicy"]["containerPolicies"][0]["maxAllowed"]["memory"]
    assert tier(unbounded) == (Tier.MEDIUM, [])
    assert tier(vpa(resources=["cpu"])) == (Tier.LOW, [])  # memory left to the HPA: no memory bound needed
    # Stateful cluster: recommendations only.
    assert tier(vpa(mode="Off"), cluster="sf") == (Tier.LOW, [])
    assert tier(vpa(), cluster="sf")[1] == ["right-sizing-mode"]


def workload(cpu="500m", memory="512Mi", pod=None, image="a@sha256:1"):
    spec = {"containers": [{"name": "api", "image": image, "resources": {"requests": {"cpu": cpu, "memory": memory}}}]}
    if pod:
        spec["resources"] = {"limits": pod}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "spec": {"replicas": 2, "template": {"spec": spec}}}


def test_resource_only_change(config):
    def tier(before, after, cluster="sl-az1"):
        change = manifest("Deployment", before=before, after=after, cluster=cluster)
        return run(config, manifests=[change], agent="rightsizing-agent")[:2]

    assert tier(workload(), workload(cpu="250m", memory="768Mi")) == (Tier.LOW, [])
    assert tier(workload(), workload(cpu="2")) == (Tier.MEDIUM, [])  # ×4 step
    assert tier(workload(cpu="10"), workload(cpu="17"))[1] == ["right-sizing-max"]
    assert tier(workload(pod={"cpu": "2"}), workload(pod={"cpu": "3"})) == (Tier.LOW, [])  # pod-level resources
    assert tier(workload(), workload(pod={"cpu": "2"})) == (Tier.MEDIUM, [])  # adds a pod-level limit
    assert tier(workload(), workload(cpu="250m"), cluster="sf") == (Tier.MEDIUM, [])
    # Resources together with anything else: not a right-sizing change.
    assert tier(workload(), workload(cpu="250m", image="a@sha256:2")) == (Tier.MEDIUM, [])
