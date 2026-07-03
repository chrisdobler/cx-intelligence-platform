-- Enable pgvector in the application database.
-- Runs once on first container start via docker-entrypoint-initdb.d.
CREATE EXTENSION IF NOT EXISTS vector;
