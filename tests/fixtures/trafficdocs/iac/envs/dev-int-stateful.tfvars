name     = "aks-dev-int-stateful"
exposure = "int"
managed_namespaces = {
  int-se-example = {
    owner = { team = "example", contact = "https://github.com/onek8s/aks-fluxcd-example/issues" }
    labels = { "platform/zone" = "int-se" }
    flux = {
      url  = "https://github.com/onek8s/aks-fluxcd-example"
      path = "./apps"
      substitute = { zone = "int-se", zone_domain = "int-se.dev.onek8s.lol" }
    }
  }
}
