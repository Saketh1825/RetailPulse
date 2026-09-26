-- Executed by scripts/init_db.py AFTER the role exists. {role} and {dbname} are substituted by
-- psycopg's sql.Identifier (safe quoting); nothing here contains a password.
--
-- Principle: start from ZERO privileges and grant only what the NL-to-SQL feature needs.

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {role};
REVOKE CREATE ON SCHEMA public FROM PUBLIC;          -- no CREATE TABLE in public (default since PG15)
REVOKE TEMPORARY ON DATABASE {dbname} FROM PUBLIC;   -- no temp tables for the read-only role

GRANT CONNECT ON DATABASE {dbname} TO {role};
GRANT USAGE   ON SCHEMA public TO {role};
GRANT SELECT  ON items, sales, forecasts TO {role};  -- the ONLY three tables the NL feature may read

-- Belt and braces: defaults applied to every session of this role (validated in tests).
ALTER ROLE {role} SET statement_timeout = '5s';
ALTER ROLE {role} SET default_transaction_read_only = on;
ALTER ROLE {role} SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE {role} SET search_path = public;
