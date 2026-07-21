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
