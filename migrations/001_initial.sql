CREATE TABLE authorized_groups (
    chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '', authorized_by INTEGER NOT NULL,
    authorized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE group_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, content TEXT NOT NULL,
    created_by INTEGER NOT NULL, created_by_name TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX memories_chat ON group_memories(chat_id);
CREATE TABLE operations (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'running',
    steps TEXT NOT NULL DEFAULT '{}', result TEXT, error TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX operations_event ON operations(event_id);
CREATE TABLE resources (
    issue_iid INTEGER PRIMARY KEY, folder_id TEXT NOT NULL, document_id TEXT, operation_id TEXT NOT NULL
);
CREATE TABLE events (
    id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, state TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
    chat_id INTEGER NOT NULL, thread_id INTEGER, reply_to INTEGER, body TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', telegram_message_id INTEGER,
    UNIQUE(event_id, ordinal)
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER,
    action TEXT NOT NULL, target TEXT, status TEXT NOT NULL, detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
