#!/usr/bin/env bash
# Export resolved quant_short trades to CSV for offline inference work.
#
# Run on the host that can reach the polyagent-db Postgres instance.
# Writes to ./quant_short_trades.csv in the current directory.
#
# Usage:
#   DATABASE_URL=postgresql://polyagent:polyagent@polyagent-db:5432/polyagent \
#     scripts/export_quant_short_trades.sh
#
# Or, if the DB only reachable from inside the compose network:
#   podman compose exec polyagent-db \
#     psql -U polyagent -d polyagent \
#     -c "$(cat scripts/export_quant_short_trades.sql)" > quant_short_trades.csv

set -euo pipefail

OUT="${1:-quant_short_trades.csv}"
: "${DATABASE_URL:?DATABASE_URL must be set (or pipe through podman exec, see header)}"

psql "$DATABASE_URL" --csv -c "
SELECT
    trade_id,
    market_id,
    asset_id,
    decision_ts,
    side,
    estimator_p_up,
    edge_at_decision,
    abs_edge,
    fill_price_assumed,
    size,
    vol_at_decision,
    window_duration_s,
    outcome,
    pnl,
    won,
    brier,
    concurrent_with_prior,
    trade_resolved_at,
    market_resolved_at,
    start_spot,
    end_spot,
    slug
FROM quant_short_v
WHERE pnl IS NOT NULL
ORDER BY decision_ts
" > "$OUT"

ROWS=$(($(wc -l < "$OUT") - 1))
echo "Wrote $ROWS resolved trades to $OUT"
