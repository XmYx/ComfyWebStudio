"""Filling in a prompt template, and turning a list of fields into a JSON Schema.

Two small jobs that the whole editable-pipeline idea rests on, kept away from anything that talks to a
model so the drawing step can use them too and so they can be tested without a fixture in sight.

**The renderer never sees a Python object.** It is handed a flat ``Mapping[str, str]`` and looks tokens up
by exact key, so ``{frame.title}`` is one dict key that happens to contain a dot — not an attribute walk.
That is the whole security argument, and it is one of arity rather than of blacklisting: ``{x.__class__}``
is not *blocked*, it is simply a key nobody put in the dict. `str.format` would have to be defended
against; this has nothing to defend.

It also means the built-in prompts could be moved into templates **verbatim**. Tokens must be lowercase,
so the literal JSON those prompts contain — ``{"frames": [{"title": "..."}]}`` — is not a token and needs
no escaping, which is exactly the trap `str.format` sets on day one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: ``{name}`` or ``{dotted.name}``. Lowercase only; see the module docstring.
TOKEN = re.compile(r"\{([a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)*)\}")

#: ``[[ ... ]]`` — kept only if something inside it resolved to a value.
OPTIONAL = re.compile(r"\[\[(.*?)\]\]", re.DOTALL)

#: Three or more blank lines, left behind when an optional block drops out.
GAP = re.compile(r"\n{3,}")


def render(template: str, context: Mapping[str, str]) -> tuple[str, list[str]]:
    """The filled-in text, and every token that had no value.

    Optional blocks are resolved first: ``[[ASPECT: {board.aspect}]]`` disappears entirely when the aspect
    is blank, rather than leaving a dangling heading. A block containing no tokens at all is kept — the
    brackets are for *conditional* text, and text conditional on nothing is just text.

    An unknown token is **left exactly as written** and reported. Not an error, because one typo should
    not refuse to draw a whole board; and not blanked, because silently deleting an instruction is how you
    get a hundred frames that quietly ignored the prompt. Left in place it is visible in the rendered
    preview, in the transcript, and in the returned list.
    """
    unknown: list[str] = []

    def substitute(text: str) -> tuple[str, bool, bool, bool]:
        """The filled-in text, and whether anything resolved, anything was present, anything was unknown."""
        resolved = False
        present = False
        missing = False

        def one(match: re.Match[str]) -> str:
            nonlocal resolved, present, missing
            present = True
            name = match.group(1)
            if name not in context:
                missing = True
                if name not in unknown:
                    unknown.append(name)
                return match.group(0)
            value = context[name]
            if value:
                resolved = True
            return value

        return TOKEN.sub(one, text), resolved, present, missing

    def block(match: re.Match[str]) -> str:
        filled, resolved, present, missing = substitute(match.group(1))
        # `missing` keeps the block: a token that is *empty* is the condition doing its job, but a token
        # that is *unknown* is a typo, and letting one quietly delete a whole instruction is the failure
        # this module exists to avoid. Kept, it shows up in the preview as the mistake it is.
        return filled if resolved or missing or not present else ""

    text = OPTIONAL.sub(block, template)
    text, _, _, _ = substitute(text)
    return GAP.sub("\n\n", text).strip(), unknown


def token_names(context: Mapping[str, str]) -> list[str]:
    """Every token the editor can offer, sorted for a palette."""
    return sorted(context)


def unknown_tokens(template: str, context: Mapping[str, str]) -> list[str]:
    """Which tokens in a template would not resolve — for underlining one as it is typed."""
    return [name for name in dict.fromkeys(TOKEN.findall(template)) if name not in context]


# -- output shape --------------------------------------------------------------------------------------

#: Field type -> JSON Schema type. `text` is a string too; the distinction is how the editor draws it.
_SCALARS = {
    "string": "string",
    "text": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
}


def json_schema(fields: list[Any]) -> dict[str, Any]:
    """A schema for constrained decoding, from the field list the user edits.

    Every scalar is a *string* by default for a reason worth keeping: it is what stops a model answering
    ``{"light": …, "subject": …}`` where a sentence was asked for.
    """
    return {
        "type": "object",
        "properties": {field.key: _schema_for(field) for field in fields},
        "required": [field.key for field in fields if field.required],
    }


def _schema_for(field: Any) -> dict[str, Any]:
    if field.type == "object_list":
        node: dict[str, Any] = {"type": "array", "items": json_schema(field.fields)}
    elif field.type == "string_list":
        node = {"type": "array", "items": {"type": "string"}}
    else:
        node = {"type": _SCALARS.get(field.type, "string")}
    # Only when there is one: an empty description in the schema is noise the model still pays for, and
    # leaving it out is what keeps the generated schemas identical to the hand-written ones they replace.
    if field.description:
        node["description"] = field.description
    return node
