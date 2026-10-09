from __future__ import annotations

import threading
import zipfile
from pathlib import Path

import pytest

import engine.epub.preparation as preparation_module
from engine.epub.preparation import PreparationConfig, prepare_book
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.services.store import RunStore
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker


def test_prepare_reads_zip_and_writes_documents_on_caller_while_parsing_in_parallel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = make_epub(
        tmp_path / "book.epub",
        {
            "one.xhtml": "<p>One resource.</p>",
            "two.xhtml": "<p>Two resource.</p>",
            "three.xhtml": "<p>Three resource.</p>",
        },
    )
    caller = threading.get_ident()
    read_threads: list[int] = []
    parse_threads: list[int] = []
    write_threads: list[int] = []
    original_zip = zipfile.ZipFile
    original_extract = preparation_module.extract_resource
    original_write = RunStore.write_document

    class ObservedZip(original_zip):
        def read(self, name, pwd=None):
            read_threads.append(threading.get_ident())
            return super().read(name, pwd)

    def observed_extract(*args, **kwargs):
        parse_threads.append(threading.get_ident())
        return original_extract(*args, **kwargs)

    def observed_write(self, document):
        write_threads.append(threading.get_ident())
        return original_write(self, document)

    monkeypatch.setattr(preparation_module.zipfile, "ZipFile", ObservedZip)
    monkeypatch.setattr(preparation_module, "extract_resource", observed_extract)
    monkeypatch.setattr(RunStore, "write_document", observed_write)

    prepared = prepare_book(
        source,
        tmp_path / "work",
        PreparationConfig(run_id="parallel", adapter_version=ADAPTER_VERSION, extractor_version=EXTRACTOR_VERSION),
        StubChecker(),
    )
    store = RunStore(prepared.work_dir)
    paths = tuple(
        store.read_document(document_id).resource.path for document_id in prepared.preparation.document_hashes
    )

    assert read_threads and set(read_threads) == {caller}
    assert parse_threads and set(parse_threads) == {caller}
    assert write_threads and set(write_threads) == {caller}
    assert paths == tuple(
        dict.fromkeys((*prepared.inventory.documents, prepared.inventory.ncx_path, prepared.inventory.opf_path))
    )


@pytest.mark.parametrize("atomic", [False, True])
def test_serial_and_process_preparation_have_identical_documents_and_hashes(
    tmp_path: Path,
    monkeypatch,
    atomic: bool,
) -> None:
    paragraph = "word " * 18_000
    source = make_epub(
        tmp_path / "large.epub",
        {f"chapter{index}.xhtml": f"<p>{paragraph}{index}</p>" for index in range(3)},
    )
    config = (
        PreparationConfig(run_id="same", adapter_version=ADAPTER_VERSION, extractor_version=EXTRACTOR_VERSION)
        if atomic
        else PreparationConfig(run_id="same")
    )
    monkeypatch.setattr(preparation_module, "PROCESS_MIN_BYTES", 10**9)
    serial = prepare_book(source, tmp_path / "serial", config, StubChecker())
    monkeypatch.setattr(preparation_module, "PROCESS_MIN_BYTES", 0)
    process = prepare_book(source, tmp_path / "process", config, StubChecker())
    serial_store = RunStore(serial.work_dir)
    process_store = RunStore(process.work_dir)
    serial_documents = tuple(
        serial_store.read_document(document_id) for document_id in serial.preparation.document_hashes
    )
    process_documents = tuple(
        process_store.read_document(document_id) for document_id in process.preparation.document_hashes
    )

    assert process_documents == serial_documents
    assert process.preparation.document_hashes == serial.preparation.document_hashes
    assert process.preparation == serial.preparation
    assert process.preparation_hash == serial.preparation_hash
