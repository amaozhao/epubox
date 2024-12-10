"""Check database content."""

import sys
from pathlib import Path

# Add the parent directory to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.translation.models import TranslationProvider

# Create database engine
DATABASE_URL = "sqlite:///epubox.db"
engine = create_engine(DATABASE_URL)


def check_db():
    """Check database content."""
    with Session(engine) as session:
        providers = session.query(TranslationProvider).all()
        for provider in providers:
            print(f"\nProvider ID: {provider.id}")
            print(f"Name: {provider.name}")
            print(f"Type: {provider.provider_type}")
            print(f"Config type: {type(provider.config)}")
            print(f"Config: {provider.config}")
            print("-" * 50)


if __name__ == "__main__":
    check_db()
