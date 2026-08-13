"""Reading what a model sent back.

Two repairs, and both exist because the failure they prevent is expensive. A model asked for JSON will
still occasionally wrap it in prose or a fenced block, and losing a whole storyboard to a stray ``` is a
poor way to spend a minute of GPU time. A model asked for a sentence will occasionally answer with an
object, and a Python dict repr reaching a ComfyUI prompt is both useless and hard to spot.

The prompts that used to live here are now stages — see `pipeline/builtin.py`. These two stayed because
they are about what comes *back*, which is the same job whoever did the asking.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .provider import LlmError

logger = logging.getLogger(__name__)

def as_text(value: Any) -> str:
    """One field's worth of prose, however the model chose to shape it.

    Asked for a string, a model will quite happily answer with an object — ``{"subject": …, "light": …}``
    instead of a sentence. Calling ``str()`` on that puts a Python dict repr into the prompt that reaches
    ComfyUI, which is both useless and hard to spot. Flattening keeps whatever it wrote and throws away
    only the shape it wrapped it in.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        # The keys are the model's own headings ("camera", "light"); the values are what it actually
        # wrote, and reading them back in order gives a sentence.
        return ". ".join(part for part in (as_text(v) for v in value.values()) if part)
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (as_text(v) for v in value) if part)
    return str(value).strip()


def parse_json_object(text: str) -> dict[str, Any]:
    """The model's answer as an object, repaired where it plausibly can be.

    Constrained decoding makes this unnecessary on Ollama and merely belt-and-braces elsewhere — but a
    storyboard is expensive enough to produce that throwing one away over a code fence is not acceptable.
    """
    cleaned = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Prose around an object: take the outermost braces and try again.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise LlmError(
                "The model did not answer with JSON. Try a different model, or one that supports "
                f"structured output. It said: {text[:200]}"
            ) from None
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LlmError(f"The model's JSON could not be read: {exc}") from exc

    if not isinstance(parsed, dict):
        raise LlmError(f"Expected a JSON object, got {type(parsed).__name__}.")
    return parsed
