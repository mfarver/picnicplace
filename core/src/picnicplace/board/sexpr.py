"""A small reader for KiCad's S-expression files."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.DOTALL)
_ESCAPE = re.compile(r"\\(.)")


class SexprError(ValueError):
    pass


@dataclass
class Node:
    """``(name value value (child ...) ...)``: atoms in ``values``, lists in ``children``."""

    name: str
    values: list[str] = field(default_factory=list[str])
    children: list[Node] = field(default_factory=list["Node"])

    def first(self, name: str) -> Node | None:
        return next((c for c in self.children if c.name == name), None)

    def all(self, name: str) -> Iterator[Node]:
        return (c for c in self.children if c.name == name)

    def value(self, name: str, index: int = 0) -> str | None:
        child = self.first(name)
        if child is None or len(child.values) <= index:
            return None
        return child.values[index]

    def floats(self, name: str) -> list[float]:
        child = self.first(name)
        return [] if child is None else [float(v) for v in child.values]


def parse(text: str) -> Node:
    """Parse one top-level list."""
    stack: list[Node] = []
    root: Node | None = None
    pos = 0
    end = len(text.rstrip())
    while pos < end:
        match = _TOKEN.match(text, pos)
        if match is None:
            raise SexprError(f"unexpected character at offset {pos}")
        pos = match.end()
        opening, closing, quoted, atom = match.groups()
        if opening:
            name_match = _TOKEN.match(text, pos)
            if name_match is None or name_match.group(4) is None:
                raise SexprError(f"a list without a name at offset {pos}")
            pos = name_match.end()
            node = Node(name_match.group(4))
            if stack:
                stack[-1].children.append(node)
            elif root is None:
                root = node
            else:
                raise SexprError("more than one top-level list")
            stack.append(node)
        elif closing:
            if not stack:
                raise SexprError(f"unbalanced ')' at offset {pos}")
            stack.pop()
        elif not stack:
            raise SexprError(f"an atom outside any list at offset {pos}")
        elif quoted is not None:
            stack[-1].values.append(_ESCAPE.sub(r"\1", quoted))
        else:
            stack[-1].values.append(atom)
    if stack or root is None:
        raise SexprError("the file ends inside a list" if stack else "empty file")
    return root
