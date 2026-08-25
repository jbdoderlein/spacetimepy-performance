import spacetimepy

from cherrypy.test import spacetimepy_custom_pickler


def pytest_unconfigure(config):
    """Finish the recording and close its database resources."""
    stp = spacetimepy.get_active_spacetime()
    if stp is None:
        return
    stp.capture.finish_recording()
    stp.close()


def pytest_collection_modifyitems(config, items):
    """Start recording and apply line capture to each collected test."""
    stp = spacetimepy.SpaceTime.open(
        'performance.db',
        custom_picklers=[spacetimepy_custom_pickler],
        profile_capture=True,
    )
    stp.capture.begin_recording()
    for item in items:
        if item.name.startswith('test_'):
            item.obj = spacetimepy.line(item.obj)
