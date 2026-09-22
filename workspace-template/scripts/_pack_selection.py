"""Optional, inert selection metadata shared by pack validation and consumers."""

from __future__ import annotations

import re
from copy import deepcopy

FIELDS = ("typical_questions", "exclusions", "required_scope_inputs", "review_requirements")


def selection_metadata(pack):
    """Validate optional declarations while retaining explicit legacy unknowns."""
    value = pack.get("selection") if isinstance(pack, dict) else None
    if value is None and isinstance(pack, dict) and "selection" not in pack:
        return {"schema_version": "1.0", **dict.fromkeys(FIELDS), "unknown_fields": list(FIELDS)}
    if not isinstance(value, dict) or value.get("schema_version") != "1.0" or set(value) - {"schema_version", *FIELDS}:
        raise ValueError("pack_selection_invalid")
    result = {"schema_version": "1.0", **dict.fromkeys(FIELDS), "unknown_fields": []}
    for field in FIELDS:
        if field not in value:
            result["unknown_fields"].append(field)
            continue
        items = value[field]
        if not isinstance(items, list) or len(items) > 32:
            raise ValueError("pack_selection_bound")
        identifiers = set()
        for item in items:
            if field == "required_scope_inputs":
                if (not isinstance(item, dict) or set(item) != {"id", "description"}
                        or not isinstance(item["id"], str)
                        or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", item["id"]) is None
                        or item["id"] in identifiers):
                    raise ValueError("pack_scope_input_invalid")
                identifiers.add(item["id"])
                text = item["description"]
            else:
                text = item
            if (not isinstance(text, str) or not text.strip() or len(text) > 1024
                    or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in text)):
                raise ValueError("pack_selection_text_invalid")
        result[field] = deepcopy(items)
    return result
