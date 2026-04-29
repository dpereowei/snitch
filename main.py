import logging
import sys
import signal
import threading
import time
from pathlib import Path

# Ensure root directory is in path for module imports BEFORE importing submodules
sys.path.insert(0, str(Path(__file__).parent))

from core.watcher import PodWatcher
from core.state import PodState
from core.processor import Processor
from workers.dispatcher import emit, worker
from infra.db import SessionLocal

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("snitch")

def start_workers(n=4):
    logger.info(f"Starting {n} worker threads")
    for _ in range(n):
        t = threading.Thread(target=worker, daemon=True)
        t.start()

def shutdown(signum, frame):
    logger.info("Shutdown signal received, gracefully terminating...")
    sys.exit(0)

def main():
    try:
        logger.info("snitch pod event listener starting...")
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
        
        start_workers()

        state = PodState()
        processor = Processor(state)

        watcher = PodWatcher()
        logger.info("✓ Listening for pod events...")
        
        # CRITICAL: Emit startup test event to validate entire pipeline
        logger.info("=" * 80)
        logger.info("STARTUP: Emitting pipeline validation event...")
        logger.info("If you see this in Slack within 5 seconds, the system is OPERATIONAL")
        logger.info("=" * 80)
        
        for test_event in processor.generate_startup_test_event():
            emit(test_event)
        
        # Give workers time to process test event
        time.sleep(2)
        logger.info("Pipeline validation event emitted. Entering watch loop.")
        logger.info("=" * 80)

        for pod in watcher.stream():
            events = processor.process(pod)

            for e in events:
                emit(e)
    
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()