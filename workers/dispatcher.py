import logging
import threading
from queue import Queue, Full
from infra.db import SessionLocal
from infra.models import Event
from infra.slack import SlackClient
from config import QUEUE_MAX_SIZE

logger = logging.getLogger("snitch.dispatcher")

event_queue = Queue(maxsize=QUEUE_MAX_SIZE)

def emit(event):
    try:
        event_queue.put_nowait(event)
    except Full:
        # backpressure signal (do NOT crash watcher)
        pass


def worker():
    slack = SlackClient()
    logger.info("Worker thread started")

    while True:
        event = event_queue.get()

        session = SessionLocal()

        try:
            # audit first
            exists = session.get(Event, event["id"])
            if not exists:
                session.add(Event(**event))
                session.commit()
                logger.info(f"Event audited: {event['id']} ({event['type']})")

            slack.send(event)

            db_event = session.get(Event, event["id"])
            db_event.delivered = True
            session.commit()
            logger.info(f"Event delivered: {event['id']}")

        except Exception as e:
            logger.error(f"Error processing event: {e}", exc_info=True)
            session.rollback()

            try:
                db_event = session.get(Event, event["id"])
                if db_event:
                    db_event.retry_count += 1
                    session.commit()
            except Exception as retry_error:
                logger.error(f"Error updating retry count: {retry_error}")

        finally:
            session.close()