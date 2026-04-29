import logging
from core.events import restart_event_id, eviction_event_id

logger = logging.getLogger("snitch.processor")

class Processor:
    def __init__(self, state):
        self.state = state

    def process(self, pod):
        events = []

        pod_uid = pod.metadata.uid
        prev = self.state.get(pod_uid)

        containers = pod.status.container_statuses or []

        # RESTARTS
        for c in containers:
            old = prev.get(c.name, 0)

            if c.restart_count > old:
                for rc in range(old + 1, c.restart_count + 1):
                    events.append({
                        "id": restart_event_id(pod_uid, c.name, rc),
                        "type": "restart",
                        "pod_uid": pod_uid,
                        "namespace": pod.metadata.namespace,
                        "pod_name": pod.metadata.name,
                        "container_name": c.name,
                        "node": pod.spec.node_name,
                        "restart_count": rc
                    })

        # EVICTION
        if pod.status.reason == "Evicted":
            events.append({
                "id": eviction_event_id(pod_uid),
                "type": "eviction",
                "pod_uid": pod_uid,
                "namespace": pod.metadata.namespace,
                "pod_name": pod.metadata.name,
                "node": pod.spec.node_name,
                "reason": pod.status.reason,
                "message": pod.status.message
            })

        # update state AFTER processing
        self.state.update(
            pod_uid,
            {c.name: c.restart_count for c in containers}
        )

        return events

    def generate_startup_test_event(self):
        """
        Generate a test event to validate the entire pipeline.
        This proves: queue → worker → db → Slack all work.
        """
        return [{
            "id": "test:startup:pipeline-validation",
            "type": "test",
            "pod_uid": "snitch-startup-check",
            "namespace": "snitch",
            "pod_name": "startup-validation",
            "container_name": "system",
            "node": "local",
            "reason": "STARTUP",
            "message": "Pipeline validation - if you see this in Slack, the system is fully operational",
            "restart_count": 0,
            "delivered": False
        }]