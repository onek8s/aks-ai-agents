import datetime as dt

from aks_agents import egress

AT = dt.datetime(2026, 10, 15, 21, 0, tzinfo=dt.UTC)
NETS = [__import__("ipaddress").ip_network("100.64.0.0/16")]
POD = {
    "kind": "pod",
    "namespace": "int-se-example",
    "pod": "aks-hello-x",
    "zone": "int-se",
    "team": "example",
    "workload": {"kind": "Deployment", "name": "aks-hello"},
}


def row(ip="100.64.32.26", fqdn="api.open-meteo.com", ingested="2026-10-15T20:58:00Z"):
    return {"SourceIp": ip, "Fqdn": fqdn, "DestinationPort": 443, "Protocol": "HTTPS", "Count": 4, "Ingested": ingested}


def test_new_flows_continue_from_the_checkpoint_and_advance_it():
    state = egress.EgressState(checkpoint="2026-10-15T20:50:00Z")
    seen = {}

    def query(kql):
        seen["kql"] = kql
        return [row(ingested="2026-10-15T20:58:00Z"), row(ip="100.64.32.28", ingested="2026-10-15T20:59:00Z")]

    rows = egress.new_flows(query, state, "LabfwFirewallLog_CL", AT)
    assert len(rows) == 2
    assert 'Action in ("Deny")' in seen["kql"] and "datetime(2026-10-15T20:50:00Z)" in seen["kql"]
    assert state.checkpoint == "2026-10-15T20:59:00Z"
    # Nothing new: the checkpoint stays, the next run continues from it
    egress.new_flows(lambda kql: [], state, "LabfwFirewallLog_CL", AT)
    assert state.checkpoint == "2026-10-15T20:59:00Z"


def test_never_further_back_than_the_lookback():
    seen = {}

    def query(kql):
        seen["kql"] = kql
        return []

    # First run, or a checkpoint from yesterday: three hours back, older events ignored
    for state in (egress.EgressState(), egress.EgressState(checkpoint="2026-10-14T12:00:00Z")):
        egress.new_flows(query, state, "LabfwFirewallLog_CL", AT)
        assert "datetime(2026-10-15T18:00:00Z)" in seen["kql"]
    egress.new_flows(query, egress.EgressState(), "LabfwFirewallLog_CL", AT, max_lookback=dt.timedelta(minutes=30))
    assert "datetime(2026-10-15T20:30:00Z)" in seen["kql"]


def test_allowed_flows_on_request():
    seen = {}

    def query(kql):
        seen["kql"] = kql
        return [{**row(), "Action": "Allow"}]

    state = egress.EgressState()
    rows = egress.new_flows(query, state, "LabfwFirewallLog_CL", AT, include_allowed=True)
    assert 'Action in ("Deny", "Allow")' in seen["kql"]
    flows = egress.attribute(rows, {"100.64.32.26": POD}, state, AT)
    assert '"action": "Allow"' in egress.task_text({"int-se-example": flows}, "dev-int")


def test_a_pending_proposal_stops_known_sources_before_any_cluster_access():
    state = egress.EgressState()
    state.addresses["100.64.32.26"] = {"namespace": "int-se-example", "zone": "int-se", "seen": "2026-10-15T20:00:00Z"}
    state.proposals[egress.key("int-se-example", "api.open-meteo.com", "https", 443)] = {
        "status": "pending",
        "pr": {"repo": "o/r", "number": 66},
        "updated": "2026-10-15T20:00:00Z",
    }
    kept = egress.drop_known([row(), row(ip="100.64.32.99"), row(ip="10.0.8.4")], state, NETS)
    assert [r["SourceIp"] for r in kept] == ["100.64.32.99"]  # known + pending dropped; outside the cells dropped


def test_only_application_pods_and_only_what_is_not_proposed_yet():
    state = egress.EgressState()
    mapping = {
        "100.64.32.26": POD,
        "100.64.0.100": {"kind": "node", "node": "aks-system-0"},
        "100.64.0.9": {**POD, "namespace": "external-dns", "team": "platform", "zone": None},
    }
    flows = egress.attribute([row(), row(ip="100.64.0.100"), row(ip="100.64.0.9")], mapping, state, AT)
    assert [f["SourceIp"] for f in flows] == ["100.64.32.26"]
    assert state.addresses["100.64.32.26"]["namespace"] == "int-se-example"
    # Nodes and platform pods are remembered as nothing to propose for: dropped before cluster access next time
    assert state.addresses["100.64.0.100"]["ignore"] and state.addresses["100.64.0.9"]["ignore"]
    assert egress.drop_known([row(ip="100.64.0.100")], state, NETS) == []
    in_main = "zone: int-se\negress:\n  - fqdn: other.example.org\n"
    assert egress.to_propose(flows, state, lambda ns: in_main, lambda: {}) == {"int-se-example": flows}
    assert egress.to_propose(flows, state, lambda ns: "egress:\n  - fqdn: api.open-meteo.com\n", lambda: {}) == {}
    pr = {"repo": "onek8s/azure-landing-zone", "number": 66, "url": "u"}
    covered = {"int-se-example": {("api.open-meteo.com", "https", "443"): pr}}
    assert egress.to_propose(flows, state, lambda ns: None, lambda: covered, AT) == {}
    # ...and remembered, so the next run drops the source before any cluster access
    assert state.proposals[egress.key("int-se-example", "api.open-meteo.com", "https", 443)]["pr"]["number"] == 66
    assert egress.drop_known([row()], state, NETS) == []


def test_proposals_are_recorded_and_follow_their_pull_request():
    state = egress.EgressState()
    flows = egress.attribute([row()], {"100.64.32.26": POD}, state, AT)
    proposed = {"int-se-example": flows}
    egress.record(
        state,
        proposed,
        [
            {
                "type": "pull_request",
                "repo": "onek8s/azure-landing-zone",
                "number": 66,
                "files": ["stacks/platform-extensions/firewall-applications/dev-int/int-se-example.yaml"],
            }
        ],
        AT,
    )
    k = egress.key("int-se-example", "api.open-meteo.com", "https", 443)
    assert state.proposals[k]["status"] == "pending" and state.suppressed(k)
    # Merged: suppressed for an hour, then forgotten; closed without merge: rejected for a week
    state.refresh(lambda repo, n: "merged", AT)
    assert state.proposals[k]["status"] == "merged"
    state.forget_old(AT + dt.timedelta(hours=2))
    assert k not in state.proposals
    egress.record(
        state, proposed, [{"type": "pull_request", "repo": "r", "number": 67, "files": ["x/int-se-example.yaml"]}], AT
    )
    state.refresh(lambda repo, n: "closed", AT)
    assert state.proposals[k]["status"] == "rejected"
    state.forget_old(AT + dt.timedelta(days=6))
    assert state.suppressed(k)
    state.forget_old(AT + dt.timedelta(days=8))
    assert not state.suppressed(k)


def test_state_survives_a_round_trip_and_a_bad_file(tmp_path):
    p = tmp_path / "s.json"
    s = egress.EgressState(checkpoint="2026-10-15T20:59:00Z")
    s.addresses["1.2.3.4"] = {"namespace": "n", "zone": "z", "seen": "2026-10-15T20:59:00Z"}
    s.save(p)
    assert egress.EgressState.load(p).addresses == s.addresses
    p.write_text("not json")
    assert egress.EgressState.load(p).checkpoint == "1970-01-01T00:00:00Z"


def test_the_task_carries_the_flows_so_the_model_need_not_look_them_up():
    state = egress.EgressState()
    flows = egress.attribute([row()], {"100.64.32.26": POD}, state, AT)
    text = egress.task_text({"int-se-example": flows}, "dev-int")
    assert "api.open-meteo.com" in text and "Do not call firewall_denied_flows" in text and "dev-int" in text


def _ns(name, zone=None, team=None, role=None):
    labels = {k: v for k, v in {"platform/zone": zone, "platform/team": team, "platform/role": role}.items() if v}
    return {"metadata": {"name": name, "labels": labels}}


def _pod(name, ns, node, host=False):
    return {
        "metadata": {"name": name, "namespace": ns, "labels": {"app": name.split("-")[0]}},
        "spec": {"nodeName": node, "hostNetwork": host},
    }


NAMESPACES = [
    _ns("int-fi-example", "int-fi", "example"),
    _ns("int-fi-other", "int-fi", "other"),
    _ns("int-fi-gateway", "int-fi", "platform", "gateway"),
    _ns("infra-observability", None, "platform"),
]


def test_the_model_proposing_nothing_declines_the_flow_for_a_day():
    state = egress.EgressState()
    flows = egress.attribute([row()], {"100.64.32.26": POD}, state, AT)
    egress.record(state, {"int-se-example": flows}, [], AT)
    assert egress.drop_known([row()], state, NETS) == []


def test_a_blocked_run_declines_nothing_and_declined_can_be_forgotten():
    state = egress.EgressState()
    flows = egress.attribute([row()], {"100.64.32.26": {**POD, "cluster": "aks-dev-int-stateless-az3"}}, state, AT)
    assert '"cluster": "aks-dev-int-stateless-az3"' in egress.task_text({"int-se-example": flows}, "dev-int")
    egress.record(state, {"int-se-example": flows}, [], AT, decline=False)
    assert egress.drop_known([row()], state, NETS) != []
    egress.record(state, {"int-se-example": flows}, [], AT)
    assert egress.drop_known([row()], state, NETS) == []
    assert state.forget_declined() == 1 and egress.drop_known([row()], state, NETS) != []
