"""Fixed native publication scope, distinct from worker proposals and public data."""

from .artifact_delivery_contracts import PROPOSAL, checked, refuse
from .artifact_delivery_input import _ACTIVE
from .mutation_contracts import canonical


def require_scope(root, request_id, arguments):
    admission = _ACTIVE.get()
    if admission is None:
        refuse("publication_native_scope_required")
    session = admission.session
    admission.held(session)
    if root != session.root or request_id != admission.identifier or canonical(arguments) != admission.arguments:
        refuse("publication_request_changed")
    plan = session.execution.profile.artifact_delivery
    proposal = checked(PROPOSAL, arguments["proposal"])
    if arguments["plan_sha256"] != plan.sha256 or proposal["plan_sha256"] != plan.sha256:
        refuse("publication_plan_changed")
    plan.destination(proposal["destination_ref"])
    session.context.require_grant("artifact_delivery_operations.deliver")


def deliver(*_args, **_kwargs):
    """The public dictionary owner cannot bypass the registered staged route."""
    refuse("publication_staged_route_required")
