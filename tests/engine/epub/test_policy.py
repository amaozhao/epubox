from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

import engine.epub.validation as validation_module
from engine.epub.publish import publish_atomic
from engine.epub.styles import FONT_POLICY
from engine.epub.upgrade import upgrade_package
from engine.epub.validation import EpubValidationError, PackageInventory, inspect_epub
from engine.epub.verification import stage_epub, verify_staged_epub
from engine.services.journal import BodyJournal
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.epub.test_publish import _case

_OPF = "http://www.idpf.org/2007/opf"

_MATHJAX_HEAD = (
    '<script type="text/x-mathjax-config">MathJax.Hub.Config({'
    'jax: ["input/TeX", "input/MathML", "output/SVG"], extensions: ["tex2jax.js"], '
    'TeX: {extensions: ["noErrors.js"]}, messageStyle: "none", showMathMenu: false});</script>'
    '<script src="mathjax/MathJax.js" type="text/javascript"/>'
)
_MATHJAX_STUB = b'MathJax.version="2.6.1"; MathJax.Hub={}; MathJax.Ajax={};'
_MATHJAX_ASSETS = {
    "MathJax.js": _MATHJAX_STUB,
    "extensions/tex2jax.js": b"/* fixture extension */",
    "extensions/TeX/noErrors.js": b"/* fixture extension */",
    **{
        f"jax/{kind}/{name}.js": b"/* fixture jax */"
        for kind in ("input/TeX", "input/MathML", "output/SVG")
        for name in ("config", "jax")
    },
}


@pytest.fixture(autouse=True)
def _registered_script_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    # Synthetic loader bytes are accepted only inside this test module.
    registered = dict(validation_module._mathjax_distributions())
    registered[hashlib.sha256(_MATHJAX_STUB).hexdigest()] = {
        name: hashlib.sha256(raw).hexdigest() for name, raw in _MATHJAX_ASSETS.items()
    }
    monkeypatch.setattr(validation_module, "_mathjax_distributions", lambda: registered)


def _script_source(
    tmp_path: Path,
    *,
    head: str = _MATHJAX_HEAD,
    body: str = "<p>Static reading content.</p>",
    loader: bytes = _MATHJAX_STUB,
    overrides: dict[str, bytes | None] | None = None,
) -> Path:
    source = make_epub(
        tmp_path / "scripts.epub",
        {
            "chapter.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title>'
            + head
            + "</head><body>"
            + body
            + "</body></html>"
        },
    )
    with zipfile.ZipFile(source) as archive:
        resources = {name: archive.read(name) for name in archive.namelist()}
    assets = _MATHJAX_ASSETS | {"MathJax.js": loader} | (overrides or {})
    script_items = b"".join(
        f'<item id="js{i}" href="mathjax/{name}" media-type="text/javascript"/>'.encode()
        for i, (name, raw) in enumerate(assets.items())
        if raw is not None
    )
    resources["OEBPS/content.opf"] = (
        resources["OEBPS/content.opf"]
        .replace(b'id="c0" href="chapter.xhtml"', b'id="c0" properties="scripted" href="chapter.xhtml"')
        .replace(
            b"<manifest>",
            b"<manifest>" + script_items,
        )
    )
    resources.update({"OEBPS/mathjax/" + name: raw for name, raw in assets.items() if raw is not None})
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in resources.items():
            archive.writestr(
                name, raw, compress_type=zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            )
    return source


@pytest.mark.parametrize("for_output", [False, True])
def test_static_book_with_local_mathjax_is_supported(tmp_path: Path, for_output: bool) -> None:
    source = _script_source(tmp_path)

    inventory = inspect_epub(source, "source", checker=StubChecker(), for_output=for_output)

    assert "scripted" in next(item for item in inventory.manifest if item.item_id == "c0").properties


@pytest.mark.parametrize(
    ("head", "body"),
    [
        ('<script>document.write("Generated content");</script>', "<p>Some static content.</p>"),
        (_MATHJAX_HEAD, '<script>document.body.innerHTML="Generated";</script>'),
        (_MATHJAX_HEAD, '<p onclick="replaceContent()">Static content.</p>'),
        (
            _MATHJAX_HEAD.replace('messageStyle: "none"', 'AuthorInit: function(){document.write("Generated")}'),
            "<p>Text.</p>",
        ),
        (_MATHJAX_HEAD.replace('MathJax.js"', 'missing.js"'), "<p>Text.</p>"),
        (_MATHJAX_HEAD.replace("mathjax/MathJax.js", "https://example.com/MathJax.js"), "<p>Text.</p>"),
        ("", "<p>Scripted but unclassified.</p>"),
        ("", '<form><input type="text"/></form>'),
        ('<script type="application/json">{"title":"Content"}</script>', "<p>Scripted data only.</p>"),
        (_MATHJAX_HEAD.split("<script src=")[0], "<p>No loader.</p>"),
        ('<script src="mathjax/MathJax.js"/>', "<p>No declarative config.</p>"),
        (_MATHJAX_HEAD, "<fieldset><legend>Interactive.</legend></fieldset>"),
        (_MATHJAX_HEAD, "<output>Interactive.</output>"),
        ('<base href="https://example.com/"/>' + _MATHJAX_HEAD, "<p>Text.</p>"),
        (_MATHJAX_HEAD.replace('messageStyle: "none"', 'AuthorInit: "document.write(1)"'), "<p>Text.</p>"),
    ],
)
def test_dynamic_or_unknown_scripts_remain_blocked(tmp_path: Path, head: str, body: str) -> None:
    source = _script_source(tmp_path, head=head, body=body)

    with pytest.raises(EpubValidationError) as caught:
        inspect_epub(source, "source", checker=StubChecker())

    assert caught.value.code == "unsupported_source"
    assert any(
        issue.code == "scripted_content" and issue.resource == "OEBPS/chapter.xhtml" for issue in caught.value.issues
    )


def test_mathjax_identifiers_cannot_hide_a_modified_loader(tmp_path: Path) -> None:
    source = _script_source(tmp_path, loader=b'document.body.innerHTML="Generated book";' + _MATHJAX_STUB)
    with pytest.raises(EpubValidationError, match="MathJax loader"):
        inspect_epub(source, "source", checker=StubChecker())


@pytest.mark.parametrize("replacement", [b'document.body.innerHTML="Generated book";', None])
def test_configured_mathjax_dependencies_must_be_present_and_unmodified(
    tmp_path: Path, replacement: bytes | None
) -> None:
    source = _script_source(tmp_path, overrides={"extensions/tex2jax.js": replacement})
    with pytest.raises(EpubValidationError, match="MathJax dependency"):
        inspect_epub(source, "source", checker=StubChecker())


def test_unknown_extra_mathjax_dependency_is_rejected(tmp_path: Path) -> None:
    source = _script_source(tmp_path, overrides={"extensions/extra.js": b"/* unregistered */"})
    with pytest.raises(EpubValidationError, match="MathJax dependency"):
        inspect_epub(source, "source", checker=StubChecker())


def test_unmanifested_mathjax_javascript_is_also_verified(tmp_path: Path) -> None:
    source = _script_source(tmp_path)
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("OEBPS/mathjax/extensions/extra.js", b'document.write("Different book");')
    with pytest.raises(EpubValidationError, match="MathJax dependency"):
        inspect_epub(source, "source", checker=StubChecker())


def test_mathjax_at_archive_root_checks_adjacent_dependencies(tmp_path: Path) -> None:
    source = tmp_path / "root.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("MathJax.js", _MATHJAX_STUB)
        archive.writestr("extensions/extra.js", b'document.write("Different book");')
    with zipfile.ZipFile(source) as archive:
        reason = validation_module._mathjax_dependencies(
            archive, "MathJax.js", {"MathJax.js": hashlib.sha256(_MATHJAX_STUB).hexdigest()}, [{}], "", {"MathJax.js"}
        )
    assert reason is not None and "extensions/extra.js" in reason


def test_mathjax_scripts_and_math_survive_translation_and_publication(tmp_path: Path) -> None:
    from engine.epub.cleanup import cleanup_resources
    from engine.epub.fill import fill_resource
    from engine.item.atoms import extract_resource

    math = '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi><mo>+</mo><mn>1</mn></math>'
    source = _script_source(tmp_path, body="<p>Static reading content." + math + "</p>")
    inventory = inspect_epub(source, "source", checker=StubChecker())
    with zipfile.ZipFile(source) as archive:
        raw = archive.read("OEBPS/chapter.xhtml")
        javascript = archive.read("OEBPS/mathjax/MathJax.js")
    extracted = extract_resource(raw, "OEBPS/chapter.xhtml", "source")
    targets = {
        item.item_id: item.source_projection.replace("Static reading content.", "静态正文。")
        for item in extracted.items
    }
    assert all("MathJax" not in view.text for view in extracted.document.source_views.values())
    replacements = upgrade_package(source, inventory, {"OEBPS/chapter.xhtml": fill_resource(raw, extracted, targets)})
    replacements, _stats = cleanup_resources(replacements, source=source)
    output = tmp_path / "output.epub"
    stage_epub(source, output, replacements, allow_additions=True)
    verify_staged_epub(
        source,
        output,
        inventory,
        replacements,
        accepted_targets={path: {} for path in replacements},
        checker=StubChecker(),
        upgraded=True,
    )
    with zipfile.ZipFile(output) as archive:
        rendered = archive.read("OEBPS/chapter.xhtml").decode()
        assert _MATHJAX_HEAD in rendered
        assert math in rendered
        assert "静态正文。" in rendered
        assert archive.read("OEBPS/mathjax/MathJax.js") == javascript


def _navigation_target_source(tmp_path: Path) -> tuple[Path, PackageInventory]:
    source = make_epub(
        tmp_path / "navigation-targets.epub",
        {
            "chapter.xhtml": "<p>Main chapter.</p>",
            "cover.xhtml": '<section id="cover"><p>Cover.</p></section>',
            "unused.xhtml": "<p>Not linked from navigation.</p>",
        },
    )
    with zipfile.ZipFile(source) as archive:
        opf = archive.read("OEBPS/content.opf")
        navigation = archive.read("OEBPS/nav.xhtml")
    opf = _spine(opf, (("c0", None), ("nav", "no")))
    unused = b'<li><a href="unused.xhtml">Chapter 3</a></li>'
    cover = b'<li><a href="cover.xhtml">Chapter 2</a></li>'
    navigation = navigation.replace(unused, b"").replace(cover, cover + cover)
    staged = tmp_path / "navigation-targets-source.epub"
    stage_epub(
        source,
        staged,
        {"OEBPS/content.opf": opf, "OEBPS/nav.xhtml": navigation},
    )
    staged.replace(source)
    return source, inspect_epub(source, "source", checker=StubChecker())


def _spine(opf: bytes, items: tuple[tuple[str, str | None], ...]) -> bytes:
    root = ET.fromstring(opf)
    spine = root.find(f"{{{_OPF}}}spine")
    assert spine is not None
    for child in list(spine):
        spine.remove(child)
    for item_id, linear in items:
        attributes = {"idref": item_id}
        if linear is not None:
            attributes["linear"] = linear
        ET.SubElement(spine, f"{{{_OPF}}}itemref", attributes)
    return ET.tostring(root, encoding="utf-8")


def _spine_items(opf: bytes) -> tuple[tuple[str, str | None], ...]:
    root = ET.fromstring(opf)
    spine = root.find(f"{{{_OPF}}}spine")
    assert spine is not None
    return tuple((item.get("idref", ""), item.get("linear")) for item in spine)


def test_upgrade_verification_accepts_only_derived_navigation_targets_appended_nonlinear(
    tmp_path: Path,
) -> None:
    source, inventory = _navigation_target_source(tmp_path)
    replacements = upgrade_package(source, inventory, {})

    assert inventory.spine == ("c0", "nav")
    assert inventory.spine_linear == {"c0": True, "nav": False}
    assert _spine_items(replacements[inventory.opf_path]) == (
        ("c0", None),
        ("nav", "no"),
        ("c1", "no"),
    )
    output = tmp_path / "navigation-targets-output.epub"
    stage_epub(source, output, replacements, allow_additions=True)

    verify_staged_epub(
        source,
        output,
        inventory,
        replacements,
        accepted_targets={path: {} for path in replacements},
        checker=StubChecker(),
        upgraded=True,
    )


@pytest.mark.parametrize(
    "items",
    [
        (("c0", None), ("nav", "no"), ("c1", "yes")),
        (("c1", "no"), ("c0", None), ("nav", "no")),
        (("c0", None), ("c1", "no"), ("nav", "no")),
        (("c0", None), ("nav", "no"), ("c1", "no"), ("c2", "no")),
        (("nav", "no"), ("c1", "no")),
        (("c0", "no"), ("nav", "no"), ("c1", "no")),
        (("c0", None), ("nav", "no")),
    ],
    ids=[
        "addition-linear-yes",
        "addition-moved-to-front",
        "addition-moved-into-source-prefix",
        "extra-unlinked-document",
        "source-item-deleted",
        "source-linear-changed",
        "derived-addition-missing",
    ],
)
def test_upgrade_verification_rejects_tampered_navigation_target_spine(
    tmp_path: Path, items: tuple[tuple[str, str | None], ...]
) -> None:
    source, inventory = _navigation_target_source(tmp_path)
    replacements = upgrade_package(source, inventory, {})
    replacements[inventory.opf_path] = _spine(replacements[inventory.opf_path], items)
    output = tmp_path / "tampered-navigation-targets.epub"
    stage_epub(source, output, replacements, allow_additions=True)

    with pytest.raises(EpubValidationError):
        verify_staged_epub(
            source,
            output,
            inventory,
            replacements,
            accepted_targets={path: {} for path in replacements},
            checker=StubChecker(),
            upgraded=True,
        )


@pytest.mark.parametrize(
    ("upgraded", "cleanup_only"),
    [(False, False), (True, True)],
)
def test_navigation_target_spine_addition_is_rejected_outside_plain_upgrade(
    tmp_path: Path, upgraded: bool, cleanup_only: bool
) -> None:
    source, inventory = _navigation_target_source(tmp_path)
    replacements = upgrade_package(source, inventory, {})
    output = tmp_path / f"other-path-{upgraded}-{cleanup_only}.epub"
    stage_epub(source, output, replacements, allow_additions=True)

    with pytest.raises(EpubValidationError):
        verify_staged_epub(
            source,
            output,
            inventory,
            replacements,
            accepted_targets={path: {} for path in replacements},
            checker=StubChecker(),
            upgraded=upgraded,
            cleanup_only=cleanup_only,
        )


def test_stage_and_verify_explicit_font_removal(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("OEBPS/unused.ttf", b"font bytes")
    inventory = inspect_epub(source, "source", checker=StubChecker())
    staged = tmp_path / "staged.epub"
    removed = frozenset({"OEBPS/unused.ttf"})
    stage_epub(source, staged, {}, removed_paths=removed)
    verify_staged_epub(
        source, staged, inventory, {}, accepted_targets={}, checker=StubChecker(), removed_paths=removed
    )
    with zipfile.ZipFile(staged) as archive:
        assert "OEBPS/unused.ttf" not in archive.namelist()
    with pytest.raises(EpubValidationError, match="inventory"):
        verify_staged_epub(source, staged, inventory, {}, accepted_targets={}, checker=StubChecker())


def test_verifier_rejects_deletion_of_nonfont_resource(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("OEBPS/image.png", b"image bytes")
    inventory = inspect_epub(source, "source", checker=StubChecker())
    staged = tmp_path / "staged.epub"
    removed = frozenset({"OEBPS/image.png"})
    stage_epub(source, staged, {}, removed_paths=removed)
    with pytest.raises(EpubValidationError, match="font"):
        verify_staged_epub(
            source, staged, inventory, {}, accepted_targets={}, checker=StubChecker(), removed_paths=removed
        )


@pytest.mark.parametrize("via_cli", [False, True])
def test_atomic_publication_cleans_fonts_and_repacks_old_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via_cli: bool
) -> None:
    case = _case(tmp_path, '<p style="font-family: Publisher; font-weight: bold">Hello world.</p>', ("Hello world.",))
    targets = {
        item.item_id: item.source_projection for inventory in case.index.inventories for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    output = tmp_path / "reader.epub"
    result = publish_atomic(case.session.store, output, StubChecker())
    proof = result["verification"]
    assert isinstance(proof, dict) and proof["font_policy"] == FONT_POLICY
    with zipfile.ZipFile(output) as archive:
        chapter = archive.read("OEBPS/chapter.xhtml")
        assert b"Publisher" not in chapter
        assert b"font-weight: bold" in chapter
    intent_path = case.session.store.root / "publish.json"
    intent = json.loads(intent_path.read_bytes())
    intent["verification"].pop("font_policy")
    intent_path.write_text(json.dumps(intent))
    if via_cli:
        from engine import cli

        monkeypatch.setattr(cli, "checker_for_source", lambda *args: StubChecker())
        monkeypatch.setattr(cli, "build_run_model", lambda *args, **kwargs: pytest.fail("model constructed"))
        monkeypatch.setattr(cli, "_record", lambda outcome: outcome)
        outcome = cli._completed_run_outcome(case.session.store.root, output)
        assert outcome is not None and outcome.status == "completed"
        assert json.loads(intent_path.read_bytes())["verification"]["font_policy"] == FONT_POLICY
    else:
        result = publish_atomic(case.session.store, output, StubChecker())
        proof = result["verification"]
        assert isinstance(proof, dict) and proof["font_policy"] == FONT_POLICY


def _with_fonts(path: Path, documents=None, *, version="3.0") -> Path:
    source = make_epub(path, documents, version=version)
    with zipfile.ZipFile(source) as archive:
        opf = archive.read("OEBPS/content.opf").replace(
            b"</manifest>",
            b'<item id="font" href="fonts/Book.ttf" media-type="font/ttf"/>'
            b'<item id="css" href="style.css" media-type="text/css"/></manifest>',
        )
        chapter = archive.read("OEBPS/chapter.xhtml").replace(
            b"</head>", b'<link rel="stylesheet" type="text/css" href="style.css"/></head>'
        )
    staged = path.with_suffix(".candidate.epub")
    stage_epub(
        source,
        staged,
        {
            "OEBPS/content.opf": opf,
            "OEBPS/chapter.xhtml": chapter,
            "OEBPS/style.css": b"@font-face {font-family:Publisher;src:url(fonts/Book.ttf)} "
            b"p {font-family:Publisher;line-height:1.5} strong {font-weight:700}",
            "OEBPS/fonts/Book.ttf": b"synthetic font bytes",
            "META-INF/encryption.xml": b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            b'<EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#">'
            b'<EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>'
            b'<CipherData><CipherReference URI="OEBPS/fonts/Book.ttf"/></CipherData>'
            b"</EncryptedData></encryption>",
        },
        allow_additions=True,
    )
    staged.replace(source)
    return source


def test_atomic_publication_removes_font_manifest_and_obfuscation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tests.engine.agents.test_workflow as workflow_fixture

    monkeypatch.setattr(workflow_fixture, "make_epub", _with_fonts)
    case = _case(tmp_path, "<p>Hello world.</p>", ("Hello world.",))
    targets = {
        item.item_id: item.source_projection for inventory in case.index.inventories for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    source_bytes = (tmp_path / "source.epub").read_bytes()
    output = tmp_path / "reader.epub"

    publish_atomic(case.session.store, output, StubChecker())

    assert (tmp_path / "source.epub").read_bytes() == source_bytes
    with zipfile.ZipFile(output) as archive:
        assert "OEBPS/fonts/Book.ttf" not in archive.namelist()
        assert "META-INF/encryption.xml" not in archive.namelist()
        assert b'id="font"' not in archive.read("OEBPS/content.opf")
        assert b"Publisher" not in archive.read("OEBPS/style.css")
        assert b"font-weight:700" in archive.read("OEBPS/style.css")
        assert b"line-height:1.5" in archive.read("OEBPS/style.css")


@pytest.mark.parametrize("identity", [False, True])
def test_legacy_publication_cleans_embedded_fonts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: bool
) -> None:
    import tests.engine.epub.test_publication as legacy
    from engine.epub.publication import publish_book

    monkeypatch.setattr(legacy, "make_epub", _with_fonts)
    source, store, plan, documents = legacy._prepared(tmp_path)
    monkeypatch.setattr(store, "read_bookplan", lambda: plan)
    if not identity:
        records = legacy._accepted_records(documents)
        manifests = legacy._review_manifests(plan, records)
        legacy._coherence_checks(store, documents, records)
        monkeypatch.setattr(store, "read_unit", lambda unit_id: records[unit_id])
        monkeypatch.setattr(store, "read_request", lambda request_id: manifests[request_id])
    output = tmp_path / "reader.epub"
    before = source.read_bytes()

    result = publish_book(store, output, StubChecker(), identity=identity)

    assert source.read_bytes() == before
    proof = result["verification"]
    assert isinstance(proof, dict) and proof["font_policy"] == FONT_POLICY
    with zipfile.ZipFile(output) as archive:
        assert "OEBPS/fonts/Book.ttf" not in archive.namelist()
        assert "META-INF/encryption.xml" not in archive.namelist()
        assert b"Publisher" not in archive.read("OEBPS/style.css")


@pytest.mark.parametrize(
    "removed", [frozenset({"missing.ttf"}), frozenset({"mimetype"}), frozenset({"OEBPS/content.opf"})]
)
def test_stage_rejects_invalid_or_conflicting_removals(tmp_path: Path, removed: frozenset[str]) -> None:
    source = make_epub(tmp_path / "source.epub")
    with zipfile.ZipFile(source) as archive:
        opf = archive.read("OEBPS/content.opf")
    with pytest.raises(EpubValidationError, match="Removed resources"):
        stage_epub(source, tmp_path / "target.epub", {"OEBPS/content.opf": opf}, removed_paths=removed)


def test_extensionless_font_mime_case_is_consistent_during_cleanup_and_verification(tmp_path: Path) -> None:
    from engine.epub.styles import use_reader_fonts

    source = make_epub(tmp_path / "source.epub")
    with zipfile.ZipFile(source) as archive:
        opf = archive.read("OEBPS/content.opf").replace(
            b"</manifest>", b'<item id="font" href="fontdata" media-type="Font/TTF"/></manifest>'
        )
    staged = tmp_path / "with-font.epub"
    stage_epub(source, staged, {"OEBPS/content.opf": opf, "OEBPS/fontdata": b"font"}, allow_additions=True)
    inventory = inspect_epub(staged, "source", checker=StubChecker())
    replacements, removed = use_reader_fonts(staged, inventory, {})
    output = tmp_path / "output.epub"
    stage_epub(staged, output, replacements, removed_paths=removed)
    verify_staged_epub(
        staged,
        output,
        inventory,
        replacements,
        accepted_targets={path: {} for path in replacements},
        checker=StubChecker(),
        removed_paths=removed,
        reader_fonts=True,
    )
    assert removed == frozenset({"OEBPS/fontdata"})


def test_verification_rejects_modified_stylesheet_after_cleanup(tmp_path: Path) -> None:
    from engine.epub.styles import use_reader_fonts

    source = _with_fonts(tmp_path / "source.epub", {"chapter.xhtml": "<p>Text</p>"})
    inventory = inspect_epub(source, "source", checker=StubChecker())
    replacements, removed = use_reader_fonts(source, inventory, {})
    output = tmp_path / "output.epub"
    stage_epub(source, output, replacements | {"OEBPS/style.css": b"p{display:none}"}, removed_paths=removed)
    with pytest.raises(EpubValidationError, match="Stylesheet differs"):
        verify_staged_epub(
            source,
            output,
            inventory,
            replacements,
            accepted_targets={path: {} for path in replacements},
            checker=StubChecker(),
            removed_paths=removed,
            reader_fonts=True,
        )
