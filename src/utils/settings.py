"""
Settings and Configurations Loader of the JAWL Framework.

Provides strict Pydantic model schemas for all settings.yaml and interfaces.yaml
configurations. Implements safe YAML parsing with duplicate key detection,
automatic config migration, and environment-driven override fallbacks.
"""

import shutil
import yaml
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from yaml.constructor import ConstructorError

from src.utils.logger import main_logger

# ==========================================
# Models for interfaces.yaml
# ==========================================


class CodingCommandProfileConfig(BaseModel):
    name: str = Field(
        min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    argv: list[str] = Field(min_length=1, max_length=128)
    relative_cwd: str = Field(default=".", min_length=1, max_length=1000)
    timeout_seconds: int | None = Field(default=None, ge=1, le=7200)
    container_profile: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, argv: list[str]) -> list[str]:
        if any(not item or "\x00" in item or len(item) > 4096 for item in argv):
            raise ValueError(
                "coding command profile argv items must be non-empty, NUL-free, "
                "and at most 4096 characters"
            )
        if sum(len(item) for item in argv) > 32768:
            raise ValueError("coding command profile argv exceeds 32768 characters")
        return argv


class CodingContainerProfileConfig(BaseModel):
    """Operator-declared reusable OCI isolation policy."""

    name: str = Field(
        min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    image: str = Field(
        min_length=1,
        max_length=500,
        pattern=(
            r"^(?:[A-Za-z0-9.-]+(?::[0-9]+)?/)?"
            r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*"
            r"(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]{64})?$"
        ),
    )
    network: Literal["none", "bridge"] = "none"
    memory_mb: int = Field(default=2048, ge=128, le=32768)
    cpus: float = Field(default=2.0, ge=0.1, le=32.0)
    pids: int = Field(default=256, ge=16, le=4096)


class HostOSConfig(BaseModel):
    enabled: bool = False
    desktop_interactions: bool = False

    access_level: int = 0
    env_access: bool = False

    require_deploy_sessions: bool = True
    deploy_max_retries: int = 5

    framework_tree_depth: int = 1

    monitoring_interval_sec: int = 30
    execution_timeout_sec: int = 60
    coding_execution_backend: Literal["disabled", "host", "container"] = "disabled"
    coding_host_allowed_commands: list[str] = Field(default_factory=list)
    coding_approval_mode: Literal["disabled", "required"] = "disabled"
    coding_approval_ttl_sec: int = Field(default=900, ge=60, le=86400)
    coding_command_profiles: list[CodingCommandProfileConfig] = Field(
        default_factory=list, max_length=100
    )
    coding_container_profiles: list[CodingContainerProfileConfig] = Field(
        default_factory=list, max_length=100
    )
    coding_container_runtime: Literal["docker", "podman"] = "docker"
    coding_container_image: str = "python:3.11-slim"
    coding_container_network: Literal["none", "bridge"] = "none"
    coding_container_memory_mb: int = Field(default=2048, ge=128, le=32768)
    coding_container_cpus: float = Field(default=2.0, ge=0.1, le=32.0)
    coding_container_pids: int = Field(default=256, ge=16, le=4096)
    file_read_max_chars: int = 10000
    file_list_limit: int = 100
    top_processes_limit: int = 10
    file_diff_max_chars: int = 300

    workspace_max_opened_files: int = 10
    recent_file_changes_limit: int = 5
    workspace_max_file_chars: int = 10000

    @model_validator(mode="after")
    def validate_coding_profile_names(self) -> "HostOSConfig":
        names = [profile.name for profile in self.coding_command_profiles]
        if len(names) != len(set(names)):
            raise ValueError("coding command profile names must be unique")
        container_names = [
            profile.name for profile in self.coding_container_profiles
        ]
        if len(container_names) != len(set(container_names)):
            raise ValueError("coding container profile names must be unique")
        available = set(container_names)
        missing = sorted(
            {
                profile.container_profile
                for profile in self.coding_command_profiles
                if profile.container_profile
                and profile.container_profile not in available
            }
        )
        if missing:
            raise ValueError(
                "coding command profiles reference unknown container profiles: "
                + ", ".join(missing)
            )
        return self


class HostTerminalConfig(BaseModel):
    enabled: bool = True
    history_limit: int = 50
    context_limit: int = 10


class HostConfig(BaseModel):
    os: HostOSConfig = Field(default_factory=HostOSConfig)
    terminal: HostTerminalConfig = Field(default_factory=HostTerminalConfig)


class TelethonConfig(BaseModel):
    enabled: bool = False
    session_name: str = "agent_telethon"
    recent_chats_limit: int = 20
    private_chat_history_limit: int = 3
    incoming_history_limit: int = 8
    coding_approval_chat_id: int | str | None = None

    @field_validator("coding_approval_chat_id")
    @classmethod
    def validate_approval_chat_id(cls, value: int | str | None):
        if isinstance(value, str):
            value = value.strip()
            if not value or len(value) > 100:
                raise ValueError(
                    "coding_approval_chat_id must be a non-empty bounded chat ID"
                )
        return value


class AiogramConfig(BaseModel):
    enabled: bool = False
    recent_chats_limit: int = 20
    coding_approval_chat_id: int | str | None = None

    @field_validator("coding_approval_chat_id")
    @classmethod
    def validate_approval_chat_id(cls, value: int | str | None):
        if isinstance(value, str):
            value = value.strip()
            if not value or len(value) > 100:
                raise ValueError(
                    "coding_approval_chat_id must be a non-empty bounded chat ID"
                )
        return value


class TelegramConfig(BaseModel):
    telethon: TelethonConfig = Field(default_factory=TelethonConfig)
    aiogram: AiogramConfig = Field(default_factory=AiogramConfig)


class GithubConfig(BaseModel):
    enabled: bool = False
    agent_account: bool = False
    request_timeout_sec: int = 15
    history_limit: int = 10
    polling_interval_sec: int = 180


class EmailConfig(BaseModel):
    enabled: bool = False
    polling_interval_sec: int = 60
    recent_limit: int = 5


class DeepResearchConfig(BaseModel):
    max_queries: int = 10
    max_results_per_query: int = 5
    max_pages_to_read: int = 15
    total_max_chars: int = 30000


class WebSearchConfig(BaseModel):
    enabled: bool = True
    search_engine: str = "duckduckgo"
    reader_engine: str = "jina"
    request_timeout_sec: int = 15
    max_page_chars: int = 10000
    deep_research: DeepResearchConfig = Field(default_factory=DeepResearchConfig)


class WebHTTPConfig(BaseModel):
    enabled: bool = True
    request_timeout_sec: int = 15
    max_response_chars: int = 10000


class WebBrowserConfig(BaseModel):
    enabled: bool = False
    headless: bool = True
    timeout_sec: int = 30
    idle_timeout_sec: int = 900


class WebHooksConfig(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    history_limit: int = 20
    preview_max_chars: int = 200


class RSSFeedConfig(BaseModel):
    name: str
    url: str


class WebRSSConfig(BaseModel):
    enabled: bool = False
    polling_interval_sec: int = 3600
    recent_limit: int = 5
    feeds: list[RSSFeedConfig] = Field(default_factory=list)


class WebConfig(BaseModel):
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    http: WebHTTPConfig = Field(default_factory=WebHTTPConfig)
    browser: WebBrowserConfig = Field(default_factory=WebBrowserConfig)
    hooks: WebHooksConfig = Field(default_factory=WebHooksConfig)
    rss: WebRSSConfig = Field(default_factory=WebRSSConfig)


class MetaConfig(BaseModel):
    enabled: bool = False
    access_level: int = 0
    custom_skills_enabled: bool = True


class CodeGraphConfig(BaseModel):
    enabled: bool = False
    exclude_dirs: list[str] = Field(
        default_factory=lambda: [
            "venv",
            ".venv",
            "env",
            "__pycache__",
            ".git",
            "node_modules",
            ".pytest_cache",
            "dist",
            "build",
        ]
    )
    max_search_results: int = 5
    max_structure_items: int = 100


class MultimodalityConfig(BaseModel):
    enabled: bool = False


class CalendarConfig(BaseModel):
    enabled: bool = True
    polling_interval_sec: int = 60
    upcoming_events_limit: int = 10


class ElevenLabsConfig(BaseModel):
    enabled: bool = False
    tts_model: str = "eleven_multilingual_v2"
    main_voice: str = "CwhRBWXzGAHq8TQ4Fs17"
    available_voices: list[str] = Field(default_factory=list)
    stability: float = 0.5
    similarity_boost: float = 0.75


class EdgeConfig(BaseModel):
    enabled: bool = False
    main_voice: str = "ru-RU-SvetlanaNeural"
    available_voices: list[str] = Field(default_factory=list)
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"


class CloudWhisperConfig(BaseModel):
    enabled: bool = False
    model: str = "whisper-1"
    temperature: float = 0.0
    timeout_sec: int = 120


class CloudSTTConfig(BaseModel):
    whisper: CloudWhisperConfig = Field(default_factory=CloudWhisperConfig)


class STTConfig(BaseModel):
    cloud: CloudSTTConfig = Field(default_factory=CloudSTTConfig)


class CloudTTSConfig(BaseModel):
    elevenlabs: ElevenLabsConfig = Field(default_factory=ElevenLabsConfig)
    edge: EdgeConfig = Field(default_factory=EdgeConfig)


class TTSConfig(BaseModel):
    cloud: CloudTTSConfig = Field(default_factory=CloudTTSConfig)


class VoiceInterfacesConfig(BaseModel):
    stt: STTConfig = Field(default_factory=STTConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)


class InterfacesConfig(BaseModel):
    host: HostConfig = Field(default_factory=HostConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    github: GithubConfig = Field(default_factory=GithubConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    meta: MetaConfig = Field(default_factory=MetaConfig)
    code_graph: CodeGraphConfig = Field(default_factory=CodeGraphConfig)
    multimodality: MultimodalityConfig = Field(default_factory=MultimodalityConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    voice: VoiceInterfacesConfig = Field(default_factory=VoiceInterfacesConfig)


# ==========================================
# Models for settings.yaml
# ==========================================


class IdentityConfig(BaseModel):
    agent_name: str = "Agent"


class LLMConfig(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    main_model: str = "unknown"
    available_models: list[str] = Field(default_factory=list)
    is_multimodal: bool = False
    temperature: float = 1.0
    max_react_steps: int = 15
    thinking_policy: Literal[
        "provider_default", "always", "never", "first_step"
    ] = "provider_default"
    tool_transport: Literal["wrapper", "native", "hybrid"] = "wrapper"
    native_tool_prefixes: list[str] = Field(
        default_factory=lambda: [
            "HostOSCoding",
            "HostOSReader",
            "HostOSSearch",
            "HostOSEditor",
        ]
    )
    native_tool_limit: int = Field(default=64, ge=1, le=128)


class LoggingConfig(BaseModel):
    max_file_size_mb: float = 5.0
    backup_count: int = 1


class VectorDBConfig(BaseModel):
    similarity_threshold: float = 0.65
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    vector_size: int = 384


class RAGConfig(BaseModel):
    enabled: bool = True
    extraction_engine: str = "flashtext"
    depth_limit: int = 2
    max_vector_blocks: int = 5
    max_graph_nodes: int = 5
    max_query_chars: int = 200


class ContextBudgetConfig(BaseModel):
    enabled: bool = False
    max_dynamic_chars: int = Field(default=60000, ge=8000, le=500000)
    skill_policy: Literal["full", "adaptive"] = "full"
    skills_max_chars: int = Field(default=12000, ge=2000, le=100000)
    recent_ticks_max_chars: int = Field(default=24000, ge=2000, le=200000)
    hypotheses_max_chars: int = Field(default=5000, ge=500, le=50000)
    provider_max_chars: int = Field(default=10000, ge=1000, le=100000)


class ContextDepthConfig(BaseModel):
    high_ticks: int = 3
    medium_ticks: int = 7
    low_ticks: int = 20

    tick_action_max_chars: int = 10000
    tick_result_max_chars: int = 20000
    tick_thoughts_short_max_chars: int = 1000
    tick_action_short_max_chars: int = 100
    tick_result_short_max_chars: int = 500

    budget: ContextBudgetConfig = Field(default_factory=ContextBudgetConfig)
    rag: RAGConfig = Field(default_factory=RAGConfig)


class EventAccelerationConfig(BaseModel):
    active_cycle_policy: Literal["interrupt", "defer", "append"] = "interrupt"
    queue_max_events: int = Field(default=100, ge=10, le=1000)
    coalesce_window_sec: float = Field(default=2.0, ge=0.0, le=60.0)
    coalesce_payload_samples: int = Field(default=3, ge=1, le=20)
    coalesce_event_names: list[str] = Field(
        default_factory=lambda: [
            "AIOGRAM_CHAT_ACTION",
            "HOST_OS_FILE_DELETED",
            "OS_FILE_CREATED",
            "OS_FILE_MODIFIED",
            "REACT_TICK_SAVED",
            "SYSTEM_DASHBOARD_UPDATE",
            "TELETHON_CHAT_ACTION",
        ]
    )
    critical_multiplier: float = 0.0
    high_multiplier: float = 0.2
    medium_multiplier: float = 0.6
    low_multiplier: float = 0.7
    background_multiplier: float = 0.8


class LifecycleCommandHookConfig(BaseModel):
    name: str = Field(
        min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    phase: Literal[
        "pre_tool_use",
        "post_tool_use",
        "tool_error",
        "tool_cancelled",
        "pre_context_compaction",
        "post_context_compaction",
        "pre_system_stop",
        "post_system_stop",
        "pre_delegation",
        "post_delegation",
        "delegation_error",
        "delegation_cancelled",
    ]
    argv: list[str] = Field(min_length=1, max_length=64)
    tool_patterns: list[str] = Field(
        default_factory=lambda: ["*"], min_length=1, max_length=64
    )
    scope: Literal["user", "repository"] = "user"
    working_directory: Literal["framework", "workspace"] = "framework"
    priority: int = Field(default=0, ge=-1000, le=1000)
    deny_exit_code: int = Field(default=10, ge=1, le=255)


class LifecycleHooksConfig(BaseModel):
    enabled: bool = False
    fail_closed: bool = True
    handler_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    command_timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    max_output_chars: int = Field(default=8000, ge=500, le=100000)
    commands: list[LifecycleCommandHookConfig] = Field(
        default_factory=list, max_length=100
    )


class TasksConfig(BaseModel):
    enabled: bool = True
    max_tasks: int = 10


class PersonalityTraitsConfig(BaseModel):
    enabled: bool = True
    max_traits: int = 10


class MentalStatesConfig(BaseModel):
    enabled: bool = True
    max_entities: int = 10


class DriveDecayConfig(BaseModel):
    rate: float = 10.0
    interval_sec: int = 900


class FundamentalDriveConfig(BaseModel):
    enabled: bool = True
    decay: DriveDecayConfig = Field(default_factory=DriveDecayConfig)


class FundamentalDrivesConfig(BaseModel):
    curiosity: FundamentalDriveConfig = Field(default_factory=FundamentalDriveConfig)
    social: FundamentalDriveConfig = Field(default_factory=FundamentalDriveConfig)
    mastery: FundamentalDriveConfig = Field(default_factory=FundamentalDriveConfig)


class DrivesConfig(BaseModel):
    enabled: bool = True
    dynamic_reduction: bool = True
    pause_on_offline: bool = True
    max_reflections_history: int = 4
    max_custom_drives: int = 5
    fundamental: FundamentalDrivesConfig = Field(default_factory=FundamentalDrivesConfig)


class NotesConfig(BaseModel):
    enabled: bool = True
    max_notes: int = 5


class HypothesesConfig(BaseModel):
    enabled: bool = True
    max_clusters: int = 3
    max_hypotheses: int = 10


class SQLConfig(BaseModel):
    tasks: TasksConfig = Field(default_factory=TasksConfig)
    personality_traits: PersonalityTraitsConfig = Field(
        default_factory=PersonalityTraitsConfig
    )
    mental_states: MentalStatesConfig = Field(default_factory=MentalStatesConfig)
    drives: DrivesConfig = Field(default_factory=DrivesConfig)
    notes: NotesConfig = Field(default_factory=NotesConfig)
    hypotheses: HypothesesConfig = Field(default_factory=HypothesesConfig)


class GraphDBConfig(BaseModel):
    max_nodes: int = 5000
    max_edges_per_node: int = 20


class DBConfig(BaseModel):
    sql: SQLConfig = Field(default_factory=SQLConfig)
    vector: VectorDBConfig = Field(default_factory=VectorDBConfig)
    graph: GraphDBConfig = Field(default_factory=GraphDBConfig)


class SwarmContextDepthConfig(BaseModel):
    max_steps: int = 20
    detailed_steps: int = 5
    action_max_chars: int = 10000
    result_max_chars: int = 20000
    thoughts_short_max_chars: int = 2000
    action_short_max_chars: int = 500
    result_short_max_chars: int = 1000


class SwarmConfig(BaseModel):
    enabled: bool = False
    subagent_model: str = "unknown"
    max_concurrent_workers: int = 3
    context_depth: SwarmContextDepthConfig = Field(default_factory=SwarmContextDepthConfig)


class TreeOfThoughtsConfig(BaseModel):
    enabled: bool = False
    llm_model: str = "unknown"
    mode: str = "hybrid"
    auto_interval_steps: int = 5

    branches: int = 3
    simulations_per_branch: int = 2
    max_depth: int = 2


class SubconsciousPatternConfig(BaseModel):
    enabled: bool = False
    activation_limit_ticks: int = 30


class SubconsciousPatternsConfig(BaseModel):
    consolidation: SubconsciousPatternConfig = Field(
        default_factory=lambda: SubconsciousPatternConfig(
            enabled=True, activation_limit_ticks=30
        )
    )
    reflection: SubconsciousPatternConfig = Field(
        default_factory=lambda: SubconsciousPatternConfig(
            enabled=True, activation_limit_ticks=60
        )
    )
    forgetting: SubconsciousPatternConfig = Field(
        default_factory=lambda: SubconsciousPatternConfig(
            enabled=True, activation_limit_ticks=90
        )
    )


class SubconsciousConfig(BaseModel):
    enabled: bool = False
    llm_model: str = "unknown"
    patterns: SubconsciousPatternsConfig = Field(default_factory=SubconsciousPatternsConfig)


class SystemConfig(BaseModel):
    timezone: int = 0
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    db: DBConfig = Field(default_factory=DBConfig)
    heartbeat_interval: int = 300
    continuous_cycle: bool = False
    proactive_guidance: bool = False
    event_acceleration: EventAccelerationConfig = Field(
        default_factory=EventAccelerationConfig
    )
    lifecycle_hooks: LifecycleHooksConfig = Field(default_factory=LifecycleHooksConfig)
    context_depth: ContextDepthConfig = Field(default_factory=ContextDepthConfig)
    swarm: SwarmConfig = Field(default_factory=SwarmConfig)
    tree_of_thoughts: TreeOfThoughtsConfig = Field(default_factory=TreeOfThoughtsConfig)
    subconscious: SubconsciousConfig = Field(default_factory=SubconsciousConfig)


class SettingsConfig(BaseModel):
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    system: SystemConfig = Field(default_factory=SystemConfig)


# ==========================================
# Loader
# ==========================================


class UniqueKeyLoader(yaml.SafeLoader):
    """Custom YAML loader that fails with an exception on duplicate keys."""

    pass


def construct_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                None,
                None,
                f"Duplicate key '{key}' detected in YAML file. Fix the configuration.",
                key_node.start_mark,
            )
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
)


def load_yaml(file_path: Path) -> dict:
    """Safely reads a YAML file and returns a dictionary with auto-fix encoding."""
    if not file_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {file_path}")

    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            return yaml.load(f, Loader=UniqueKeyLoader) or {}
    except UnicodeDecodeError:
        with open(file_path, "r", encoding="cp1251") as f:
            return yaml.load(f, Loader=UniqueKeyLoader) or {}


def _log_missing_defaults(model: BaseModel, prefix: str = "", file_name: str = ""):
    """
    Recursively checks which fields were set to defaults (not passed by the user),
    and outputs a clean warning to the log.
    """
    missing_keys = set(model.model_fields.keys()) - model.model_fields_set

    if missing_keys:
        keys_str = ", ".join(f"'{prefix}{k}'" for k in missing_keys)
        main_logger.debug(
            f"[{file_name}] Missing configuration for {keys_str}. Default values applied."
        )

    for key, value in model.__dict__.items():
        if isinstance(value, BaseModel):
            _log_missing_defaults(value, prefix=f"{prefix}{key}.", file_name=file_name)


def _deep_update_ruamel(base_map, user_map) -> bool:
    """
    Recursively traverses the base dictionary (example) and adds
    missing keys to the user dictionary.
    """

    from ruamel.yaml.comments import CommentedMap

    modified = False
    if isinstance(base_map, CommentedMap) and isinstance(user_map, CommentedMap):
        for key in base_map:
            if key not in user_map:
                user_map[key] = base_map[key]
                modified = True
            else:
                if _deep_update_ruamel(base_map[key], user_map[key]):
                    modified = True
    return modified


def _sync_yaml_file(user_file: Path, example_file: Path) -> None:
    """
    Smart synchronization: if the user's YAML is missing new fields,
    they will be copied from the .example file, preserving old settings.
    """
    if not example_file.exists():
        return

    if not user_file.exists():
        shutil.copy(example_file, user_file)
        main_logger.info(f"[Config] Created base configuration file {user_file.name}")
        return

    from ruamel.yaml import YAML

    ryaml = YAML()
    ryaml.preserve_quotes = True

    try:
        with open(example_file, "r", encoding="utf-8") as f:
            example_data = ryaml.load(f)
        with open(user_file, "r", encoding="utf-8") as f:
            user_data = ryaml.load(f)

        if _deep_update_ruamel(example_data, user_data):
            with open(user_file, "w", encoding="utf-8") as f:
                ryaml.dump(user_data, f)
            main_logger.info(
                f"[Config] File {user_file.name} automatically updated (added new fields from template)."
            )

    except Exception as e:
        main_logger.error(f"[Config] Error auto-updating {user_file.name}: {e}")


def load_config() -> tuple[SettingsConfig, InterfacesConfig]:
    """
    Loads, validates, and automatically heals settings from YAML files if necessary.
    """

    base_dir = Path.cwd() / "config"

    settings_file = base_dir / "settings.yaml"
    settings_example = base_dir / "settings.example.yaml"

    interfaces_file = base_dir / "interfaces.yaml"
    interfaces_example = base_dir / "interfaces.example.yaml"

    _sync_yaml_file(settings_file, settings_example)
    _sync_yaml_file(interfaces_file, interfaces_example)

    settings_data = load_yaml(settings_file)
    interfaces_data = load_yaml(interfaces_file)

    settings_config = SettingsConfig(**settings_data)
    interfaces_config = InterfacesConfig(**interfaces_data)

    return settings_config, interfaces_config
