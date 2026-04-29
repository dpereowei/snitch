import logging
import time
import threading
from kubernetes import client, watch
from kubecontext import load_kubeconfig, get_v1_client

logger = logging.getLogger("snitch.watcher")

class PodWatcher:
    def __init__(self):
        api_client = load_kubeconfig()
        self.v1 = get_v1_client(api_client)
        self.w = watch.Watch()
        self.resource_version = None
        self.reconnect_delay = 2  # seconds
        self.max_reconnect_delay = 30
        logger.info("PodWatcher initialized")

    def stream(self):
        reconnect_delay = self.reconnect_delay
        connection_attempts = 0
        
        while True:
            connection_attempts += 1
            try:
                logger.info(f"Attempting to connect to Kubernetes API (attempt {connection_attempts})")
                
                stream = self.v1.list_pod_for_all_namespaces(
                    watch=True,
                    resource_version=self.resource_version,
                    timeout_seconds=300  # 5 minute timeout per chunk
                )

                last_activity_log = time.time()
                event_count = 0
                reconnect_delay = self.reconnect_delay  # Reset on successful connection
                connection_attempts = 0

                for event in stream:
                    pod = event["object"]
                    self.resource_version = pod.metadata.resource_version
                    event_count += 1
                    
                    # Periodic heartbeat to prove stream is alive
                    now = time.time()
                    if now - last_activity_log > 30:
                        logger.info(f"✓ Stream alive - processed {event_count} pod events in last 30s")
                        last_activity_log = now
                        event_count = 0
                    
                    yield pod

            except Exception as e:
                logger.error(f"Watch stream error: {e}", exc_info=True)
                logger.warning(f"Will reconnect in {reconnect_delay}s...")
                time.sleep(reconnect_delay)
                
                # Exponential backoff with cap
                reconnect_delay = min(reconnect_delay * 2, self.max_reconnect_delay)
                
                # Log persistence issue after multiple failures
                if connection_attempts > 3:
                    logger.critical(f"⚠️  Multiple connection failures ({connection_attempts}). Check cluster connectivity.")