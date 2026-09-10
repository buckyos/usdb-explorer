# Explorer Change Fragments

Use the USDB/go-ethereum fragment model: one independently reviewable behavior
change per `.release-notes/fragments/<change_id>.json`, with globally unique
lowercase kebab-case IDs. This repository owns Explorer changes; ordinary
generation and validation never read sibling repositories.

```json
{
  "schema_version": "usdb-change-fragment:v1",
  "change_id": "explorer-example-change",
  "type": "fixed",
  "scopes": ["gateway"],
  "summary": "Describe the observable result",
  "details": ["Describe behavior and important failure or recovery boundaries."],
  "operator_actions": [],
  "compatibility": {
    "network_reset": false,
    "data_rebuild": false,
    "config_change": false,
    "restart_required": false
  },
  "references": []
}
```

The fragment fields, types, compatibility flags and trailer semantics match
USDB. Explorer's allowed scopes are `deployment`, `documentation`, `release`,
`security`, `testing`, `gateway`, `explorer`, and `network`. Types are `added`,
`changed`, `deprecated`, `fixed`, `internal`, `removed`, and `security`.

- `network_reset`: existing installations cannot retain their network identity.
- `data_rebuild`: derived/local data must be rebuilt without changing the network.
- `config_change`: operator-owned settings must change.
- `restart_required`: running components must be replaced or restarted.

Do not also set `data_rebuild` when `network_reset` is true. Declare only concrete
operator actions; an empty list is valid. References must use HTTPS.

Use one or more trailers in a commit body:

```text
Release-Note: explorer-example-change
Release-Note: explorer-another-change
```

Pure maintenance can use `Release-Note: none`. Duplicate IDs and mixing `none`
with real IDs are invalid. Missing/unknown trailers are **unclassified**, with
report-only coverage in `usdb-explorer-release-changes:v1`; they do not become
exempt automatically. Review them before tagging.

Fragments already present at the previous published revision are append-only.
The generator rejects edits, deletion and renaming. Unpublished fragments can
be refined when they describe the same behavior contract.

```bash
python3 scripts/release_notes.py validate-fragments --repository-root .
```

`.release-notes/config.json` enables structured notes for tagged source. Tags
without it retain the legacy four-file asset contract. New releases add
`release-changes.json`, its checksum, and `release-changes.md`; removing those
assets cannot downgrade an enabled tag to legacy publication.

See [release change management](../docs/release-change-management.md) and the
local [fragment skill](../.agents/skills/explorer-release-fragments/SKILL.md).
