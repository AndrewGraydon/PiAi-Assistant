"""
Configuration loader for PiAi Assistant.

Loads config.yaml for structural settings and .env for secrets.
All relative paths are resolved to absolute paths at load time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


# ---------------------------------------------------------------------------
# Dataclasses — one per config.yaml section
# ---------------------------------------------------------------------------

@dataclass
class LLMConfig:
    host: str
    temperature: float
    top_k: int
    enable_thinking: bool
    poll_interval_ms: int
    max_tool_rounds: int


@dataclass
class ASRConfig:
    host: str
    language: str
    timeout_s: int


@dataclass
class TTSConfig:
    host: str
    voice: str
    speed: float
    language: str
    sample_rate: int


@dataclass
class DisplaySocketConfig:
    host: str
    port: int


@dataclass
class VisionConfig:
    provider: str               # "placeholder" | "gemini" | "openai" | "anthropic"
    cloud_host: Optional[str]
    api_key: Optional[str] = None  # injected from .env at load time


@dataclass
class N8NConfig:
    enabled: bool
    url: str
    webhook_base: str
    token: str                  # may be overridden by N8N_TOKEN env var
    timeout_s: int


@dataclass
class AudioConfig:
    sample_rate: int
    channels: int
    device_name: Optional[str]
    vad_aggressiveness: int
    silence_timeout_s: float
    max_record_s: float


@dataclass
class MemoryConfig:
    enabled: bool
    db_path: str                # resolved to absolute path
    collection: str
    embedding_model: str
    top_k: int
    score_threshold: float
    default_hint_ttl_s: int


@dataclass
class ToolsConfig:
    data_cache_size: int
    capture_image_enabled: bool
    home_automation_enabled: bool


@dataclass
class AssistantConfig:
    name: str
    system_prompt: str
    tools_enabled: bool
    thinking_indicator: bool
    wake_word: Optional[str] = None


@dataclass
class DisplayThemeConfig:
    brightness: int
    idle_emoji: str
    listen_emoji: str
    think_emoji: str
    speak_emoji: str
    idle_color: str
    listen_color: str
    think_color: str
    speak_color: str


@dataclass
class BatteryConfig:
    enabled: bool
    host: str           # pisugar-server TCP host (usually 127.0.0.1)
    port: int           # pisugar-server TCP port (8423)
    poll_interval_s: float


@dataclass
class PathsConfig:
    recordings_dir: str         # all resolved to absolute paths
    tts_dir: str
    captures_dir: str
    logs_dir: str


@dataclass
class Config:
    llm: LLMConfig
    asr: ASRConfig
    tts: TTSConfig
    display_socket: DisplaySocketConfig
    vision: VisionConfig
    n8n: N8NConfig
    audio: AudioConfig
    memory: MemoryConfig
    tools: ToolsConfig
    assistant: AssistantConfig
    display_theme: DisplayThemeConfig
    battery: BatteryConfig
    paths: PathsConfig


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_config(config_path: str = "config.yaml") -> Config:
    """
    Load configuration from config_path (YAML) and the .env file.

    .env is searched for in the same directory as config_path, then CWD.
    All relative paths in the config are resolved relative to the directory
    that contains config_path.
    """
    # ---- Resolve config file location ----
    config_path = os.path.abspath(config_path)
    if not os.path.exists(config_path):
        raise ConfigError(f"Config file not found: {config_path}")

    project_root = os.path.dirname(config_path)

    # ---- Load .env (secrets) ----
    env_path = os.path.join(project_root, ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path, override=False)
    else:
        load_dotenv(override=False)  # fall back to CWD / already-set env vars

    # ---- Parse YAML ----
    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    def _resolve(relative: str) -> str:
        """Resolve a path relative to the project root."""
        return os.path.abspath(os.path.join(project_root, relative))

    def _get(d: dict, *keys, default=None, required=False):
        """Nested dict accessor with optional requirement enforcement."""
        val = d
        for k in keys:
            if not isinstance(val, dict) or k not in val:
                if required:
                    raise ConfigError(f"Missing required config key: {'.'.join(str(k) for k in keys)}")
                return default
            val = val[k]
        return val

    svc = raw.get("services", {})

    # ---- LLM ----
    llm_raw = svc.get("llm", {})
    llm = LLMConfig(
        host=llm_raw.get("host", "http://localhost:8000"),
        temperature=float(llm_raw.get("temperature", 0.7)),
        top_k=int(llm_raw.get("top_k", 40)),
        enable_thinking=bool(llm_raw.get("enable_thinking", False)),
        poll_interval_ms=int(llm_raw.get("poll_interval_ms", 500)),
        max_tool_rounds=int(llm_raw.get("max_tool_rounds", 5)),
    )

    # ---- ASR ----
    asr_raw = svc.get("asr", {})
    asr = ASRConfig(
        host=asr_raw.get("host", "http://localhost:8801"),
        language=asr_raw.get("language", "en"),
        timeout_s=int(asr_raw.get("timeout_s", 120)),
    )

    # ---- TTS ----
    tts_raw = svc.get("tts", {})
    tts = TTSConfig(
        host=tts_raw.get("host", "http://localhost:8803"),
        voice=tts_raw.get("voice", "af_heart"),
        speed=float(tts_raw.get("speed", 1.0)),
        language=tts_raw.get("language", "en"),
        sample_rate=int(tts_raw.get("sample_rate", 24000)),
    )

    # ---- Display socket ----
    disp_raw = svc.get("display", {})
    display_socket = DisplaySocketConfig(
        host=disp_raw.get("host", "127.0.0.1"),
        port=int(disp_raw.get("port", 12345)),
    )

    # ---- Vision ----
    vis_raw = svc.get("vision", {})
    vision_provider = vis_raw.get("provider", "placeholder").lower()
    # Inject API key from env based on provider
    vision_api_key: Optional[str] = None
    if vision_provider == "gemini":
        vision_api_key = os.environ.get("GEMINI_API_KEY") or None
    elif vision_provider == "openai":
        vision_api_key = os.environ.get("OPENAI_API_KEY") or None
    elif vision_provider == "anthropic":
        vision_api_key = os.environ.get("ANTHROPIC_API_KEY") or None

    vision = VisionConfig(
        provider=vision_provider,
        cloud_host=vis_raw.get("cloud_host"),
        api_key=vision_api_key,
    )

    # ---- n8n ----
    n8n_raw = raw.get("n8n", {})
    # N8N_TOKEN env var overrides config.yaml token
    n8n_token = os.environ.get("N8N_TOKEN") or n8n_raw.get("token", "")
    n8n = N8NConfig(
        enabled=bool(n8n_raw.get("enabled", False)),
        url=n8n_raw.get("url", "http://localhost:5678"),
        webhook_base=n8n_raw.get("webhook_base", "http://localhost:5678/webhook"),
        token=n8n_token,
        timeout_s=int(n8n_raw.get("timeout_s", 30)),
    )

    # ---- Audio ----
    audio_raw = raw.get("audio", {})
    audio = AudioConfig(
        sample_rate=int(audio_raw.get("sample_rate", 16000)),
        channels=int(audio_raw.get("channels", 1)),
        device_name=audio_raw.get("device_name") or None,
        vad_aggressiveness=int(audio_raw.get("vad_aggressiveness", 2)),
        silence_timeout_s=float(audio_raw.get("silence_timeout_s", 2.0)),
        max_record_s=float(audio_raw.get("max_record_s", 30.0)),
    )

    # ---- Memory ----
    mem_raw = raw.get("memory", {})
    memory = MemoryConfig(
        enabled=bool(mem_raw.get("enabled", True)),
        db_path=_resolve(mem_raw.get("db_path", "./data/chroma")),
        collection=mem_raw.get("collection", "assistant_memory"),
        embedding_model=mem_raw.get("embedding_model", "all-MiniLM-L6-v2"),
        top_k=int(mem_raw.get("top_k", 3)),
        score_threshold=float(mem_raw.get("score_threshold", 0.65)),
        default_hint_ttl_s=int(mem_raw.get("default_hint_ttl_s", 604800)),
    )

    # ---- Tools ----
    tools_raw = raw.get("tools", {})
    builtin_raw = tools_raw.get("builtin", {})
    tools = ToolsConfig(
        data_cache_size=int(tools_raw.get("data_cache_size", 3)),
        capture_image_enabled=bool(builtin_raw.get("capture_image", True)),
        home_automation_enabled=bool(builtin_raw.get("home_automation", True)),
    )

    # ---- Assistant ----
    asst_raw = raw.get("A", {})
    assistant = AssistantConfig(
        name=asst_raw.get("name", "PiAi"),
        system_prompt=asst_raw.get("system_prompt", "You are a helpful assistant.").rstrip(),
        tools_enabled=bool(asst_raw.get("tools_enabled", True)),
        thinking_indicator=bool(asst_raw.get("thinking_indicator", True)),
        wake_word=asst_raw.get("wake_word") or None,
    )

    # ---- Display theme ----
    dt_raw = raw.get("display", {})
    display_theme = DisplayThemeConfig(
        brightness=int(dt_raw.get("brightness", 80)),
        idle_emoji=dt_raw.get("idle_emoji", "😴"),
        listen_emoji=dt_raw.get("listen_emoji", "😐"),
        think_emoji=dt_raw.get("think_emoji", "🤔"),
        speak_emoji=dt_raw.get("speak_emoji", "🗣️"),
        idle_color=dt_raw.get("idle_color", "#000055"),
        listen_color=dt_raw.get("listen_color", "#00ff00"),
        think_color=dt_raw.get("think_color", "#ff6800"),
        speak_color=dt_raw.get("speak_color", "#0055ff"),
    )

    # ---- Battery ----
    batt_raw = raw.get("battery", {})
    battery = BatteryConfig(
        enabled=bool(batt_raw.get("enabled", True)),
        host=batt_raw.get("host", "127.0.0.1"),
        port=int(batt_raw.get("port", 8423)),
        poll_interval_s=float(batt_raw.get("poll_interval_s", 30)),
    )

    # ---- Paths (all resolved to absolute) ----
    paths_raw = raw.get("paths", {})
    paths = PathsConfig(
        recordings_dir=_resolve(paths_raw.get("recordings_dir", "./data/recordings")),
        tts_dir=_resolve(paths_raw.get("tts_dir", "./data/tts")),
        captures_dir=_resolve(paths_raw.get("captures_dir", "./data/captures")),
        logs_dir=_resolve(paths_raw.get("logs_dir", "./data/logs")),
    )

    return Config(
        llm=llm,
        asr=asr,
        tts=tts,
        display_socket=display_socket,
        vision=vision,
        n8n=n8n,
        audio=audio,
        memory=memory,
        tools=tools,
        assistant=assistant,
        display_theme=display_theme,
        battery=battery,
        paths=paths,
    )
