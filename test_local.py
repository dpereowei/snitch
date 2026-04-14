#!/usr/bin/env python3
"""
Local testing utilities for Snitch.

Test database creation, event emission, and deduplication logic
without needing a Kubernetes cluster.
"""

import os
import sys
import tempfile
from datetime import datetime
from kubernetes import client

# Add module to path
sys.path.insert(0, os.path.dirname(__file__))

from main import EventDatabase, EventEmitter, EventType


def test_database():
    """Test database creation and schema."""
    print("\n=== Testing Database ===")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = EventDatabase(db_path=db_path)

        # Test event insertion
        event1 = {
            "id": "restart:uid-001:app:1",
            "type": EventType.RESTART.value,
            "pod_uid": "uid-001",
            "namespace": "default",
            "pod_name": "app-xyz",
            "container_name": "app",
            "node": "node-1",
            "reason": "CrashLoopBackOff",
            "message": "Exit code 1",
            "restart_count": 1,
        }

        inserted = db.insert_event(event1)
        assert inserted, "First insert should succeed"
        print("✓ Insert new event: success")

        # Test deduplication
        duplicate = db.insert_event(event1)
        assert not duplicate, "Duplicate insert should fail"
        print("✓ Deduplication: duplicate rejected")

        # Test query
        undelivered = db.get_undelivered()
        assert len(undelivered) == 1, "Should have 1 undelivered event"
        assert undelivered[0]["id"] == "restart:uid-001:app:1"
        print("✓ Query undelivered: found event")

        # Test retry tracking
        db.increment_retry("restart:uid-001:app:1")
        updated = db.get_undelivered()
        assert updated[0]["retry_count"] == 1, "Retry count should be 1"
        print("✓ Retry increment: tracked")

        # Test mark delivered
        db.mark_delivered("restart:uid-001:app:1")
        undelivered = db.get_undelivered()
        assert len(undelivered) == 0, "Delivered event should not appear in undelivered"
        print("✓ Mark delivered: event removed from queue")

        # Test DLQ
        event2 = {
            "id": "eviction:uid-002",
            "type": EventType.EVICTION.value,
            "pod_uid": "uid-002",
            "namespace": "default",
            "pod_name": "bad-pod",
            "container_name": None,
            "node": "node-2",
            "reason": "Evicted",
            "message": "Node cordoned",
            "restart_count": None,
        }
        db.insert_event(event2)
        db.move_to_dead_letter("eviction:uid-002", "Test DLQ move")
        print("✓ DLQ: event moved to dead-letter queue")

        print("✓ All database tests passed!\n")

    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_event_emission():
    """Test event ID generation and deduplication logic."""
    print("\n=== Testing Event Emission ===")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = EventDatabase(db_path=db_path)
        emitter = EventEmitter(db)

        # Create mock pod with restart
        pod = client.V1Pod(
            api_version="v1",
            kind="Pod",
            metadata=client.V1ObjectMeta(
                uid="pod-001",
                name="app-pod",
                namespace="default",
            ),
            spec=client.V1PodSpec(
                node_name="node-1",
                containers=[],
            ),
            status=client.V1PodStatus(
                container_statuses=[
                    client.V1ContainerStatus(
                        name="app",
                        restart_count=3,
                        last_state=client.V1ContainerState(
                            terminated=client.V1ContainerStateTerminated(
                                reason="CrashLoopBackOff",
                                message="Exit code 1",
                                exit_code=1,
                            )
                        ),
                        ready=False,
                        image="app:1.0",
                        image_id="app@sha256:abc",
                    )
                ]
            )
        )

        # First detection: should emit events 1, 2, 3
        events = emitter.handle_pod(pod)
        assert len(events) == 3, "Should generate 3 events (1, 2, 3)"
        for i, event in enumerate(events, 1):
            assert event["restart_count"] == i, f"Event {i} should have restart_count={i}"
            assert event["id"] == f"restart:pod-001:app:{i}"
        print(f"✓ Gap detection: emitted {len(events)} events for restart_count jump")

        # Insert these events
        for event in events:
            db.insert_event(event)
        print("✓ All events inserted without duplicates")

        # Second detection: should emit nothing (already in DB)
        events2 = emitter.handle_pod(pod)
        assert len(events2) == 0, "Should not emit duplicates"
        print("✓ Duplicate prevention: no new events on second pass")

        # Pod restarts again: 3 → 5
        pod.status.container_statuses[0].restart_count = 5
        events3 = emitter.handle_pod(pod)
        assert len(events3) == 2, "Should emit events 4 and 5"
        assert events3[0]["restart_count"] == 4
        assert events3[1]["restart_count"] == 5
        print("✓ Incremental gap: emitted only new restart counts")

        print("✓ All emission tests passed!\n")

    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_eviction():
    """Test eviction event detection."""
    print("\n=== Testing Eviction Events ===")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = EventDatabase(db_path=db_path)
        emitter = EventEmitter(db)

        # Create mock evicted pod
        pod = client.V1Pod(
            api_version="v1",
            kind="Pod",
            metadata=client.V1ObjectMeta(
                uid="evicted-pod-001",
                name="evicted-pod",
                namespace="default",
            ),
            spec=client.V1PodSpec(
                node_name="node-1",
                containers=[],
            ),
            status=client.V1PodStatus(
                reason="Evicted",
                message="The node had condition: DiskPressure.",
                container_statuses=[]
            )
        )

        events = emitter.handle_pod(pod)
        assert len(events) == 1, "Should emit 1 eviction event"
        assert events[0]["type"] == EventType.EVICTION.value
        assert events[0]["id"] == "eviction:evicted-pod-001"
        print("✓ Eviction event: detected and ID correct")

        # Insert and verify dedup
        db.insert_event(events[0])
        
        # Emitter will still generate the event (emitter doesn't check DB)
        # But a second insert should be rejected (dedup at DB level)
        events2 = emitter.handle_pod(pod)
        assert len(events2) == 1, "Emitter generates event again (normal)"
        duplicate = db.insert_event(events2[0])
        assert not duplicate, "Duplicate eviction insert should fail"
        print("✓ Eviction dedup: duplicate rejected at DB level")

        print("✓ All eviction tests passed!\n")

    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def main():
    print("\n" + "="*60)
    print("Snitch Local Test Suite")
    print("="*60)

    try:
        test_database()
        test_event_emission()
        test_eviction()

        print("\n" + "="*60)
        print("[SUCCESS] ALL TESTS PASSED")
        print("="*60 + "\n")
        return 0

    except AssertionError as e:
        print(f"\n[FAILED] TEST FAILED: {e}\n")
        return 1
    except Exception as e:
        print(f"\n[ERROR] ERROR: {e}\n")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
