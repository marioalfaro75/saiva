"""Create the database schema from SQLAlchemy metadata.

Run on container start (see Dockerfile). This is the Phase-0 baseline; Alembic
migrations are the planned mechanism for schema evolution (PRD NFR4) and are a
documented follow-up rather than wired here.
"""

from . import models  # noqa: F401  (import registers all models on Base.metadata)
from .db import Base, engine


def main() -> None:
    Base.metadata.create_all(bind=engine)
    print("Saiva: database schema ensured.")


if __name__ == "__main__":
    main()
