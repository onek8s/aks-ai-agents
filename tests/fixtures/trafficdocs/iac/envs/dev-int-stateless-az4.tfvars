name     = "aks-dev-int-stateless-az4"
exposure = "int"
advanced_networking = null
managed_namespaces = {
  int-fi-quiet = {
    owner = {
      team    = "quiet"
      contact = "https://example.org/quiet"
    }
    labels = { "platform/zone" = "int-fi" }
    flux = {
      url  = "https://github.com/onek8s/aks-fluxcd-example"
      path = "./quiet"
      substitute = { zone = "int-fi", zone_domain = "int-fi.dev.onek8s.lol" }
    }
  }
}
