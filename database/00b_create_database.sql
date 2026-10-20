-- =====================================================================
--  STEP 2 of 3  -  run in pgAdmin Query Tool connected to the "postgres"
--  database. This file contains ONE statement on purpose: PostgreSQL does
--  not allow CREATE DATABASE inside a multi-statement script.
--  (Alternative: in pgAdmin right-click Databases -> Create -> Database...,
--   name it quantara, owner quantara_app.)
-- =====================================================================
CREATE DATABASE quantara OWNER quantara_app ENCODING 'UTF8' TEMPLATE template0;
