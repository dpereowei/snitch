#!/usr/bin/env python3
"""
Loss-aware, deduplicated, event-driven alert pipeline with audit logging.

Core principle: Track unique anomaly events, not pods.
- Event identity is deterministic: (uid, name, count)
- Database PRIMARY KEY ensures dedup
- No duplicates. No missed events.
"""

import sqlite3
import logging
import sys
import signal
import time
import json
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone, timedelta
from threading import Thread, Event
from enum import Enum
import os

import requests
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from requests.exceptions import RequestException

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("snitch")


# ============================================================================
# TYPES & CONSTANTS
# ============================================================================

class EventType(Enum):
    RESTART = "restart"
    EVICTION = "eviction"


class EventState:
    UNDELIVERED = False
    DELIVERED = True


# ============================================================================
# DATABASE LAYER
# ============================================================================

class EventDatabase:
    """Audit log + dedup + retry tracking combined."""

    def __init__(self, db_path: str = "/tmp/snitch_events.db"):
        self.db_path = db_path
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """Get DB connection with row factory."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        """Create schema if not exists."""
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    pod_uid TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    pod_name TEXT NOT NULL,
                    container_name TEXT,
                    node TEXT,
                    reason TEXT,
                    message TEXT,
                    restart_count INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    delivered BOOLEAN DEFAULT 0,
                    retry_count INTEGER DEFAULT 0,
                    last_retry_at TIMESTAMP
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS dead_letter (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (event_id) REFERENCES events(id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS deployment_marker (
                    id TEXT PRIMARY KEY,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS container_baseline (
                    id TEXT PRIMARY KEY,
                    pod_uid TEXT NOT NULL,
                    container_name TEXT NOT NULL,
                    baseline_restart_count INTEGER NOT NULL,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(pod_uid, container_name)
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_undelivered 
                ON events(delivered, retry_count)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pod_uid 
                ON events(pod_uid)
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS container_baseline (
                    id TEXT PRIMARY KEY,
                    pod_uid TEXT NOT NULL,
                    container_name TEXT NOT NULL,
                    baseline_restart_count INTEGER NOT NULL,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(pod_uid, container_name)
                )
            """)

            conn.commit()
            logger.info(f"Database initialized: {self.db_path}")
        except Exception as e:
            logger.error(f"Schema init failed: {e}")
            raise
        finally:
            conn.close()

    def record_deployment_start(self):
        """Record that this deployment instance started now."""
        conn = self._get_conn()
        try:
            # Clear old marker and record new one
            conn.execute("DELETE FROM deployment_marker")
            conn.execute("INSERT INTO deployment_marker (id) VALUES (?)", ("current",))
            conn.commit()
            logger.info("Deployment marker recorded")
        except Exception as e:
            logger.error(f"Failed to record deployment marker: {e}")
        finally:
            conn.close()

    def mark_old_events_as_delivered(self):
        """
        Mark all undelivered events created before this deployment as delivered.
        Prevents re-sending historic events on pod restart.
        """
        conn = self._get_conn()
        try:
            # Get the deployment start time
            marker = conn.execute(
                "SELECT started_at FROM deployment_marker WHERE id = ?", ("current",)
            ).fetchone()
            
            if not marker:
                logger.warning("No deployment marker found, skipping old event cleanup")
                return
            
            started_at = marker["started_at"]
            
            # Mark all undelivered events created before deployment as delivered
            conn.execute("""
                UPDATE events
                SET delivered = 1
                WHERE delivered = 0 AND created_at < ?
            """, (started_at,))
            
            count = conn.total_changes
            conn.commit()
            
            if count > 0:
                logger.info(f"Marked {count} old events as delivered (before deployment)")
        except Exception as e:
            logger.error(f"Failed to mark old events: {e}")
        finally:
            conn.close()

    def insert_event(self, event: Dict[str, Any]) -> bool:
        """
        Insert event (dedup happens via PRIMARY KEY).
        Returns True if inserted, False if duplicate.
        """
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO events
                (id, type, pod_uid, namespace, pod_name, container_name,
                 node, reason, message, restart_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event["id"],
                event["type"],
                event["pod_uid"],
                event["namespace"],
                event["pod_name"],
                event.get("container_name"),
                event.get("node"),
                event.get("reason"),
                event.get("message"),
                event.get("restart_count"),
            ))
            conn.commit()
            logger.info(f"Event inserted: {event['id']}")
            return True
        except sqlite3.IntegrityError:
            # Duplicate - already processed
            logger.debug(f"Duplicate event skipped: {event['id']}")
            return False
        except Exception as e:
            logger.error(f"Insert failed for {event.get('id')}: {e}")
            raise
        finally:
            conn.close()

    def get_undelivered(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch undelivered events for processing."""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT * FROM events
                WHERE delivered = 0 AND retry_count < 6
                ORDER BY created_at ASC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def mark_delivered(self, event_id: str):
        """Mark event as successfully delivered."""
        conn = self._get_conn()
        try:
            conn.execute("""
                UPDATE events SET delivered = 1 WHERE id = ?
            """, (event_id,))
            conn.commit()
            logger.info(f"Event marked delivered: {event_id}")
        finally:
            conn.close()

    def increment_retry(self, event_id: str):
        """Increment retry count."""
        conn = self._get_conn()
        try:
            conn.execute("""
                UPDATE events
                SET retry_count = retry_count + 1,
                    last_retry_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (event_id,))
            conn.commit()
            logger.warning(f"Retry incremented: {event_id}")
        finally:
            conn.close()

    def move_to_dead_letter(self, event_id: str, reason: str):
        """Move event to dead-letter queue after max retries."""
        conn = self._get_conn()
        try:
            # Generate unique dead-letter ID
            dl_id = f"dl:{event_id}:{datetime.utcnow().timestamp()}"
            conn.execute("""
                INSERT INTO dead_letter (id, event_id, reason)
                VALUES (?, ?, ?)
            """, (dl_id, event_id, reason))
            conn.execute("""
                UPDATE events SET delivered = -1 WHERE id = ?
            """, (event_id,))
            conn.commit()
            logger.error(f"Event moved to DLQ: {event_id} - {reason}")
        except Exception as e:
            logger.error(f"DLQ move failed: {e}")
        finally:
            conn.close()

    def get_pod_events(self, pod_uid: str) -> List[Dict[str, Any]]:
        """Fetch all events for a pod (audit trail)."""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT * FROM events WHERE pod_uid = ?
                ORDER BY created_at DESC
            """, (pod_uid,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_last_restart_count(self, pod_uid: str, container_name: str) -> int:
        """Get last known restart count for a container."""
        conn = self._get_conn()
        try:
            row = conn.execute("""
                SELECT MAX(restart_count) as max_count FROM events
                WHERE pod_uid = ? AND container_name = ? AND type = ?
            """, (pod_uid, container_name, EventType.RESTART.value)).fetchone()
            return row["max_count"] or 0
        finally:
            conn.close()

    def record_container_baseline(self, pod_uid: str, container_name: str, restart_count: int):
        """Record the baseline restart count for a container during initial sync."""
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO container_baseline
                (id, pod_uid, container_name, baseline_restart_count)
                VALUES (?, ?, ?, ?)
            """, (
                f"baseline:{pod_uid}:{container_name}",
                pod_uid,
                container_name,
                restart_count
            ))
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to record baseline for {pod_uid}/{container_name}: {e}")
        finally:
            conn.close()

    def get_container_baseline(self, pod_uid: str, container_name: str) -> Optional[int]:
        """Get baseline restart count for a container, or None if not set."""
        conn = self._get_conn()
        try:
            row = conn.execute("""
                SELECT baseline_restart_count FROM container_baseline
                WHERE pod_uid = ? AND container_name = ?
            """, (pod_uid, container_name)).fetchone()
            return row["baseline_restart_count"] if row else None
        finally:
            conn.close()

    def record_container_baseline(self, pod_uid: str, container_name: str, restart_count: int):
        """Record the baseline restart count for a container during initial sync."""
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO container_baseline
                (id, pod_uid, container_name, baseline_restart_count)
                VALUES (?, ?, ?, ?)
            """, (
                f"baseline:{pod_uid}:{container_name}",
                pod_uid,
                container_name,
                restart_count
            ))
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to record baseline for {pod_uid}/{container_name}: {e}")
        finally:
            conn.close()

    def backfill_baseline_from_events(self, pod_uid: str, container_name: str) -> bool:
        """
        Backfill baseline from highest restart_count in events table.
        Used for backward compatibility with existing pods that already have events.
        Returns True if backfilled, False if already exists or no events found.
        """
        conn = self._get_conn()
        try:
            # Check if baseline already exists
            existing = conn.execute("""
                SELECT baseline_restart_count FROM container_baseline
                WHERE pod_uid = ? AND container_name = ?
            """, (pod_uid, container_name)).fetchone()
            
            if existing:
                return False  # Already has baseline
            
            # Get highest restart count from events table
            row = conn.execute("""
                SELECT MAX(restart_count) as max_count FROM events
                WHERE pod_uid = ? AND container_name = ? AND type = ?
            """, (pod_uid, container_name, EventType.RESTART.value)).fetchone()
            
            max_count = row["max_count"] if row else None
            
            if max_count is None:
                return False  # No events found
            
            # Backfill baseline from highest event
            conn.execute("""
                INSERT OR IGNORE INTO container_baseline
                (id, pod_uid, container_name, baseline_restart_count)
                VALUES (?, ?, ?, ?)
            """, (
                f"baseline:{pod_uid}:{container_name}",
                pod_uid,
                container_name,
                max_count
            ))
            conn.commit()
            logger.info(f"Backfilled baseline for {pod_uid}/{container_name}: {max_count}")
            return True
        except Exception as e:
            logger.error(f"Failed to backfill baseline: {e}")
            return False
        finally:
            conn.close()


# ============================================================================
# EVENT EMISSION LOGIC
# ============================================================================

class EventEmitter:
    """Detects and emits anomaly events from Kubernetes pods."""

    def __init__(self, db: EventDatabase):
        self.db = db

    def handle_pod(self, pod: client.V1Pod, record_baseline: bool = False) -> List[Dict[str, Any]]:
        """
        Detect restart + eviction events from a pod.
        Returns list of emitted events (even if duplicates).
        
        If record_baseline=True, records current state as baseline without emitting events.
        This is used during initial sync to skip alerting on historical restarts.
        """
        events = []

        # ---- Restart Events ----
        if pod.status and pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                if record_baseline:
                    # During initial sync, record baseline without emitting events
                    if cs.restart_count > 0:
                        self.db.record_container_baseline(
                            pod.metadata.uid, cs.name, cs.restart_count
                        )
                else:
                    # After baseline established, emit events only for increases
                    if cs.restart_count > 0:
                        # Check if we have a baseline for this container
                        baseline = self.db.get_container_baseline(pod.metadata.uid, cs.name)
                        
                        if baseline is not None:
                            # We have a baseline - only emit if count exceeds it
                            for restart_idx in range(baseline + 1, cs.restart_count + 1):
                                event = self._build_restart_event(pod, cs, restart_idx)
                                events.append(event)
                        else:
                            # No baseline - try to backfill from events table (backward compat)
                            backfilled = self.db.backfill_baseline_from_events(pod.metadata.uid, cs.name)
                            if backfilled:
                                # After backfilling, use the new baseline
                                baseline = self.db.get_container_baseline(pod.metadata.uid, cs.name)
                                for restart_idx in range(baseline + 1, cs.restart_count + 1):
                                    event = self._build_restart_event(pod, cs, restart_idx)
                                    events.append(event)
                            else:
                                # No events table entry either - use gap handling
                                last_known = self.db.get_last_restart_count(
                                    pod.metadata.uid, cs.name
                                )
                                for restart_idx in range(last_known + 1, cs.restart_count + 1):
                                    event = self._build_restart_event(pod, cs, restart_idx)
                                    events.append(event)

        # ---- Eviction Events ----
        # Only emit eviction events after baseline established (not during initial sync)
        if not record_baseline and pod.status and pod.status.reason == "Evicted":
            event = self._build_eviction_event(pod)
            events.append(event)

        return events

    def _build_restart_event(
        self, pod: client.V1Pod, container_status: client.V1ContainerStatus, restart_count: int) -> Dict[str, Any]:
        """Build a restart event."""
        event_id = f"restart:{pod.metadata.uid}:{container_status.name}:{restart_count}"

        last_state = container_status.last_state or client.V1ContainerState()
        reason = getattr(last_state.terminated, "reason", "Unknown") if last_state.terminated else "Unknown"
        message = getattr(last_state.terminated, "message", "") if last_state.terminated else ""

        return {
            "id": event_id,
            "type": EventType.RESTART.value,
            "pod_uid": pod.metadata.uid,
            "namespace": pod.metadata.namespace,
            "pod_name": pod.metadata.name,
            "container_name": container_status.name,
            "node": pod.spec.node_name or "unknown",
            "reason": reason,
            "message": message,
            "restart_count": restart_count,
        }

    def _build_eviction_event(self, pod: client.V1Pod) -> Dict[str, Any]:
        """Build an eviction event."""
        event_id = f"eviction:{pod.metadata.uid}"

        return {
            "id": event_id,
            "type": EventType.EVICTION.value,
            "pod_uid": pod.metadata.uid,
            "namespace": pod.metadata.namespace,
            "pod_name": pod.metadata.name,
            "container_name": None,
            "node": pod.spec.node_name or "unknown",
            "reason": "Evicted",
            "message": pod.status.message or "",
            "restart_count": None,
        }


# ============================================================================
# SLACK DELIVERY
# ============================================================================

class SlackDelivery:
    """Sends formatted events to Slack."""

    def __init__(self, webhook_url: str):
        if not webhook_url:
            raise ValueError("SLACK_WEBHOOK_URL not set")
        self.webhook_url = webhook_url

    def send(self, event: Dict[str, Any]) -> bool:
        """
        Send event to Slack. Returns True if successful.
        """
        payload = self._format_event(event)
        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10
            )
            if response.status_code == 200:
                logger.info(f"Slack delivery success: {event['id']}")
                return True
            else:
                logger.error(
                    f"Slack rejected {event['id']}: "
                    f"HTTP {response.status_code}"
                )
                return False
        except RequestException as e:
            logger.error(f"Slack delivery failed for {event['id']}: {e}")
            return False

    def _format_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Format event as Slack Block Kit payload."""
        event_type = event["type"].upper()
        icon = "[RESTART]" if event["type"] == "restart" else "[EVICTION]"

        fields = [
            {
                "type": "mrkdwn",
                "text": f"*Pod:*\n`{event['namespace']}/{event['pod_name']}`"
            },
            {
                "type": "mrkdwn",
                "text": f"*Node:*\n`{event['node']}`"
            },
        ]

        if event.get("container_name"):
            fields.append({
                "type": "mrkdwn",
                "text": f"*Container:*\n`{event['container_name']}`"
            })

        if event.get("reason"):
            fields.append({
                "type": "mrkdwn",
                "text": f"*Reason:*\n`{event['reason']}`"
            })

        if event.get("restart_count") is not None:
            fields.append({
                "type": "mrkdwn",
                "text": f"*Restart #:*\n`{event['restart_count']}`"
            })

        # Parse and format timestamp if available
        if event.get("created_at"):
            try:
                # Handle both string and datetime formats
                if isinstance(event["created_at"], str):
                    # Parse ISO format or SQLite format
                    ts = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
                else:
                    ts = event["created_at"]
                
                # Ensure timestamp is UTC-aware
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                
                # Convert to WAT (UTC+1)
                wat_tz = timezone(timedelta(hours=1))
                ts_wat = ts.astimezone(wat_tz)
                formatted_time = ts_wat.strftime("%Y-%m-%d %H:%M:%S WAT")
            except Exception:
                formatted_time = str(event["created_at"])
            
            fields.append({
                "type": "mrkdwn",
                "text": f"*Occurred:*\n`{formatted_time}`"
            })

        sections = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{icon} {event_type}"
                }
            },
            {
                "type": "section",
                "fields": fields
            }
        ]

        if event.get("message"):
            sections.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Message:*\n```{event['message'][:500]}```"
                }
            })

        return {
            "text": f"{icon} {event_type}",
            "blocks": sections
        }


# ============================================================================
# DELIVERY WORKER
# ============================================================================

class DeliveryWorker:
    """Process events and deliver to Slack."""

    def __init__(self, db: EventDatabase, slack: SlackDelivery):
        self.db = db
        self.slack = slack
        self.running = False
        self.stop_event = Event()

    def start(self):
        """Start worker in background thread."""
        self.running = True
        self.stop_event.clear()
        thread = Thread(target=self._run, daemon=True)
        thread.start()
        logger.info("Delivery worker started")

    def stop(self):
        """Stop worker."""
        self.running = False
        self.stop_event.set()
        logger.info("Delivery worker stopping")

    def _run(self):
        """Main worker loop."""
        while self.running:
            try:
                events = self.db.get_undelivered(limit=50)

                if not events:
                    self.stop_event.wait(timeout=5)
                    continue

                for event in events:
                    if not self.running:
                        break

                    success = self.slack.send(event)

                    if success:
                        self.db.mark_delivered(event["id"])
                    else:
                        retry_count = event.get("retry_count", 0)

                        if retry_count >= 5:
                            self.db.move_to_dead_letter(
                                event["id"],
                                f"Max retries exceeded (attempt {retry_count + 1})"
                            )
                        else:
                            self.db.increment_retry(event["id"])

                time.sleep(1)

            except Exception as e:
                logger.error(f"Worker loop error: {e}", exc_info=True)
                time.sleep(5)


# ============================================================================
# KUBERNETES WATCHER
# ============================================================================

class PodWatcher:
    """Watch Kubernetes pods and emit events."""

    def __init__(self, db: EventDatabase, emitter: EventEmitter):
        self.db = db
        self.emitter = emitter
        self.running = False
        self.stop_event = Event()
        self.resource_version = None
        self.initial_sync_done = False

    def start(self):
        """Start watcher in background thread."""
        self.running = True
        self.stop_event.clear()
        thread = Thread(target=self._run, daemon=True)
        thread.start()
        logger.info("Pod watcher started")

    def stop(self):
        """Stop watcher."""
        self.running = False
        self.stop_event.set()
        logger.info("Pod watcher stopping")

    def _run(self):
        """Main watch loop with restart on error."""
        while self.running:
            try:
                self._watch()
            except Exception as e:
                logger.error(f"Watch loop error: {e}", exc_info=True)
                logger.info("Retrying in 5 seconds...")
                time.sleep(5)

    def _watch(self):
        """Watch for pod changes."""
        try:
            config.load_incluster_config()
        except:
            config.load_kube_config()

        v1 = client.CoreV1Api()
        watcher = watch.Watch()

        try:
            for event in watcher.stream(
                v1.list_pod_for_all_namespaces,
                resource_version=self.resource_version,
                timeout_seconds=30,
                _preload_content=True
            ):
                if not self.running:
                    break

                event_type = event["type"]
                pod: client.V1Pod = event["object"]

                # Update resource version for resume
                if pod.metadata.resource_version:
                    self.resource_version = pod.metadata.resource_version

                logger.debug(
                    f"Pod event: {event_type} "
                    f"{pod.metadata.namespace}/{pod.metadata.name}"
                )

                # During initial sync (LIST phase), record baselines for all pods
                # without emitting alerts. The LIST phase only sends ADDED events.
                # Once we see MODIFIED or DELETED, we've transitioned to watch mode.
                record_baseline = not self.initial_sync_done and event_type == "ADDED"
                
                if record_baseline:
                    logger.debug(
                        f"Initial sync in progress - recording baseline for "
                        f"{pod.metadata.namespace}/{pod.metadata.name}"
                    )

                # Detect anomalies
                anomalies = self.emitter.handle_pod(pod, record_baseline=record_baseline)

                # Transition from LIST phase to watch phase: 
                # If we see a MODIFIED or DELETED event, we're past the initial sync
                if not self.initial_sync_done and event_type in ("MODIFIED", "DELETED"):
                    self.initial_sync_done = True
                    logger.info(
                        "Initial sync complete - baselines recorded, "
                        "now alerting on new restarts/evictions only"
                    )

                for anomaly in anomalies:
                    inserted = self.db.insert_event(anomaly)

                    if inserted:
                        logger.info(
                            f"Event emitted: {anomaly['type']} "
                            f"{anomaly['namespace']}/{anomaly['pod_name']}"
                        )

        except ApiException as e:
            logger.error(f"API error: {e}")
        except Exception as e:
            logger.error(f"Watcher error: {e}")


# ============================================================================
# ORCHESTRATION
# ============================================================================

class Snitch:
    """Main orchestrator."""

    def __init__(self):
        self.db = EventDatabase()
        self.emitter = EventEmitter(self.db)

        slack_url = os.getenv("SLACK_WEBHOOK_URL")
        if not slack_url:
            logger.warning("SLACK_WEBHOOK_URL not set - alerts will not be delivered")
            self.slack = None
        else:
            self.slack = SlackDelivery(slack_url)

        self.watcher = PodWatcher(self.db, self.emitter)
        self.worker = None if not self.slack else DeliveryWorker(self.db, self.slack)

    def start(self):
        """Start all components."""
        logger.info("Starting Snitch...")

        # Record this deployment start time and clean up old events
        self.db.record_deployment_start()
        self.db.mark_old_events_as_delivered()

        self.watcher.start()

        if self.worker:
            self.worker.start()
        else:
            logger.warning("Delivery worker not started (no Slack configured)")

        logger.info("Snitch running")

    def stop(self):
        """Stop all components gracefully."""
        logger.info("Stopping Snitch...")
        self.watcher.stop()
        if self.worker:
            self.worker.stop()
        logger.info("Snitch stopped")

    def wait(self):
        """Wait for interrupt signal."""
        def handler(signum, frame):
            logger.info("Received interrupt signal")
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    snitch = Snitch()
    snitch.start()
    snitch.wait()
