# Upgrade and adopt existing research

Installing EvidenceWiki changes the package in the selected Python environment.
Existing workspace scripts, raw evidence, notes, questions and pending work stay
under their existing owners. Choose one interpreter for a run and use it consistently.

Before changing an active workspace, read its installed instructions and current
run, source-request and pack-revision status. Finish or explicitly close active
work under its original controls. Preserve unresolved claims and partial deliveries;
an installation upgrade is not permission to replay or reset them.

For a workspace named `research`, preview the starter update:

```sh
evidence-wiki upgrade --target research --dry-run
```

After reviewing that preview, apply it explicitly:

```sh
evidence-wiki upgrade --target research
evidence-wiki doctor --target research --format json
evidence-wiki agent bootstrap --target research --format json
evidence-wiki agent next --target research --agent-id current
```

Ordinary upgrade refreshes starter-owned scripts and version metadata. Optional
docs and skills are separate selections; local edits can require a conflict decision.
Keep the preserved replacement history. Research configuration and domain-pack
criteria are not silently adopted from a new installation.

Use `pack guide --topic revisions --format text` for a same-pack change. Plan the
revision, inspect conflicts and affected answers, then apply the saved plan and
explicit coverage migration. Old answers remain archived. Changed evidence,
instructions, pack rules or computation definitions need fresh checks and reviews.
Resume an unchanged run through its owner; start a new run after controls change.
Legacy sessions without adequate bindings must be explicitly closed before revision.

Strict research requires an independently controlled review authority. A local
reviewer name, successful setup, framework acknowledgment or old export is not a
current approval. The protected host is an optional macOS boundary; it is not
implemented by an ordinary framework shell or protected parent work order.

Computation accepts the closed declarative language and selected retained records.
It refuses missing required data, unsafe expressions, invalid clock selections
and failed required invariants. Warnings and due local actions require explicit,
identity-bound application. Exact decimal arithmetic does not establish factual
accuracy or domain validity.
