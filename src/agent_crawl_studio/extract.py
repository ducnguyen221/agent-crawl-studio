"""Content extraction (SPR-04). TK4 holds only the engine name and a probe that never imports it."""

from .capabilities import default_version_of

ENGINE = "trafilatura"


def find_engine(version_of=default_version_of):
    """Installed version of the extract engine, or None when it is missing. Reads metadata only."""
    try:
        return version_of(ENGINE)
    except LookupError:
        return None
