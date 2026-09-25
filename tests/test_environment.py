"""Credential setup reports and terminal failure boundaries."""

import getpass
import json
import os
import subprocess
import sys
import warnings
from types import SimpleNamespace
from unittest import mock

import pytest

from evidence_wiki import cli, environment


@pytest.fixture(autouse=True)
def clean_credentials(monkeypatch):
    for name in (*environment.SERVICE_VARIABLES.values(), "CUSTOM_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_default_inventory_is_optional_and_imports_do_not_prompt(capsys):
    with mock.patch.object(getpass, "getpass", side_effect=AssertionError("Unexpected input")):
        assert cli.main(["env", "check", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] and report["validation"] == "presence_only"
    assert not report["credentials_persisted"]
    assert all(not row["required"] and not row["present"] for row in report["checks"])
    assert {row["name"] for row in report["checks"]} == set(environment.SERVICE_VARIABLES.values())


@pytest.mark.parametrize("missing_value", [None, "", " \t\n"])
def test_selected_variables_are_required_and_reports_never_include_values(missing_value, monkeypatch, capsys):
    secret = "credential-content-not-for-output"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    if missing_value is not None:
        monkeypatch.setenv("OPENALEX_API_KEY", missing_value)
    assert cli.main(["env", "check", "--service", "github", "--service", "openalex", "--format", "json"]) == 1
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    report = json.loads(output.out)
    assert report["missing_required"] == ["OPENALEX_API_KEY"]
    assert report["checks"] == [
        {"name": "GITHUB_TOKEN", "present": True, "required": True},
        {"name": "OPENALEX_API_KEY", "present": False, "required": True},
    ]


def test_existing_and_duplicate_requirements_succeed_without_prompt(monkeypatch, capsys):
    monkeypatch.setenv("CUSTOM_API_KEY", "private-value")
    assert cli.main(["env", "check", "--require-env", "CUSTOM_API_KEY", "--require-env", "CUSTOM_API_KEY"]) == 0
    text = capsys.readouterr().out
    assert text.count("CUSTOM_API_KEY") == 1 and "private-value" not in text


def test_windows_custom_variable_names_use_environment_case_rules(monkeypatch):
    monkeypatch.setattr(environment, "os", SimpleNamespace(name="nt", environ={"CUSTOM_API_KEY": "present"}))
    report = environment.environment_report(required_names=["custom_api_key"], environ=dict(environment.os.environ))
    assert report["ready"] and report["checks"] == [{"name": "CUSTOM_API_KEY", "required": True, "present": True}]


@pytest.mark.parametrize("name", ["CUSTOM_API_KEY=do-not-reflect-this", "bad-name", "", "A" * 129])
def test_invalid_names_are_rejected_without_reflecting_input(name, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["env", "check", "--require-env", name])
    assert error.value.code == 2
    assert "do-not-reflect-this" not in capsys.readouterr().err


@pytest.mark.parametrize("options", [[], ["--prompt"]])
def test_missing_credentials_never_start_command_without_protected_input(options, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with mock.patch.object(environment.subprocess, "run") as run, mock.patch.object(getpass, "getpass") as prompt:
        code = cli.main(["env", "run", "--service", "openalex", *options, "--", "unused-command"])
    assert code == (2 if options else 1)
    run.assert_not_called()
    prompt.assert_not_called()
    assert capsys.readouterr().out == ""


def interactive(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)


def test_prompt_only_missing_values_and_keep_them_in_child_environment(monkeypatch, tmp_path, capsys):
    interactive(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "already-configured-secret")
    before = dict(os.environ)
    with mock.patch.object(getpass, "getpass", return_value="entered-secret") as prompt, \
            mock.patch.object(environment.subprocess, "run", return_value=SimpleNamespace(returncode=7)) as run:
        code = cli.main(["env", "run", "--service", "github", "--service", "openalex", "--prompt", "--",
                         "my-agent", "--message", "literal; $(no-shell)"])
    assert code == 7
    assert prompt.call_count == 1 and prompt.call_args.args[0] == "OPENALEX_API_KEY: "
    assert run.call_args.args[0] == ["my-agent", "--message", "literal; $(no-shell)"]
    assert run.call_args.kwargs["env"]["OPENALEX_API_KEY"] == "entered-secret"
    assert run.call_args.kwargs["env"]["GITHUB_TOKEN"] == "already-configured-secret"
    assert not run.call_args.kwargs.get("shell", False)
    assert dict(os.environ) == before and list(tmp_path.iterdir()) == []
    output = capsys.readouterr()
    assert "entered-secret" not in output.out + output.err
    assert "already-configured-secret" not in output.out + output.err


@pytest.mark.parametrize("failure", [EOFError(), KeyboardInterrupt(), OSError("sensitive detail")])
def test_cancelled_or_unavailable_input_discards_partial_environment(failure, monkeypatch, capsys):
    interactive(monkeypatch)
    with mock.patch.object(getpass, "getpass", side_effect=["partial-secret", failure]), \
            mock.patch.object(environment.subprocess, "run") as run:
        code = cli.main(["env", "run", "--service", "github", "--service", "openalex", "--prompt", "--", "unused"])
    assert code == (2 if isinstance(failure, OSError) else 130)
    run.assert_not_called()
    assert "GITHUB_TOKEN" not in os.environ
    output = capsys.readouterr()
    assert "partial-secret" not in output.out + output.err
    assert "sensitive detail" not in output.out + output.err


def test_echo_fallback_is_refused_before_reading(monkeypatch, capsys):
    interactive(monkeypatch)
    fallback_input = mock.Mock()

    def unprotected(*args, **kwargs):
        warnings.warn("Cannot control echo", getpass.GetPassWarning, stacklevel=2)
        return fallback_input()

    with mock.patch.object(getpass, "getpass", side_effect=unprotected), \
            mock.patch.object(environment.subprocess, "run") as run:
        assert cli.main(["env", "run", "--service", "openai", "--prompt", "--", "unused"]) == 2
    fallback_input.assert_not_called()
    run.assert_not_called()
    assert "echo suppression" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["", " \t", "secret\nsecond-line", "secret\0suffix"])
def test_invalid_prompt_values_never_run_or_leak(value, monkeypatch, capsys):
    interactive(monkeypatch)
    with mock.patch.object(getpass, "getpass", return_value=value), mock.patch.object(environment.subprocess, "run") as run:
        assert cli.main(["env", "run", "--service", "anthropic", "--prompt", "--", "unused"]) == 2
    run.assert_not_called()
    assert "secret" not in capsys.readouterr().err


def test_existing_credentials_work_unattended_even_with_prompt_flag(monkeypatch, capsys):
    monkeypatch.setenv("CUSTOM_API_KEY", "inherited-value")
    with mock.patch.object(getpass, "getpass", side_effect=AssertionError("Unexpected input")):
        code = cli.main(["env", "run", "--require-env", "CUSTOM_API_KEY", "--prompt", "--", sys.executable, "-c",
                         "import os, sys; sys.exit(9 if os.environ.get('CUSTOM_API_KEY') == 'inherited-value' else 8)"])
    assert code == 9
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("failure, expected", [(FileNotFoundError("private-detail"), 127),
                                               (PermissionError("private-detail"), 126), (KeyboardInterrupt(), 130)])
def test_launch_failures_do_not_echo_arguments_or_environment(failure, expected, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "private-detail")
    with mock.patch.object(environment.subprocess, "run", side_effect=failure):
        assert cli.main(["env", "run", "--service", "github", "--", "unused"]) == expected
    output = capsys.readouterr()
    assert "private-detail" not in output.out + output.err


def test_signal_status_is_preserved(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "present")
    with mock.patch.object(environment.subprocess, "run", return_value=SimpleNamespace(returncode=-15)):
        assert cli.main(["env", "run", "--service", "github", "--", "unused"]) == 143


@pytest.mark.parametrize("arguments", [["--", "unused"], ["--service", "openai"],
                                        ["--service", "openai", "--"], ["--service", "openai", "unused"]])
def test_run_requires_explicit_selection_and_command_separator(arguments):
    with mock.patch.object(getpass, "getpass") as prompt, mock.patch.object(environment.subprocess, "run") as run:
        with pytest.raises(SystemExit) as error:
            cli.main(["env", "run", *arguments])
    assert error.value.code == 2
    prompt.assert_not_called()
    run.assert_not_called()


def test_package_import_is_noninteractive_and_does_not_load_environment_helper():
    result = subprocess.run(
        [sys.executable, "-c", "import sys, evidence_wiki; assert 'evidence_wiki.environment' not in sys.modules"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
