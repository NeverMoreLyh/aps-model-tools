from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple


@dataclass(frozen=True)
class SearchScope:
    """Domain filters shared by exact find and FTS search."""

    kinds: Tuple[str, ...] = ()
    project: Optional[str] = None
    module: Optional[str] = None
    path: Optional[str] = None
    file: Optional[str] = None
    owner: Optional[str] = None
    top_level: bool = False

    @classmethod
    def from_values(
        cls,
        kinds: Optional[Sequence[str]] = None,
        project: Optional[str] = None,
        module: Optional[str] = None,
        path: Optional[str] = None,
        file: Optional[str] = None,
        owner: Optional[str] = None,
        top_level: bool = False,
    ) -> "SearchScope":
        normalized = tuple(dict.fromkeys(value.strip().upper() for value in (kinds or ()) if value.strip()))
        return cls(normalized, project, module, path, file, owner, top_level)

    def is_empty(self) -> bool:
        return not (self.kinds or self.project or self.module or self.path or self.file or self.owner or self.top_level)
