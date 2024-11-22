from typing import Dict, Optional, Type
from .base import BaseTranslationAdapter
from .google_translate import GoogleTranslateAdapter
from .deepl import DeepLTranslationAdapter

class TranslationAdapterFactory:
    """Factory for creating translation service adapters."""

    _adapters: Dict[str, Type[BaseTranslationAdapter]] = {
        "google": GoogleTranslateAdapter,
        "deepl": DeepLTranslationAdapter
    }

    @classmethod
    def get_adapter(
        cls,
        service: str,
        api_key: str,
        **kwargs
    ) -> Optional[BaseTranslationAdapter]:
        """Get a translation adapter instance.
        
        Args:
            service: Name of the translation service ("google" or "deepl")
            api_key: API key for the service
            **kwargs: Additional configuration options for the adapter
        
        Returns:
            An instance of the translation adapter or None if service not found
        """
        adapter_class = cls._adapters.get(service.lower())
        if adapter_class:
            return adapter_class(api_key=api_key, **kwargs)
        return None

    @classmethod
    def register_adapter(
        cls,
        name: str,
        adapter_class: Type[BaseTranslationAdapter]
    ) -> None:
        """Register a new translation adapter.
        
        Args:
            name: Name of the translation service
            adapter_class: The adapter class to register
        """
        cls._adapters[name.lower()] = adapter_class

    @classmethod
    def get_available_services(cls) -> list[str]:
        """Get list of available translation services.
        
        Returns:
            List of registered service names
        """
        return list(cls._adapters.keys())
