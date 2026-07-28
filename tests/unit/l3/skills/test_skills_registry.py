import pytest
from typing import Literal
from src.l3_agent.skills import registry
from src.l3_agent.skills.schema import ActionCall
from src.l3_agent.skills.registry import (
    call_skill,
    skill,
    register_instance,
    execute_skill,
    SkillResult,
    get_skills_library,
    build_tools_schema,
    get_native_tools_schema,
    resolve_native_tool_name,
    search_skill_docs,
)
from src.l3_agent.skills.catalog import SkillCatalog

# ===================================================================
# FIXTURES
# ===================================================================


@pytest.fixture(autouse=True)
def clean_registry():
    original_registry = registry._REGISTRY.copy()
    original_native_index = registry._NATIVE_TOOL_INDEX.copy()

    registry._REGISTRY.clear()
    registry._NATIVE_TOOL_INDEX.clear()

    yield

    registry._REGISTRY.clear()
    registry._REGISTRY.update(original_registry)
    registry._NATIVE_TOOL_INDEX.clear()
    registry._NATIVE_TOOL_INDEX.update(original_native_index)


class DummyInterface:
    def __init__(self, prefix: str):
        self.prefix = prefix

    @skill(name_override="mock.class_func")
    async def dummy_class_func(self, text: str) -> SkillResult:
        """Метод класса для теста."""
        return SkillResult.ok(f"{self.prefix}: {text}")

    @skill(name_override="mock.fail_func")
    async def failing_func(self) -> SkillResult:
        return SkillResult.fail("Упс, ошибка")

    async def not_a_skill(self):
        pass


DeferredMode = Literal["fast", "safe"]


class DeferredAnnotationInterface:
    @skill(name_override="mock.deferred_annotation")
    async def run(self, modes: "list[DeferredMode]") -> SkillResult:
        return SkillResult.ok(",".join(modes))


@pytest.fixture
def mock_plain_func():
    """Регистрируем обычную функцию после того, как clean_registry очистит кэш."""

    @skill(name_override="mock.plain_func")
    async def dummy_plain_func(text: str) -> SkillResult:
        return SkillResult.ok(f"Plain: {text}")

    return dummy_plain_func


# ===================================================================
# TESTS
# ===================================================================


@pytest.mark.asyncio
async def test_plain_function_registration(mock_plain_func):
    assert "mock.plain_func" in registry._REGISTRY
    docs = get_skills_library()
    assert "mock.plain_func" in docs


def test_native_schema_is_reversible_typed_and_hybrid_compatible(mock_typed_func):
    native = get_native_tools_schema(prefixes=["mock."], limit=5)
    assert len(native) == 1
    function = native[0]["function"]

    assert len(function["name"]) <= 64
    assert function["name"].startswith("jawl_")
    assert resolve_native_tool_name(function["name"]) == "mock.typed_func"
    assert function["parameters"]["properties"]["my_int"]["type"] == "integer"
    assert "my_list" in function["parameters"]["required"]

    hybrid = build_tools_schema(
        "hybrid", native_prefixes=["mock."], native_limit=5
    )
    assert hybrid[0]["function"]["name"] == "execute_skill"
    assert hybrid[1]["function"]["name"] == function["name"]


def test_native_schema_enforces_selection_limit(mock_plain_func):
    @skill(name_override="mock.second")
    async def second() -> SkillResult:
        return SkillResult.ok("ok")

    with pytest.raises(ValueError, match="exceeding"):
        get_native_tools_schema(prefixes=["mock."], limit=1)


def test_adaptive_skill_library_is_bounded_and_keeps_discovery_path():
    @skill(name_override="HostOSCodingFiles.read")
    async def coding_read(path: str) -> SkillResult:
        """Read a task-relative coding file."""
        return SkillResult.ok(path)

    @skill(name_override="TelethonMessages.send")
    async def telegram_send(text: str) -> SkillResult:
        """Send a Telegram message."""
        return SkillResult.ok(text)

    register_instance(SkillCatalog())

    library = get_skills_library(
        prefixes=["SkillCatalog", "HostOSCoding"],
        max_chars=700,
        include_omitted_index=True,
    )

    assert len(library) <= 700
    assert "SkillCatalog.search_skills" in library
    assert "HostOSCodingFiles.read" in library
    assert "TelethonMessages.send" not in library
    assert "TelethonMessages(1)" in library


@pytest.mark.asyncio
async def test_skill_catalog_returns_exact_signatures_on_demand():
    @skill(name_override="TelethonMessages.send_message")
    async def send_message(chat_id: int, text: str) -> SkillResult:
        """Send text to a Telegram chat."""
        return SkillResult.ok(text)

    register_instance(SkillCatalog())

    docs = search_skill_docs("how telegram send text", limit=5)
    report = await execute_skill(
        [
            ActionCall(
                tool_name="SkillCatalog.search_skills",
                parameters={"query": "telegram send", "limit": 5},
            )
        ]
    )

    assert docs and "TelethonMessages.send_message" in docs[0]
    assert "TelethonMessages.send_message" in report


@pytest.mark.asyncio
async def test_execute_skill_success(mock_plain_func):
    dummy = DummyInterface(prefix="Agent")
    register_instance(dummy)

    actions = [
        ActionCall(tool_name="mock.plain_func", parameters={"text": "Hello"}),
        ActionCall(tool_name="mock.class_func", parameters={"text": "World"}),
    ]

    report = await execute_skill(actions=actions)

    assert "* mock.plain_func: Plain: Hello" in report
    assert "* mock.class_func: Agent: World" in report


@pytest.mark.asyncio
async def test_execute_skill_ignores_extra_kwargs(mock_plain_func):
    actions = [
        ActionCall(
            tool_name="mock.plain_func", parameters={"text": "Valid", "hallucination": 123}
        )
    ]
    report = await execute_skill(actions=actions)
    assert "* mock.plain_func: Plain: Valid" in report


@pytest.mark.asyncio
async def test_terminal_message_alias_is_repaired_without_weakening_schema():
    received = []

    @skill(name_override="HostTerminalMessages.send_message_to_terminal")
    async def send_terminal(text: str) -> SkillResult:
        received.append(text)
        return SkillResult.ok("sent")

    native = get_native_tools_schema(
        prefixes=["HostTerminalMessages.send_message_to_terminal"],
        limit=1,
    )
    result = await call_skill(
        "HostTerminalMessages.send_message_to_terminal",
        {"message": "runtime status"},
    )

    assert result.is_success is True
    assert received == ["runtime status"]
    properties = native[0]["function"]["parameters"]["properties"]
    assert "text" in properties
    assert "message" not in properties


@pytest.mark.asyncio
async def test_execute_skill_mixed_results(mock_plain_func):
    """Тест: оркестратор должен переварить смесь валидных, падающих и несуществующих скиллов."""

    @skill(name_override="mock.fail_func")
    async def fail_func():
        raise RuntimeError("Critical failure")

    actions = [
        ActionCall(tool_name="mock.plain_func", parameters={"text": "A"}),
        ActionCall(tool_name="mock.unknown_func", parameters={}),
        ActionCall(tool_name="mock.fail_func", parameters={}),
    ]

    report = await execute_skill(actions)

    assert "* mock.plain_func: Plain: A" in report
    assert "* mock.unknown_func: Skill 'mock.unknown_func' not found" in report
    assert "* mock.fail_func: Internal skill error: Critical failure" in report


@pytest.fixture
def mock_typed_func():
    @skill(name_override="mock.typed_func")
    async def dummy_typed_func(
        my_list: list[str], my_int: int, my_bool: bool = False
    ) -> SkillResult:
        return SkillResult.ok(f"Int: {my_int}, Bool: {my_bool}, ListLen: {len(my_list)}")

    return dummy_typed_func


@pytest.mark.asyncio
async def test_guard_type_coercion(mock_typed_func):
    """Тест: Pydantic Guard Layer на лету чинит простые типы (Type Coercion)."""
    # LLM присылает строку "42" вместо числа и строку "true" вместо bool
    actions = [
        ActionCall(
            tool_name="mock.typed_func",
            parameters={"my_list": ["a", "b"], "my_int": "42", "my_bool": "true"},
        )
    ]
    report = await execute_skill(actions)

    # Guard должен сам сконвертировать типы и проemptyить вызов
    assert "Int: 42, Bool: True, ListLen: 2" in report


@pytest.mark.asyncio
async def test_guard_resolves_deferred_annotations_from_wrapped_skill_globals():
    register_instance(DeferredAnnotationInterface())

    report = await execute_skill(
        [
            ActionCall(
                tool_name="mock.deferred_annotation",
                parameters={"modes": ["fast", "safe"]},
            )
        ]
    )

    assert "fast,safe" in report


@pytest.mark.asyncio
async def test_guard_validation_error_feedback(mock_typed_func):
    """Тест: Guard Layer отлавливает критические галлюцинации и возвращает понятную ошибку LLM."""
    # LLM галлюцинирует и присылает строку вместо списка
    actions = [
        ActionCall(
            tool_name="mock.typed_func",
            parameters={"my_list": "This is a string, not a list", "my_int": 42},
        )
    ]
    report = await execute_skill(actions)

    # Функция НЕ должна была выполниться, а LLM должна получить фидбек с указанием ошибки
    assert "Parameter validation error" in report
    assert "my_list" in report
    # Ошибка Pydantic о том, что ожидался массив
    assert "valid array" in report.lower() or "valid list" in report.lower()
