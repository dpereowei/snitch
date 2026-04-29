import logging
import time
from kubernetes import client, watch
from kubecontext import load_kubeconfig, get_v1_client

logger = logging.getLogger("snitch.watcher")

class PodWatcher:
    def __init__(self):
        api_client = load_kubeconfig()
        self.v1 = get_v1_client(api_client)
        self.w = watch.Watch()
        self.resource_version = None
        logger.info("PodWatcher initialized")

    def stream(self):
        while True:
            try:
                stream = self.v1.list_pod_for_all_namespaces(
                    watch=True,
                    resource_version=self.resource_version
                )

                last_activity_log = time.time()
                event_count = 0

                for event in stream:
                    pod = event["object"]
                    self.resource_version = pod.metadata.resource_version
                    event_count += 1
                    
                    # Periodic heartbeat to prove stream is alive
                    now = time.time()
                    if now - last_activity_log > 30:
                        logger.info(f"Stream alive - processed {event_count} pod events in last 30s")
                        last_activity_log = now
                        event_count = 0
                    
                    yield pod

            except Exception as e:
                logger.error(f"Watch stream error: {e}", exc_info=True)
                time.sleep(2)