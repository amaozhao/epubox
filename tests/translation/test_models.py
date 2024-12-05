"""Test translation models."""

from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.translation.models import LimitType, ProviderStats, TranslationProvider


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
