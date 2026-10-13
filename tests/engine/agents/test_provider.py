from types import SimpleNamespace

import pytest

from engine.agents import models


def test_run_model_requires_exact_frozen_provider_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        models,
        "settings",
        SimpleNamespace(AGNES_API_KEY="secret-for-test", CR_PROXY_API_KEY="secret-for-test"),
    )
    monkeypatch.setattr(models, "agnes_keys", lambda _: ("secret-for-test", "second-key"))

    class Model(SimpleNamespace):
        pass

    built: list[dict[str, object]] = []

    def build(**kwargs):
        built.append(kwargs)
        return Model(id="actual-model")

    monkeypatch.setattr(models, "build_primary_model", build)

    model = models.build_run_model("agnes", "actual-model", max_output_tokens=128)
    assert model.id == "actual-model"
    assert built == [{}]
    assert not hasattr(model, "_epubox_keys")
    with pytest.raises(ValueError, match="differs from the frozen"):
        models.build_run_model("agnes", "another-model", max_output_tokens=128)
    with pytest.raises(ValueError, match="unsupported"):
        models.build_run_model("other", "actual-model", max_output_tokens=128)
