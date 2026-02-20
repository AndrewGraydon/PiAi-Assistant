"""
n8n webhook tool integration for PiAi Assistant.

Based on the CAAL project's n8n integration pattern.

Architecture:
  - n8n workflows are discovered via the n8n REST API
  - Each active workflow with a webhook trigger becomes a tool
  - Tools are executed by POSTing to {webhook_base}/{workflow_name}
  - Credentials stay in n8n (never visible to LLM or orchestrator)

n8n response contract (workflows must return this format):
  {
    "message": "Spoken response for TTS",         // required
    "data": {...},                                  // optional: cached in ToolDataCache
    "memory_hint": {"key": "value"}               // optional: auto-stored in ChromaDB
  }

Tool description extraction:
  The webhook trigger node's "notes" field is used as the tool description.
  This is the standard CAAL convention — document tools in n8n, not in code.

Naming convention:
  Workflow name (sanitised to lowercase_underscores) becomes the tool name.
  e.g. "Google Tasks" → "google_tasks"
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)


def _sanitise_name(name: str) -> str:
    """Convert a workflow name to a safe tool name: lowercase, underscores."""
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    name = name.strip("_")
    return name or "workflow"


class N8NClient:
    """
    Discovers n8n workflows and executes them as tools.
    Gracefully skips all operations if n8n.enabled = false.
    """

    def __init__(
        self,
        enabled: bool,
        url: str,
        webhook_base: str,
        token: str,
        timeout_s: int,
    ) -> None:
        self.enabled = enabled
        self.url = url.rstrip("/")
        self.webhook_base = webhook_base.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

        # Maps sanitised tool name → original workflow name
        self._name_map: Dict[str, str] = {}
        # Maps sanitised tool name → tool description
        self._descriptions: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_workflows(self) -> List[Dict]:
        """
        Query n8n for active workflows with webhook triggers.
        Returns a list of tool definition dicts:
          {"name": str, "description": str}

        Also populates self._name_map for use during execution.
        """
        if not self.enabled:
            return []

        self._name_map.clear()
        self._descriptions.clear()

        workflows = self._fetch_workflows()
        tools = []

        for wf in workflows:
            if not wf.get("active", False):
                continue

            name = wf.get("name", "")
            if not name:
                continue

            # Check for a webhook trigger node
            description = self._extract_description(wf)
            if description is None:
                # No webhook trigger found — skip
                continue

            tool_name = _sanitise_name(name)
            self._name_map[tool_name] = name
            self._descriptions[tool_name] = description

            tools.append({
                "name": tool_name,
                "description": description,
            })
            log.info("n8n tool discovered: %s → %r", tool_name, description[:80])

        log.info("n8n: discovered %d workflow tools", len(tools))
        return tools

    def _fetch_workflows(self) -> List[Dict]:
        """GET /api/v1/workflows from n8n."""
        headers = {}
        if self.token:
            headers["X-N8N-API-KEY"] = self.token

        try:
            resp = requests.get(
                f"{self.url}/api/v1/workflows",
                headers=headers,
                timeout=10.0,
                params={"limit": 100},
            )
            resp.raise_for_status()
            data = resp.json()
            # n8n v1 API returns {"data": [...]} or just [...]
            if isinstance(data, dict):
                return data.get("data", [])
            return data
        except Exception as e:
            log.warning("n8n workflow discovery failed: %s", e)
            return []

    def _extract_description(self, workflow: Dict) -> Optional[str]:
        """
        Extract a tool description from a workflow's webhook trigger node.
        Returns None if no webhook trigger is found.

        Priority:
          1. Webhook trigger node "notes" field
          2. Webhook trigger node "description" field
          3. Workflow root "description" field
          4. Workflow name (last resort)
        """
        nodes = workflow.get("nodes", [])
        webhook_node = None

        for node in nodes:
            node_type = node.get("type", "")
            if "webhook" in node_type.lower() and "trigger" in node_type.lower():
                webhook_node = node
                break
            # Also match n8n.nodes.base.Webhook
            if node_type in ("n8n-nodes-base.webhook", "n8n-nodes-base.webhookTrigger"):
                webhook_node = node
                break

        if webhook_node is None:
            return None

        # Try notes field on the node
        notes = webhook_node.get("notes", "").strip()
        if notes:
            return notes

        # Try description on the node parameters
        params = webhook_node.get("parameters", {})
        desc = params.get("description", "").strip()
        if desc:
            return desc

        # Try workflow root description
        wf_desc = workflow.get("description", "").strip()
        if wf_desc:
            return wf_desc

        # Fall back to workflow name
        return f"Run the '{workflow.get('name', 'workflow')}' automation"

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, tool_name: str, arguments: Dict) -> Any:
        """
        Execute an n8n workflow via its webhook.
        Returns the parsed response dict, or an error string.

        The response should follow the contract:
          {"message": str, "data": {...}, "memory_hint": {...}}
        """
        if not self.enabled:
            return "n8n integration is not enabled."

        original_name = self._name_map.get(tool_name, tool_name)
        # Webhook paths use the sanitised name (lowercase_underscores)
        webhook_url = f"{self.webhook_base}/{tool_name}"

        log.info("n8n execute: POST %s  args=%s", webhook_url, json.dumps(arguments)[:200])

        try:
            resp = requests.post(
                webhook_url,
                json=arguments,
                timeout=self.timeout_s,
            )
            resp.raise_for_status()

            # Try to parse JSON response
            try:
                data = resp.json()
            except ValueError:
                # Plain text response — wrap it
                return {"message": resp.text.strip()}

            return data

        except requests.Timeout:
            log.error("n8n workflow '%s' timed out after %ds", tool_name, self.timeout_s)
            return f"The {original_name} workflow timed out. Please try again."
        except requests.HTTPError as e:
            log.error("n8n workflow '%s' HTTP error: %s", tool_name, e)
            return f"The {original_name} workflow returned an error."
        except requests.RequestException as e:
            log.error("n8n webhook request failed: %s", e)
            return f"Could not reach the {original_name} workflow."

    def is_n8n_tool(self, tool_name: str) -> bool:
        """Return True if tool_name is a discovered n8n workflow."""
        return tool_name in self._name_map

    def get_descriptions(self) -> Dict[str, str]:
        return dict(self._descriptions)
