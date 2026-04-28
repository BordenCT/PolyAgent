# Quant Refactor: Operational Notes

## After PR 1 ships

On every host that has a PolyAgent database (dev, prod):

```bash
polyagent migrate baseline   # records 001-005 as applied without re-executing
polyagent migrate status     # verify all five appear under "Applied:"
```

Then wire `polyagent migrate up` into bot startup:

- compose: add to `command:` of the bot service: `sh -c 'polyagent migrate up && polyagent run'`
- systemd: `ExecStartPre=/path/to/polyagent migrate up`

## After PR 5 ships (migration 006)

Bot startup will auto-apply migration 006 via the `migrate up` step. Verify
with `polyagent migrate status` post-deploy. `006` should be under Applied.

## After PR 6 ships

Update `.env` on each host:

```
# Remove
BTC5M_ENABLED=true
BTC5M_VOL_WINDOW_S=300
BTC5M_EDGE_THRESHOLD=0.05
BTC5M_POSITION_SIZE_USD=5.0
BTC5M_FEES_BPS=0.0
BTC5M_SPOT_POLL_S=2.0
BTC5M_MARKET_POLL_S=60
CRYPTO_QUANT_ENABLED=true
CRYPTO_QUANT_BTC_VOL=0.60
CRYPTO_QUANT_ETH_VOL=0.75

# Add
QUANT_SHORT_ENABLED=true
QUANT_MARKET_POLL_S=60
QUANT_POSITION_SIZE_USD=5.0
# Per-asset overrides (optional, registry defaults shown):
# QUANT_BTC_VOL=0.60
# QUANT_BTC_EDGE_THRESHOLD=0.05
# QUANT_ETH_VOL=0.75
# QUANT_ETH_EDGE_THRESHOLD=0.05
```
