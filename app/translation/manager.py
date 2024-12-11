"""Translation manager."""

import json
from datetime import datetime
from typing import Dict, Optional, Type

from sqlalchemy.future import select
from sqlalchemy.orm import Session

from app.db.models import ProviderStats
from app.db.models import TranslationProvider as ProviderModel

from .errors import ConfigurationError, ProviderError
from .providers.base import AsyncContextManager, TranslationProvider


class TranslationManager(AsyncContextManager["TranslationManager"]):
    """Manager for translation services."""

    def __init__(self, db: Session):
        self.db = db
        self._providers: Dict[str, Type[TranslationProvider]] = {}
        self._active_providers: Dict[int, TranslationProvider] = {}
        self._default_provider_id: Optional[int] = None

    async def initialize(self):
        """Initialize translation manager."""
        # Load provider configurations from database
        stmt = select(ProviderModel).where(ProviderModel.enabled == True)
        result = await self.db.execute(stmt)
        providers = result.scalars().all()

        print(f"Found providers: {len(providers)}")  # Debug log
        print(f"Registered provider types: {self._providers}")  # Debug log

        for provider in providers:
            print(
                f"Processing provider: {provider.name}, type: {provider.provider_type}"
            )  # Debug log
            if provider.is_default:
                self._default_provider_id = provider.id

            provider_class = self._providers.get(provider.provider_type)
            if not provider_class:
                print(
                    f"No provider class found for type: {provider.provider_type}"
                )  # Debug log
                continue

            try:
                instance = provider_class(provider_model=provider)
                await instance.initialize()
                self._active_providers[provider.id] = instance
                print(
                    f"Successfully initialized provider: {provider.name}"
                )  # Debug log
            except Exception as e:
                # Log error but continue with other providers
                print(f"Failed to initialize provider {provider.name}: {str(e)}")

    async def cleanup(self):
        """Cleanup translation manager resources."""
        for provider in self._active_providers.values():
            await provider.cleanup()
        self._active_providers.clear()

    def register_provider(
        self, provider_type: str, provider_class: Type[TranslationProvider]
    ):
        """Register a new provider type."""
        self._providers[provider_type] = provider_class

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        provider_id: Optional[int] = None,
        **kwargs,
    ) -> str:
        """Translate text using specified or default provider."""
        if not provider_id:
            provider_id = self._default_provider_id
        if not provider_id:
            raise ConfigurationError(
                "No provider specified and no default provider configured"
            )

        provider = self._active_providers.get(provider_id)
        if not provider:
            raise ProviderError(f"Provider {provider_id} not found or not initialized")

        try:
            # Perform translation
            translated_text = await provider.translate(
                text=text, source_lang=source_lang, target_lang=target_lang, **kwargs
            )

            # Update stats
            await self._update_stats(provider_id, success=True)

            return translated_text
        except Exception as e:
            # Update stats
            await self._update_stats(provider_id, success=False)
            raise

    async def _update_stats(self, provider_id: int, success: bool = True):
        """Update provider statistics."""
        today = datetime.utcnow().date()

        # Get or create stats record for today
        stmt = select(ProviderStats).where(
            ProviderStats.provider_id == provider_id, ProviderStats.date == today
        )
        result = await self.db.execute(stmt)
        stats = result.scalar_one_or_none()

        if not stats:
            stats = ProviderStats(provider_id=provider_id, date=today)
            self.db.add(stats)
            # Commit to let default values take effect
            await self.db.commit()

            # Refresh stats to get the record with default values
            await self.db.refresh(stats)

        # Update stats
        stats.total_requests += 1
        if success:
            stats.success_count += 1
        else:
            stats.error_count += 1

        # Commit changes
        await self.db.commit()
