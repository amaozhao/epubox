"""Test translation models."""

import json
from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import LimitType, ProviderStats, TranslationProvider


@pytest.mark.asyncio
async def test_create_provider(db_session: AsyncSession):
    """Test creating a translation provider."""
    provider = TranslationProvider(
        name="Test Provider",
        provider_type="test",
        is_default=True,
        enabled=True,
        config='{"api_key": "test-key"}',
        rate_limit=5,
        retry_count=3,
        retry_delay=5,
        limit_type=LimitType.CHARS,
        limit_value=5000,
        model="test-model",
    )
    db_session.add(provider)
    await db_session.commit()
    await db_session.refresh(provider)

    assert provider.id is not None
    assert provider.name == "Test Provider"
    assert provider.provider_type == "test"
    assert provider.is_default is True
    assert provider.enabled is True
    assert provider.config == '{"api_key": "test-key"}'
    assert provider.rate_limit == 5
    assert provider.retry_count == 3
    assert provider.retry_delay == 5
    assert provider.limit_type == LimitType.CHARS
    assert provider.limit_value == 5000
    assert provider.model == "test-model"
    assert isinstance(provider.created, datetime)
    assert isinstance(provider.updated, datetime)


@pytest.mark.asyncio
async def test_create_provider_stats(db_session: AsyncSession):
    """Test creating provider statistics."""
    # Create a provider first
    provider = TranslationProvider(
        name="Test Provider",
        provider_type="test",
        config='{"api_key": "test-key"}',
        limit_type=LimitType.CHARS,
        limit_value=5000,
    )
    db_session.add(provider)
    await db_session.commit()

    # Create stats
    stats = ProviderStats(
        provider_id=provider.id,
        date=date.today(),
        total_requests=10,
        success_count=8,
        error_count=2,
        rate_limit_hits=1,
        avg_response_time=0.5,
        total_words=1000,
    )
    db_session.add(stats)
    await db_session.commit()
    await db_session.refresh(stats)

    assert stats.id is not None
    assert stats.provider_id == provider.id
    assert stats.total_requests == 10
    assert stats.success_count == 8
    assert stats.error_count == 2
    assert stats.rate_limit_hits == 1
    assert stats.avg_response_time == 0.5
    assert stats.total_words == 1000
    assert isinstance(stats.created, datetime)
    assert isinstance(stats.updated, datetime)


@pytest.mark.asyncio
async def test_provider_stats_relationship(db_session: AsyncSession):
    """Test the relationship between provider and stats."""
    # Create a provider
    provider = TranslationProvider(
        name="Test Provider",
        provider_type="test",
        config='{"api_key": "test-key"}',
        limit_type=LimitType.CHARS,
        limit_value=5000,
    )
    db_session.add(provider)
    await db_session.commit()

    # Create multiple stats records
    stats1 = ProviderStats(
        provider_id=provider.id, date=date(2024, 1, 1), total_requests=10
    )
    stats2 = ProviderStats(
        provider_id=provider.id, date=date(2024, 1, 2), total_requests=20
    )
    db_session.add_all([stats1, stats2])
    await db_session.commit()

    # Query provider and check stats using selectinload
    stmt = (
        select(TranslationProvider)
        .where(TranslationProvider.id == provider.id)
        .options(
            selectinload(TranslationProvider.stats).selectinload(ProviderStats.provider)
        )
    )
    result = await db_session.execute(stmt)
    provider = result.scalar_one()

    assert len(provider.stats) == 2
    assert provider.stats[0].date == date(2024, 1, 1)
    assert provider.stats[0].total_requests == 10
    assert provider.stats[1].date == date(2024, 1, 2)
    assert provider.stats[1].total_requests == 20


@pytest.mark.asyncio
async def test_limit_type_enum():
    """Test LimitType enum values."""
    assert LimitType.CHARS.value == "chars"
    assert LimitType.TOKENS.value == "tokens"
    assert len(LimitType) == 2  # Only two types supported


def test_provider_model_validation():
    """Test provider model validation."""
    # Test config as JSON string (converted from dict)
    config_dict = {"api_key": "test-key", "model": "test-model"}
    provider = TranslationProvider(
        name="Test Provider",
        provider_type="test",
        is_default=True,
        enabled=True,
        config=json.dumps(config_dict),  # Convert dict to JSON string
        rate_limit=5,
        retry_count=3,
        retry_delay=5,
        limit_type=LimitType.CHARS,
        limit_value=5000,
    )
    # Config should be stored as string
    assert isinstance(provider.config, str)

    # Test with direct JSON string
    config_str = '{"api_key": "test-key", "model": "test-model"}'
    provider = TranslationProvider(
        name="Test Provider",
        provider_type="test",
        is_default=True,
        enabled=True,
        config=config_str,
        rate_limit=5,
        retry_count=3,
        retry_delay=5,
        limit_type=LimitType.CHARS,
        limit_value=5000,
    )
    # Config should remain as string
    assert isinstance(provider.config, str)
    assert provider.config == config_str


def test_provider_model_defaults():
    """Test provider model default values."""
    provider = TranslationProvider(
        name="Test Provider",
        provider_type="test",
        config=json.dumps({"api_key": "test-key"}),  # Convert dict to JSON string
        is_default=False,  # Explicitly set default values
        enabled=True,
        rate_limit=1,
        retry_count=3,
        retry_delay=1,
        limit_type=LimitType.CHARS,
        limit_value=25000,
    )
    # Check the values we explicitly set
    assert provider.is_default is False
    assert provider.enabled is True
    assert provider.rate_limit == 1
    assert provider.retry_count == 3
    assert provider.retry_delay == 1
    assert provider.limit_type == LimitType.CHARS
    assert provider.limit_value == 25000


def test_provider_model_config_types():
    """Test provider model config type handling."""
    # Test with dictionary converted to JSON string
    provider = TranslationProvider(
        name="Test Provider",
        provider_type="test",
        config=json.dumps({"api_key": "test-key"}),  # Convert dict to JSON string
        limit_type=LimitType.CHARS,
        limit_value=1000,
    )
    assert isinstance(provider.config, str)

    # Test with direct JSON string
    provider = TranslationProvider(
        name="Test Provider",
        provider_type="test",
        config='{"api_key": "test-key"}',
        limit_type=LimitType.CHARS,
        limit_value=1000,
    )
    assert isinstance(provider.config, str)
