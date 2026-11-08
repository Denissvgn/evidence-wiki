"""An automatic simulated drive keeps the counterevidence gate and replays it from history.

Each test runs a complete native research drive, so this module forms a suite group of its own.
"""

import pytest

from evidence_wiki import Workspace
from tests.test_coverage_session_reference import configured as configured


@pytest.mark.parametrize("configured, expected", [
    ({"counter_disposition": "examined_no_unresolved_objection"}, "accepted"),
    ({"counter_disposition": "opposing"}, "abstained"),
], indirect=["configured"], ids=["simulated-examined", "simulated-opposing"])
def test_simulated_automatic_drive_preserves_counter_gate(configured, expected):
    root, policy, execution, context = configured
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="examination-drive", execution=execution) as session:
            result = session.run_cycle(request_id="drive", max_steps=48)
            assert result["terminal"] and result["outcome"] == expected, result
            records = execution.records(session)
            purposes = {row["data"].get("purpose") for row in records if row["kind"] == "execution_attempt"}
            assert {"coverage", "counterevidence"} <= purposes
            assert not result["evidence_accepted"]
            assert all(role.model_ref.startswith("simulated/") for role in execution.profile.roles)
            replay = session.run_cycle(request_id="drive", max_steps=48)
            assert replay["historical"] and replay["outcome"] == expected
