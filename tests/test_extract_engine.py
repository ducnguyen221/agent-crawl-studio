"""SPR-04 TK4 (FR21.6, FR18.5): the extract engine is named in one place and found without importing it."""

import sys

from agent_crawl_studio import capabilities, extract


def test_engine_is_trafilatura():
    assert extract.ENGINE == "trafilatura"
    assert extract.ENGINE in capabilities.ENGINES


def test_find_engine_present_returns_version():
    seen = []

    def version_of(name):
        seen.append(name)
        return "2.3.1"

    assert extract.find_engine(version_of=version_of) == "2.3.1"
    assert seen == ["trafilatura"]


def test_find_engine_missing_returns_none():
    def version_of(name):
        raise LookupError(name)

    assert extract.find_engine(version_of=version_of) is None


def test_find_engine_does_not_import_the_package():
    sys.modules.pop("trafilatura", None)
    extract.find_engine(version_of=lambda name: "2.3.1")
    extract.find_engine()  # the default probe reads package metadata only
    assert "trafilatura" not in sys.modules


def test_default_probe_is_capabilities_default():
    import inspect

    default = inspect.signature(extract.find_engine).parameters["version_of"].default
    assert default is capabilities.default_version_of
