import json

import pytest

from src.l2_interfaces.host.os.skills.coding_dependencies import (
    HostOSCodingDependencies,
)


@pytest.mark.asyncio
async def test_dependency_slice_resolves_python_imports_by_direction_and_depth(os_client):
    dependencies = HostOSCodingDependencies(os_client)
    repository = os_client.sandbox_dir / "dependency_repo"
    (repository / "services").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "__init__.py").write_text("", encoding="utf-8")
    (repository / "app.py").write_text(
        "import external_package\nfrom services.user import load_user\n",
        encoding="utf-8",
    )
    (repository / "services" / "__init__.py").write_text("", encoding="utf-8")
    (repository / "services" / "user.py").write_text(
        "from .store import Store\n\ndef load_user(): return Store()\n",
        encoding="utf-8",
    )
    (repository / "services" / "store.py").write_text(
        "class Store: pass\n", encoding="utf-8"
    )
    (repository / "tests" / "test_app.py").write_text(
        "from app import load_user\n", encoding="utf-8"
    )
    (repository / "unrelated.py").write_text("VALUE = 1\n", encoding="utf-8")

    downstream = await dependencies.get_code_dependency_slice(
        "app.py",
        "sandbox/dependency_repo",
        direction="dependencies",
        max_depth=2,
    )
    downstream_payload = json.loads(downstream.message)
    upstream = await dependencies.get_code_dependency_slice(
        "app.py",
        "sandbox/dependency_repo",
        direction="dependents",
        max_depth=1,
    )
    upstream_payload = json.loads(upstream.message)

    assert downstream.is_success is True
    assert [node["path"] for node in downstream_payload["nodes"]] == [
        "app.py",
        "services/user.py",
        "services/store.py",
    ]
    assert {
        (edge["source"], edge["target"], edge["line"])
        for edge in downstream_payload["edges"]
    } == {
        ("app.py", "services/user.py", 2),
        ("services/user.py", "services/store.py", 1),
    }
    assert [node["path"] for node in upstream_payload["nodes"]] == [
        "app.py",
        "tests/test_app.py",
    ]
    assert "unrelated.py" not in {
        node["path"] for node in downstream_payload["nodes"]
    }


@pytest.mark.asyncio
async def test_dependency_slice_resolves_relative_typescript_and_local_include(os_client):
    dependencies = HostOSCodingDependencies(os_client)
    repository = os_client.sandbox_dir / "polyglot_dependencies"
    (repository / "web").mkdir(parents=True)
    (repository / "native").mkdir()
    (repository / "web" / "app.ts").write_text(
        "import { value } from './lib'\n", encoding="utf-8"
    )
    (repository / "web" / "lib.ts").write_text(
        "export const value = 1\n", encoding="utf-8"
    )
    (repository / "native" / "main.c").write_text(
        '#include "local.h"\n', encoding="utf-8"
    )
    (repository / "native" / "local.h").write_text(
        "int local(void);\n", encoding="utf-8"
    )

    typescript = await dependencies.get_code_dependency_slice(
        "web/app.ts", "sandbox/polyglot_dependencies", direction="dependencies"
    )
    c_include = await dependencies.get_code_dependency_slice(
        "native/main.c", "sandbox/polyglot_dependencies", direction="dependencies"
    )

    typescript_payload = json.loads(typescript.message)
    include_payload = json.loads(c_include.message)
    assert [node["path"] for node in typescript_payload["nodes"]] == [
        "web/app.ts",
        "web/lib.ts",
    ]
    assert typescript_payload["edges"][0]["kind"] == "relative_module"
    assert [node["path"] for node in include_payload["nodes"]] == [
        "native/main.c",
        "native/local.h",
    ]
    assert include_payload["edges"][0]["kind"] == "local_include"


@pytest.mark.asyncio
async def test_dependency_slice_enforces_scope_file_and_result_limits(os_client):
    dependencies = HostOSCodingDependencies(os_client)
    repository = os_client.sandbox_dir / "bounded_dependencies"
    repository.mkdir()
    (repository / "app.py").write_text(
        "import one\nimport two\n", encoding="utf-8"
    )
    (repository / "one.py").write_text("", encoding="utf-8")
    (repository / "two.py").write_text("", encoding="utf-8")

    bounded = await dependencies.get_code_dependency_slice(
        "app.py",
        "sandbox/bounded_dependencies",
        direction="dependencies",
        max_files=2,
    )
    escaped = await dependencies.get_code_dependency_slice(
        "../outside.py", "sandbox/bounded_dependencies"
    )
    invalid = await dependencies.get_code_dependency_slice(
        "app.py", "sandbox/bounded_dependencies", direction="sideways"
    )
    payload = json.loads(bounded.message)

    assert bounded.is_success is True
    assert payload["node_count"] == 2
    assert payload["slice_truncated"] is True
    assert escaped.is_success is False
    assert "inside path" in escaped.message
    assert invalid.is_success is False
    assert "direction" in invalid.message
