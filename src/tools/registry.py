"""
Tool registry and ToolDataCache for PiAi Assistant.

ToolRegistry: maps tool names to callables. The orchestrator calls
  registry.call(name, arguments) and gets back a string result.

ToolDataCache (from CAAL): a rolling buffer of the last N tool
  call results. Injected into each LLM generate() call as context so
  the model can answer follow-up questions without re-calling tools.
  e.g. "What was the temperature again?" after a weather tool call.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolDataCache
# ---------------------------------------------------------------------------

class ToolDataCache:
    """
    Rolling cache of the last max_size tool call results.
    Injected as a system-level context block before each LLM generate() call.
    """

    def __init__(self, max_size: int = 3) -> None:
        self._cache: deque = deque(maxlen=max_size)

    def add(self, tool_name: str, arguments: Dict, result: Any) -> None:
        """Store a tool call result. result can be a string or a dict."""
        self._cache.append({
            "tool": tool_name,
            "args": arguments,
            "result": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
        })

    def get_context_block(self) -> str:
        """
        Returns a formatted context string to prepend to the next LLM prompt.
        Empty string if cache is empty.
        """
        if not self._cache:
            return ""

        lines = ["Recent tool results (use these to answer follow-up questions):"]
        for entry in self._cache:
            args_str = json.dumps(entry["args"], ensure_ascii=False) if entry["args"] else ""
            result_preview = entry["result"][:200]
            if len(entry["result"]) > 200:
                result_preview += "..."
            lines.append(f"  - {entry['tool']}({args_str}) → {result_preview}")

        return "\n".join(lines)

    def clear(self) -> None:
        self._cache.clear()


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """
    Registry of callable tools. Each tool is registered with a name,
    a description string, and a callable that accepts keyword arguments.
    """

    def __init__(self) -> None:
        self._tools: Dict[str, Callable] = {}
        self._descriptions: Dict[str, str] = {}

    def register(self, name: str, fn: Callable, description: str = "") -> None:
        self._tools[name] = fn
        self._descriptions[name] = description
        log.info("Tool registered: %s", name)

    def call(self, name: str, arguments: Dict) -> str:
        """
        Execute a named tool with the given arguments dict.
        Returns the result as a string.
        Unknown tools return an error string (not an exception).
        """
        if name not in self._tools:
            log.warning("Unknown tool called: '%s' — available: %s", name, list(self._tools))
            return f"Error: tool '{name}' is not available."

        try:
            log.info("Calling tool: %s(%s)", name, json.dumps(arguments, ensure_ascii=False)[:100])
            result = self._tools[name](**arguments)
            # Ensure result is a string
            if isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False)
            return str(result)
        except TypeError as e:
            log.error("Tool '%s' argument error: %s", name, e)
            return f"Error: invalid arguments for tool '{name}': {e}"
        except Exception as e:
            log.exception("Tool '%s' raised an exception: %s", name, e)
            return f"Error executing tool '{name}': {e}"

    def get_names(self) -> List[str]:
        return list(self._tools.keys())

    def get_descriptions(self) -> Dict[str, str]:
        return dict(self._descriptions)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
