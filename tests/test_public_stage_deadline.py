"""Public applications share one observed finite deadline without renewal."""

import copy
import math
import subprocess
import sys
import time

import pytest

from tools._installed_receipts import PUBLIC_STAGES, replay_commands
from tools._qualification_process import CommandRunner
from tools.probe_installed_public import (
    PUBLIC_APPLICATIONS,
    PUBLIC_COMMAND_ORDER,
    PUBLIC_FIXED_LIMITS,
    PUBLIC_STAGE_SECONDS,
    PublicStage,
    checked_supervision,
    supervision_contract,
)


def synthetic_supervision():
    """Clock-shape vectors only; actual process receipt joins are exercised separately."""
    records, selections = [], []
    instant, start, deadline = 101.0, 100.0, 5500.0
    for name in PUBLIC_COMMAND_ORDER:
        remaining = deadline-instant
        limit = remaining if name in PUBLIC_APPLICATIONS else min(PUBLIC_FIXED_LIMITS[name], remaining)
        selection = {"stage":name, "selected_monotonic":instant, "remaining_seconds":remaining, "timeout_seconds":limit,
                     "stage_start_monotonic":start,"stage_deadline_monotonic":deadline}
        selections.append(selection)
        records.append({"stage":name,"supervision":selection,"deadline_monotonic":deadline,
                        "budget_started_monotonic":instant+0.01,"started_monotonic":instant+0.02,
                        "effective_deadline_monotonic":min(deadline,instant+0.01+limit),
                        "completed_monotonic":instant+1,"timeout_seconds":min(limit,deadline-(instant+0.01))})
        instant += 2
    return {"commands":records,"supervision":{"start_monotonic":start,"deadline_monotonic":deadline,
            "entered_monotonic":100.5,"completed_monotonic":instant,"selections":selections}}


def test_applications_receive_only_remaining_shared_stage_time_and_fixed_limits_stay_fixed():
    clock = [100.0]
    stage = PublicStage(100, 5500, clock=lambda:clock[0])
    clock[0] = 120
    first = stage.select("public-direct-host")
    clock[0] = 1900
    second = stage.select("public-current-caller")
    clock[0] = 3600
    third = stage.select("public-mcp")
    assert [row["timeout_seconds"] for row in (first,second,third)] == [5380,3600,1900]
    assert all(row["stage_deadline_monotonic"] == 5500 for row in stage.selections)
    assert stage.select("public-cli-contract")["timeout_seconds"] == 900
    assert stage.select("public-cli-runtime-refusal")["timeout_seconds"] == 60
    assert stage.select("public-mcp-stdio")["timeout_seconds"] == 60
    assert supervision_contract()["stage_seconds_maximum"] == PUBLIC_STAGE_SECONDS == 5400


def test_expired_public_stage_does_not_start_a_process():
    clock = [100.0]
    stage = PublicStage(100, 5500, clock=lambda:clock[0])
    clock[0] = 5500
    class NeverRun:
        def run(self,*args,**kwargs):
            pytest.fail("expired public stage started a process")
    with pytest.raises(ValueError, match="expired before command"):
        stage.run(NeverRun(),[sys.executable],label="public-direct-host")
    assert stage.selections == []


def test_runner_rejects_expired_absolute_deadline_before_process_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess,"Popen",lambda *args,**kwargs:pytest.fail("expired deadline started process"))
    runner = CommandRunner(tmp_path)
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run([sys.executable],label="expired",deadline_monotonic=time.monotonic()-1)
    assert runner.records == []


@pytest.mark.parametrize("defect", [None,"renewed","parent","remaining","receipt","overrun","reordered","effective","individual"])
def test_deadline_shape_and_receipt_tampering_refuse(defect):
    report = synthetic_supervision()
    parent_stage = {"start_monotonic":100.0,"deadline_monotonic":5500.0}
    parent = {"deadline_monotonic":5500.0,"budget_started_monotonic":100.01,"started_monotonic":100.02,
              "effective_deadline_monotonic":5500.0,
              "completed_monotonic":report["supervision"]["completed_monotonic"]+1,"timeout_seconds":5399.99,
              "supervision":{"stage_start_monotonic":100.0,"stage_deadline_monotonic":5500.0,
                             "selected_monotonic":100.0,"remaining_seconds":5400.0,"timeout_seconds":5400.0}}
    if defect == "renewed":
        report["supervision"]["selections"][-1]["stage_deadline_monotonic"] += 5400
    elif defect == "parent":
        parent["deadline_monotonic"] += 5400
    elif defect == "remaining":
        report["supervision"]["selections"][-1]["timeout_seconds"] = 5400
    elif defect == "receipt":
        report["commands"][-1]["timeout_seconds"] += 10
    elif defect == "overrun":
        report["commands"][-1]["completed_monotonic"] = 5501
    elif defect == "reordered":
        report["commands"][-1],report["commands"][-2] = report["commands"][-2],report["commands"][-1]
    elif defect == "effective":
        report["commands"][0]["effective_deadline_monotonic"] = 5500.0
    elif defect == "individual":
        report["commands"][0]["completed_monotonic"] = report["commands"][0]["budget_started_monotonic"]+61
        report["commands"][0]["seconds"] = 61
        instant = report["commands"][0]["completed_monotonic"]+1
        for selection,record in zip(report["supervision"]["selections"][1:],report["commands"][1:],strict=True):
            name=record["stage"]; remaining=5500.0-instant
            limit=remaining if name in PUBLIC_APPLICATIONS else min(PUBLIC_FIXED_LIMITS[name],remaining)
            selection.update(selected_monotonic=instant,remaining_seconds=remaining,timeout_seconds=limit)
            record.update(budget_started_monotonic=instant+0.01,started_monotonic=instant+0.02,completed_monotonic=instant+1,
                          timeout_seconds=min(limit,5500.0-(instant+0.01)),effective_deadline_monotonic=min(5500.0,instant+0.01+limit))
            instant+=2
        report["supervision"]["completed_monotonic"]=instant
        parent["completed_monotonic"]=instant+1
    if defect:
        with pytest.raises(ValueError):
            checked_supervision(report,parent_record=parent,parent_stage=parent_stage)
    else:
        assert checked_supervision(report,parent_record=parent,parent_stage=parent_stage)


def test_real_retained_commands_join_actual_deadline_selections(tmp_path):
    start = time.monotonic()
    stage = PublicStage(start,start+30)
    runner = CommandRunner(tmp_path)
    for name in PUBLIC_COMMAND_ORDER:
        exit_code = 2 if name == "public-cli-runtime-refusal" else 0
        result = stage.run(runner,[sys.executable,"-c",f"import sys; print('{{}}'); sys.exit({exit_code})"],
                           label=name,expected=(exit_code,),input="{}" if name == "public-mcp-stdio" else None)
        assert result.returncode == exit_code
    report = {"commands":runner.records,"supervision":stage.finish()}
    replay_commands(tmp_path,runner.records,PUBLIC_STAGES)
    checked_supervision(report)
    changed = copy.deepcopy(report)
    changed["commands"][-1]["supervision"]["timeout_seconds"] = 5400
    with pytest.raises(ValueError):
        checked_supervision(changed)


def test_absolute_deadline_overrun_and_cleanup_cannot_report_success(tmp_path):
    runner = CommandRunner(tmp_path)
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run([sys.executable,"-c","import time; time.sleep(2)"],label="overrun",timeout=900,
                   deadline_monotonic=time.monotonic()+0.1)
    assert runner.records[0]["status"] == "timed_out"
    assert runner.records[0]["timeout_seconds"] <= 0.1


def test_existing_native_default_budget_has_no_public_deadline_options(tmp_path):
    runner = CommandRunner(tmp_path)
    result = runner.run([sys.executable,"-c","pass"],label="native-control")
    assert result.returncode == 0
    assert runner.records[0]["timeout_seconds"] == 900
    assert "deadline_monotonic" not in runner.records[0]
    assert "supervision" not in runner.records[0]


def test_individual_public_timeout_expires_before_later_shared_deadline(tmp_path):
    runner=CommandRunner(tmp_path)
    started=time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run([sys.executable,"-c","import time; time.sleep(.15)"],label="individual",timeout=0.1,
                   deadline_monotonic=started+10)
    record=runner.records[0]
    assert record["status"] == "timed_out"
    assert record["effective_deadline_monotonic"] <= record["budget_started_monotonic"]+0.1
    assert record["effective_deadline_monotonic"] < record["deadline_monotonic"]


def absolute_clock_vector(start, selected=5400.0):
    deadline=start+selected
    report=synthetic_supervision()
    report['supervision'].update(start_monotonic=start,deadline_monotonic=deadline,
        entered_monotonic=start+0.5,completed_monotonic=start+18)
    instant=start+1
    for selection,record in zip(report['supervision']['selections'],report['commands'],strict=True):
        name=record['stage'];remaining=deadline-instant
        limit=remaining if name in PUBLIC_APPLICATIONS else min(PUBLIC_FIXED_LIMITS[name],remaining)
        selection.update(stage_start_monotonic=start,stage_deadline_monotonic=deadline,
            selected_monotonic=instant,remaining_seconds=remaining,timeout_seconds=limit)
        record.update(deadline_monotonic=deadline,budget_started_monotonic=instant+0.01,
            started_monotonic=instant+0.02,completed_monotonic=instant+1,
            effective_deadline_monotonic=min(deadline,instant+0.01+limit),timeout_seconds=min(limit,deadline-(instant+0.01)))
        instant+=2
    parent={'deadline_monotonic':deadline,'budget_started_monotonic':start+0.01,'started_monotonic':start+0.02,
        'completed_monotonic':start+19,'effective_deadline_monotonic':min(deadline,start+0.01+selected),
        'timeout_seconds':min(selected,deadline-(start+0.01)),
        'supervision':{'stage_start_monotonic':start,'stage_deadline_monotonic':deadline,
            'selected_monotonic':start,'remaining_seconds':selected,'timeout_seconds':selected}}
    return report,parent,{'start_monotonic':start,'deadline_monotonic':deadline}


@pytest.mark.parametrize('start',[0.01,1000.1,11675.621009695,16383.9,1e9+0.1])
def test_absolute_stage_bound_accepts_exact_addition_without_subtractive_cancellation(start):
    deadline=start+5400.0
    stage=PublicStage(start,deadline,clock=lambda:start+0.5)
    assert stage.deadline == deadline
    report,parent,handoff=absolute_clock_vector(start)
    assert checked_supervision(report,parent_record=parent,parent_stage=handoff)
    with pytest.raises(ValueError,match='frozen maximum'):
        PublicStage(start,math.nextafter(deadline,math.inf),clock=lambda:start+0.5)
    changed=copy.deepcopy(report)
    changed['supervision']['deadline_monotonic']=math.nextafter(deadline,math.inf)
    with pytest.raises(ValueError):
        checked_supervision(changed,parent_record=parent,parent_stage=handoff)


def test_exact_observed_linux_clock_vector_does_not_add_budget():
    start,deadline=11675.621009695,17075.621009695
    assert deadline-start == 5400.000000000002
    assert deadline == start+5400
    assert PublicStage(start,deadline,clock=lambda:start+1).deadline == deadline


@pytest.mark.parametrize('defect',['shortened','extended','remaining','effective','bool','nan','infinity'])
def test_parent_limit_and_effective_deadline_joins_refuse_adjacent_or_invalid_values(defect):
    report,parent,handoff=absolute_clock_vector(1e9+0.1)
    if defect in ('shortened','extended'):
        parent['supervision']['timeout_seconds']=math.nextafter(5400.0,-math.inf if defect=='shortened' else math.inf)
    elif defect=='remaining':
        parent['supervision']['remaining_seconds']=math.nextafter(5400.0,-math.inf)
    elif defect=='effective':
        report['commands'][0]['effective_deadline_monotonic']=math.nextafter(report['commands'][0]['effective_deadline_monotonic'],math.inf)
    else:
        report['commands'][0]['budget_started_monotonic']={'bool':True,'nan':math.nan,'infinity':math.inf}[defect]
    with pytest.raises(ValueError):
        checked_supervision(report,parent_record=parent,parent_stage=handoff)
