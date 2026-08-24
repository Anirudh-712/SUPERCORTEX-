"""Shared validation used by the fabricated-number hooks. Stdlib only — see numbers.py."""
from supercortex.validation.numbers import (
    Decision,
    EditImpossible,
    PathClass,
    RegistryUnavailable,
    classify_path,
    decide,
    extract_claimed,
    reconstruct_edit,
    validate_content,
)

__all__ = [
    "Decision",
    "EditImpossible",
    "PathClass",
    "RegistryUnavailable",
    "classify_path",
    "decide",
    "extract_claimed",
    "reconstruct_edit",
    "validate_content",
]
