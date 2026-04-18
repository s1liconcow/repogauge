from __future__ import annotations

import pytest

import repogauge.lang as lang_module


@pytest.fixture(autouse=True)
def reset_language_registry():
    original_adapters = list(lang_module._REGISTERED_ADAPTERS)
    original_builtins = lang_module._BUILTINS_REGISTERED
    lang_module._REGISTERED_ADAPTERS.clear()
    lang_module._BUILTINS_REGISTERED = False
    try:
        yield
    finally:
        lang_module._REGISTERED_ADAPTERS[:] = original_adapters
        lang_module._BUILTINS_REGISTERED = original_builtins
        assert [adapter.name() for adapter in lang_module._REGISTERED_ADAPTERS] == [
            adapter.name() for adapter in original_adapters
        ]
