#!/usr/bin/env bash
# Inventory market_data tables. Same connection logic as
# scripts/export_quant_short_trades.sh.

set -euo pipefail

SQL_FILE="$(dirname "$0")/inspect_market_data.sql"

if command -v psql >/dev/null 2>&1 && [ -n "${DATABASE_URL:-}" ]; then
    psql "$DATABASE_URL" -f "$SQL_FILE" || \
        psql "postgresql://polyagent:polyagent@localhost:5432/polyagent" -f "$SQL_FILE"
elif command -v psql >/dev/null 2>&1; then
    psql "postgresql://polyagent:polyagent@localhost:5432/polyagent" -f "$SQL_FILE"
elif command -v podman >/dev/null 2>&1; then
    podman compose exec -T polyagent-db \
        psql -U polyagent -d polyagent -f - < "$SQL_FILE"
else
    echo "need either psql or podman on PATH" >&2; exit 1
fi
