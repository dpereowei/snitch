import logging
import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DB_URL
from infra.models import Base

logger = logging.getLogger("snitch.db")

# Ensure database directory exists and is writable
db_path = Path(DB_URL.replace("sqlite:///", ""))
db_dir = db_path.parent
db_dir.mkdir(parents=True, exist_ok=True)
logger.info(f"Database directory: {db_dir} (writable: {os.access(db_dir, os.W_OK)})")

engine = create_engine(DB_URL, echo=False, future=True)

# Create all tables
Base.metadata.create_all(engine)
logger.info(f"Database tables created/verified: {DB_URL}")
logger.info(f"Database file: {db_path} (exists: {db_path.exists()})")

SessionLocal = sessionmaker(bind=engine)