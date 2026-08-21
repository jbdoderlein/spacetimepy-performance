# conftest.py
import spacetimepy
import pytest


def pytest_configure(config):
    # One-time initialization before any test runs
    stp = spacetimepy.SpaceTime.open("performance.db")
    print("at start", stp)
    stp.capture.begin_recording()

def pytest_unconfigure(config):
    # One-time initialization before any test runs
    stp = spacetimepy.get_active_spacetime()
    assert stp is not None
    stp.capture.finish_recording()

def pytest_collection_modifyitems(config, items):
    # Wrap every test function with the monitoring decorator
    for item in items:
        if item.name.startswith("test_"):
            item.obj = spacetimepy.line(item.obj)