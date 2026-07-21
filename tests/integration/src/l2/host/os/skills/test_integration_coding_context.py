import json

import pytest

from src.l2_interfaces.host.os.skills.coding_context import HostOSCodingContext


@pytest.mark.asyncio
async def test_repository_map_extracts_cross_language_symbols(os_client):
    context = HostOSCodingContext(os_client)
    repository = os_client.sandbox_dir / "mapped_repo"
    repository.mkdir()
    (repository / "service.py").write_text(
        """class Service:
    async def run(self, item: int):
        return item

def helper(value, default=1):
    return value or default
""",
        encoding="utf-8",
    )
    (repository / "client.ts").write_text(
        """export interface Client { id: string }
export function connect(url: string) { return url }
""",
        encoding="utf-8",
    )
    (repository / "worker.rs").write_text(
        """pub struct Worker {}
pub fn execute(task: &str) { println!(\"{}\", task); }
""",
        encoding="utf-8",
    )
    ignored = repository / "node_modules"
    ignored.mkdir()
    (ignored / "vendor.js").write_text("function hidden() {}\n", encoding="utf-8")

    result = await context.get_repository_map("sandbox/mapped_repo")
    payload = json.loads(result.message)
    files = {entry["path"]: entry for entry in payload["files"]}

    assert result.is_success is True
    assert payload["file_count"] == 3
    assert payload["languages"] == {"typescript": 1, "python": 1, "rust": 1}
    assert "node_modules/vendor.js" not in files
    python_symbols = {symbol["name"]: symbol for symbol in files["service.py"]["symbols"]}
    assert python_symbols["Service"]["line"] == 1
    assert python_symbols["Service.run"]["kind"] == "async_function"
    assert "item: int" in python_symbols["Service.run"]["signature"]
    assert python_symbols["helper"]["line"] == 5
    assert {symbol["name"] for symbol in files["client.ts"]["symbols"]} == {
        "Client",
        "connect",
    }
    assert {symbol["name"] for symbol in files["worker.rs"]["symbols"]} == {
        "Worker",
        "execute",
    }


@pytest.mark.asyncio
async def test_repository_map_reports_parse_errors_and_global_symbol_limit(os_client):
    context = HostOSCodingContext(os_client)
    repository = os_client.sandbox_dir / "limited_map"
    repository.mkdir()
    (repository / "a.py").write_text(
        "def first(): pass\ndef second(): pass\n", encoding="utf-8"
    )
    (repository / "b.py").write_text("def broken(:\n", encoding="utf-8")

    result = await context.get_repository_map(
        "sandbox/limited_map", max_symbols=1
    )
    payload = json.loads(result.message)

    assert result.is_success is True
    assert payload["symbol_count"] == 1
    assert payload["symbols_truncated"] is True

    parse_result = await context.get_repository_map(
        "sandbox/limited_map", max_symbols=20
    )
    parse_payload = json.loads(parse_result.message)
    broken = next(item for item in parse_payload["files"] if item["path"] == "b.py")
    assert "SyntaxError" in broken["parse_error"]


@pytest.mark.asyncio
async def test_repository_map_enforces_serialized_character_budget(os_client):
    context = HostOSCodingContext(os_client)
    repository = os_client.sandbox_dir / "budgeted_map"
    repository.mkdir()
    for number in range(20):
        (repository / f"module_{number}.py").write_text(
            "\n".join(
                f"def function_{number}_{item}(argument: str): pass"
                for item in range(10)
            ),
            encoding="utf-8",
        )
    os_client.config.file_read_max_chars = 500

    result = await context.get_repository_map("sandbox/budgeted_map")
    payload = json.loads(result.message)

    assert result.is_success is True
    assert payload["output_truncated"] is True
    assert payload["serialized_chars"] <= 1500
    assert len(result.message) <= 1500


@pytest.mark.asyncio
async def test_locate_code_symbol_uses_python_ast_and_prioritizes_definitions(os_client):
    context = HostOSCodingContext(os_client)
    repository = os_client.sandbox_dir / "symbol_repo"
    repository.mkdir()
    (repository / "library.py").write_text(
        '''class Engine:
    def run(self):
        return "run in a string is not a reference"

def run():
    return Engine().run()

# run in a comment is not a reference
''',
        encoding="utf-8",
    )
    (repository / "consumer.py").write_text(
        "from library import run\nresult = run()\n", encoding="utf-8"
    )

    result = await context.locate_code_symbol("run", "sandbox/symbol_repo")
    payload = json.loads(result.message)

    assert result.is_success is True
    assert payload["definition_count"] == 2
    assert payload["reference_count"] == 2
    assert payload["backend_counts"] == {"python_ast": 2}
    assert [item["kind"] for item in payload["results"][:2]] == [
        "definition",
        "definition",
    ]
    assert {item.get("qualified_name") for item in payload["results"][:2]} == {
        "Engine.run",
        "run",
    }
    assert all(item["confidence"] == "syntactic" for item in payload["results"])
    assert not any("comment" in item["preview"] for item in payload["results"])
    assert not any("string" in item["preview"] for item in payload["results"])


@pytest.mark.asyncio
async def test_locate_code_symbol_labels_lexical_fallback_and_bounds_output(
    os_client, monkeypatch
):
    context = HostOSCodingContext(os_client)
    repository = os_client.sandbox_dir / "fallback_repo"
    repository.mkdir()
    (repository / "client.ts").write_text(
        "export function connect(url: string) { return url }\n"
        + "\n".join(f"const value_{item} = connect('url')" for item in range(50)),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        context,
        "_get_tree_sitter_parser",
        lambda language: (None, "forced unavailable backend"),
    )
    os_client.config.file_read_max_chars = 500

    result = await context.locate_code_symbol(
        "connect", "sandbox/fallback_repo", max_results=100
    )
    payload = json.loads(result.message)

    assert result.is_success is True
    assert payload["definition_count"] == 1
    assert payload["reference_count"] == 50
    assert payload["backend_counts"] == {"lexical_fallback": 1}
    assert payload["fallback_reasons"] == ["forced unavailable backend"]
    assert payload["results"][0]["kind"] == "definition"
    assert payload["results"][0]["confidence"] == "lexical"
    assert payload["results_truncated"] is True
    assert payload["output_truncated"] is True
    assert payload["serialized_chars"] <= 1000
    assert len(result.message) <= 1000


@pytest.mark.asyncio
async def test_locate_code_symbol_uses_available_tree_sitter_parser(
    os_client, monkeypatch
):
    class FakeNode:
        def __init__(
            self,
            node_type,
            start_byte,
            end_byte,
            start_point,
            end_point,
            children=(),
            name_node=None,
        ):
            self.type = node_type
            self.start_byte = start_byte
            self.end_byte = end_byte
            self.start_point = start_point
            self.end_point = end_point
            self.children = list(children)
            self._name_node = name_node

        def child_by_field_name(self, field):
            return self._name_node if field == "name" else None

    definition_name = FakeNode("identifier", 9, 16, (0, 9), (0, 16))
    reference = FakeNode("identifier", 22, 29, (1, 0), (1, 7))
    function = FakeNode(
        "function_declaration",
        0,
        21,
        (0, 0),
        (0, 21),
        children=[definition_name],
        name_node=definition_name,
    )
    root = FakeNode(
        "program", 0, 31, (0, 0), (1, 9), children=[function, reference]
    )

    class FakeParser:
        def parse(self, source):
            assert source == b"function connect() {}\nconnect()\n"
            return type("Tree", (), {"root_node": root})()

    context = HostOSCodingContext(os_client)
    repository = os_client.sandbox_dir / "parsed_repo"
    repository.mkdir()
    (repository / "client.js").write_text(
        "function connect() {}\nconnect()\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        context, "_get_tree_sitter_parser", lambda language: (FakeParser(), None)
    )

    result = await context.locate_code_symbol("connect", "sandbox/parsed_repo")
    payload = json.loads(result.message)

    assert result.is_success is True
    assert payload["backend_counts"] == {"tree_sitter": 1}
    assert payload["fallback_reasons"] == []
    assert payload["definition_count"] == 1
    assert payload["reference_count"] == 1
    assert [item["kind"] for item in payload["results"]] == [
        "definition",
        "reference",
    ]
    assert all(item["confidence"] == "syntactic" for item in payload["results"])


@pytest.mark.asyncio
async def test_locate_code_symbol_validates_identifier_and_limits(os_client):
    context = HostOSCodingContext(os_client)

    invalid_symbol = await context.locate_code_symbol("run(); rm", "sandbox")
    invalid_limit = await context.locate_code_symbol(
        "run", "sandbox", max_results=1001
    )

    assert invalid_symbol.is_success is False
    assert "dotted identifier" in invalid_symbol.message
    assert invalid_limit.is_success is False
    assert "max_results" in invalid_limit.message
