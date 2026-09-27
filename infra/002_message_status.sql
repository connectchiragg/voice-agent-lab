ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'
    CHECK (status IN ('completed', 'interrupted'));
