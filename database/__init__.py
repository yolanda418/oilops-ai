

"""OilOps AI database package.

Provides SQLite connection management and schema bootstrap.
"""

from .db import get_connection, init_db

__all__ = ["get_connection", "init_db"]