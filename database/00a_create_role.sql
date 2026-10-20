-- =====================================================================
--  STEP 1 of 3  -  run in pgAdmin Query Tool connected to the "postgres"
--  database as the "postgres" superuser.
--  Creates the login the trading software uses.
--  >>> CHANGE THE PASSWORD BELOW, and put the same password in .env <<<
-- =====================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quantara_app') THEN
        CREATE ROLE quantara_app WITH LOGIN PASSWORD 'CHANGE_ME_BEFORE_RUNNING';
    END IF;
END
$$;
