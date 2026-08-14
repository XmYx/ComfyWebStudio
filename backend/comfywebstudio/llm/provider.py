"""Talking to a language model.

Two jobs, and they are deliberately separate: *writing* (a premise becomes a storyboard) and *looking*
(a generated frame becomes a description). They are separate because the models are: the one that writes
well is rarely the one that can see, and pretending otherwise means silently sending an image to a model
that will ignore it and confidently describe nothing.

Providers are registered by kind, exactly like the ComfyUI backends, so adding one is adding a module
rather than editing a conditional. Everything here is local-first: Ollama on the machine, or anything
speaking the OpenAI chat API — vLLM, llama.cpp, LM Studio — which is what "an open-source provider" means
in practice.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..settings import LlmProviderConfig

logger = logging.getLogger(__name__)


class LlmError(Exception):
    """Anything that went wrong talking to a model, in terms the user can act on."""


@dataclass(slots=True)
class ModelInfo:
    """One model a provider offers."""

    name: str
    #: True when it can be given images. Detected, never assumed — sending a picture to a text-only model
    #: produces a confident description of nothing at all.
    vision: bool = False
    size: str = ""
    family: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "vision": self.vision, "size": self.size, "family": self.family}


@dataclass(slots=True)
class LoadedModel:
    """One model currently sitting in memory, and how much of it is on the graphics card."""

    name: str
    #: Bytes of VRAM. 0 when it is on the processor, or when the provider does not break it down.
    vram: int = 0
    size: int = 0
    #: When the provider intends to release it by itself, if it says.
    expires: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "vram": self.vram, "size": self.size, "expires": self.expires}


@dataclass(slots=True)
class Reply:
    text: str
    model: str = ""
    #: Whatever the provider reported about the exchange, for the log rather than the user.
    meta: dict[str, Any] = field(default_factory=dict)


class LlmProvider(ABC):
    """One configured endpoint."""

    def __init__(self, config: LlmProviderConfig):
        self.config = config

    @abstractmethod
    async def models(self) -> list[ModelInfo]:
        """Everything this endpoint can run, with whether each can see."""

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: str = "",
        images: list[bytes] | None = None,
        json_object: bool = False,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.7,
    ) -> Reply:
        """One turn.

        `images` is what makes it a vision request. `schema` is a JSON Schema the answer must satisfy —
        far stronger than `json_object`, which only guarantees *some* JSON: a schema pins the keys and
        their types, so a model cannot answer with an object where a sentence was asked for, or echo a
        field name back as its own value. Both are real things smaller models do.
        """

    async def pull(self, model: str, on_progress=None) -> None:
        """Fetch a model onto the machine running the provider.

        Only meaningful where the provider *owns* its models. A remote OpenAI-compatible server serves
        whatever it was started with, so this says so rather than pretending to work.
        """
        raise LlmError(
            f"{self.config.name} does not manage its own models — they are whatever the server was "
            "started with. Pull it there, or use Ollama."
        )

    async def loaded(self) -> list[LoadedModel]:
        """Models resident in memory right now. Empty when the provider cannot say."""
        return []

    async def unload(self, model: str | None = None) -> list[str]:
        """Put loaded models out of memory, and say which ones went.

        The point is the graphics card. A language model and an image model competing for the same VRAM
        means the second one runs on the processor, or does not run at all — and the storyboard flow uses
        both, one after the other, so releasing one before the other starts is worth a button.

        Only meaningful where the provider owns the process holding the memory.
        """
        raise LlmError(
            f"{self.config.name} runs somewhere this app does not manage, so its memory is not ours to "
            "free. Stop the model there instead."
        )

    async def close(self) -> None:  # pragma: no cover - most providers hold nothing open
        return None


@dataclass(slots=True)
class LibraryModel:
    """One model worth suggesting, with enough detail to choose between them."""

    name: str
    size: str
    vision: bool
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "size": self.size, "vision": self.vision, "note": self.note}


#: A short, opinionated shelf rather than a catalogue.
#:
#: Ollama publishes no API for browsing its library, so anything here is a maintained list — which is why
#: it is kept small and the UI also lets any name be typed. These are the ones worth reaching for first:
#: two sizes of a good vision model, two sizes of a good writer, and one of each that runs on very little.
MODEL_LIBRARY: list[LibraryModel] = [
    LibraryModel("qwen2.5vl:7b", "~6 GB", True, "Reads a frame well. The default choice for describing."),
    LibraryModel("qwen2.5vl:3b", "~3 GB", True, "The same model, smaller and quicker."),
    LibraryModel("llava:7b", "~4.7 GB", True, "The long-standing option; weaker at fine detail."),
    LibraryModel("moondream", "~1.7 GB", True, "Tiny. Runs anywhere, describes broadly."),
    LibraryModel("llama3.1:8b", "~4.9 GB", False, "A dependable writer for storyboards."),
    LibraryModel("qwen2.5:14b", "~9 GB", False, "Better structure and pacing, if you have the room."),
    LibraryModel("gemma3:4b", "~3.3 GB", True, "Small, recent, and can see."),
]


#: kind -> class. Populated by the adapters at import.
REGISTRY: dict[str, type[LlmProvider]] = {}


def register(kind: str):
    def decorate(cls: type[LlmProvider]) -> type[LlmProvider]:
        REGISTRY[kind] = cls
        return cls
    return decorate


def create_provider(config: LlmProviderConfig) -> LlmProvider:
    # Imported for their registration side effect, and here rather than at module scope so the registry
    # cannot depend on import order.
    from . import ollama, openai_compat  # noqa: F401

    cls = REGISTRY.get(config.kind)
    if cls is None:
        raise LlmError(
            f"No language-model provider of kind {config.kind!r}. "
            f"Known kinds: {', '.join(sorted(REGISTRY)) or 'none'}."
        )
    return cls(config)
