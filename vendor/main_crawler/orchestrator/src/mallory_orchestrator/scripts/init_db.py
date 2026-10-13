"""Create the orchestrator control-plane tables."""

from __future__ import annotations

from .. import models  # noqa: F401  (registers tables)
from ..db import Base, engine


def main() -> None:
    Base.metadata.create_all(engine)
    print(f"Created {len(Base.metadata.tables)} orchestrator tables.")


if __name__ == "__main__":
    main()
