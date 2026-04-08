from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.item.manifest import ManifestManager, TranslationManifest
from engine.schemas.chunk import Chunk
from engine.schemas.translator import TranslationStatus


class TestTranslationManifest:
    def test_create_manifest(self):
        """Test creating a TranslationManifest."""
        manifest = TranslationManifest(
            doc_id="test-doc-001",
            chunks=[],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert manifest.doc_id == "test-doc-001"
        assert manifest.chunks == []
        assert manifest.created_at is not None
        assert manifest.updated_at is not None

    def test_manifest_with_chunks(self):
        """Test manifest holds chunk list."""
        chunk = Chunk(
            name="chunk-1",
            original="<p>Hello</p>",
            translated="<p>Bonjour</p>",
            status=TranslationStatus.TRANSLATED,
            tokens=5,
            local_tag_map={},
        )
        manifest = TranslationManifest(
            doc_id="test-doc-002",
            chunks=[chunk],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert len(manifest.chunks) == 1
        assert manifest.chunks[0].name == "chunk-1"
        assert manifest.chunks[0].translated == "<p>Bonjour</p>"

    def test_manifest_serialization_round_trip(self):
        """Test manifest serializes and deserializes chunks correctly."""
        chunk = Chunk(
            name="chunk-a",
            original="<p>World</p>",
            translated=None,
            status=TranslationStatus.PENDING,
            tokens=3,
            local_tag_map={},
        )
        manifest = TranslationManifest(
            doc_id="test-doc-003",
            chunks=[chunk],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        data = manifest.model_dump(mode="json")
        assert data["doc_id"] == "test-doc-003"
        assert data["chunks"][0]["name"] == "chunk-a"
        assert data["chunks"][0]["status"] == "pending"


class TestManifestManager:
    """Test ManifestManager with a temporary storage directory."""

    @pytest.fixture
    def storage_dir(self, tmp_path):
        """Provide a temporary directory for manifest storage."""
        return str(tmp_path / "manifests")

    @pytest.fixture
    def manager(self, storage_dir):
        """Provide a ManifestManager backed by a temporary directory."""
        return ManifestManager(storage_dir=storage_dir)

    @pytest.fixture
    def sample_manifest(self) -> TranslationManifest:
        """Provide a sample manifest with two chunks."""
        return TranslationManifest(
            doc_id="sample-doc",
            chunks=[
                Chunk(
                    name="c1",
                    original="<p>Hello</p>",
                    translated=None,
                    status=TranslationStatus.PENDING,
                    tokens=2,
                    local_tag_map={},
                ),
                Chunk(
                    name="c2",
                    original="<p>World</p>",
                    translated="<p>Mondo</p>",
                    status=TranslationStatus.TRANSLATED,
                    tokens=2,
                    local_tag_map={},
                ),
            ],
            created_at=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            updated_at=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        )

    # -------------------------------------------------------------------------
    # exists
    # -------------------------------------------------------------------------

    def test_exists_false_for_missing(self, manager):
        """exists returns False when manifest does not exist."""
        assert manager.exists("nonexistent") is False

    def test_exists_true_after_save(self, manager, sample_manifest, storage_dir):
        """exists returns True after saving a manifest."""
        manager.save(sample_manifest)
        assert manager.exists("sample-doc") is True

    # -------------------------------------------------------------------------
    # load
    # -------------------------------------------------------------------------

    def test_load_returns_manifest(self, manager, sample_manifest):
        """load returns the saved manifest."""
        manager.save(sample_manifest)
        loaded = manager.load("sample-doc")
        assert loaded is not None
        assert loaded.doc_id == "sample-doc"
        assert len(loaded.chunks) == 2

    def test_load_returns_none_for_missing(self, manager):
        """load returns None when manifest does not exist."""
        assert manager.load("nonexistent") is None

    def test_load_preserves_chunk_fields(self, manager, sample_manifest):
        """load preserves all chunk fields including status and translated."""
        manager.save(sample_manifest)
        loaded = manager.load("sample-doc")
        assert loaded is not None
        assert loaded.chunks[0].status == TranslationStatus.PENDING
        assert loaded.chunks[0].translated is None
        assert loaded.chunks[1].status == TranslationStatus.TRANSLATED
        assert loaded.chunks[1].translated == "<p>Mondo</p>"

    # -------------------------------------------------------------------------
    # save (atomic write)
    # -------------------------------------------------------------------------

    def test_save_creates_file(self, manager, sample_manifest, storage_dir):
        """save writes a .json file to the storage directory."""
        manager.save(sample_manifest)
        path = os.path.join(storage_dir, "sample-doc.json")
        assert os.path.exists(path)

    def test_save_is_atomic(self, manager, sample_manifest, storage_dir):
        """save atomically replaces the existing file."""
        manager.save(sample_manifest)
        # Overwrite with new content
        new_manifest = TranslationManifest(
            doc_id="sample-doc",
            chunks=[],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        manager.save(new_manifest)
        loaded = manager.load("sample-doc")
        assert loaded is not None
        assert loaded.chunks == []

    # -------------------------------------------------------------------------
    # delete
    # -------------------------------------------------------------------------

    def test_delete_removes_file(self, manager, sample_manifest, storage_dir):
        """delete removes the manifest file."""
        manager.save(sample_manifest)
        assert manager.exists("sample-doc")
        manager.delete("sample-doc")
        assert manager.exists("sample-doc") is False

    def test_delete_nonexistent_is_silent(self, manager):
        """delete does not raise when manifest does not exist."""
        manager.delete("nonexistent")  # should not raise

    # -------------------------------------------------------------------------
    # rebuild_html
    # -------------------------------------------------------------------------

    def test_rebuild_html_concatenates_translated(self, manager):
        """rebuild_html concatenates all chunk.translated values."""
        manifest = TranslationManifest(
            doc_id="rebuild-doc",
            chunks=[
                Chunk(
                    name="rb1",
                    original="<p>Hello</p>",
                    translated="<p>Bonjour</p>",
                    status=TranslationStatus.TRANSLATED,
                    tokens=1,
                    local_tag_map={},
                ),
                Chunk(
                    name="rb2",
                    original="<p>World</p>",
                    translated="<p>Le Monde</p>",
                    status=TranslationStatus.TRANSLATED,
                    tokens=1,
                    local_tag_map={},
                ),
            ],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        html = manager.rebuild_html(manifest)
        assert "<p>Bonjour</p>" in html
        assert "<p>Le Monde</p>" in html

    def test_rebuild_html_uses_translated_html(self, manager):
        """rebuild_html uses translated HTML directly without placeholder restoration."""
        manifest = TranslationManifest(
            doc_id="html-doc",
            chunks=[
                Chunk(
                    name="html1",
                    original="<p>Hello <strong>World</strong></p>",
                    translated="<p>你好 <strong>世界</strong></p>",
                    status=TranslationStatus.TRANSLATED,
                    tokens=3,
                    local_tag_map={},
                ),
            ],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        html = manager.rebuild_html(manifest)
        assert "<strong>世界</strong>" in html
        assert "<p>你好" in html

    def test_rebuild_html_uses_original_for_untranslated(self, manager):
        """rebuild_html uses original text when translated is None."""
        manifest = TranslationManifest(
            doc_id="untranslated-doc",
            chunks=[
                Chunk(
                    name="u1",
                    original="<p>Original</p>",
                    translated=None,
                    status=TranslationStatus.PENDING,
                    tokens=1,
                    local_tag_map={},
                ),
            ],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        html = manager.rebuild_html(manifest)
        assert "<p>Original</p>" in html

    def test_rebuild_html_uses_original_for_untranslated_status(self, manager):
        """rebuild_html falls back to original when status is UNTRANSLATED."""
        manifest = TranslationManifest(
            doc_id="untranslated-status-doc",
            chunks=[
                Chunk(
                    name="us1",
                    original="<p>Keep me</p>",
                    translated="<p>Translated</p>",
                    status=TranslationStatus.UNTRANSLATED,
                    tokens=1,
                    local_tag_map={},
                ),
            ],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        html = manager.rebuild_html(manifest)
        assert "<p>Keep me</p>" in html
        assert "<p>Translated</p>" not in html

    def test_rebuild_html_empty_manifest(self, manager):
        """rebuild_html returns empty string for manifest with no chunks."""
        manifest = TranslationManifest(
            doc_id="empty-doc",
            chunks=[],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert manager.rebuild_html(manifest) == ""

    # -------------------------------------------------------------------------
    # Edge cases
    # -------------------------------------------------------------------------

    def test_load_corrupted_json(self, manager, storage_dir):
        """load returns None when the JSON file is malformed."""
        path = Path(storage_dir) / "bad-doc.json"
        path.write_text("not valid json{", encoding="utf-8")
        assert manager.load("bad-doc") is None

    def test_load_missing_chunks_field_uses_default(self, manager, storage_dir):
        """load fills in the default empty list when 'chunks' is absent from JSON."""
        path = Path(storage_dir) / "incomplete-doc.json"
        path.write_text(
            json.dumps({"doc_id": "incomplete-doc"}), encoding="utf-8"
        )
        loaded = manager.load("incomplete-doc")
        assert loaded is not None
        assert loaded.chunks == []

    def test_save_oserror_handling(self, manager, sample_manifest, storage_dir):
        """save cleans up the tmp file and re-raises when os.replace fails."""
        path = Path(storage_dir) / f"{sample_manifest.doc_id}.json"
        tmp = path.with_suffix(".tmp")
        with patch("engine.item.manifest.os.replace", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                manager.save(sample_manifest)
        assert not tmp.exists(), "tmp file should be removed after OSError"

    def test_rebuild_html_translated_but_none(
        self, manager
    ):
        """rebuild_html falls back to original when status is TRANSLATED but translated is None."""
        chunk = Chunk(
            name="t1",
            original="<p>Keep me</p>",
            translated=None,
            status=TranslationStatus.TRANSLATED,
            tokens=1,
            local_tag_map={},
        )
        manifest = TranslationManifest(
            doc_id="translated-none-doc",
            chunks=[chunk],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        html = manager.rebuild_html(manifest)
        assert "<p>Keep me</p>" in html

    def test_rebuild_html_empty_translated_string(
        self, manager
    ):
        """rebuild_html falls back to original when translated is an empty string."""
        chunk = Chunk(
            name="e1",
            original="<p>Keep me</p>",
            translated="",
            status=TranslationStatus.TRANSLATED,
            tokens=1,
            local_tag_map={},
        )
        manifest = TranslationManifest(
            doc_id="empty-translated-doc",
            chunks=[chunk],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        html = manager.rebuild_html(manifest)
        assert "<p>Keep me</p>" in html

    def test_rebuild_html_placeholder_leak(self, manager):
        """rebuild_html does not crash when translated contains an unrecognised placeholder."""
        chunk = Chunk(
            name="l1",
            original="<p>Hello</p>",
            translated="<p>[id0]Bonjour</p>",
            status=TranslationStatus.TRANSLATED,
            tokens=3,
            local_tag_map={
                "[id1]": "<strong>",
            },
        )
        manifest = TranslationManifest(
            doc_id="placeholder-leak-doc",
            chunks=[chunk],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        html = manager.rebuild_html(manifest)
        assert "[id0]" in html
        assert "<strong>Bonjour</strong>" not in html

    def test_manifest_with_extra_fields(self):
        """TranslationManifest ignores extra fields in the input JSON."""
        data = {
            "doc_id": "extra-doc",
            "chunks": [],
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
            "_internal": "ignored",
            "version": 99,
        }
        manifest = TranslationManifest.model_validate(data)
        assert manifest.doc_id == "extra-doc"
        assert "_internal" not in manifest.model_dump()

    # -------------------------------------------------------------------------
    # Integration
    # -------------------------------------------------------------------------

    def test_full_cycle(self, manager):
        """Test save -> load -> delete."""
        manifest = TranslationManifest(
            doc_id="cycle-doc",
            chunks=[
                Chunk(
                    name="c1",
                    original="<p>Start</p>",
                    translated="<p>Démarré</p>",
                    status=TranslationStatus.TRANSLATED,
                    tokens=1,
                    local_tag_map={},
                ),
            ],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        manager.save(manifest)
        loaded = manager.load("cycle-doc")
        assert loaded is not None
        assert loaded.chunks[0].translated == "<p>Démarré</p>"

        manager.delete("cycle-doc")
        assert manager.load("cycle-doc") is None
