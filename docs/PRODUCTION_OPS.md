# VN Edge — Production Operations Guide

## Cron Jobs (install on VM1)

```bash
crontab -e
# Add these lines:

# Database backup nightly at 2am UTC
0 2 * * * /home/opc/crypto-trading-bot/scripts/backup_db.sh >> /home/opc/cron_backup.log 2>&1

# Health monitoring every 5 minutes
*/5 * * * * /home/opc/crypto-trading-bot/scripts/health_alerts.sh >> /home/opc/cron_health.log 2>&1

# ML model sync every 15 minutes
*/15 * * * * /home/opc/crypto-trading-bot/scripts/sync_ml_models.sh >> /home/opc/cron_ml_sync.log 2>&1

# ML weekly retrain (Monday 4am UTC)
0 4 * * 1 /home/opc/crypto-trading-bot/scripts/trigger_ml_retrain.sh >> /home/opc/cron_retrain.log 2>&1
```

## Database VM Migration (Wave 7 Item)

To move PostgreSQL to a dedicated VM:

1. Provision VM (4GB RAM minimum) — `cryptobot-db`
2. Install PostgreSQL 15+
3. `pg_dump vnedge | psql -h <new-vm> -U vnedge vnedge_new`
4. Update VM1 `.env` `DATABASE_URL=postgresql://vnedge:pwd@<new-vm-ip>:5432/vnedge`
5. Open firewall: only VM1 IP allowed on port 5432
6. Restart cryptobot service
7. Verify DB connection in logs

## Orderbook L2 API Fix (Wave 7 Item)

`orderbook.enabled: false` in settings.yaml — Delta L2 API was returning 3027 errors.

To re-enable:
1. Investigate Delta API: response format may have changed
2. Test endpoint: `curl -s "https://api.india.delta.exchange/v2/l2orderbook/{product_id}?depth=20"`
3. Fix `exchange/delta_client.py:get_l2_orderbook()` parsing logic
4. Set `orderbook.enabled: true`
5. Monitor logs for fetch errors

## Database Backups

Backups stored in `/home/opc/db_backups/vnedge_YYYYMMDD_HHMMSS.sql.gz`
14-day rolling retention (older backups auto-pruned).

Restore: `gunzip -c <backup>.sql.gz | sudo -u postgres psql vnedge`

## Multi-User Setup

1. Admin logs in → Admin tab → "+ Add User" (TODO: build UI)
2. Or DB direct: `INSERT INTO users (email, password_hash, role) VALUES (...)`
3. Admin sets user's API keys via Admin → user card → "+ Add Key"
4. User logs in → Profile → switches mode to demo/live
5. Signal fires → user's UserRealManager executes on their exchange
