import sqlite3
from pathlib import Path

# --------------------------------------------------
# Database configuration
# --------------------------------------------------

DB_PATH = Path(__file__).parent / "agent_memory.db"


def column_exists(cursor, table_name, column_name):
    """Check whether a column already exists in a SQLite table."""
    columns = cursor.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return any(column[1] == column_name for column in columns)


# --------------------------------------------------
# Connect to database
# --------------------------------------------------

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()


# --------------------------------------------------
# Conversations table
# --------------------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")


# --------------------------------------------------
# Memories table
# --------------------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT DEFAULT 'conversation',
    importance REAL DEFAULT 0.5,
    confidence REAL DEFAULT 1.0,
    embedding TEXT,
    memory_type TEXT DEFAULT 'long_term',
    expires_at DATETIME DEFAULT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")


# --------------------------------------------------
# Upgrade older databases
# --------------------------------------------------
# Keep existing memories. Add missing columns only.

memory_columns = {
    "source": "TEXT DEFAULT 'conversation'",
    "importance": "REAL DEFAULT 0.5",
    "confidence": "REAL DEFAULT 1.0",
    "embedding": "TEXT",
    "memory_type": "TEXT DEFAULT 'long_term'",
    "expires_at": "DATETIME DEFAULT NULL",
    "created_at": "DATETIME DEFAULT CURRENT_TIMESTAMP",
    "updated_at": "DATETIME DEFAULT CURRENT_TIMESTAMP",
}

for column_name, column_definition in memory_columns.items():
    if not column_exists(cursor, "memories", column_name):
        cursor.execute(
            f"ALTER TABLE memories ADD COLUMN {column_name} {column_definition}"
        )


# --------------------------------------------------
# Indexes
# --------------------------------------------------

cursor.execute("""
CREATE INDEX IF NOT EXISTS idx_conversations_user_created
ON conversations(user_id, created_at)
""")

cursor.execute("""
CREATE INDEX IF NOT EXISTS idx_memories_user_category_key
ON memories(user_id, category, key)
""")

cursor.execute("""
CREATE INDEX IF NOT EXISTS idx_memories_user_expires
ON memories(user_id, expires_at)
""")

cursor.execute("""
CREATE INDEX IF NOT EXISTS idx_memories_user_updated
ON memories(user_id, updated_at)
""")


# --------------------------------------------------
# Save changes
# --------------------------------------------------

conn.commit()
conn.close()

print(f"Database initialized successfully: {DB_PATH}")
