"""
Vision provider interface for PiAi Assistant.

Defines the VisionProvider ABC that all vision backends implement.
Currently only PlaceholderVisionProvider exists (returns a canned message).

To add a real provider (e.g. Gemini):
  1. Create GeminiVisionProvider(VisionProvider)
  2. Implement analyze_image() and health_check()
  3. Add an elif branch in create_vision_client()

The placeholder is intentional — vision queries will still go through the
full tool-call pipeline (camera captures, image saved) so swapping to a real
provider later requires only adding the provider class.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import VisionConfig

log = logging.getLogger(__name__)


class VisionProvider(ABC):
    @abstractmethod
    def analyze_image(self, image_path: str, question: str) -> str:
        """
        Analyze an image and answer the question about it.
        Returns a natural-language description suitable for TTS.
        """
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the provider is available."""
        ...


class PlaceholderVisionProvider(VisionProvider):
    """
    Stub provider. Returns a canned response indicating vision is not configured.
    The camera still captures the image — the file is saved and logged.
    Replace this with GeminiVisionProvider or similar when ready.
    """

    def analyze_image(self, image_path: str, question: str) -> str:
        log.info(
            "Vision placeholder called: image=%s, question=%r",
            image_path, question,
        )
        return (
            "Vision analysis is not yet configured. "
            "I captured an image, but cannot describe it right now. "
            "To enable vision, set services.vision.provider in config.yaml."
        )

    def health_check(self) -> bool:
        return True


def create_vision_client(config: "VisionConfig") -> VisionProvider:
    """
    Factory — returns the configured vision provider.
    Unknown providers fall back to placeholder with a warning.
    """
    provider = (config.provider or "placeholder").lower()

    if provider == "placeholder":
        log.info("Vision provider: placeholder (no cloud vision configured)")
        return PlaceholderVisionProvider()

    # Future providers — uncomment and implement as needed:
    # elif provider == "gemini":
    #     from src.services.vision_gemini import GeminiVisionProvider
    #     return GeminiVisionProvider(api_key=config.api_key, host=config.cloud_host)
    #
    # elif provider == "openai":
    #     from src.services.vision_openai import OpenAIVisionProvider
    #     return OpenAIVisionProvider(api_key=config.api_key)
    #
    # elif provider == "anthropic":
    #     from src.services.vision_anthropic import AnthropicVisionProvider
    #     return AnthropicVisionProvider(api_key=config.api_key)

    else:
        log.warning(
            "Unknown vision provider '%s' — falling back to placeholder", provider
        )
        return PlaceholderVisionProvider()
