"""Native repair continuations drive reference profiles with explicitly simulated model meters."""

from types import SimpleNamespace

from tests import test_calculation_execution_fixture as scientific
from tests import test_execution_session as reference


def configured(tmp_path, monkeypatch, *, calculated=False):
    factory = scientific if calculated else reference
    options = {"role_isolation": True, "simulated_model_usage": True, "disagreement": True, "repair": True}
    options.update({"calculation": True, "calculated_generation": True} if calculated else {"review_services": True, "service_repair": True})
    return factory.fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param=options))


def change_child_output(execution, changes):
    # Model outputs remain untrusted after qualification. This controlled child
    # produces a genuine owned observation; no policy, receipt or key is edited.
    command = execution.executor._commands["repairer"]
    wrapper = ("import json,subprocess,sys; incoming=sys.stdin.read(); d=json.loads(incoming)['input']; "
        "result=subprocess.run(" + repr(list(command)) + ",input=incoming,text=True,capture_output=True,check=True); "
        "output=json.loads(result.stdout); target=next(c for c in output['proposal']['inventory']['claims'] if c['id']==d['repair_scope']['target_claim_id']); "
        + changes + "; print(json.dumps(output))")
    execution.executor._commands["repairer"] = (*command[:-1], wrapper)
