# Snitch: Loss-Aware Pod Alert Pipeline

**A production-grade anomaly detection system for Kubernetes pods with deterministic deduplication and audit logging.**

## Core Architecture

Snitch tracks unique anomaly events, not pods. Every event has a deterministic identity that prevents duplicates without distributed caching.

### Event Types

#### Restart Events
```
ID: restart:{pod_uid}:{container_name}:{restart_count}
```
- Detected when `container_status.restart_count > 0`
- Gap-aware: if count jumps 1→5, all intermediate events (2,3,4) are emitted
- Ensures no missed restarts in fast-cycling containers

#### Eviction Events
```
ID: eviction:{pod_uid}
```
- Detected when `pod.status.reason == "Evicted"`
- Only emitted once per pod lifecycle

## Database Schema

All functionality (dedup, audit, retry tracking) lives in a single schema:

```sql
CREATE TABLE events (
    id TEXT PRIMARY KEY,              -- Auto-dedup via PK
    type TEXT,                        -- restart | eviction
    pod_uid TEXT,
    namespace TEXT,
    pod_name TEXT,
    container_name TEXT,
    node TEXT,
    reason TEXT,
    message TEXT,
    restart_count INTEGER,
    created_at TIMESTAMP,
    delivered BOOLEAN,                -- Delivery tracking
    retry_count INTEGER,              -- Retry backoff
    last_retry_at TIMESTAMP
);

CREATE TABLE dead_letter (
    id TEXT PRIMARY KEY,
    event_id TEXT UNIQUE,             -- After 6 failed retries
    reason TEXT,
    created_at TIMESTAMP
);
```

## Event Flow

```
1. Watcher → Detect pod state change
   └─ Extract container.restart_count, pod.reason
   
2. Emitter → Generate deterministic event ID
   └─ If restart_count gap, emit all intermediate events
   
3. Database → Insert event
   └─ PRIMARY KEY ensures dedup (duplicate → silently skipped)
   
4. Worker → Fetch undelivered events (limit=50)
   ├─ SUCCESS → mark_delivered()
   └─ FAILURE:
      ├─ retry_count < 5 → increment_retry()
      └─ retry_count >= 5 → move_to_dead_letter()
      
5. Slack → Human notification
```

## Failure Modes & Mitigations

### Watch Dies
**Mitigation:** Infinite retry loop with `resourceVersion` resume
- When watch crashes, watcher thread catches exception
- Logs error and retries in 5 seconds
- `resourceVersion` preserved → only new events from disconnect point

### Service Restarts
**Mitigation:** Persisted database + deterministic IDs
- DB on persistent volume
- Event IDs deterministic (always same for same pod+container+count)
- On restart: only undelivered events in DB queue
- No re-emission of delivered events

### Slack Delivery Fails
**Mitigation:** Retry queue with dead-letter fallback
- Event stays in DB with `delivered=0`
- Worker retries every loop (~5 sec)
- After 6 attempts (30s cumulative): moves to DLQ
- Dead-letter prevents infinite retry spam

### Pod Restarts Too Fast
**Example:** `restart_count` jumps 1→5 instantly
**Mitigation:** Gap detection
```python
last_known = db.get_last_restart_count(pod_uid, container)
for i in range(last_known + 1, current + 1):
    emit_event(restart_event(i))
```
Each intermediate count generates unique event ID → no lost anomalies

## Configuration

### Environment Variables

```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
```

If not set, watcher and emitter run but worker doesn't deliver (useful for testing).

## Deployment

### Local Testing
```bash
pip install -r requirements.txt

# With local kubeconfig
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
python main.py
```

### Kubernetes Deployment
```bash
# 1. Build image
docker build -t snitch:latest .

# 2. Update secret in k8s-deployment.yaml
# 3. Deploy
kubectl apply -f k8s-deployment.yaml

# 4. Verify
kubectl logs -f -n monitoring deployment/snitch
```

### Slack Setup
1. Create Slack App: https://api.slack.com/apps
2. Enable Incoming Webhooks
3. Create webhook for target channel
4. Set `SLACK_WEBHOOK_URL` environment variable

## Monitoring & Audit

### View Event Audit Trail
```bash
sqlite3 /tmp/snitch_events.db
SELECT * FROM events WHERE pod_uid = 'xyz' ORDER BY created_at DESC;
```

### Check Dead-Letter Queue
```bash
sqlite3 /tmp/snitch_events.db
SELECT event_id, reason, created_at FROM dead_letter ORDER BY created_at DESC;
```

### Check Undelivered Events
```bash
sqlite3 /tmp/snitch_events.db
SELECT id, type, retry_count, last_retry_at FROM events
WHERE delivered = 0 ORDER BY last_retry_at ASC;
```

## Guarantees

[OK] **No Duplicate Alerts** — Deterministic event IDs + PRIMARY KEY
[OK] **No Missed Events** — Gap detection for restart count jumps
[OK] **Full Audit Trail** — Every event logged with timestamps
[OK] **Retry-Safe Delivery** — At-least-once with configurable backoff
[OK] **Graceful Failure** — Dead-letter prevents cascade failures

## Slack Message Format

Structured Block Kit payloads with:
- Event type ([RESTART] / [EVICTION])
- Pod namespace/name
- Node assignment
- Container name (if applicable)
- Failure reason
- Restart count
- Full termination message (first 500 chars)

## Performance

- **Single-threaded watcher:** Handles all pod events
- **Separate delivery worker:** Non-blocking Slack delivery
- **Batch processing:** Worker fetches 50 undelivered events per loop
- **DB limits:** Index on `(delivered, retry_count)` for fast queries
- **Memory:** ~128MB baseline, 512MB limit recommended

## Troubleshooting

### Events Not Being Emitted
- Check watcher logs: `kubectl logs -n monitoring deployment/snitch`
- Verify RBAC: `kubectl auth can-i get pods --as=system:serviceaccount:monitoring:snitch -n monitoring`
- Verify pod has `restart_count > 0` or `status.reason == "Evicted"`

### Events In DB But Not Delivered
- Check Slack webhook URL in secret
- Verify network access to Slack (curl test if in Pod)
- Check retry_count and last_retry_at timestamps
- Move stalled events to DLQ manually if needed:
  ```sql
  UPDATE events SET delivered = -1 WHERE id = 'xyz';
  INSERT INTO dead_letter VALUES ('dl-...', 'xyz', 'Manual DLQ');
  ```

### High DB Disk Usage
- Prune old delivered events:
  ```sql
  DELETE FROM events WHERE delivered = 1 
  AND created_at < datetime('now', '-30 days');
  ```

## Production Checklist

- [ ] Verify RBAC permissions (see k8s-deployment.yaml)
- [ ] Provision persistent volume for events DB
- [ ] Set Slack webhook secret securely
- [ ] Configure resource limits per your environment
- [ ] Test Slack delivery before going live
- [ ] Set up monitoring on DLQ growth
- [ ] Plan retention policy for delivered events
- [ ] Document runbook for manual DLQ recovery
