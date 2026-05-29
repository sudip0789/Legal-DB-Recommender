"""
Loads catalog.json and builds the system prompt once at module import time.
SYSTEM_PROMPT is a module-level constant — byte-identical across all requests,
which is required for Anthropic prompt caching to produce cache hits.
"""

import json
from pathlib import Path

_BASE = Path(__file__).parent.parent
_CATALOG_PATH = _BASE / "data" / "catalog.json"
_PROMPT_TEMPLATE_PATH = _BASE / "prompts" / "system_prompt.md"

with open(_CATALOG_PATH, encoding="utf-8") as _f:
    CATALOG: dict = json.load(_f)

with open(_PROMPT_TEMPLATE_PATH, encoding="utf-8") as _f:
    _template = _f.read()

# Strip the builder comment header (everything up to and including the first
# '---' separator — that block is instructions to the developer, not the prompt).
if "\n---\n" in _template:
    _template = _template.split("\n---\n", 1)[1].lstrip("\n")

# Inject the full catalog as compact JSON (saves tokens; model handles it fine).
SYSTEM_PROMPT: str = _template.replace(
    "{{CATALOG_JSON}}", json.dumps(CATALOG, ensure_ascii=False)
)
