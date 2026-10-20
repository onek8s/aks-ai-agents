name     = "aks-dev-ext-stateless-az1"
exposure = "ext"
managed_namespaces = {
  ext-fi-example = {
    owner = { team = "example", contact = "https://example.org" }
    labels = { "platform/zone" = "ext-fi" }
    flux = { url = "https://github.com/onek8s/aks-fluxcd-example", path = "./stateless" }
  }
}
