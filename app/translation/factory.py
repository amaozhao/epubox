"""Provider factory for translation service."""

import importlib
from pathlib import Path
from typing import Dict, Type

import yaml

from .models import TranslationProvider as TranslationProviderModel
from .providers.base import TranslationProvider


class ProviderFactory:
    """Provider factory for creating translation provider instances."""

    def __init__(self):
        self._provider_configs = self._load_provider_configs()
        self._provider_classes: Dict[str, Type[TranslationProvider]] = {}

    def _load_provider_configs(self) -> dict:
        """Load provider configurations from YAML file."""
        config_path = Path(__file__).parent / "config" / "providers.yaml"
        with open(config_path) as f:
            return yaml.safe_load(f)

    def _load_provider_class(self, provider_type: str) -> Type[TranslationProvider]:
        """Dynamically load provider class based on configuration."""
        config = self._provider_configs.get(provider_type)
        if not config:
            raise ValueError(f"No configuration found for provider: {provider_type}")

        module_path = config["module"]
        class_name = config["class"]

        try:
            module = importlib.import_module(module_path)
            return getattr(module, class_name)
        except (ImportError, AttributeError) as e:
            raise ValueError(f"Failed to load provider {provider_type}: {e}")

    def get_provider_class(self, provider_type: str) -> Type[TranslationProvider]:
        """Get provider class, loading it if necessary."""
        if provider_type not in self._provider_classes:
            self._provider_classes[provider_type] = self._load_provider_class(
                provider_type
            )
        return self._provider_classes[provider_type]

    def create_provider(
        self, provider_model: TranslationProviderModel
    ) -> TranslationProvider:
        """Create a provider instance based on the provider model."""
        provider_class = self.get_provider_class(provider_model.provider_type)
        return provider_class(provider_model)

    def get_provider_config(self, provider_type: str) -> dict:
        """Get provider configuration from YAML file."""
        return self._provider_configs.get(provider_type, {})
