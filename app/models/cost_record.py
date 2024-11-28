"""Cost record model."""

from datetime import datetime
from enum import Enum

from sqlalchemy import JSON, Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class CostType(str, Enum):
    TRANSLATION = "translation"
    VALIDATION = "validation"
    MEMORY_LOOKUP = "memory_lookup"
    API_CALL = "api_call"
    STORAGE = "storage"


class CostRecord(Base):
    """Model for tracking translation-related costs."""

    __tablename__ = "cost_records"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("translation_projects.id"), nullable=False)
    cost_type = Column(SQLEnum(CostType), nullable=False)
    amount = Column(Float, nullable=False)  # Cost in default currency (e.g., USD)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    details = Column(JSON, nullable=True)  # Additional cost details
    provider = Column(String(50), nullable=False)  # Service provider
    resource_type = Column(String(50), nullable=True)  # Specific resource type
    resource_id = Column(String(255), nullable=True)  # ID of the resource
    usage_metrics = Column(
        JSON, nullable=True
    )  # Usage details (e.g., tokens, API calls)

    # Relationships
    project = relationship("TranslationProject", back_populates="cost_records")

    def __repr__(self):
        return (
            f"<CostRecord(id={self.id}, type={self.cost_type}, amount={self.amount})>"
        )
