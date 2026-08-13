from scripts.traffic_report import (
    bytes_value,
    cache_hit_ratio,
    estimate_non_node_egress,
    identity,
    is_host_network_mirror,
    print_workload_section,
    rows,
)


def test_bytes_value_uses_binary_units():
    assert bytes_value(1024**3) == "1.00 GiB"


def test_rows_sort_largest_first():
    samples = [
        {"value": [0, "2"], "metric": {"name": "small"}},
        {"value": [0, "10"], "metric": {"name": "large"}},
    ]
    assert [metric["name"] for _, metric in rows(samples)] == ["large", "small"]


def test_identity_supports_cadvisor_kubernetes_labels():
    assert identity(
        {
            "container_label_io_kubernetes_pod_namespace": "argo",
            "container_label_io_kubernetes_pod_name": "worker-123",
            "container_label_io_kubernetes_container_name": "main",
        }
    ) == "argo/worker-123/main"


def test_identity_never_prints_unlabelled_payload_data():
    assert identity({"instance": "node-1", "token": "must-not-be-used"}) == "node-1"


def test_estimate_non_node_egress_subtracts_peer_receive_totals():
    tx = [
        (10.0, {"node": "ghost"}),
        (4.0, {"node": "exo-0"}),
    ]
    rx = [
        (6.0, {"node": "ghost"}),
        (3.0, {"node": "exo-0"}),
    ]

    assert estimate_non_node_egress(tx, rx) == {"ghost": 7.0, "exo-0": None}


def test_estimate_non_node_egress_is_unavailable_without_peer_data():
    assert estimate_non_node_egress([(10.0, {"node": "ghost"})], []) == {
        "ghost": None
    }


def test_cache_hit_ratio_uses_upstream_bytes_as_misses():
    upstream_rx = [(2.0, {"node": "ghost"})]
    cache_tx = [(10.0, {"node": "ghost"})]

    assert cache_hit_ratio(upstream_rx, cache_tx) == 0.8


def test_host_network_mirror_is_detected_by_label_or_known_pod_name():
    assert is_host_network_mirror({"host_network": "true"}) is True
    assert is_host_network_mirror({"pod": "usb4-link-monitor-abc12"}) is True
    assert is_host_network_mirror({"pod": "zot-cache-abc12"}) is False


def test_workload_section_excludes_host_network_mirrors(capsys):
    print_workload_section(
        "Workload incoming:",
        [
            (10.0, {"namespace": "kube-system", "pod": "usb4-link-monitor-abc12"}),
            (5.0, {"namespace": "argo", "pod": "worker-abc12"}),
        ],
    )

    output = capsys.readouterr().out
    assert "usb4-link-monitor" not in output
    assert "argo/worker-abc12" in output
