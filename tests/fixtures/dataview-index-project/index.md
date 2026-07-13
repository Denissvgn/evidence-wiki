# Dataview Index Fixture

This index intentionally uses Dataview sections instead of static page links.
The linter should treat pages in these directories as indexed when
`lint.dataview_aware` is enabled.

## Sources

```dataview
TABLE summary, updated, source_ids
FROM "wiki/sources"
SORT updated DESC
```

## Concepts

```dataview
TABLE summary, updated, source_ids
FROM "wiki/concepts"
SORT updated DESC
```

## Claims

```dataview
TABLE claim_type, subject, predicate, object, scope, updated, source_ids
FROM "wiki/claims"
SORT updated DESC
```
