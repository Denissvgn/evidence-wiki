# Environment setup

Workspace initialization copies tooling and records provider policy. It does
not need API credentials, prompt for secrets, or contact providers. Check the
environment before the first command that needs an external service:

```sh
evidence-wiki env check
evidence-wiki env check --service github --service openalex --format json
```

With no selections, `check` lists the known variables as optional. Use repeated
`--service` or `--require-env NAME` options to define the variables required by
your workflow. The command exits with status 1 if a selected variable is absent,
empty or whitespace-only, and 0 when all selected variables are present.

| Service | Variable |
| --- | --- |
| `openai` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `github` | `GITHUB_TOKEN` |
| `openalex` | `OPENALEX_API_KEY` |

LLM credentials belong to the selected agent runner or host. A runner may use
its own login instead. GitHub and OpenAlex access requirements depend on the
operation and provider limits; choosing their preset makes the variable a
requirement for this command. This helper checks presence only. It does not
validate key formats, authentication, permissions, billing or connectivity,
enable providers, or establish research readiness. Use `evidence-wiki doctor`
and `evidence-wiki agent inspect --target WORKSPACE` for other local capabilities.

## Protected manual input

Launch the command that needs credentials through the environment helper:

```sh
evidence-wiki env run --service openalex --prompt -- \
  evidence-wiki orchestrate run --target ./my-research --runner codex
```

The helper reuses existing non-empty values and asks only for missing selected
variables. Terminal echo is disabled. An unavailable protected terminal, empty
input or cancellation prevents the command from starting. Input is never taken
from a pipe and echo-enabled fallback is refused. In unattended runs, inject
credentials through the host or CI secret manager and omit `--prompt`.

Entered values exist only in the launched command's environment and its child
processes. The helper does not save them in `.env`, `research.yml`, profiles,
reports, logs or shell startup files, and it cannot change the parent shell.
The selected command receives the values, so run only a command you trust to
handle credentials. For repeated use, let an operator-managed secret store
inject the environment; see [provider secret handling](acquisition.md#provider-secrets-and-rotation).

The `--` separator is required. Arguments are passed directly without shell
expansion. The helper preserves the command's exit status; missing variables
without `--prompt` return 1, unusable protected input returns 2, cancellation
returns 130, a missing executable returns 127 and another launch failure returns
126. A child terminated by a signal returns 128 plus its signal number.

For other integrations, name each required variable without supplying its value
on the command line:

```sh
evidence-wiki env check --require-env CUSTOM_API_KEY --format json
evidence-wiki env run --require-env CUSTOM_API_KEY --prompt -- my-agent
```

The JSON report uses `schema_version: evidence-environment/v1`, lists name,
required and present fields in `checks`, and records `missing_required` and
`ready`. `ready` describes only the selected variable presence check.
