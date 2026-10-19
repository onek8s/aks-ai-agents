name     = "aks-dev-int-stateless-az3"
location = "germanywestcentral"
exposure = "int"
advanced_networking = {
  observability = false
}
network_profile = {
  outbound_type = "userDefinedRouting"
}
managed_namespaces = {
  int-se-example = {
    # Who owns it: "team" and the contact (a # inside quotes: "https://x/#frag")
    owner = {
      team        = "example"
      cost_center = "example"
      contact     = "https://github.com/onek8s/aks-fluxcd-example/issues"
    }
    labels = {
      "platform/zone" = "int-se"
    }
    flux = {
      url  = "https://github.com/onek8s/aks-fluxcd-example"
      path = "./firewall-egress"
      substitute = {
        zone        = "int-se"
        zone_domain = "int-se.dev.onek8s.lol"
        home_cell   = "aks-dev-int-stateless-az3"
      }
    }
  }
}
