#!/usr/bin/env python3
"""
Snitch operational utilities for monitoring and debugging.
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path


def get_db_path() -> str:
    """Get database path from environment or use default."""
    import os
    return os.getenv("SNITCH_DB_PATH", "/tmp/snitch_events.db")


def print_table(headers, rows):
    """Pretty-print a table."""
    if not rows:
        print("  (empty)")
        return

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))

    # Header
    print("  " + " | ".join(
        h.ljust(w) for h, w in zip(headers, col_widths)
    ))
    print("  " + "-+-".join("-" * w for w in col_widths))

    # Rows
    for row in rows:
        print("  " + " | ".join(
            str(cell).ljust(w) for cell, w in zip(row, col_widths)
        ))


def cmd_stats(db_path: str):
    """Show summary statistics."""
    print("\n[STATS] Snitch Event Statistics")
    print("=" * 60)

    conn = sqlite3.connect(db_path)
    try:
        # Total counts
        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        delivered = conn.execute(
            "SELECT COUNT(*) FROM events WHERE delivered = 1"
        ).fetchone()[0]
        undelivered = conn.execute(
            "SELECT COUNT(*) FROM events WHERE delivered = 0"
        ).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM dead_letter"
        ).fetchone()[0]

        print(f"\n  Total Events:       {total}")
        print(f"  Delivered:          {delivered}")
        print(f"  Undelivered:        {undelivered}")
        print(f"  Dead-Letter Queue:  {failed}")

        # Event type breakdown
        print("\n  By Type:")
        types = conn.execute("""
            SELECT type, COUNT(*) as count FROM events GROUP BY type
        """).fetchall()
        for event_type, count in types:
            status = conn.execute(
                "SELECT COUNT(*) FROM events WHERE type = ? AND delivered = 1",
                (event_type,)
            ).fetchone()[0]
            print(f"    {event_type.upper():12} {count:6} (delivered: {status})")

        # Retry distribution
        print("\n  Retry Distribution:")
        retries = conn.execute("""
            SELECT retry_count, COUNT(*) as count FROM events
            WHERE delivered = 0 GROUP BY retry_count ORDER BY retry_count
        """).fetchall()
        for retry_count, count in retries:
            print(f"    Attempts {retry_count}: {count}")

    finally:
        conn.close()


def cmd_undelivered(db_path: str, limit: int = 20):
    """Show undelivered events."""
    print(f"\n[QUEUE] Undelivered Events (limit {limit})")
    print("=" * 60)

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT id, type, pod_name, namespace, retry_count, created_at
            FROM events
            WHERE delivered = 0
            ORDER BY created_at ASC
            LIMIT ?
        """, (limit,)).fetchall()

        headers = ["Event ID", "Type", "Pod", "Namespace", "Retries", "Created"]
        print_table(headers, rows)

    finally:
        conn.close()


def cmd_dlq(db_path: str, limit: int = 20):
    """Show dead-letter queue."""
    print(f"\n[DLQ] Dead-Letter Queue (limit {limit})")
    print("=" * 60)

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT dl.event_id, dl.reason, dl.created_at
            FROM dead_letter dl
            ORDER BY dl.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

        headers = ["Event ID", "Reason", "DLQ'd At"]
        print_table(headers, rows)

    finally:
        conn.close()


def cmd_pod_events(db_path: str, pod_uid: str):
    """Show all events for a pod."""
    print(f"\n[SEARCH] Events for Pod {pod_uid}")
    print("=" * 60)

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT id, type, container_name, reason, restart_count,
                   delivered, retry_count, created_at
            FROM events
            WHERE pod_uid = ?
            ORDER BY created_at DESC
        """, (pod_uid,)).fetchall()

        if not rows:
            print("  (no events found)")
            return

        headers = ["Event ID", "Type", "Container", "Reason", "Restart", "Delivered", "Retries", "Created"]
        print_table(headers, rows)

    finally:
        conn.close()


def cmd_retry_events(db_path: str, min_retries: int = 3):
    """Show events with high retry counts."""
    print(f"\n[RETRY] High-Retry Events (>= {min_retries} attempts)")
    print("=" * 60)

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT id, type, pod_name, retry_count, last_retry_at
            FROM events
            WHERE retry_count >= ? AND delivered = 0
            ORDER BY retry_count DESC, last_retry_at ASC
        """, (min_retries,)).fetchall()

        headers = ["Event ID", "Type", "Pod", "Retries", "Last Retry"]
        print_table(headers, rows)

    finally:
        conn.close()


def cmd_recent(db_path: str, hours: int = 1, limit: int = 20):
    """Show recently created events."""
    print(f"\n[RECENT] Recent Events (last {hours}h, limit {limit})")
    print("=" * 60)

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT id, type, pod_name, namespace, delivered, created_at
            FROM events
            WHERE created_at > datetime('now', '-' || ? || ' hours')
            ORDER BY created_at DESC
            LIMIT ?
        """, (hours, limit)).fetchall()

        headers = ["Event ID", "Type", "Pod", "Namespace", "Delivered", "Created"]
        print_table(headers, rows)

    finally:
        conn.close()


def cmd_export_json(db_path: str, output: str):
    """Export events to JSON."""
    import json

    print(f"\n[EXPORT] Exporting events to {output}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        events = [dict(row) for row in conn.execute(
            "SELECT * FROM events ORDER BY created_at DESC"
        ).fetchall()]

        with open(output, "w") as f:
            json.dump(events, f, indent=2, default=str)

        print(f"  ✓ Exported {len(events)} events")

    finally:
        conn.close()


def cmd_cleanup(db_path: str, days: int = 30, dry_run: bool = True):
    """Remove old delivered events."""
    print(f"\n[CLEANUP] Cleanup old delivered events (>{days} days, dry_run={dry_run})")
    print("=" * 60)

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("""
            SELECT COUNT(*) FROM events
            WHERE delivered = 1 AND created_at < datetime('now', '-' || ? || ' days')
        """, (days,)).fetchone()[0]

        if count == 0:
            print("  (nothing to clean)")
            return

        if dry_run:
            print(f"  Would delete {count} events")
        else:
            conn.execute("""
                DELETE FROM events
                WHERE delivered = 1 AND created_at < datetime('now', '-' || ? || ' days')
            """, (days,))
            conn.commit()
            print(f"  ✓ Deleted {count} events")

    finally:
        conn.close()


def main():
    if len(sys.argv) < 2:
        print("""
Usage: snitch-ops.py <command> [options]

Commands:
  stats                 Show summary statistics
  undelivered [N]       Show N undelivered events (default: 20)
  dlq [N]               Show N dead-letter events (default: 20)
  pod-events <UID>      Show all events for a pod UID
  retry [N]             Show events with ≥N retry attempts (default: 3)
  recent [HOURS] [N]    Show recent events (default: 1h, 20 events)
  export-json <FILE>    Export all events to JSON
  cleanup [DAYS]        Remove delivered events older than DAYS (default: 30, dry-run)
  cleanup-force [DAYS]  Same as cleanup but actually deletes

Environment:
  SNITCH_DB_PATH        Path to snitch_events.db (default: /tmp/snitch_events.db)
        """)
        return 1

    db_path = get_db_path()
    if not Path(db_path).exists():
        print(f"[ERROR] Database not found: {db_path}")
        return 1

    cmd = sys.argv[1]

    try:
        if cmd == "stats":
            cmd_stats(db_path)
        elif cmd == "undelivered":
            limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
            cmd_undelivered(db_path, limit)
        elif cmd == "dlq":
            limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
            cmd_dlq(db_path, limit)
        elif cmd == "pod-events":
            if len(sys.argv) < 3:
                print("[ERROR] Missing pod UID")
                return 1
            cmd_pod_events(db_path, sys.argv[2])
        elif cmd == "retry":
            min_retries = int(sys.argv[2]) if len(sys.argv) > 2 else 3
            cmd_retry_events(db_path, min_retries)
        elif cmd == "recent":
            hours = int(sys.argv[2]) if len(sys.argv) > 2 else 1
            limit = int(sys.argv[3]) if len(sys.argv) > 3 else 20
            cmd_recent(db_path, hours, limit)
        elif cmd == "export-json":
            if len(sys.argv) < 3:
                print("[ERROR] Missing output file")
                return 1
            cmd_export_json(db_path, sys.argv[2])
        elif cmd == "cleanup":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
            cmd_cleanup(db_path, days, dry_run=True)
        elif cmd == "cleanup-force":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
            cmd_cleanup(db_path, days, dry_run=False)
        else:
            print(f"[ERROR] Unknown command: {cmd}")
            return 1

        print()
        return 0

    except Exception as e:
        print(f"[ERROR] Error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
