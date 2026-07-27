import pytest
from src.l3_agent.context.builder import ContextBuilder
from src.l0_state.agent.state import AgentState
from src.l3_agent.context.registry import ContextRegistry, ContextSection
from src.utils.settings import ContextBudgetConfig
from src.l3_agent.hooks.lifecycle import HookPhase, LifecycleHooks
from src.l3_agent.goals.manager import GoalManager


@pytest.mark.asyncio
async def test_context_builder_build():
    """Тест: ContextBuilder успешно делегирует сборку реестру и склеивает результаты."""
    agent_state = AgentState()
    agent_state.llm_model = "test-model"

    registry = ContextRegistry()

    # Добавляем фейковый провайдер, как это делают интерфейсы
    async def fake_telethon(**kwargs):
        return "### TELETHON [ON]\nAccount info..."

    registry.register_provider("telethon", fake_telethon, section=ContextSection.INTERFACES)

    builder = ContextBuilder(agent_state=agent_state, registry=registry)

    payload = {"chat_id": 123, "text": "Hello Agent"}
    context = await builder.build(event_name="TEST_EVENT", payload=payload, missed_events=[])

    # Проверяем, что в итоговом тексте есть куски от всех систем
    assert "## SKILLS" in context
    assert "### TELETHON [ON]" in context
    assert "## EVENT LOG" in context
    assert "## CURRENT TRIGGER" in context
    assert "TEST_EVENT" in context
    assert "text: Hello Agent" in context


@pytest.mark.asyncio
async def test_context_registry_resilience():
    """Тест: Если один провайдер падает, реестр игнорирует его и отдает остальные."""
    registry = ContextRegistry()
    agent_state = AgentState()

    async def success_provider(**kwargs):
        return "Успешный блок"

    async def failing_provider(**kwargs):
        raise ValueError("Критическая ошибка БД/Сети")

    registry.register_provider("good", success_provider, section=ContextSection.DRIVES)
    registry.register_provider("bad", failing_provider, section=ContextSection.SKILLS)

    results = await registry.gather_all("EVENT", {}, [], agent_state=agent_state)

    # Реестр должен проглотить ошибку failing_provider и вернуть только good
    assert "good" in results
    assert results["good"] == "Успешный блок"
    assert "bad" not in results

def test_context_builder_format_single_event_proactive():
    """Тест: Сборщик контекста подставляет проактивный промпт только если включен тумблер."""
    agent_state = AgentState()
    builder = ContextBuilder(agent_state, ContextRegistry())

    # Proactive OFF (по умолчанию)
    agent_state.proactive_guidance = False
    res_off = builder._format_single_event("HEARTBEAT", {})
    assert "Proactive action execution is recommended" not in res_off

    # Proactive ON
    agent_state.proactive_guidance = True
    res_on = builder._format_single_event("HEARTBEAT", {})
    assert "Proactive action execution is recommended" in res_on
    assert "Activity vectors may include" in res_on


@pytest.mark.asyncio
async def test_adaptive_context_routes_coding_and_event_namespaces(monkeypatch):
    captured = {}

    def fake_library(*args, **kwargs):
        captured.update(kwargs)
        return "adaptive library"

    monkeypatch.setattr(
        "src.l3_agent.context.builder.get_skills_library", fake_library
    )
    state = AgentState(current_goal="Fix the repository tests")
    builder = ContextBuilder(
        state,
        ContextRegistry(),
        budget_config=ContextBudgetConfig(
            enabled=True,
            skill_policy="adaptive",
            skills_max_chars=4000,
            max_dynamic_chars=8000,
        ),
    )

    await builder.build(
        "TELETHON_MESSAGE_INCOMING",
        {"message": "Исправь код и запусти тесты"},
        [],
    )

    assert captured["include_omitted_index"] is True
    assert captured["max_chars"] == 4000
    assert "SkillCatalog" in captured["prefixes"]
    assert "Telethon" in captured["prefixes"]
    assert "HostOSCoding" in captured["prefixes"]
    assert "HostOSExecution" in captured["prefixes"]


@pytest.mark.asyncio
async def test_context_budget_preserves_newest_evidence_and_current_trigger(monkeypatch):
    monkeypatch.setattr(
        "src.l3_agent.context.builder.get_skills_library",
        lambda *args, **kwargs: "SkillCatalog.search_skills(query, limit=12)",
    )
    registry = ContextRegistry()

    async def ticks(**kwargs):
        return "## RECENT TICKS\n" + ("old evidence\n" * 1200) + "NEWEST_EVIDENCE"

    async def hypotheses(**kwargs):
        return "## CLUSTERS OF HYPOTHESES\n" + ("hypothesis\n" * 800)

    async def interface(**kwargs):
        return "### LARGE INTERFACE\n" + ("state\n" * 600)

    registry.register_provider("sql_ticks", ticks, ContextSection.RECENT_TICKS)
    registry.register_provider(
        "sql_hypotheses", hypotheses, ContextSection.HYPOTHESES
    )
    registry.register_provider("large_interface", interface, ContextSection.INTERFACES)
    builder = ContextBuilder(
        AgentState(),
        registry,
        budget_config=ContextBudgetConfig(
            enabled=True,
            skill_policy="adaptive",
            max_dynamic_chars=8000,
            skills_max_chars=2000,
            recent_ticks_max_chars=3000,
            hypotheses_max_chars=1000,
            provider_max_chars=1200,
        ),
    )

    context = await builder.build(
        "TELETHON_MESSAGE_INCOMING",
        {"message": "CURRENT_TRIGGER_SENTINEL"},
        [],
    )

    assert len(context) <= 8000
    assert "NEWEST_EVIDENCE" in context
    assert "CURRENT_TRIGGER_SENTINEL" in context
    assert "context budget truncation" in context
    assert builder.last_build_metrics["original_chars"] > len(context)
    assert {"sql_ticks", "sql_hypotheses", "large_interface"} <= set(
        builder.last_build_metrics["trimmed_providers"]
    )


@pytest.mark.asyncio
async def test_context_budget_is_hard_when_skills_and_heartbeat_exceed_it(monkeypatch):
    monkeypatch.setattr(
        "src.l3_agent.context.builder.get_skills_library",
        lambda *args, **kwargs: "SkillCatalog.search_skills(query, limit=12)\n"
        + ("skill docs\n" * 900),
    )
    builder = ContextBuilder(
        AgentState(),
        ContextRegistry(),
        budget_config=ContextBudgetConfig(
            enabled=True,
            skill_policy="adaptive",
            max_dynamic_chars=8000,
            skills_max_chars=12000,
            provider_max_chars=10000,
        ),
    )

    context = await builder.build(
        "TELETHON_MESSAGE_INCOMING",
        {"message": ("large current request " * 500) + "CURRENT_TRIGGER_TAIL"},
        [],
    )

    assert len(context) <= 8000
    assert "SkillCatalog.search_skills" in context
    assert "CURRENT_TRIGGER_TAIL" in context
    assert {"skills", "heartbeat"} <= set(
        builder.last_build_metrics["trimmed_providers"]
    )


@pytest.mark.asyncio
async def test_context_compaction_emits_observational_boundary_hooks(monkeypatch):
    monkeypatch.setattr(
        "src.l3_agent.context.builder.get_skills_library",
        lambda *args, **kwargs: "large skills\n" + ("x" * 5000),
    )
    observed = []
    hooks = LifecycleHooks(fail_closed=True)

    async def observer(context):
        observed.append(context)
        return False

    hooks.subscribe(HookPhase.PRE_CONTEXT_COMPACTION, observer)
    hooks.subscribe(HookPhase.POST_CONTEXT_COMPACTION, observer)
    builder = ContextBuilder(
        AgentState(),
        ContextRegistry(),
        budget_config=ContextBudgetConfig(
            enabled=True,
            max_dynamic_chars=8000,
            skills_max_chars=2000,
            provider_max_chars=1000,
        ),
        hooks=hooks,
    )

    context = await builder.build("TEST_EVENT", {"message": "keep me"}, [])

    assert [item.phase for item in observed] == [
        HookPhase.PRE_CONTEXT_COMPACTION,
        HookPhase.POST_CONTEXT_COMPACTION,
    ]
    assert observed[0].tool_name == "Context.compaction"
    assert observed[1].outcome["final_chars"] == len(context)
    assert builder.last_build_metrics["hook_failures"] == []


@pytest.mark.asyncio
async def test_context_without_trimming_does_not_emit_compaction_hooks():
    observed = []
    hooks = LifecycleHooks()

    async def observer(context):
        observed.append(context.phase)

    hooks.subscribe(HookPhase.PRE_CONTEXT_COMPACTION, observer)
    builder = ContextBuilder(
        AgentState(),
        ContextRegistry(),
        budget_config=ContextBudgetConfig(enabled=True, max_dynamic_chars=60000),
        hooks=hooks,
    )

    await builder.build("TEST_EVENT", {}, [])

    assert observed == []


@pytest.mark.asyncio
async def test_active_goal_uses_compact_context_projection(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.l3_agent.context.builder.get_skills_library",
        lambda *args, **kwargs: "GoalSkills.get_goal(goal_id='')\n"
        + ("skill\n" * 2000),
    )
    state = AgentState()
    manager = GoalManager(
        tmp_path / "goals.json",
        state,
        compact_context=True,
        compact_max_chars=8000,
    )
    await manager.create("Finish a bounded repository task")
    registry = ContextRegistry()

    async def hypotheses(**kwargs):
        return "HYPOTHESIS_NOISE\n" + ("noise\n" * 3000)

    async def ticks(**kwargs):
        return "OLD_TICKS\n" + ("old\n" * 3000) + "NEWEST_GOAL_EVIDENCE"

    registry.register_provider(
        "active_goal", manager.get_context_block, ContextSection.AGENT_STATE
    )
    registry.register_provider(
        "sql_hypotheses", hypotheses, ContextSection.HYPOTHESES
    )
    registry.register_provider("sql_ticks", ticks, ContextSection.RECENT_TICKS)
    builder = ContextBuilder(
        state,
        registry,
        goal_manager=manager,
        budget_config=ContextBudgetConfig(enabled=False),
    )

    context = await builder.build(
        "TELETHON_MESSAGE_INCOMING",
        {"message": "Continue exact goal"},
        [],
    )

    assert len(context) <= 8000
    assert "## ACTIVE GOAL" in context
    assert "Continue exact goal" in context
    assert "NEWEST_GOAL_EVIDENCE" in context
    assert "HYPOTHESIS_NOISE" not in context
    assert builder.last_build_metrics["policy"] == "goal_compact"
