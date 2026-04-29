from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, String, Integer, Text, DateTime, Boolean
import datetime

Base = declarative_base()

class Event(Base):
    __tablename__ = "events"

    id = Column(String, primary_key=True)  # deterministic event id
    type = Column(String)

    pod_uid = Column(String)
    namespace = Column(String)
    pod_name = Column(String)
    container_name = Column(String)

    node = Column(String)
    reason = Column(Text)
    message = Column(Text)

    restart_count = Column(Integer)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    delivered = Column(Boolean, default=False)
    retry_count = Column(Integer, default=0)