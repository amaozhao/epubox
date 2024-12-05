"""Test translation manager."""

from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest

from app.translation.errors import ConfigurationError, ProviderError
from app.translation.manager import TranslationManager
from app.translation.models import LimitType, ProviderStats, TranslationProvider
from app.translation.providers.base import TranslationProvider as BaseProvider


class MockProvider(BaseProvider):
    """Mock translation provider for testing."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.translate_mock = AsyncMock()

    def get_provider_type(self) -> str:
        return "mock"

    def validate_config(self, config: dict) -> bool:
        return True

    async def _initialize(self):
        pass

    async def _cleanup(self):
        pass

    async def _translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        return await self.translate_mock(
            text=text, source_lang=source_lang, target_lang=target_lang
        )


@pytest.fixture
async def translation_manager(db_session):
    """Create a translation manager with a mock provider."""
    manager = TranslationManager(db_session)

    # Create and register mock provider
    provider_model = TranslationProvider(
        name="Mock Provider",
        provider_type="mock",
        is_default=True,
        enabled=True,
        config='{"key": "value"}',
        limit_type=LimitType.CHARS,
        limit_value=5000,
    )
    db_session.add(provider_model)
    await db_session.commit()

    # Register provider type
    manager.register_provider("mock", MockProvider)

    # Initialize manager
    await manager.initialize()

    yield manager

    await manager.cleanup()


async def test_manager_initialization(db_session, translation_manager):
    """Test translation manager initialization."""
    # Verify provider is registered and initialized
    assert len(translation_manager._active_providers) == 1
    assert translation_manager._default_provider_id is not None


async def test_translate_with_default_provider(translation_manager):
    """Test translation using default provider."""
    # Setup mock response
    provider = list(translation_manager._active_providers.values())[0]
    provider.translate_mock.return_value = "Translated Text"

    # Perform translation
    result = await translation_manager.translate(
        text="Original Text", source_lang="en", target_lang="zh"
    )

    assert result == "Translated Text"
    provider.translate_mock.assert_called_once_with(
        text="Original Text", source_lang="en", target_lang="zh"
    )


async def test_translate_with_specific_provider(db_session, translation_manager):
    """Test translation using a specific provider."""
    # Create another provider
    provider_model = TranslationProvider(
        name="Another Mock Provider",
        provider_type="mock",
        is_default=False,
        enabled=True,
        config='{"key": "value"}',
        limit_type=LimitType.TOKENS,
        limit_value=4000,
    )
    db_session.add(provider_model)
    await db_session.commit()

    # Reinitialize manager to load new provider
    await translation_manager.initialize()

    # Get the new provider
    provider = translation_manager._active_providers[provider_model.id]
    provider.translate_mock.return_value = "Another Translation"

    # Perform translation with specific provider
    result = await translation_manager.translate(
        text="Original Text",
        source_lang="en",
        target_lang="zh",
        provider_id=provider_model.id,
    )

    assert result == "Another Translation"
    provider.translate_mock.assert_called_once()


async def test_translation_updates_stats(db_session, translation_manager):
    """Test that translation updates provider statistics."""
    provider_id = translation_manager._default_provider_id
    provider = translation_manager._active_providers[provider_id]
    provider.translate_mock.return_value = "Translated Text"

    # Perform translation
    await translation_manager.translate(
        text="Original Text", source_lang="en", target_lang="zh"
    )

    # Check stats
    stats = await db_session.get(ProviderStats, provider_id)
    assert stats is not None
    assert stats.total_requests == 1
    assert stats.success_count == 1
    assert stats.error_count == 0


async def test_translation_error_handling(translation_manager):
    """Test error handling during translation."""
    provider_id = translation_manager._default_provider_id
    provider = translation_manager._active_providers[provider_id]
    provider.translate_mock.side_effect = Exception("Translation failed")

    with pytest.raises(Exception) as exc_info:
        await translation_manager.translate(
            text="Original Text", source_lang="en", target_lang="zh"
        )

    assert str(exc_info.value) == "Translation failed"


async def test_provider_not_found(translation_manager):
    """Test error when provider is not found."""
    with pytest.raises(ProviderError) as exc_info:
        await translation_manager.translate(
            text="Original Text", source_lang="en", target_lang="zh", provider_id=999
        )

    assert "Provider 999 not found" in str(exc_info.value)


async def test_no_default_provider(db_session):
    """Test error when no default provider is configured."""
    # Create manager without default provider
    manager = TranslationManager(db_session)

    with pytest.raises(ConfigurationError) as exc_info:
        await manager.translate(
            text="Original Text", source_lang="en", target_lang="zh"
        )

    assert "no default provider configured" in str(exc_info.value).lower()
