CREATE TABLE review_packets (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER,
    issue_iid INTEGER NOT NULL,
    document_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    notice TEXT NOT NULL,
    pdf BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE document_signatures (
    document_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY(document_id, user_id)
);
CREATE TABLE review_reads (
    packet_id TEXT NOT NULL REFERENCES review_packets(id),
    user_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(packet_id, user_id)
);
CREATE TABLE review_messages (
    packet_id TEXT NOT NULL REFERENCES review_packets(id),
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    dirty INTEGER NOT NULL DEFAULT 0,
    page INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(chat_id, message_id)
);
