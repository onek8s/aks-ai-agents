# Identities, roles and jobs of the AI agents (design section 6.1, "Identities and access rights").
# This module belongs to the guardrails repository: only its pipeline may create role assignments for agent
# identities, and every change needs two human approvals.

locals {
  agents = [
    "operations-agent", "platform-agent", "upgrade-agent", "release-agent",
    "capacity-agent", "security-agent", "onboarding-agent", "rightsizing-agent", "reviewer-agent",
  ]

  # Agents that read the clusters (Azure Kubernetes Service RBAC Reader – does not include Secrets). The security
  # agent is added for the stateful cluster drift report, the right-sizing agent for the VPA objects, pod resources
  # and resize conditions.
  cluster_readers = [
    "operations-agent", "platform-agent", "upgrade-agent", "release-agent", "security-agent", "rightsizing-agent",
  ]

  pairs = { for p in setproduct(local.agents, keys(var.environments)) : "${p[0]}-${p[1]}" => {
    agent = p[0]
    env   = p[1]
  } }

  # Role assignments per pair; keys must be static strings for for_each.
  subscription_roles = merge([
    for key, p in local.pairs : merge(
      {
        "${key}-reader"     = { pair = key, scope = "/subscriptions/${var.environments[p.env].subscription_id}", role = "Reader" }
        "${key}-monitoring" = { pair = key, scope = "/subscriptions/${var.environments[p.env].subscription_id}", role = "Monitoring Reader" }
      },
      p.agent == "capacity-agent" ? {
        "${key}-cost" = { pair = key, scope = "/subscriptions/${var.environments[p.env].subscription_id}", role = "Cost Management Reader" }
      } : {},
      p.agent == "security-agent" ? {
        "${key}-security" = { pair = key, scope = "/subscriptions/${var.environments[p.env].subscription_id}", role = "Security Reader" }
      } : {},
      # Build tenant, phases 1-2 only (section 7.1): full permissions, every action logged.
      var.mode == "prototype" ? {
        "${key}-owner" = { pair = key, scope = "/subscriptions/${var.environments[p.env].subscription_id}", role = "Owner" }
      } : {},
    )
  ]...)

  cluster_roles = merge([
    for key, p in local.pairs : {
      for i, cluster in var.environments[p.env].cluster_ids : "${key}-aks-${i}" => {
        pair  = key
        scope = cluster
        role  = var.mode == "prototype" ? "Azure Kubernetes Service RBAC Cluster Admin" : "Azure Kubernetes Service RBAC Reader"
      }
    } if contains(local.cluster_readers, p.agent) || var.mode == "prototype"
  ]...)

  vault_roles = merge([
    for key, p in local.pairs : {
      for i, vault in var.environments[p.env].key_vault_ids : "${key}-kv-${i}" => {
        pair = key, scope = vault, role = "Key Vault Reader"
      }
    } if p.agent == "security-agent"
  ]...)

  table_roles = merge([
    for key, p in local.pairs : {
      for table in var.platform_tables : "${key}-table-${table}" => {
        pair  = key
        scope = "${var.environments[p.env].log_analytics_workspace_id}/tables/${table}"
      }
    }
  ]...)
}

resource "azurerm_user_assigned_identity" "agent" {
  for_each            = local.pairs
  name                = "id-agent-${each.key}" # matches limits.yaml agentIdentityPattern
  resource_group_name = var.management.resource_group_name
  location            = var.management.location
  tags                = { agent = each.value.agent, environment = each.value.env, managed-by = "guardrails" }
}

resource "azurerm_role_assignment" "subscription" {
  for_each             = local.subscription_roles
  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.agent[each.value.pair].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "cluster" {
  for_each             = local.cluster_roles
  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.agent[each.value.pair].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "vault_metadata" {
  for_each             = local.vault_roles
  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.agent[each.value.pair].principal_id
  principal_type       = "ServicePrincipal"
}

# Table-level read of the platform tables only, so a prompt injected through a log line cannot reach application
# logs that may hold personal data.
resource "azurerm_role_definition" "log_tables_reader" {
  name        = "Log Analytics table reader (AI agents)"
  scope       = "/subscriptions/${var.management.subscription_id}" # the workspaces are in sub-mgmt
  description = "Query the Log Analytics tables a role assignment is scoped to."
  permissions {
    actions = [
      "Microsoft.OperationalInsights/workspaces/read",
      "Microsoft.OperationalInsights/workspaces/query/read",
      "Microsoft.OperationalInsights/workspaces/tables/query/read",
    ]
  }
  assignable_scopes = distinct([for e in var.environments : e.log_analytics_workspace_id])
}

resource "azurerm_role_assignment" "log_tables" {
  for_each           = local.table_roles
  scope              = each.value.scope
  role_definition_id = azurerm_role_definition.log_tables_reader.role_definition_resource_id
  principal_id       = azurerm_user_assigned_identity.agent[each.value.pair].principal_id
  principal_type     = "ServicePrincipal"
}

# GitHub: each identity reads only its own agent's GitHub App key (secret-scoped role assignment). On Azure DevOps
# the identity itself is the Azure DevOps user and no secret exists.
resource "azurerm_role_assignment" "github_app_key" {
  for_each             = var.git_platform == "github" ? local.pairs : {}
  scope                = "${var.management.key_vault_id}/secrets/${var.github_apps[each.value.agent].key_secret_name}"
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.agent[each.key].principal_id
  principal_type       = "ServicePrincipal"
}

# Runs are appended to the immutable audit container; the container's WORM policy prevents changes and deletes.
resource "azurerm_role_assignment" "audit" {
  for_each             = local.pairs
  scope                = "${var.management.audit_storage_account_id}/blobServices/default/containers/${var.management.audit_container_name}"
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.agent[each.key].principal_id
  principal_type       = "ServicePrincipal"
}
