import contextlib
import io
import json
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import cli, orchestration


def bash_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)\n```", text, flags=re.DOTALL)


def shell_argv(command: str) -> list[str]:
    return shlex.split(command.replace("\\\n", " "))


class ReadmeAutonomousTourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        tour = cls.readme.split("## Five-Minute Tour", 1)[1].split("## Drive It With An Agent", 1)[0]
        cls.tour_blocks = bash_blocks(tour)

    def test_primary_tour_deploy_question_and_runner_commands_parse_and_execute(self):
        deploy_block = next(block for block in self.tour_blocks if "evidence-wiki deploy" in block)
        question_block = next(block for block in self.tour_blocks if "questions add" in block)
        run_block = next(block for block in self.tour_blocks if "orchestrate run" in block)

        deploy_command = deploy_block.split("evidence-wiki deploy", 1)[1]
        deploy_command = "evidence-wiki deploy" + deploy_command.split("\ncd ", 1)[0]
        deploy_argv = shell_argv(deploy_command)
        self.assertEqual(["arxiv", "openalex"], [
            deploy_argv[index + 1]
            for index, value in enumerate(deploy_argv)
            if value == "--discovery-provider"
        ])
        self.assertEqual(["arxiv", "openalex"], [
            deploy_argv[index + 1]
            for index, value in enumerate(deploy_argv)
            if value == "--acquisition-provider"
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "solid-state-batteries"
            deploy_argv[deploy_argv.index("--target") + 1] = str(workspace)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cli.main(deploy_argv[1:]))

            batch_text = question_block.split("<<'EOF'\n", 1)[1].split("\nEOF", 1)[0]
            batch_path = Path(tmpdir) / "batch.yaml"
            batch_path.write_text(batch_text + "\n", encoding="utf-8")
            question_command = question_block.split("\nEOF\n", 1)[1]
            question_argv = shell_argv(question_command)
            question_argv[question_argv.index("--target") + 1] = str(workspace)
            question_argv[question_argv.index("--from-file") + 1] = str(batch_path)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cli.main(question_argv[1:]))

            run_argv = shell_argv(run_block)
            run_argv[run_argv.index("--target") + 1] = str(workspace)
            parsed = orchestration.build_parser().parse_args(run_argv[2:])
            self.assertEqual("run", parsed.command)
            self.assertEqual("codex", parsed.runner)
            self.assertEqual("battery-demo", parsed.agent_id)

    def test_documented_external_protocol_start_next_and_status_execute(self):
        protocol_block = next(
            block
            for block in bash_blocks(self.readme)
            if "orchestrate start" in block and "orchestrate submit" in block
        )
        commands = [line for line in protocol_block.splitlines() if line.startswith("evidence-wiki orchestrate")]
        self.assertEqual(4, len(commands))

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "protocol-workspace"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    cli.main(
                        [
                            "deploy",
                            "--target",
                            str(workspace),
                            "--project-name",
                            "protocol-workspace",
                            "--project-description",
                            "README protocol validation",
                        ]
                    ),
                )

            start_argv = shell_argv(commands[0])
            start_argv[start_argv.index("PATH")] = str(workspace)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, cli.main(start_argv[1:]))
            orchestration_id = json.loads(output.getvalue())["orchestration_id"]

            for command in (commands[1], commands[3]):
                argv = shell_argv(command)
                argv[argv.index("PATH")] = str(workspace)
                argv[argv.index("ORCH_ID")] = orchestration_id
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, cli.main(argv[1:]))


if __name__ == "__main__":
    unittest.main()
