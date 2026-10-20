# The exposures' firewall policies (a comment with { braces })
firewall_exposures = {
  int = {
    corporate = true
    environments = {
      dev = {
        system_prefixes     = ["172.22.0.0/24"]
        observability_fqdns = ["*.grafana.net", "stats.grafana.org"]
        zones = {
          int-fi = {
            prefixes        = ["172.22.1.0/24"]
            fqdn_allow_list = ["login.microsoftonline.com"]
          }
          int-se = {
            prefixes        = ["172.22.2.0/24"]
            fqdn_allow_list = ["login.microsoftonline.com"]
          }
        }
      }
    }
  }
}
