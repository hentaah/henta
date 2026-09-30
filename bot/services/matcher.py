"""Exact name matcher for /catch attempts.

A player must enter either the character's full catalog name or one of its
semicolon-separated aliases. Matching is case-insensitive and ignores leading,
trailing, or repeated whitespace, but it never accepts a misspelling, prefix,
or fuzzy substring.
"""
from __future__ import annotations


def _normalize(value: str) -> str:
    """Keep case/spacing convenient without relaxing character spelling."""
    return " ".join(value.casefold().split())


def matches(guess: str, name: str, aliases: str) -> tuple[bool, int]:
    """Return an exact match result while keeping the legacy tuple API.

    The numeric result is retained only for internal compatibility.  It is no
    longer a fuzzy score and must not be exposed to players.
    """
    normalized_guess = _normalize(guess)
    if not normalized_guess:
        return False, 0

    candidates = {_normalize(name)}
    if aliases:
        candidates.update(
            _normalize(alias)
            for alias in aliases.split(";")
            if _normalize(alias)
        )

    matched = normalized_guess in candidates
    return matched, 100 if matched else 0
