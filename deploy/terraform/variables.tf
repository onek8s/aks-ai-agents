variable "management" {
  description = "Management subscription resources the agents run in (design section 6.1: management VNet)."
  type = object({
    subscription_id              = string
    resource_group_name          = string
    location                     = string
    container_app_environment_id = string # Container Apps environment in the management VNet
    key_vault_id                 = string # vault holding only the agents' GitHub App keys
    key_vault_name               = string
    audit_storage_account_id     = string # immutable (WORM) storage account for agent runs
    audit_container_name         = string
  })
}

variable "image" {
  description = "Agent image (from the management ACR), pinned by digest."
  type        = string
}

variable "environments" {
  description = "What the agents may read per environment."
  type = map(object({
    subscription_id            = string
    cluster_ids                = list(string)
    log_analytics_workspace_id = string
    key_vault_ids              = list(string) # zone and platform vaults – metadata only (Key Vault Reader)
  }))
}

variable "git_platform" {
  description = "github: each agent has a GitHub App. azure-devops: each agent identity is an Azure DevOps user (no secret)."
  type        = string
  default     = "github"
  validation {
    condition     = contains(["github", "azure-devops"], var.git_platform)
    error_message = "git_platform must be github or azure-devops"
  }
}

variable "github_apps" {
  description = "GitHub only: each agent's own GitHub App – ID, installation ID and the Key Vault secret holding its private key."
  type = map(object({
    app_id          = string
    installation_id = string
    key_secret_name = string
  }))
  default = {}
}

variable "platform_tables" {
  description = "Log Analytics tables agents may query (table-level RBAC; no application tables)."
  type        = list(string)
  default = [
    "AzureActivity", "AzureDiagnostics", "AzureMetrics", "KubeEvents", "KubeNodeInventory", "KubePodInventory",
    "KubeServices", "InsightsMetrics", "Perf", "ContainerInventory", "Heartbeat", "AKSAudit", "AKSAuditAdmin",
    "AKSControlPlane", "ContainerLogV2", "AzureFirewallNetworkRule", "AzureFirewallApplicationRule",
  ]
}

variable "mode" {
  description = "guarded (read-only identities, changes by pull request) or prototype (build tenant phases 1-2)."
  type        = string
  default     = "guarded"
  validation {
    condition     = contains(["guarded", "prototype"], var.mode)
    error_message = "mode must be guarded or prototype"
  }
}

variable "schedules" {
  description = "Scheduled runs: agent => { cron (UTC), task }. Event-driven runs use the manual jobs."
  type = map(object({
    cron = string
    task = string
  }))
  default = {
    operations-agent  = { cron = "15 5 * * *", task = "Write the daily health report for this environment." }
    security-agent    = { cron = "45 5 * * *", task = "Run the daily security and compliance review for this environment." }
    capacity-agent    = { cron = "30 6 * * 1", task = "Run the weekly capacity and cost review for this environment." }
    rightsizing-agent = { cron = "0 7 * * *", task = "Run the daily right-sizing review for this environment." }
  }
}
