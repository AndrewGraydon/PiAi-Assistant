"""
Home automation stub tool for PiAi Assistant.

When n8n is enabled, home automation should be handled as an n8n webhook
workflow (e.g. a Home Assistant workflow in n8n). This in-process stub
acts as a fallback when n8n is disabled.

To wire up real home automation without n8n:
  Replace the __call__ body with direct Home Assistant REST API calls:
    POST http://{ha_host}/api/services/{domain}/{service}
    Headers: Authorization: Bearer {ha_token}
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class HomeTool:
    def __call__(self, action: str = "", device: str = "") -> str:
        """
        Stub home automation tool.
        Returns a message explaining that home automation is not configured.
        """
        log.info("HomeTool stub called: action=%r, device=%r", action, device)
        return (
            f"Home automation is not yet configured. "
            f"I received a request to {action} the {device}, but cannot action it. "
            f"To enable home automation, configure an n8n workflow or connect Home Assistant."
        )
