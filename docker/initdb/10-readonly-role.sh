#!/bin/sh
# Runs once when the PostgreSQL volume is first created.
# The dashboard connects as dashboard_ro, which can only SELECT.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE dashboard_ro LOGIN PASSWORD '${DASHBOARD_DB_PASSWORD}';
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO dashboard_ro;
GRANT USAGE ON SCHEMA public TO dashboard_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE ${POSTGRES_USER} IN SCHEMA public GRANT SELECT ON TABLES TO dashboard_ro;
SQL
