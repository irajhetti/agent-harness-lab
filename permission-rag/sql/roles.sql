-- Run by the superuser inside the container (see `make up`). Safe to run again.
-- Two roles: rag_owner owns and seeds the tables, rag_reader is what the application
-- uses for retrieval. Neither is a superuser and neither may bypass row level security.
CREATE EXTENSION IF NOT EXISTS vector;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_owner') THEN
        CREATE ROLE rag_owner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_reader') THEN
        CREATE ROLE rag_reader LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO rag_owner;
GRANT USAGE ON SCHEMA public TO rag_reader;
