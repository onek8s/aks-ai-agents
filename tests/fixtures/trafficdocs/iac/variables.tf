variable "gateway_implementation" {
  type        = string
  default     = "approuting-istio"
  description = "The Gateway API implementation"
}

variable "zone_node_pools" {
  type = object({
    amd64_vm_size  = optional(string, "Standard_D2alds_v6")
    arm64_vm_size  = optional(string, "Standard_D2pds_v6")
    max_count      = optional(number, 3)
    spot_max_count = optional(number, 1)
    max_pods       = optional(number, 250)
    os_disk_type   = optional(string, "Ephemeral")
  })
  default = null
}
