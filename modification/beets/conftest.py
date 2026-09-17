from __future__ import annotations

import inspect
import logging as std_logging
import os
import sys
from typing import TYPE_CHECKING

import pytest

from beets import logging
from beets.autotag import Distance
from beets.dbcore.query import Query
from beets.test._common import DummyIO
from beets.test.helper import RUNNING_IN_CI, ConfigMixin, TestHelper
from beets.test.helper import is_importable as check_import
from beets.util import cached_classproperty

import spacetimepy
import test.spacetimepy_picklers


_CAPTURE_LOG = pytest.StashKey[tuple[std_logging.Handler, bool]]()


def _capture_options(function):
    """Exclude confirmed runtime values at their specific capture sites."""
    identity = (function.__module__, function.__qualname__)
    registry_sites = {
        ("test.autotag.test_hooks", "test_correct_list_fields"),
        ("test.ui.test_ui_init", "ParentalDirCreation.test_create_no"),
        ("test.ui.test_ui_init", "ParentalDirCreation.test_create_yes"),
    }
    excluded = {}
    if identity in registry_sites:
        excluded["dispatch_table"] = {
            "scope": "global",
            "reason": (
                "copyreg.dispatch_table is the process serialization registry, "
                "reached through copy.deepcopy. It is not Beets application data."
            ),
        }
    if identity == (
        "test.test_logging", "TestConcurrentEvents.test_concurrent_events"
    ):
        for name in ("t1", "t2"):
            excluded[name] = {
                "scope": "local",
                "reason": "A live operating-system thread cannot be restored.",
            }
    if not excluded:
        return {}

    def metadata(*_):
        return {"capture_exclusion_policy": excluded}

    return {
        "ignored_names": tuple(excluded),
        "start_attributes": metadata,
        "line_attributes": metadata,
    }

if TYPE_CHECKING:
    from typing import TextIO


def skip_marked_items(items: list[pytest.Item], marker_name: str, reason: str):
    for item in (i for i in items if i.get_closest_marker(marker_name)):
        test_name = item.nodeid.split("::", 1)[-1]
        item.add_marker(pytest.mark.skip(f"{reason}: {test_name}"))


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
):
    if not os.environ.get("INTEGRATION_TEST") == "true":
        skip_marked_items(
            items, "integration_test", "INTEGRATION_TEST=1 required"
        )

    if not os.environ.get("LYRICS_UPDATED") == "true":
        skip_marked_items(
            items, "on_lyrics_update", "No change in lyrics source code"
        )

    for item in items:
        if marker := item.get_closest_marker("requires_import"):
            force_ci = marker.kwargs.get("force_ci", True)
            if (
                force_ci
                and RUNNING_IN_CI
                # only apply this to our repository, to allow other projects to
                # run tests without installing all dependencies
                and os.environ.get("GITHUB_REPOSITORY", "") == "beetbox/beets"
            ):
                continue

            modname = marker.args[0]
            if not check_import(modname):
                test_name = item.nodeid.split("::", 1)[-1]
                item.add_marker(
                    pytest.mark.skip(
                        f"{modname!r} is not installed: {test_name}"
                    )
                )

        if isinstance(item, pytest.Function) and item.name.startswith("test_"):
            item.obj = spacetimepy.line(item.obj, **_capture_options(item.obj))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration_test: mark a test as an integration test"
    )
    config.addinivalue_line(
        "markers",
        "on_lyrics_update: run test only when lyrics source code changes",
    )
    config.addinivalue_line(
        "markers",
        (
            "requires_import(module, force_ci=True): run test only if module"
            " is importable (use force_ci=False to allow CI to skip the test too)"
        ),
    )


def pytest_collection_finish(session: pytest.Session) -> None:
    # Build the exact-type reducer table after pytest imports the test classes.
    # Keep capture diagnostics outside the application logs that tests assert.
    logger = std_logging.getLogger("spacetimepy")
    handler = std_logging.FileHandler("serialization.log", mode="w")
    session.config.stash[_CAPTURE_LOG] = (handler, logger.propagate)
    logger.addHandler(handler)
    logger.propagate = False
    stp = spacetimepy.SpaceTime.open(
        "performance.db",
        profile_capture=True,
        custom_picklers=[test.spacetimepy_picklers],
    )
    stp.capture.begin_recording()


def pytest_unconfigure(config):
    stp = spacetimepy.get_active_spacetime()
    try:
        if stp is not None:
            stp.capture.finish_recording()
            stp.close()
    finally:
        if _CAPTURE_LOG in config.stash:
            handler, propagate = config.stash[_CAPTURE_LOG]
            logger = std_logging.getLogger("spacetimepy")
            logger.removeHandler(handler)
            logger.propagate = propagate
            handler.close()


def pytest_make_parametrize_id(config, val, argname):
    """Generate readable test identifiers for pytest parametrized tests.

    Provides custom string representations for:
    - Query classes/instances: use class name
    - Lambda functions: show abbreviated source
    - Other values: use standard repr()
    """
    if inspect.isclass(val) and issubclass(val, Query):
        return val.__name__

    if inspect.isfunction(val) and val.__name__ == "<lambda>":
        return inspect.getsource(val).split("lambda")[-1][:30]

    return repr(val)


def pytest_assertrepr_compare(op, left, right):
    if isinstance(left, Distance) or isinstance(right, Distance):
        return [f"Comparing Distance: {float(left)} {op} {float(right)}"]
    return None


class _CurrentStderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Write CLI logs to the active standard error stream.

    Logging is bootstrapped when the CLI runs instead of when this module is
    imported. That startup can happen while callers have temporarily replaced
    ``sys.stderr`` for capture or redirection, such as pytest's per-test capture
    streams. The handler must not retain the stream that happened to be active
    during the first command invocation because that stream may later be closed.
    Resolving the stream for each record keeps logs attached to the current CLI
    environment.
    """

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, stream: TextIO) -> None:
        pass


@pytest.fixture(autouse=True)
def patch_logging_handler(monkeypatch):
    """Ensure that beets logs are captured by pytest's capture system."""
    monkeypatch.setattr(
        "beets.ui._get_logging_handler", lambda: _CurrentStderrHandler()
    )


@pytest.fixture(autouse=True)
def do_not_log_sources(monkeypatch):
    monkeypatch.setattr("beets.config.log_sources", lambda _: None)


@pytest.fixture(autouse=True)
def clear_cached_classproperty():
    cached_classproperty.cache.clear()


@pytest.fixture
def config():
    """Provide a fresh beets configuration when requested."""
    return ConfigMixin().config


@pytest.fixture
def io(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    capteesys: pytest.CaptureFixture[str],
) -> DummyIO:
    """Fixture for tests that need controllable stdin and captured stdout.

    This fixture builds a per-test ``DummyIO`` helper and exposes it to the
    test. When used on a test class, it attaches the helper as ``self.io``
    attribute to make it available to all test methods, including
    ``unittest.TestCase``-based ones.
    """
    io = DummyIO(monkeypatch, capteesys)

    if request.instance:
        request.instance.io = io

    return io


@pytest.fixture
def is_importable():
    """Fixture that provides a function to check if a module can be imported."""

    return check_import


# Inheriting from TestHelper gives each test function isolated state. Use the
# fixtures below instead when a broader scope is safe and the suite benefits
# from reusing the same helper instance.
@pytest.fixture(scope="session")
def session_helper():
    """Share beets test state across the full test session.

    Use this for suites that tolerate shared library contents and global
    configuration. Tests should target specific records rather than assume a
    completely fresh overall state.
    """
    with TestHelper() as helper:
        yield helper


@pytest.fixture(scope="module")
def module_helper():
    """Share beets test state within one test module.

    Use this when tests in the same file can reuse setup and side effects, but
    later modules should still begin from a clean environment.
    """
    with TestHelper() as helper:
        yield helper


@pytest.fixture(scope="class")
def class_helper():
    """Share beets test state within one test class.

    Use this when methods in a class can build on the same setup, while nearby
    classes still need independent libraries, files, or configuration.
    """
    with TestHelper() as helper:
        yield helper
