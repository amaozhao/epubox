"""
Phase 3: Translation manifest persistence.

Provides TranslationManifest (Pydantic model) and ManifestManager for
storing, loading, and rebuilding translation state.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from engine.schemas.chunk import Chunk
from engine.schemas.translator import TranslationStatus

try:
    import appdirs
except ImportError:  # pragma: no cover
    appdirs = None


# -----------------------------------------------------------------------------
# TranslationManifest
# -----------------------------------------------------------------------------

class TranslationManifest(BaseModel):
    """
    Holds all translation state for a single EPUB document.

    Attributes:
        doc_id: Unique document identifier (typically the EPUB filename stem).
        chunks: Ordered list of translation chunks.
        created_at: UTC timestamp when this manifest was first created.
        updated_at: UTC timestamp of the most recent save.
    """

    doc_id: str
    chunks: List[Chunk] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def model_dump(self, **kwargs):
        # Pydantic v2 compatibility - handle datetime serialization
        data = super().model_dump(**kwargs)
        if kwargs.get("mode") == "json":
            data["created_at"] = self.created_at.isoformat()
            data["updated_at"] = self.updated_at.isoformat()
        return data


# -----------------------------------------------------------------------------
# ManifestManager
# -----------------------------------------------------------------------------

class ManifestManager:
    """
    Persists TranslationManifest objects as JSON files.

    Files are stored under ``storage_dir`` with the naming scheme
    ``{doc_id}.json``.
    """

    def __init__(self, storage_dir: Optional[str] = None) -> None:
        """
        Args:
            storage_dir: Directory for manifest files.
                Defaults to ``{app_data}/epubox/manifests`` when omitted.
        """
        if storage_dir:
            self._storage_dir = Path(storage_dir)
        else:
            self._storage_dir = self._default_storage_dir()
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _default_storage_dir() -> Path:
        """Return platform-appropriate application data directory."""
        if appdirs is None:
            return Path.cwd() / ".epubox" / "manifests"
        return Path(appdirs.user_data_dir("epubox", "epubox")) / "manifests"

    def _manifest_path(self, doc_id: str) -> Path:
        """Return the absolute path for a manifest file."""
        safe_name = doc_id.replace(os.sep, "_")
        return self._storage_dir / f"{safe_name}.json"

    @staticmethod
    def _to_json(manifest: TranslationManifest) -> str:
        """Serialize manifest to a JSON string with UTC-aware timestamps."""
        data = manifest.model_dump()
        data["created_at"] = manifest.created_at.isoformat()
        data["updated_at"] = manifest.updated_at.isoformat()
        return json.dumps(data, indent=2, ensure_ascii=False)

    @staticmethod
    def _from_json(raw: str) -> TranslationManifest:
        """Deserialize a JSON string into a TranslationManifest."""
        return TranslationManifest.model_validate_json(raw)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def exists(self, doc_id: str) -> bool:
        """Return True when a manifest file exists for ``doc_id``."""
        return self._manifest_path(doc_id).is_file()

    def load(self, doc_id: str) -> Optional[TranslationManifest]:
        """
        Load and return the manifest for ``doc_id``, or None if not found.

        Args:
            doc_id: Document identifier.

        Returns:
            TranslationManifest if the file exists; None otherwise.
        """
        path = self._manifest_path(doc_id)
        if not path.is_file():
            return None
        try:
            return self._from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, manifest: TranslationManifest) -> None:
        """
        Atomically persist ``manifest`` to disk.

        Uses a write-to-temp-then-rename pattern so the file is never
        left in a partially-written state.

        Args:
            manifest: The manifest to persist. Its ``updated_at`` field is
                updated to the current UTC time before writing.
        """
        manifest.updated_at = datetime.now(timezone.utc)
        path = self._manifest_path(manifest.doc_id)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(self._to_json(manifest), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            if tmp.exists():
                tmp.unlink()
            raise

    def delete(self, doc_id: str) -> None:
        """
        Remove the manifest file for ``doc_id``, silently ignoring missing files.

        Args:
            doc_id: Document identifier.
        """
        path = self._manifest_path(doc_id)
        if path.is_file():
            path.unlink()

    def rebuild_html(self, manifest: TranslationManifest) -> str:
        """
        Reconstruct the full translated HTML from ``manifest``.

        Each chunk is processed in document order:
        - If the chunk is TRANSLATED: use ``chunk.translated``.
        - Otherwise: use ``chunk.original``.

        Args:
            manifest: The manifest to rebuild from.

        Returns:
            Concatenated HTML string for the entire document.
        """
        if not manifest.chunks:
            return ""

        parts: List[str] = []
        for chunk in manifest.chunks:
            if (
                chunk.status in (TranslationStatus.TRANSLATED, TranslationStatus.COMPLETED)
                and chunk.translated
            ):
                parts.append(chunk.translated)
            else:
                parts.append(chunk.original)

        return "".join(parts)
