# Codebase Intake Fixture

This wholly synthetic fixture models an external worker depositing inert,
bounded codebase evidence. `fixture-snapshot.zip` is deliberately plain text;
the product must inventory and hash it but never unpack or execute it.

`artifact-manifest.json` binds `context.json` to a structured external-worker
invocation, byte count, and checksum. Its fake repository identity is an
adversarial field and must not be promoted into normalized repository identity.
`malicious-pre-commit` is hook-shaped text used to prove unsupported files stay
inert and invalidate the deposit.
