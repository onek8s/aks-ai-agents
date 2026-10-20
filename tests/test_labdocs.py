"""The lab examples of the design site (labdocs.py): one page per isolation zone with a section per application, and
an index - from the traffic flow documentation's fixtures, deterministic, Git only."""

import re
from pathlib import Path

from test_trafficdocs import APPS, FIXTURES, sources

from aks_agents import labdocs as lab
from aks_agents import trafficdocs as t

APP_SOURCES = {"onek8s/aks-hello": FIXTURES / "aks-hello"}


def generate(**kw) -> lab.Result:
    return lab.generate(sources(**kw), APP_SOURCES)


def test_pages_one_per_zone_and_an_index():
    result = generate()
    assert set(result.files) == {"index.md", "int-fi.md", "int-se.md"}
    assert result.files["int-se.md"].startswith("# Internal - Sweden\n")
    assert result.files["int-fi.md"].startswith("# Internal - Finland\n")
    assert result.files["index.md"].startswith("# Lab examples\n")
    for text in result.files.values():
        assert not text.startswith("---")  # no front matter: the first heading titles the page


def test_country_names_and_anchors():
    assert lab.country("int-fi") == "Finland" and lab.country("ext-se") == "Sweden"
    assert lab.country("int-xx") == "int-xx" and lab.zone_title("lab") == "lab"
    assert lab.zone_title("int-fi") == "Internal - Finland" and lab.zone_title("ext-se") == "External - Sweden"
    assert lab.zone_title("dmz-fi") == "dmz-fi - Finland"
    assert lab.anchor("aks-hello in int-se-example") == "aks-hello-in-int-se-example"


def test_each_application_section_is_on_its_zone_page():
    result = generate()
    se = result.files["int-se.md"]
    assert "\n## aks-hello in int-se-example\n" in se
    assert "aks-hello in int-se-example" not in result.files["int-fi.md"]
    assert "[aks-hello in int-se-example](#aks-hello-in-int-se-example)" in se
    index = result.files["index.md"]
    assert "[aks-hello in int-se-example](int-se.md#aks-hello-in-int-se-example)" in index
    assert "[Internal - Finland](int-fi.md)" in index and "[Internal - Sweden](int-se.md)" in index
    # A namespace without a workload in Git is listed, not given a section
    assert "- `int-se-gone`:" in se and "- `int-fi-quiet`:" in result.files["int-fi.md"]


def test_the_section_states_what_git_deploys():
    se = generate().files["int-se.md"]
    assert "| Deployment | `aks-hello`, replicas 3 |" in se
    assert "`acronek8sdev.azurecr.io/aks-hello`, version `0.5.0`" in se
    assert "`platform/arch: amd64`, `platform/capacity: spot` - the zone's pools `intsespot`, `intse`" in se
    assert "| `aks-hello` | 80 | `h2c` | `kubernetes.io/h2c` |" in se
    assert "`hello-aks-dev-int-stateless-az3.int-se.dev.onek8s.lol`" in se
    assert "| `CLUSTER_NAME` | `aks-dev-int-stateless-az3` |" in se  # Flux's variables substituted
    assert "| `api.open-meteo.com` | 443/https | **allowed** - the application's rule" in se
    assert "| `license.example.org` | 443/https | **missing**" in se
    assert "```mermaid\nflowchart LR" in se and 'fw -.->|"denied: no rule"| d_license_example_org' in se
    assert "(https://github.com/onek8s/aks-fluxcd-example/tree/main/firewall-egress)" in se
    assert (
        "https://github.com/onek8s/azure-landing-zone/blob/main/stacks/platform-extensions/firewall-applications/"
        "dev-int/int-se-example.yaml" in se
    )
    assert "Other cells sync another path of the same repository" in se  # the trafficdocs note


def test_the_zone_facts_cells_pools_gateway_allow_list():
    se = generate().files["int-se.md"]
    assert "`aks-dev-int-stateless-az3` (germanywestcentral, availability zone 3)" in se
    assert "`int-se-gateway/int-se` (traefik), listener HTTPS 443 `*.int-se.dev.onek8s.lol`" in se
    assert "| `aks-dev-int-stateless-az3` | `intsespot` | amd64 | spot | Standard_D2alds_v6 | 0-1 |" in se
    assert "| `aks-dev-int-stateless-az3` | `intsearm` | arm64 | on-demand | Standard_D2pds_v6 | 0-1 |" in se
    assert "| Zone allow-list | `login.microsoftonline.com`, `*.blob.example.net`" in se
    assert "a_int_se_example" in se and "z_login_microsoftonline_com" in se  # trafficdocs' zone picture
    fi = generate().files["int-fi.md"]
    assert "No cell that is built gives this zone node pools." in fi


def test_cell_settings_merge_the_shared_file_and_the_defaults():
    cells = t.read_cells(FIXTURES / "iac", "dev")
    settings = lab.read_cell_settings(FIXTURES / "iac", "dev", cells)
    az3 = settings["aks-dev-int-stateless-az3"]
    assert az3.gateway_implementation == "traefik"  # from envs/dev-int-stateless.tfvars
    assert az3.availability_zones == ["3"] and az3.zones == ["int-se"]
    assert az3.pools["max_count"] == "1" and az3.pools["spot_max_count"] == "1"  # the cell's, then the default
    assert settings["aks-dev-int-stateless-az4"].pools is None
    assert settings["aks-dev-int-stateless-az4"].gateway_implementation == "traefik"
    assert lab.read_fleet_zones(FIXTURES / "fleet", "dev-int-stateless-az3") == {"int-se": "int-se.dev.onek8s.lol"}


def test_the_application_source_is_matched_by_the_image_name():
    se = generate().files["int-se.md"]
    assert "From [onek8s/aks-hello](https://github.com/onek8s/aks-hello)'s README: A hello world website" in se
    assert "| Source | [onek8s/aks-hello](https://github.com/onek8s/aks-hello), version `0.5.0` |" in se
    assert "| `/status` | The same as JSON |" in se
    assert lab.image_parts("r.io/a/aks-hello:0.8.0@sha256:ab") == ("r.io/a/aks-hello", "0.8.0", "sha256:ab")
    assert lab.image_parts("localhost:5000/x") == ("localhost:5000/x", "", "")
    source = lab.read_app_source("onek8s/aks-hello", FIXTURES / "aks-hello")
    assert lab.match_source("r.io/aks-hello:1", {"onek8s/aks-hello": source}) is source
    assert lab.match_source("r.io/other:1", {"onek8s/aks-hello": source}) is None
    # Without the source the pages still work
    plain = lab.generate(sources()).files["int-se.md"]
    assert "## aks-hello in int-se-example" in plain and "README" not in plain and "| Source |" not in plain


def test_links_are_relative_within_the_lab_or_absolute():
    result = generate()
    for name, text in result.files.items():
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if target.startswith("https://"):
                continue
            page, _, frag = target.partition("#")
            assert "/" not in page, (name, target)
            assert (page or name) in result.files, (name, target)
            if frag:
                heads = re.findall(r"(?m)^#+\s+(.*)$", result.files[page or name])
                assert frag in {lab.anchor(h.replace("`", "")) for h in heads}, (name, target)


def test_deterministic():
    assert generate().files == generate().files


def test_write_check_and_the_removal_of_a_stale_zone_page(tmp_path):
    result = generate()
    changed, removed = lab.write(result, tmp_path)
    assert changed == sorted(result.files) and removed == []
    assert lab.write(result, tmp_path) == ([], [])
    assert lab.diff(result, tmp_path) == []
    (tmp_path / "int-xx.md").write_text("# int-xx\n")  # a zone that no longer exists
    (tmp_path / "int-se.md").write_text("edited by hand")
    (tmp_path / "notes.txt").write_text("not a page")
    assert lab.diff(result, tmp_path) == ["int-se.md", "int-xx.md"]
    assert lab.write(result, tmp_path) == (["int-se.md"], ["int-xx.md"])
    assert (tmp_path / "notes.txt").is_file()


def test_cli_writes_into_the_site_and_checks(tmp_path, capsys, monkeypatch):
    from aks_agents.cli import main

    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    site = tmp_path / "site"
    args = [
        "docs",
        "lab",
        "--spoke",
        "dev-int",
        "--landing-zone",
        str(FIXTURES / "landing-zone"),
        "--fleet",
        str(FIXTURES / "fleet"),
        "--iac",
        str(FIXTURES / "iac"),
        "--app-repo",
        f"{APPS}={FIXTURES / 'apps'}",
        "--app-source",
        f"onek8s/aks-hello={FIXTURES / 'aks-hello'}",
        "--site",
        str(site),
    ]
    assert main(args + ["--check"]) == 1
    assert main(args) == 0
    printed = capsys.readouterr().out
    assert "2 zones and 1 applications of dev-int; 3 pages" in printed and "changed: 3" in printed
    assert (site / "examples" / "lab" / "int-se.md").is_file()
    assert main(args + ["--check"]) == 0
    assert out.read_text().splitlines()[-2:] == ["findings=3", "changed=0"]
    assert main(args[:-2] + ["--output", str(tmp_path / "o")]) == 0 and (tmp_path / "o" / "index.md").is_file()
    assert main(args[:-2]) == 2  # neither --site nor --output
    assert main(args + ["--app-source", "no-directory"]) == 2


def test_the_documentation_agent_may_write_the_lab_pages():
    from aks_agents.definition import AgentDefinition

    root = Path(__file__).resolve().parents[1]
    definition = AgentDefinition.load(root / "agents" / "documentation-agent.yaml")
    paths = definition.grant("repo_write").options["paths"]
    assert paths["design"] == ["examples/lab/*"] and paths["firewall"] == ["docs/traffic-flows/*"]
