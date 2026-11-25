# The cells and their zones (a comment with a { brace)
catalogue_dir = "catalogue/dev"
cells = {
  aks-dev-int-stateless-az3 = { zones = ["int-fi", "int-se"] }
  aks-dev-int-stateful      = { zones = ["int-fi"] }
}
