"""Stable public entry points for the translation executor."""

from engine.agents.runtime import model_input_budget  # noqa: F401 - legacy monkeypatch seam
from engine.execution.engine import TranslationEngine
from engine.execution.repair import (
    import_repair_file,
    retry_failed_units,
    run_translation,
    validate_repair_file,
    validate_retry_failed_units,
)
from engine.execution.state import TranslationRunResult
from engine.services.coherence import save_document_check, save_window_result  # noqa: F401 - legacy test seam

__all__ = [
    "TranslationEngine",
    "TranslationRunResult",
    "import_repair_file",
    "retry_failed_units",
    "run_translation",
    "validate_repair_file",
    "validate_retry_failed_units",
]
