-- ============================================================================================================
--  OPTIONAL. Renames an EXISTING asad_trading database and asad_trader role to the Quantara names.
--
--  YOU DO NOT NEED TO RUN THIS.
--
--  The rename to Quantara is cosmetic everywhere except here. Your existing database keeps working untouched:
--  the database name and role name live only in DATABASE_URL in your .env, and nothing in the application code
--  cares what they are called. Leaving them as asad_trading / asad_trader costs you nothing and risks nothing.
--
--  Run this ONLY if you want the names to match for tidiness, and only when the bot is STOPPED.
--  Fresh installs never need it - 00a/00b already create the Quantara names.
--
--  Before running:
--    1. Stop the bot and the dashboard. An open connection blocks ALTER DATABASE ... RENAME.
--    2. Back up:  pgAdmin -> right-click asad_trading -> Backup... -> Format "Custom".
--       Do not skip this. A rename is cheap to redo; a lost trade history is not.
--
--  After running, update DATABASE_URL in your .env to:
--    postgresql://quantara_app:YOUR_PASSWORD@localhost:5432/quantara
--  The password does not change - renaming a role keeps its password.
-- ============================================================================================================

-- ---- Step 1: run this on the "postgres" database, NOT on asad_trading ----------------------------------

-- Disconnect anything still holding the database open (the bot, pgAdmin query tabs, the dashboard).
SELECT pg_terminate_backend(pid)
FROM   pg_stat_activity
WHERE  datname = 'asad_trading' AND pid <> pg_backend_pid();

ALTER DATABASE asad_trading RENAME TO quantara;
ALTER ROLE     asad_trader  RENAME TO quantara_app;

-- ---- Step 2: verify ------------------------------------------------------------------------------------
-- Expect one row: quantara | quantara_app
SELECT d.datname AS database, r.rolname AS owner
FROM   pg_database d
JOIN   pg_roles    r ON r.oid = d.datdba
WHERE  d.datname = 'quantara';

-- ---- Step 3: update .env --------------------------------------------------------------------------------
--   DATABASE_URL=postgresql://quantara_app:YOUR_PASSWORD@localhost:5432/quantara
-- then start the bot and confirm the dashboard shows your trade history. If anything looks wrong, the rename
-- is reversible: ALTER DATABASE quantara RENAME TO asad_trading;  ALTER ROLE quantara_app RENAME TO asad_trader;
