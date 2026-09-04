# conftest.py
import spacetimepy  # type: ignore[import-untyped]
import pytest

import spacetimepy_custom_pickler


def pytest_configure(config):
    # One-time initialization before any test runs
    stp = spacetimepy.SpaceTime.open(
        "performance.db",
        custom_picklers=(spacetimepy_custom_pickler,),
        # Only when finding serialization problem
        profile_capture=True,
        #logging_level="WARNING",
    )
    print("at start   ", stp)
    stp.capture.begin_recording()

def pytest_unconfigure(config):
    stp = spacetimepy.get_active_spacetime()
    assert stp is not None
    stp.capture.finish_recording()

def pytest_collection_modifyitems(config, items):
    # Wrap every test function with the monitoring decorator
    for item in items:
        if item.name.startswith("test_"):
            item.obj = spacetimepy.line(item.obj)
