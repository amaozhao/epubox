# Semantic Anchor Translation Pipeline Design

## Purpose

EPUBox needs higher-quality Chinese translation without weakening EPUB structural safety. The current pipeline is structurally conservative, but strict placeholder and text-node constraints can force English word order into Chinese output. This design introduces a semantic-anchor intermediate representation (IR) so the model can translate natural language freely while program code remains responsible for HTML/XML reconstruction.

Core principle:

```text
The model translates meaning; the program owns markup.
```

## Goals

- Preserve recoverability: no accepted translation may produce missing closing tags, malformed XML, broken attributes, or unrecoverable placeholders.
- Improve fluency: allow Chinese word-order changes, sentence splitting, and local rephrasing inside safe semantic blocks.
- Avoid model-authored final HTML: the model should not directly generate structural tags for normal prose.
- Keep failures explicit: invalid anchors, residual raw HTML, XML parse errors, and unsafe moves must fail validation before writeback.
- Fit the existing architecture: extend the DOM/xpath flow instead of returning to whole-document string stitching or global tag placeholders.

## Non-Goals

- No free-form full-chapter rewriting in the first implementation.
- No cross-chapter context rewriting.
- No model-driven changes to `href`, `id`, `class`, `src`, dimensions, EPUB roles, or ARIA attributes.
- No attempt to translate executable code, CSS, MathML, or raw style/script payloads.
- No silent repair of invalid model output when a retry or manual report is safer.

## Current Pipeline Assessment

The current chunker already supports the important invariant that an `html_fragment` chunk is assembled from complete DOM elements:

- `DomChunker` documents complete DOM elements as the minimum split unit in `engine/item/chunker.py`.
- `_collect_blocks()` iterates child DOM nodes, serializes each whole child with `str(child)`, and recurses only into child elements when a container exceeds limits.
- `_create_chunk()` joins complete element fragments and records one xpath per top-level translated element.
- Navigation files use `nav_text` markers, so they avoid HTML tag generation entirely.

The current writeback path also protects final output:

- `validate_translated_html()` rejects malformed translated chunk HTML before writeback.
- `DomReplacer._replace_by_xpaths()` requires translated top-level element count to match the chunk xpath count.
- `verify_final_html()` rejects restored files with residual secondary placeholders or XML parse errors.

The weakness is not chunk closure. The weakness is that the model still sometimes authors HTML or rigid marker-aligned text. To avoid broken tags, the prompt pushes the model toward strict preservation. That pressure causes literal Chinese.

## Design Summary

Introduce a block-scoped semantic-anchor IR:

```text
Original DOM block
  -> Anchor IR text
  -> model-translated Anchor IR text
  -> programmatic DOM render
  -> chunk/file validation
```

For normal prose, the model receives text plus anchors, not final HTML tags. Example:

```text
Source IR:
A record is a collection of related ⟦A0|fields⟧.

Translated IR:
一条记录是若干相关 ⟦A0|字段⟧ 的集合。
```

The renderer maps `A0` back to the original inline element tree, preserving attributes while replacing its translatable text with `字段`.

## Invariants

### Structural Invariants

- A translatable block maps to exactly one original top-level DOM element.
- The renderer clones the original top-level DOM element; the model never creates that element.
- Inline anchors may move only within their owning block in the initial version.
- Anchor IDs must be unique within a block.
- Every required anchor in the source IR must appear exactly once in translated IR unless the anchor policy marks it optional.
- The model output must not contain raw HTML tags. Any raw `<tag>` or `</tag>` in translated IR is a validation failure unless escaped as visible text by the renderer.
- Renderer output must pass chunk-level structural checks and final XML parsing.

### Language Quality Invariants

- The model may reorder anchored phrases inside a block to fit natural Chinese.
- The model may split or merge sentences inside a block.
- The model may translate anchor payload text when the anchor is translatable.
- The model must preserve technical terms from the glossary unless the glossary marks alternatives.
- Code-like, formula-like, identifier-like, and style/script payloads remain protected.

### Recovery Invariants

- A failed anchor validation must not write partial translated DOM.
- A failed render must fall back to retry, current HTML translation mode, text-node fallback, or manual report according to configured policy.
- Existing final XML validation remains mandatory even if programmatic rendering is expected to be safe.

## Anchor IR

### Data Model

Each `AnchorBlock` represents one DOM element selected from the current chunking logic.

```json
{
  "block_id": "b12",
  "xpath": "/html/body/p[3]",
  "tag": "p",
  "source_text": "A record is a collection of related ⟦A0|fields⟧.",
  "anchors": [
    {
      "id": "A0",
      "kind": "inline_translatable",
      "owner_block_id": "b12",
      "source_payload": "fields",
      "allowed_move_scope": "same_block",
      "dom_subtree": "<a class=\"KT\" href=\"...\"><span class=\"bold\">fields</span></a>",
      "text_policy": "translate_payload"
    }
  ]
}
```

The persisted checkpoint should store enough metadata to validate and render translated IR without reparsing model output as HTML.

### Anchor Syntax

Use a delimiter that is unlikely to appear in EPUB text:

```text
⟦A0|fields⟧
```

Rules:

- Prefix: `A`
- Numeric ID: block-local integer sequence
- Separator: `|`
- Payload: visible source text, or translated payload in model output
- Closing delimiter: `⟧`

If delimiter compatibility becomes an issue, use ASCII fallback:

```text
[[ANCHOR:A0|fields]]
```

The parser should support one configured delimiter family per run, not both simultaneously in the same checkpoint.

## Anchor Classification

### Coverage Rule

Every descendant node inside an anchor-mode block must be accounted for before the model sees the block:

- plain text becomes IR text
- supported inline markup becomes a non-overlapping anchor
- protected markup becomes an atomic anchor or remains outside translation
- unsupported markup makes the whole block ineligible for anchor mode and sends it to the existing safe fallback

Anchor mode must not silently drop or flatten an inline tag. If the extractor cannot prove that all descendant markup is represented, it must refuse anchor mode for that block.

### Translatable Inline Anchors

Convert these inline elements when they interrupt a prose sentence:

- `a`
- `span` with semantic classes such as glossary, bold, italic, term, keyword
- `em`, `i`, `strong`, `b`, `u`
- `sub` and `sup` when they are part of a prose phrase rather than a formula

The anchor payload is the element's visible text. The renderer updates text nodes inside the cloned subtree while preserving element structure and attributes.

### Non-Overlapping Anchor Rule

Anchors must represent non-overlapping DOM ranges. If supported inline tags are nested, the extractor chooses one of these strategies:

- prefer the outermost semantic inline subtree as a single anchor when moving the whole phrase is acceptable
- prefer inner anchors only when the outer wrapper is stylistic and can be reconstructed deterministically
- reject anchor mode for the block when nested markup cannot be represented without ambiguity

The first implementation should prefer the outermost-anchor strategy because it minimizes overlapping-range errors and makes restoration easier to prove.

### Protected Atomic Anchors

Keep these as atomic, normally untranslated anchors:

- `pre`
- code-like containers
- `style`
- `script`
- `math`
- `svg`
- formula-heavy runs
- images and media elements

These anchors either render the original subtree unchanged or remain outside the model payload if they are standalone non-translatable blocks.

### Plain Text

Plain text remains plain text in the IR. It is not split into one line per text node. This is the key fluency improvement over text-node fallback.

### Escaping Rule

Anchor delimiters and separators that appear in source text must be escaped before model input and unescaped during rendering. The same applies to visible literal angle brackets in prose. Literal `<` and `>` from source text must be represented as escaped text in IR, not as raw model-authored HTML.

## Block Selection

The first implementation should use current `DomChunker` block boundaries, with one refinement:

- Translation IR should operate at top-level block elements for ordinary prose.
- A block should not be split into independent text nodes merely because it contains multiple inline tags.
- Very large blocks may be recursively decomposed by child elements, but never by raw string slicing.
- Tables, figures, and alt-description-heavy content may remain in the existing HTML mode initially if anchor rendering is too risky.

The existing chunk can contain multiple `AnchorBlock` objects. The model may receive a JSON array of blocks and must return one translated text per block with the same `block_id`s.

## Translation Prompt Strategy

The anchor-mode prompt should be simpler than the current HTML preservation prompt:

- Return JSON only.
- Translate each block into fluent Simplified Chinese.
- Preserve every anchor ID exactly once in its block.
- You may move anchors inside the same block to fit Chinese word order.
- Translate anchor payloads when natural.
- Do not output raw HTML/XML tags.
- Keep code-like terms and glossary terms accurate.

The prompt should not say "do not rephrase around placeholders." That instruction is the source of literal translations.

## Rendering Strategy

Rendering is deterministic:

1. Load the original DOM block.
2. Parse translated IR text into text segments and anchor tokens.
3. Clone the original top-level block element.
4. Clear only the children covered by the anchor IR renderer while preserving the top-level element name, namespace, and attributes.
5. Append translated text segments and rendered anchor nodes in translated order.
6. For translatable anchors, clone the original anchor subtree and replace its visible payload text with the translated payload.
7. For protected anchors, clone the original subtree unchanged.
8. Serialize the DOM through the configured markup serializer.

Because all tags are cloned or created by the program, missing closing tags cannot be introduced by model text.

Renderer implementation must preserve XHTML namespaces and EPUB-specific attributes. It must not normalize attributes in a way that changes `epub:type`, `role`, `aria-*`, IDs, link targets, image references, or namespace prefixes needed by the original document.

## Validation Gates

### Pre-Translation Gate

- Verify source item HTML/XML integrity.
- Verify chunk fragments are complete DOM fragments.
- Verify each selected block has one stable xpath.
- Verify extracted anchors have unique IDs and supported policies.
- Verify anchor coverage: no descendant markup in an anchor-mode block is unrepresented.
- Verify anchors are non-overlapping.

### Model Output Gate

- JSON schema validates.
- Returned `block_id`s match source exactly.
- No raw HTML tags appear in translated text.
- Required anchors appear exactly once in their owner block.
- Anchor IDs do not cross block boundaries.
- Anchor payloads satisfy policy:
  - translatable anchors may change payload text
  - protected anchors must preserve payload or omit payload changes according to policy
- Translated payloads do not introduce unescaped anchor delimiters or raw markup.
- No residual untranslated English hard-fail findings, except allowed technical/name/reference patterns.

### Render Gate

- Rendered block count matches original block count.
- Rendered top-level tag names match original block tag names.
- Original structural attributes are preserved.
- Link targets, IDs, ARIA, EPUB roles, and image sources are preserved.
- XHTML namespaces and required namespace-qualified attributes are preserved.
- Secondary protected placeholders are fully restored.
- Rendered chunk passes `validate_translated_html()` or an anchor-aware equivalent.

### File Gate

- Restored item passes XML parsing.
- No residual `[PRE:N]`, `[CODE:N]`, `[STYLE:N]`, `[TAG:N]`, or anchor delimiters remain.
- EPUB packing is skipped if any chunk is `translation_failed` or `writeback_failed`.

## Fallback Policy

Recommended order:

1. Anchor mode translation.
2. Anchor mode retry with validation feedback.
3. Existing HTML mode translation for block classes not yet supported by anchor rendering.
4. Existing text-node fallback only for structure repair, not as the default for fluency-sensitive prose.
5. Manual translation report.

The system should track which fallback produced the final chunk, so quality regressions can be audited.

Fallback must be block-local where possible. If one block in a multi-block chunk is ineligible for anchor mode, the system may either split the chunk at block boundaries or translate the ineligible block through existing HTML mode. It must not force the entire file into text-node fallback just because one block has unsupported markup.

## Polish Pass

The polish pass should operate on the same anchor IR, not on raw HTML.

Allowed:

- Improve Chinese fluency.
- Remove translationese.
- Split or merge sentences inside the same block.
- Move anchors inside the same block.

Forbidden:

- Output raw HTML.
- Drop, duplicate, or cross-move anchors.
- Change protected anchor payloads.
- Rewrite code or formulas.

The polish pass must run through the same model-output, render, and file gates as translation.

## Testing Strategy

### Unit Tests

- Chunker emits only complete `html_fragment` chunks for representative nested HTML.
- Anchor extractor handles `a`, nested `span`, `em`, `strong`, `sub`, and mixed text.
- Anchor extractor avoids `pre`, code-like containers, `style`, `script`, `math`, and standalone media.
- Anchor extractor rejects unsupported descendant markup instead of silently dropping it.
- Anchor extractor never produces overlapping anchor ranges.
- Anchor escaping round-trips delimiter characters, pipes, and visible angle brackets.
- Anchor parser rejects malformed delimiters, duplicate IDs, missing IDs, and cross-block IDs.
- Renderer preserves attributes and restores translated anchor payloads.
- Renderer preserves XHTML namespace declarations and namespace-qualified attributes.
- Renderer round trip with unchanged payload preserves structure.
- Model-output validator rejects raw HTML.
- Polish validator uses the same anchor validation as translation.

### Integration Tests

- Translate a paragraph with glossary links into fluent Chinese and restore valid XHTML.
- Translate a paragraph where Chinese requires moving an anchor.
- Translate nested inline markup and preserve nested element attributes.
- Reject a model output with a missing anchor.
- Reject a model output with raw `<p>` tags.
- Reject a rendered file that fails XML parsing.
- Verify fallback to manual report when all safe modes fail.

### Regression Corpus

Maintain a small fixture suite of difficult EPUB fragments:

- glossary-heavy technical paragraphs
- figure captions
- long sentences with multiple inline links
- navigation text
- alt descriptions
- code-adjacent prose
- references/bibliography

Each fixture should assert:

- structural validity
- recoverability
- absence of residual hard-fail English
- stable technical terms
- expected fluent Chinese sample snippets

## Migration Plan

### Phase 1: Documentation and Test Fixtures

- Land this design.
- Add fixture examples for current stiff translations and expected fluent alternatives.
- Add tests that prove current chunk fragments are complete DOM fragments.

### Phase 2: Anchor IR Prototype

- Implement extractor, parser, validator, and renderer behind a feature flag.
- Support ordinary paragraph/list/caption blocks first.
- Keep existing HTML mode as fallback.
- Persist an explicit anchor IR schema version in checkpoint data so old checkpoints can be detected and safely ignored or migrated.

### Phase 3: Anchor Translation Workflow

- Add anchor mode to the translator.
- Validate model outputs before rendering.
- Persist anchor-mode metadata and final mode in checkpoint JSON.

### Phase 4: Anchor Polish Pass

- Add optional polish pass on anchor IR.
- Run the same validation/render gates.
- Compare quality against existing proofer-only output.

### Phase 5: Default Rollout

- Enable anchor mode by default for supported ordinary prose.
- Keep figures/tables/alt descriptions on existing mode until fixture coverage is strong.
- Expand anchor support incrementally.

## Acceptance Criteria

- No accepted translated EPUB contains malformed XML.
- No accepted chunk has missing or duplicated required anchors.
- No model output raw HTML reaches writeback in anchor mode.
- No anchor-mode block has unrepresented descendant markup.
- Existing tests continue passing.
- New anchor tests cover extraction, validation, rendering, fallback, and polish.
- On a representative sample, prose with inline markup reads more naturally than current strict placeholder/text-node mode.

## Risks and Mitigations

- Risk: anchor syntax appears in source text.
  - Mitigation: escape source delimiter text before model input and unescape after render.
- Risk: model translates or corrupts anchor ID.
  - Mitigation: strict parser rejects missing/renamed IDs and retries.
- Risk: payload replacement in nested anchors changes too much structure.
  - Mitigation: replace only visible text nodes inside cloned anchor subtree; preserve tags and attributes.
- Risk: moving anchors changes semantic scope.
  - Mitigation: allow movement only inside the same block in the first version.
- Risk: tables and complex alt descriptions need different treatment.
  - Mitigation: start anchor mode with ordinary prose; keep existing modes for unsupported structures.
- Risk: programmatic renderer serializes XHTML differently.
  - Mitigation: assert XML parse success and structural equivalence, not byte-for-byte equality.

## Review Checklist

- No model-authored final HTML in anchor mode.
- Chunk closure remains a precondition and is testable.
- Anchor validation happens before rendering.
- Rendering is deterministic and DOM-based.
- Final XML validation remains mandatory.
- Fallbacks never write partial invalid DOM.
- Fluency is improved by allowing same-block anchor movement.
- Unsupported complex structures stay on existing safe modes.
- Delimiter escaping is specified and tested.
- Namespaces and EPUB-specific attributes are preserved.

## Self-Review Result

Review performed against the stated goal: translated output must be recoverable, must not introduce missing closing tags, and must still allow fluent Chinese.

- Completeness: the design covers extraction, IR, validation, rendering, fallback, polish, tests, migration, and acceptance criteria.
- Structural safety: the design removes model-authored final HTML from anchor mode and requires deterministic DOM rendering plus final XML parsing.
- Chunk safety: the design keeps complete-DOM-fragment chunking as a precondition and adds tests to prove it stays true.
- Fluency: the design allows same-block anchor movement and sentence-level rephrasing while keeping cross-block structure fixed.
- Omission check: unsupported or ambiguous descendant markup must make a block ineligible for anchor mode instead of being dropped.
- Risk check: delimiter escaping, nested anchors, namespace preservation, and fallback tracking are explicitly covered.
- Remaining deliberate limitation: the first implementation does not support unrestricted table/figure/alt-description rewriting; those structures stay on existing safe modes until fixture coverage is strong.
