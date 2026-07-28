"""
Global Skills Registry and Guard Layer.

Handles registration of native and custom functions, dynamic creation of Pydantic
models for argument validation (protection against LLM hallucinations), and
Role-Based Access Control (RBAC) for subagents.
"""

import inspect
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable, Dict, Any, TypeVar, List, get_type_hints
import logging

from pydantic import create_model, BaseModel, ValidationError

from src.utils.logger import agent_logger
from src.utils._tools import truncate_text
from src.utils.settings import SubconsciousConfig

from src.l3_agent.skills.schema import ActionCall
from src.l3_agent.skills.execution import ActionExecutionEngine
from src.l3_agent.hooks.lifecycle import LifecycleHooks
from src.l3_agent.skills.journal import ActionJournal, NullActionJournal
from src.l3_agent.swarm.roles import SubagentRole
from src.l3_agent.subconscious.schema import Pattern


@dataclass
class SkillResult:
    """
    Standardized response of any agent tool.
    """

    is_success: bool
    message: str

    @classmethod
    def ok(cls, message: str) -> "SkillResult":
        return cls(is_success=True, message=message)

    @classmethod
    def fail(cls, message: str) -> "SkillResult":
        return cls(is_success=False, message=message)


_REGISTRY: Dict[str, Dict[str, Any]] = {}
_NATIVE_TOOL_INDEX: Dict[str, str] = {}
_ACTION_ENGINE = ActionExecutionEngine()
_SKILL_PARAMETER_ALIASES: Dict[str, Dict[str, str]] = {
    # Qwen frequently uses the generic chat field name even after discovering
    # the exact terminal signature. Keep the public schema canonical while
    # repairing this one unambiguous transport-level synonym.
    "HostTerminalMessages.send_message_to_terminal": {"message": "text"},
}


def configure_action_journal(path: Path) -> ActionJournal:
    """Configure durable action journaling once the runtime data path is known."""

    journal = ActionJournal(path)
    _ACTION_ENGINE.set_journal(journal)
    return journal


def configure_lifecycle_hooks(hooks: LifecycleHooks) -> LifecycleHooks:
    """Attach one lifecycle policy layer to main, Swarm, and Subconscious actions."""

    _ACTION_ENGINE.set_hooks(hooks)
    return hooks


async def execute_action_plan(actions: List[ActionCall], runner: Callable) -> list:
    """Execute actions through the shared deterministic engine.

    Main, Swarm, and Subconscious callers provide their own RBAC-aware runner
    while sharing dependency handling, resource locks, and cancellation rules.
    """

    return await _ACTION_ENGINE.execute(actions, runner)


def clear_registry() -> None:
    """Clears the global registry (called during agent reboot)."""
    _REGISTRY.clear()
    _NATIVE_TOOL_INDEX.clear()
    _ACTION_ENGINE.journal = NullActionJournal()
    _ACTION_ENGINE.hooks = LifecycleHooks()


def unregister_skill(skill_name: str) -> None:
    """Removes a skill from the registry by name."""
    if skill_name in _REGISTRY:
        del _REGISTRY[skill_name]


def _build_skill_name(
    func: Callable[..., Any], override: Optional[str] = None, instance: Optional[Any] = None
) -> str:
    """
    Generates a clean function name (removing system prefixes like src.l2_interfaces...).
    """

    if override:
        return override

    if instance:
        return f"{instance.__class__.__name__}.{func.__name__}"

    segments = func.__module__.split(".")
    useless = {"src", "l0_state", "l1_databases", "l2_interfaces", "skills", "l3_agent"}
    clean_segments = [s for s in segments if s not in useless]

    return ".".join(clean_segments) + f".{func.__name__}"


def _create_pydantic_guard(func: Callable[..., Any], skill_name: str) -> type[BaseModel]:
    """
    Dynamically generates a Pydantic model (Guard) based on the function signature (type-hints).
    Provides Type Coercion and protects against junk parameters.
    """

    target = inspect.unwrap(func)
    sig = inspect.signature(target)
    try:
        resolved_hints = get_type_hints(target)
    except (NameError, TypeError):
        resolved_hints = {}
    fields = {}

    for name, param in sig.parameters.items():
        if name == "self":
            continue

        annotation = resolved_hints.get(
            name,
            param.annotation if param.annotation is not inspect.Parameter.empty else Any,
        )
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[name] = (annotation, default)

    safe_name = skill_name.replace(".", "_") + "_Guard"
    return create_model(safe_name, **fields)


def _register_callable(
    func: Callable[..., Any],
    override: Optional[str] = None,
    instance: Optional[Any] = None,
    swarm: Optional[List[SubagentRole]] = None,
    subconscious: Optional[List[Pattern]] = None,
    hidden: bool = False,
) -> None:
    """
    Internal method to register a Python function in the registry.
    """

    skill_name = _build_skill_name(func, override, instance)
    sig = inspect.signature(func)

    formatted_params = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        param_str = str(param)
        if param.default is inspect.Parameter.empty and param.kind not in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            param_str += " <REQ>"
        formatted_params.append(param_str)

    clean_sig = f"({', '.join(formatted_params)})"
    raw_doc = inspect.getdoc(func) or "No description."
    clean_doc = " ".join(raw_doc.split())

    doc_str = f"{skill_name}{clean_sig} - {clean_doc}"

    _REGISTRY[skill_name] = {
        "func": func,
        "guard": _create_pydantic_guard(func, skill_name),
        "instance": instance,
        "doc_string": doc_str,
        "is_custom": False,
        "swarm": swarm or [],
        "subconscious": subconscious or [],
        "hidden": hidden,
    }
    log = f"[Skills] Registered skill: {skill_name}"
    agent_logger.debug(log)


def register_custom_callable(
    func: Callable[..., Any], skill_name: str, description: str, filepath: str
) -> None:
    """
    Registers a custom proxy skill (script from the sandbox).
    """

    sig = inspect.signature(func)
    formatted_params = []
    for name, param in sig.parameters.items():
        param_str = f"{name}: {param.annotation.__name__ if hasattr(param.annotation, '__name__') else param.annotation}"
        if param.default is inspect.Parameter.empty:
            param_str += " <REQ>"
        formatted_params.append(param_str)

    clean_sig = f"({', '.join(formatted_params)})"
    clean_doc = " ".join(description.split())
    doc_str = f"`{skill_name}{clean_sig}` - {clean_doc} [File: {filepath}]"

    _REGISTRY[skill_name] = {
        "func": func,
        "guard": _create_pydantic_guard(func, skill_name),
        "instance": None,
        "doc_string": doc_str,
        "is_custom": True,
        "swarm": [],
        "subconscious": [],
        "hidden": False,
    }
    log = f"[Skills] Registered custom skill: {skill_name}"
    agent_logger.info(log)


F = TypeVar("F", bound=Callable[..., Any])


def skill(
    name_override: Optional[str] = None,
    swarm: Optional[List[SubagentRole]] = None,
    subconscious: Optional[List[Pattern]] = None,
    hidden: bool = False,
) -> Callable[[F], F]:
    """
    Decorator that automatically registers a new skill for the agent.
    Extracts docstring, arguments, and their types, forming the context block 'function(arg1: type) - docstring'.

    Args:
        name_override: Name override (defaults to 'Class.func_name').
        swarm: Array of subagents authorized to invoke this skill (RBAC).
        subconscious: Array of subconscious patterns authorized to invoke this skill.
        hidden: If True, hides the skill from the main agent prompt (useful for system-only tasks).
    """

    def decorator(func: F) -> F:
        sig = inspect.signature(func)
        if "self" in sig.parameters:
            setattr(func, "__is_skill__", True)
            setattr(func, "__skill_name_override__", name_override)
            setattr(func, "__swarm__", swarm)
            setattr(func, "__subconscious__", subconscious)
            setattr(func, "__skill_hidden__", hidden)
            return func
        _register_callable(
            func, name_override, swarm=swarm, subconscious=subconscious, hidden=hidden
        )
        return func

    return decorator


def register_instance(instance: Any) -> None:
    """Registers all decorated methods inside the passed class instance."""
    for attr_name in dir(instance):
        method = getattr(instance, attr_name)
        if callable(method) and getattr(method, "__is_skill__", False):
            override = getattr(method, "__skill_name_override__", None)
            swarm = getattr(method, "__swarm__", None)
            subconscious = getattr(method, "__subconscious__", None)
            hidden = getattr(method, "__skill_hidden__", False)
            _register_callable(
                method,
                override,
                instance,
                swarm=swarm,
                subconscious=subconscious,
                hidden=hidden,
            )


def _visible_skill_items(
    subconscious_config: Optional[SubconsciousConfig] = None,
) -> List[tuple[str, Dict[str, Any]]]:
    """Return registry items using the same visibility rules for every transport."""

    visible = []
    for skill_name in sorted(_REGISTRY.keys()):
        data = _REGISTRY[skill_name]
        if data.get("hidden", False):
            continue
        if subconscious_config and subconscious_config.enabled:
            patterns_list = data.get("subconscious", [])
            if any(
                (cfg := getattr(subconscious_config.patterns, pattern.value, None))
                and cfg.enabled
                for pattern in patterns_list
            ):
                continue
        func = data["func"]
        instance = data["instance"]
        req_level = getattr(func, "__required_os_level__", None)
        if req_level is not None and instance is not None:
            host_os = getattr(instance, "host_os", None)
            if host_os is not None and host_os.access_level.value < req_level:
                continue
        visibility_check = getattr(func, "__visibility_check__", None)
        if visibility_check and instance is not None:
            try:
                if not visibility_check(instance):
                    continue
            except Exception as exc:
                agent_logger.warning(
                    f"[Skills Registry] Exception in __visibility_check__ for "
                    f"{skill_name}: {exc}"
                )
                continue
        visible.append((skill_name, data))
    return visible


def _native_tool_name(skill_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", skill_name).strip("_") or "skill"
    digest = hashlib.sha256(skill_name.encode("utf-8")).hexdigest()[:10]
    return f"jawl_{slug[:47]}_{digest}"[:64]


def get_native_tools_schema(
    prefixes: Optional[List[str]] = None,
    limit: int = 64,
    subconscious_config: Optional[SubconsciousConfig] = None,
) -> List[Dict[str, Any]]:
    """Export visible registered skills as reversible OpenAI native tools."""

    if limit < 1 or limit > 128:
        raise ValueError("native tool limit must be between 1 and 128")
    normalized_prefixes = [prefix for prefix in (prefixes or []) if prefix]
    selected = [
        (name, data)
        for name, data in _visible_skill_items(subconscious_config)
        if not normalized_prefixes
        or any(name.startswith(prefix) for prefix in normalized_prefixes)
    ]
    if len(selected) > limit:
        raise ValueError(
            f"Native tool selection contains {len(selected)} skills, exceeding "
            f"the configured limit of {limit}. Narrow native_tool_prefixes."
        )
    tools = []
    for skill_name, data in selected:
        native_name = _native_tool_name(skill_name)
        _NATIVE_TOOL_INDEX[native_name] = skill_name
        parameters = data["guard"].model_json_schema()
        parameters.pop("title", None)
        parameters.setdefault("type", "object")
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": native_name,
                    "description": truncate_text(
                        data["doc_string"], max_chars=1000, suffix="..."
                    ),
                    "parameters": parameters,
                },
            }
        )
    return tools


def resolve_native_tool_name(name: str) -> str:
    """Resolve an encoded native name while leaving legacy skill names intact."""

    return _NATIVE_TOOL_INDEX.get(name, name)


def build_tools_schema(
    transport: str = "wrapper",
    native_prefixes: Optional[List[str]] = None,
    native_limit: int = 64,
    subconscious_config: Optional[SubconsciousConfig] = None,
) -> List[Dict[str, Any]]:
    """Build wrapper, native, or hybrid provider schemas on demand."""

    from src.l3_agent.skills.schema import ACTION_SCHEMA

    if transport not in {"wrapper", "native", "hybrid"}:
        raise ValueError("tool transport must be wrapper, native, or hybrid")
    wrapper = list(ACTION_SCHEMA) if transport in {"wrapper", "hybrid"} else []
    native = (
        get_native_tools_schema(
            prefixes=native_prefixes,
            limit=native_limit,
            subconscious_config=subconscious_config,
        )
        if transport in {"native", "hybrid"}
        else []
    )
    return wrapper + native


def _format_skill_docs(items: List[tuple[str, Dict[str, Any]]]) -> str:
    """Format an already selected list while preserving namespace separation."""
    formatted_docs = []
    last_prefix = ""
    custom_docs = []
    for skill_name, data in items:
        doc = data["doc_string"]
        if data.get("is_custom"):
            custom_docs.append(doc)
            continue
        prefix = skill_name.split(".", 1)[0] if "." in skill_name else ""
        if last_prefix and prefix != last_prefix:
            formatted_docs.append("")
        formatted_docs.append(doc)
        last_prefix = prefix
    base = "\n".join(formatted_docs)
    if custom_docs:
        base += "\n\n### CUSTOM SKILLS\n" + "\n".join(custom_docs)
    return base.strip()


def get_skill_namespace_index(
    items: Optional[List[tuple[str, Dict[str, Any]]]] = None,
) -> str:
    """Return a compact namespace/count index for hierarchical discovery."""
    counts: Dict[str, int] = {}
    source_items = items if items is not None else _visible_skill_items()
    for skill_name, _ in source_items:
        namespace = skill_name.split(".", 1)[0]
        counts[namespace] = counts.get(namespace, 0) + 1
    return ", ".join(f"{name}({counts[name]})" for name in sorted(counts))


def search_skill_docs(
    query: str,
    limit: int = 12,
    subconscious_config: Optional[SubconsciousConfig] = None,
) -> List[str]:
    """Search visible skill names and descriptions with deterministic ranking."""
    if limit < 1 or limit > 50:
        raise ValueError("skill search limit must be between 1 and 50")
    normalized = " ".join(query.lower().split())
    if len(normalized) < 2:
        raise ValueError("skill search query must contain at least 2 characters")
    terms = normalized.split()
    ranked = []
    for skill_name, data in _visible_skill_items(subconscious_config):
        name = skill_name.lower()
        doc = data["doc_string"].lower()
        matched = [term for term in terms if term in doc]
        if not matched:
            continue
        score = len(matched) * 10
        if len(matched) == len(terms):
            score += 50
        score += sum(20 if term in name else 1 for term in matched)
        if normalized in name:
            score += 100
        ranked.append((-score, skill_name, data["doc_string"]))
    ranked.sort()
    return [doc for _, _, doc in ranked[:limit]]


def get_skills_library(
    subconscious_config: Optional[SubconsciousConfig] = None,
    prefixes: Optional[List[str]] = None,
    max_chars: Optional[int] = None,
    include_omitted_index: bool = False,
) -> str:
    """
    Collects all skills. Automatically hides skills if they are delegated to
    an active subconscious pattern to offload the Orchestrator's context.
    """
    visible = _visible_skill_items(subconscious_config)
    normalized_prefixes = [prefix for prefix in (prefixes or []) if prefix]
    if normalized_prefixes:
        selected = [
            item
            for item in visible
            if any(item[0].startswith(prefix) for prefix in normalized_prefixes)
        ]
        selected.sort(
            key=lambda item: (
                min(
                    index
                    for index, prefix in enumerate(normalized_prefixes)
                    if item[0].startswith(prefix)
                ),
                item[0],
            )
        )
    else:
        selected = visible

    omitted = [item for item in visible if item not in selected]
    if max_chars is not None:
        if max_chars < 100:
            raise ValueError("skills context max_chars must be at least 100")
        bounded = []
        for item in selected:
            candidate = _format_skill_docs([*bounded, item])
            if len(candidate) > max_chars:
                omitted.append(item)
                continue
            bounded.append(item)
        selected = bounded

    base = _format_skill_docs(selected)
    if include_omitted_index and omitted:
        while True:
            index = (
                "\n\n### OMITTED SKILL NAMESPACES\n"
                "Use `SkillCatalog.search_skills` to load exact signatures on demand.\n"
                + get_skill_namespace_index(omitted)
            )
            base = _format_skill_docs(selected)
            if max_chars is None or len(base) + len(index) <= max_chars:
                base += index
                break
            if not selected:
                base = index[:max_chars]
                break
            omitted.append(selected.pop())
    return base


async def execute_skill(
    actions: List[ActionCall], logger: logging.Logger = agent_logger
) -> str:
    """
    Executes actions deterministically, with explicit opt-in parallelism.

    Args:
        actions: List of ActionCall objects.
        logger: Destination logger (defaults to agent_logger).
    """
    if not actions:
        return "Cycle completed: no actions provided."

    async def _runner(action: ActionCall) -> SkillResult:
        return await call_skill(action.tool_name, action.parameters, logger=logger)

    outcomes = await execute_action_plan(actions, _runner)
    report = []
    for outcome in outcomes:
        status = "success" if outcome.is_success else "failed"
        report.append(f"* {outcome.tool_name}: {outcome.message}")
        report.append(
            f"  [action_id={outcome.action_id}; status={status}; "
            f"duration_ms={outcome.duration_ms:g}]"
        )
    return "\n".join(report)


async def call_skill(
    name: str, params: Dict[str, Any], logger: logging.Logger = agent_logger
) -> SkillResult:
    """
    Low-level call of an individual skill passing parameters through the Pydantic Guard Layer.
    """
    params_str = truncate_text(str(params), max_chars=250, suffix="... [Params truncated]")

    logger.info(f"[Agent Action] {name}({params_str})")

    item = _REGISTRY.get(name)
    if not item:
        err_msg = f"Skill '{name}' not found."
        logger.warning(f"[Agent Action Result] {err_msg}")
        return SkillResult.fail(err_msg)

    func = item["func"]
    guard_model = item["guard"]

    try:
        normalized_params = dict(params)
        for alias, canonical in _SKILL_PARAMETER_ALIASES.get(name, {}).items():
            if canonical not in normalized_params and alias in normalized_params:
                normalized_params[canonical] = normalized_params.pop(alias)
        validated_params = guard_model(**normalized_params)
        clean_kwargs = validated_params.model_dump()
    except ValidationError as e:
        errors = [
            f"- Parameter '{err['loc'][0] if err['loc'] else 'unknown'}': {err['msg']}"
            for err in e.errors()
        ]
        err_msg = "Parameter validation error:\n" + "\n".join(errors)
        logger.warning(f"[Guard] Rejected call {name}: Type error.")
        return SkillResult.fail(err_msg)
    except Exception as e:
        err_msg = f"Parameter schema error for skill '{name}': {e}"
        logger.error(f"[Guard] {err_msg}")
        return SkillResult.fail(err_msg)

    try:
        result = await func(**clean_kwargs)
        res_msg = truncate_text(
            str(result.message), max_chars=200, suffix="... [Result truncated for logs]"
        )
        status = "Success" if result.is_success else "Fail"

        logger.info(f"[Agent Action Result] {name} ({status}): {res_msg}")
        return result
    except Exception as e:
        logger.error(f"[Agent Action Result] Error in skill {name}: {str(e)}")
        return SkillResult.fail(f"Internal skill error: {str(e)}")
