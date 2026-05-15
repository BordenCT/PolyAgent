#!/usr/bin/env bash
# Export resolved quant_short trades joined to market_data features
# (OB imbalance/spread, perp basis, funding, 60s aggressor flow) for
# offline modeling. Same connection logic as the other export scripts.

set -euo pipefail

OUT="${1:-quant_short_trades_with_micro.csv}"
SQL_FILE="$(dirname "$0")/export_trades_with_microstructure.sql"

if [ ! -f "$SQL_FILE" ]; then
    echo "missing $SQL_FILE" >&2; exit 1
fi

run_with_psql_url() {
    local url="$1"
    psql "$url" --csv -f "$SQL_FILE"
}

run_with_podman() {
    podman compose exec -T polyagent-db \
        psql -U polyagent -d polyagent --csv -f - < "$SQL_FILE"
}

if command -v psql >/dev/null 2>&1 && [ -n "${DATABASE_URL:-}" ]; then
    echo "exporting via psql + DATABASE_URL..." >&2
    run_with_psql_url "$DATABASE_URL" > "$OUT" || {
        echo "DATABASE_URL path failed, falling back to localhost..." >&2
        run_with_psql_url "postgresql://polyagent:polyagent@localhost:5432/polyagent" > "$OUT"
    }
elif command -v psql >/dev/null 2>&1; then
    echo "exporting via psql + localhost:5432..." >&2
    run_with_psql_url "postgresql://polyagent:polyagent@localhost:5432/polyagent" > "$OUT"
elif command -v podman >/dev/null 2>&1; then
    echo "exporting via podman compose exec..." >&2
    run_with_podman > "$OUT"
else
    echo "need either psql or podman on PATH" >&2; exit 1
fi

ROWS=$(($(wc -l < "$OUT") - 1))
echo "wrote $ROWS rows to $OUT"
