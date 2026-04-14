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
from datetime import datetime
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
                CREATE INDEX IF NOT EXISTS idx_undelivered 
                ON events(delivered, retry_count)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pod_uid 
                ON events(pod_uid)
            """)

            conn.commit()
            logger.info(f"Database initialized: {self.db_path}")
        except Exception as e:
            logger.error(f"Schema init failed: {e}")
            raise
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


# ============================================================================
# EVENT EMISSION LOGIC
# ============================================================================

class EventEmitter:
    """Detects and emits anomaly events from Kubernetes pods."""

    def __init__(self, db: EventDatabase):
        self.db = db

    def handle_pod(self, pod: client.V1Pod) -> List[Dict[str, Any]]:
        """
        Detect restart + eviction events from a pod.
        Returns list of emitted events (even if duplicates).
        """
        events = []

        # ---- Restart Events ----
        if pod.status and pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                if cs.restart_count > 0:
                    # Gap handling: emit events for all unprocessed restart counts
                    last_known = self.db.get_last_restart_count(
                        pod.metadata.uid, cs.name
                    )

                    for restart_idx in range(last_known + 1, cs.restart_count + 1):
                        event = self._build_restart_event(pod, cs, restart_idx)
                        events.append(event)

        # ---- Eviction Events ----
        if pod.status and pod.status.reason == "Evicted":
            event = self._build_eviction_event(pod)
            events.append(event)

        return events

    def _build_restart_event(
        self, pod: client.V1Pod, container_status: client.V1ContainerStatus,
        restart_count: int
    ) -> Dict[str, Any]:
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

                # Detect anomalies
                anomalies = self.emitter.handle_pod(pod)

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
