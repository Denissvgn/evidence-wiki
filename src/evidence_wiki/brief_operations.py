"""A current brief is projected only inside actual native resolution custody."""

from contextlib import contextmanager

from .brief_contracts import refuse
from .brief_projection import project, render
from .resolution_operations import _held_current


@contextmanager
def _held_brief(session):
    from .research_session import ResearchSession
    if type(session) is not ResearchSession:
        refuse("bound_session_required")
    session._check("brief")
    session.context.require_grant("brief_operations.current")
    with _held_current(session) as resolution:
        # Readability projection is a purpose-specific native journal read. It
        # never dispatches, signs or substitutes for resolution closing checks.
        selected = getattr(session.execution.profile, "readability", None)
        state = None
        if selected is not None:
            state = resolution.bind_readability()
        yield project(resolution.document(), readability=state), resolution
        session._check("brief")


def current(session, *, format="structured"):
    """Read one current output; formatting never starts model work."""
    if type(format) is not str or format not in {"structured", "markdown"}:
        refuse("format_invalid")
    with _held_brief(session) as (brief, _resolution):
        if format == "markdown" and brief["readability"]["state"] in {"pending", "refused"}:
            refuse("readability_not_current")
        result = brief if format == "structured" else render(brief)
    return result
