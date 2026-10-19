"""Native owner profiles for retained application allocation fixtures."""

from evidence_wiki import ExecutionProfile, ExecutionRole
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.review_application_lease import ReviewApplicationLease


def native_application_profile(lease):
    role = ExecutionRole("evidence", "evaluator", "evidence-controller", "simulated/fixture-evidence", "review/v1")
    scopes = ExecutionRoleRegistry((ExecutionRoleScope("evidence", "evaluator", ("model",)),))
    return ExecutionProfile("retained-fixture-only", 512, "EUR", "simulated-no-provider-charge/v1", (role,),
        1, 1, 1000, "0", 1, role_scopes=scopes,
        review_application_lease=ReviewApplicationLease(lease["seconds"], lease["not_after_utc"], schema_version=lease["schema_version"]))
