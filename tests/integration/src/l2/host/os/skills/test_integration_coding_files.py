import json
import re
import subprocess
from pathlib import Path

import pytest

from src.l2_interfaces.host.os.skills.coding_files import HostOSCodingFiles
from src.l2_interfaces.host.os.skills.coding_context import HostOSCodingContext
from src.l2_interfaces.host.os.skills.coding_workspaces import HostOSCodingWorkspaces
from src.l2_interfaces.host.os.skills.files.editor import HostOSEditor
from src.l2_interfaces.host.os.skills.files.reader import HostOSReader
from src.l2_interfaces.host.os.skills.files.search import HostOSSearch


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, timeout=30
    )


@pytest.mark.asyncio
async def test_task_scoped_files_read_search_and_patch_without_workspace_path(
    os_client,
):
    repository = os_client.sandbox_dir / "task_files_repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test User")
    git(repository, "config", "user.email", "test@example.com")
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "initial")
    workspaces = HostOSCodingWorkspaces(os_client)
    created = await workspaces.create_coding_workspace(
        "sandbox/task_files_repo", "task-files"
    )
    assert created.is_success is True, created.message
    files = HostOSCodingFiles(
        os_client,
        workspaces,
        HostOSReader(os_client),
        HostOSEditor(os_client),
        HostOSSearch(os_client),
    )

    read = await files.read_coding_file_range("task-files", "app.py")
    digest = re.search(r"SHA-256: ([0-9a-f]{64})", read.message)
    searched = await files.search_coding_workspace("task-files", "value")
    patched = await files.apply_coding_file_patch(
        "task-files",
        "app.py",
        edits=[{"search": "value = 1", "replace": "value = 2"}],
        expected_sha256=digest.group(1) if digest else None,
    )

    assert read.is_success is True
    assert "value = 1" in read.message
    assert digest is not None
    assert searched.is_success is True
    assert (
        json.loads(searched.message)["matches"][0]["path"].removeprefix("./")
        == "app.py"
    )
    assert patched.is_success is True, patched.message
    workspace = Path(json.loads(created.message)["workspace_path"])
    assert (workspace / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (repository / "app.py").read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.asyncio
async def test_task_scoped_files_reject_workspace_escape(os_client):
    repository = os_client.sandbox_dir / "escape_repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test User")
    git(repository, "config", "user.email", "test@example.com")
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "initial")
    workspaces = HostOSCodingWorkspaces(os_client)
    await workspaces.create_coding_workspace("sandbox/escape_repo", "escape")
    files = HostOSCodingFiles(
        os_client,
        workspaces,
        HostOSReader(os_client),
        HostOSEditor(os_client),
        HostOSSearch(os_client),
    )

    escaped = await files.read_coding_file_range("escape", "../outside.txt")
    git_metadata = await files.read_coding_file_range("escape", ".git")

    assert escaped.is_success is False
    assert "inside the managed workspace" in escaped.message
    assert git_metadata.is_success is False
    assert "inside the managed workspace" in git_metadata.message


@pytest.mark.asyncio
async def test_structural_python_symbol_edit_is_exact_validated_and_reversible(os_client):
    repository = os_client.sandbox_dir / "structural_python_repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test User")
    git(repository, "config", "user.email", "test@example.com")
    original = (
        "class Service:\n"
        "    @staticmethod\n"
        "    def run(value):\n"
        "        return value - 1\n\n"
        "def run(value):\n"
        "    return value\n\n"
        "try:\n"
        "    recover()\n"
        "except Exception:\n"
        "    def recovered(value):\n"
        "        return value\n"
    )
    (repository / "app.py").write_text(original, encoding="utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "initial")
    workspaces = HostOSCodingWorkspaces(os_client)
    created = await workspaces.create_coding_workspace(
        "sandbox/structural_python_repo", "structural-python"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    editor = HostOSEditor(os_client)
    context = HostOSCodingContext(os_client)
    files = HostOSCodingFiles(
        os_client,
        workspaces,
        HostOSReader(os_client),
        editor,
        HostOSSearch(os_client),
        context,
    )

    ambiguous = await files.inspect_coding_symbol(
        "structural-python", "app.py", "run"
    )
    assert ambiguous.is_success is False
    assert "found 2" in ambiguous.message
    inspected = await files.inspect_coding_symbol(
        "structural-python", "app.py", "Service.run"
    )
    assert inspected.is_success is True, inspected.message
    inspection = json.loads(inspected.message)
    assert inspection["backend"] == "python_ast"
    assert inspection["start_line"] == 2
    assert inspection["end_line"] == 4
    assert inspection["content"].startswith("    @staticmethod")
    nested_in_handler = await files.inspect_coding_symbol(
        "structural-python", "app.py", "recovered"
    )
    assert nested_in_handler.is_success is True, nested_in_handler.message

    wrong_symbol_hash = await files.replace_coding_symbol(
        "structural-python",
        "app.py",
        "Service.run",
        "@staticmethod\ndef run(value):\n    return value + 1",
        inspection["file_sha256"],
        "0" * 64,
    )
    assert wrong_symbol_hash.is_success is False
    assert "symbol changed since inspection" in wrong_symbol_hash.message
    assert (workspace / "app.py").read_text(encoding="utf-8") == original

    renamed = await files.replace_coding_symbol(
        "structural-python",
        "app.py",
        "Service.run",
        "@staticmethod\ndef renamed(value):\n    return value + 1",
        inspection["file_sha256"],
        inspection["symbol_sha256"],
    )
    assert renamed.is_success is False
    assert "found 0" in renamed.message
    assert (workspace / "app.py").read_text(encoding="utf-8") == original

    malformed = await files.replace_coding_symbol(
        "structural-python",
        "app.py",
        "Service.run",
        "def broken(:\n    pass",
        inspection["file_sha256"],
        inspection["symbol_sha256"],
    )
    assert malformed.is_success is False
    assert "Structural edit unavailable" in malformed.message
    assert (workspace / "app.py").read_text(encoding="utf-8") == original

    replaced = await files.replace_coding_symbol(
        "structural-python",
        "app.py",
        "Service.run",
        "@staticmethod\ndef run(value):\n    return value + 1",
        inspection["file_sha256"],
        inspection["symbol_sha256"],
    )
    assert replaced.is_success is True, replaced.message
    payload = json.loads(replaced.message)
    updated = (workspace / "app.py").read_text(encoding="utf-8")
    assert "        return value + 1" in updated
    assert "def run(value):\n    return value\n" in updated
    compile(updated, "app.py", "exec")

    stale = await files.replace_coding_symbol(
        "structural-python",
        "app.py",
        "Service.run",
        "@staticmethod\ndef run(value):\n    return value + 2",
        inspection["file_sha256"],
        inspection["symbol_sha256"],
    )
    assert stale.is_success is False
    assert "file changed since inspection" in stale.message
    restored = await editor.restore_file_checkpoint(payload["checkpoint_id"])
    assert restored.is_success is True, restored.message
    assert (workspace / "app.py").read_text(encoding="utf-8") == original


class _FakeNode:
    def __init__(
        self,
        node_type,
        start_byte=0,
        end_byte=0,
        start_point=(0, 0),
        end_point=(0, 0),
        children=None,
        name_node=None,
        has_error=False,
    ):
        self.type = node_type
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.start_point = start_point
        self.end_point = end_point
        self.children = children or []
        self._name_node = name_node
        self.has_error = has_error

    def child_by_field_name(self, field):
        return self._name_node if field == "name" else None


class _FakeJavascriptParser:
    def parse(self, source):
        text = source.decode("utf-8")
        children = []
        invalid = text.count("{") != text.count("}")
        for match in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", text):
            start = match.start()
            opening = text.find("{", match.end())
            closing = text.find("}", opening + 1) if opening >= 0 else -1
            if closing < 0:
                invalid = True
                continue
            end = closing + 1
            name_start, name_end = match.span(1)

            def point(offset):
                prefix = text[:offset]
                return prefix.count("\n"), len(prefix.rsplit("\n", 1)[-1])

            name_node = _FakeNode(
                "identifier",
                name_start,
                name_end,
                point(name_start),
                point(name_end),
            )
            children.append(
                _FakeNode(
                    "function_declaration",
                    start,
                    end,
                    point(start),
                    point(end),
                    [name_node],
                    name_node,
                )
            )
        return type(
            "FakeTree",
            (),
            {"root_node": _FakeNode("program", children=children, has_error=invalid)},
        )()


@pytest.mark.asyncio
async def test_structural_tree_sitter_edit_refuses_lexical_fallback(
    os_client, monkeypatch
):
    repository = os_client.sandbox_dir / "structural_js_repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test User")
    git(repository, "config", "user.email", "test@example.com")
    original = "export function target(value) {\n  return value - 1;\n}\n"
    (repository / "app.js").write_text(original, encoding="utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "initial")
    workspaces = HostOSCodingWorkspaces(os_client)
    created = await workspaces.create_coding_workspace(
        "sandbox/structural_js_repo", "structural-js"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    context = HostOSCodingContext(os_client)
    files = HostOSCodingFiles(
        os_client,
        workspaces,
        HostOSReader(os_client),
        HostOSEditor(os_client),
        HostOSSearch(os_client),
        context,
    )

    monkeypatch.setattr(
        context,
        "_get_tree_sitter_parser",
        lambda language: (None, "parser unavailable"),
    )
    unavailable = await files.inspect_coding_symbol(
        "structural-js", "app.js", "target"
    )
    assert unavailable.is_success is False
    assert "parser unavailable" in unavailable.message

    monkeypatch.setattr(
        context,
        "_get_tree_sitter_parser",
        lambda language: (_FakeJavascriptParser(), None),
    )
    inspected = await files.inspect_coding_symbol(
        "structural-js", "app.js", "target"
    )
    assert inspected.is_success is True, inspected.message
    inspection = json.loads(inspected.message)
    assert inspection["backend"] == "tree_sitter"
    replaced = await files.replace_coding_symbol(
        "structural-js",
        "app.js",
        "target",
        "export function target(value) {\n  return value + 1;\n}",
        inspection["file_sha256"],
        inspection["symbol_sha256"],
    )
    assert replaced.is_success is True, replaced.message
    assert "return value + 1" in (workspace / "app.js").read_text(encoding="utf-8")
