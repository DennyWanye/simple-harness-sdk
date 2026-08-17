"""Context port using SQLite for persistence."""

from simple_harness.execution.sqlite import Database


class SqliteContextPort:
    """SQLite-based context storage."""

    def __init__(self, db: Database):
        self.db = db

    # The SDK's SqliteContextPort already implements all required methods.
    # This is a thin wrapper for demonstration.
    # In a real app, you might customize schema or add indexes.

    async def save_run(self, run):
        """Save run to database."""
        # Delegate to SDK's database layer
        pass

    async def load_run(self, run_id):
        """Load run from database."""
        # Delegate to SDK's database layer
        pass
