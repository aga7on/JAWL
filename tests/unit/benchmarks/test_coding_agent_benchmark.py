import json
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPOSITORY_ROOT / "benchmarks" / "coding_agent" / "manifest.json"


def test_coding_benchmark_manifest_has_unique_resolvable_test_nodes():
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    capabilities = payload["capabilities"]
    identifiers = [item["id"] for item in capabilities]

    assert payload["version"] == 1
    assert payload["minimum_capability_pass_rate"] == 1.0
    assert len(identifiers) == len(set(identifiers))
    assert len(capabilities) >= 7
    for capability in capabilities:
        assert capability["tests"]
        for node in capability["tests"]:
            relative_path, separator, test_name = node.partition("::")
            assert separator == "::"
            assert test_name.startswith("test_")
            assert (REPOSITORY_ROOT / relative_path).is_file()
