import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DB_URL
from infra.models import Base

logger = logging.getLogger("snitch.db")

engine = create_engine(DB_URL, echo=False, future=True)

# Create all tables
Base.metadata.create_all(engine)
logger.info(f"Database tables created/verified: {DB_URL}")

SessionLocal = sessionmaker(bind=engine)