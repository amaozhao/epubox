from engine.agents.proofer import get_proofer
from engine.agents.proofer import instructions as proofer_instructions
from engine.agents.translator import instructions as translator_instructions


def test_translator_prompt_requires_placeholder_order_stability():
    joined = "\n".join(translator_instructions)

    assert "left-to-right order EXACTLY the same" in joined
    assert "Do NOT move, swap, merge, split, or regroup" in joined


def test_proofer_prompt_discourages_placeholder_reordering():
    joined = "\n".join(proofer_instructions)

    assert "Keep the SAME left-to-right order" in joined
    assert "Never swap two placeholders" in joined
    assert "prefer returning NO correction" in joined


def test_proofer_keeps_provider_json_schema_enabled():
    proofer = get_proofer()

    assert proofer.output_schema is not None
    assert proofer.use_json_mode is True
