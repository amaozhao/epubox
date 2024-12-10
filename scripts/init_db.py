"""Initialize the database with default translation providers."""

import json
import os
import sys
from pathlib import Path

# Add the parent directory to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.translation.models import LimitType, TranslationProvider

# Create database engine
DATABASE_URL = "sqlite:///epubox.db"
engine = create_engine(DATABASE_URL)


def init_db():
    """Initialize the database."""
    # Create all tables
    Base.metadata.create_all(engine)

    # Create a session
    with Session(engine) as session:
        # Check if we already have providers
        if session.query(TranslationProvider).count() > 0:
            print("Database already initialized")
            return

        # Add Mistral provider
        mistral_config = {
            "api_key": os.getenv("MISTRAL_API_KEY", ""),
            "model": "mistral-tiny",
            "max_retries": 3,
            "retry_delay": 5,
        }

        mistral = TranslationProvider(
            name="Mistral",
            provider_type="mistral",
            is_default=True,
            enabled=True,
            config=mistral_config,
            rate_limit=3,
            retry_count=3,
            retry_delay=5,
            limit_type=LimitType.TOKENS,  # 添加限制类型
            limit_value=1000,  # 添加限制值
        )

        session.add(mistral)
        session.commit()

        print("Database initialized with default providers")


if __name__ == "__main__":
    init_db()
