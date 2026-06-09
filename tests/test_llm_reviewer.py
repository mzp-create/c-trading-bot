"""Regression tests for the DeepSeek LLM reviewer request config — 2026-06-09
switch to deepseek-v4-flash (a reasoning model). The key risk: reasoning tokens
count against max_tokens, so the old 150-token budget truncated the response
before the JSON verdict, silently turning every review into HOLD. These lock in
a reasoning-sized budget and config-driven model selection."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import analysis.llm_reviewer as llm_mod
from analysis.llm_reviewer import LLMReviewer


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _reviewer(extra=None):
    cfg = {"llm_reviewer": {"api_key": "test-key"}}
    if extra:
        cfg["llm_reviewer"].update(extra)
    return LLMReviewer(cfg)


def test_default_model_is_v4_flash_with_reasoning_budget(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        captured["timeout"] = timeout
        return _Resp({"choices": [{"message": {
            "content": '{"action": "SKIP", "confidence": 0.3, "reason": "chop"}',
            # a reasoning model returns this separately; it MUST be ignored
            "reasoning_content": "long internal chain of thought ..."}}]})

    monkeypatch.setattr(llm_mod.requests, "post", fake_post)

    out = _reviewer()._call_llm("prompt")
    assert captured["json"]["model"] == "deepseek-v4-flash"
    assert captured["json"]["max_tokens"] >= 2048   # room for reasoning + JSON
    assert captured["timeout"] >= 30                 # reasoning models are slower
    assert out["action"] == "SKIP"                   # JSON content parsed
    assert out["llm_called"] is True


def test_model_and_budget_overridable_via_config(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _Resp({"choices": [{"message": {
            "content": '{"action": "CONFIRM", "confidence": 0.6, "reason": "ok"}'}}]})

    monkeypatch.setattr(llm_mod.requests, "post", fake_post)

    _reviewer({"model": "deepseek-v4-pro", "max_tokens": 4096})._call_llm("p")
    assert captured["json"]["model"] == "deepseek-v4-pro"
    assert captured["json"]["max_tokens"] == 4096
