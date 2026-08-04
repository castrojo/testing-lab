from scripts.traffic_report import bytes_value, identity, rows


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
