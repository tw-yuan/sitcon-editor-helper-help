-- Card-only requests have no Drive resources. Preserve all existing mappings.
-- Store.open applies each migration inside a transaction.
CREATE TABLE resources_new (
    issue_iid INTEGER PRIMARY KEY, folder_id TEXT, document_id TEXT, operation_id TEXT NOT NULL
);
INSERT INTO resources_new (issue_iid, folder_id, document_id, operation_id)
SELECT issue_iid, folder_id, document_id, operation_id FROM resources;
DROP TABLE resources;
ALTER TABLE resources_new RENAME TO resources;
