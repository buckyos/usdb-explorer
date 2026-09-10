---
name: explorer-release-fragments
description: Maintain Explorer release fragments for user-visible, operator-visible, compatibility, security, and release changes; audit the published source range before tagging.
---

# Explorer Release Fragments

Adapted from the canonical USDB `usdb-release-fragments` skill and its
go-ethereum workflow. This standalone variant uses Explorer's own schema
documentation and validator; it is not a synchronized copy of the three node
repositories' skill.

Read `.release-notes/README.md` and `docs/release-change-management.md` before
writing a fragment. Use `scripts/release_notes.py` when validator behavior is
unclear. Do not require sibling repositories to build, validate, or release.

1. Inspect status, relevant diffs, unpublished fragments, and the previous
   **published** Explorer release. Drafts do not establish a release boundary.
2. Record one independently reviewable behavior change per fragment. Reuse an
   unpublished entry only for the same behavior contract. Prefer globally
   unique `explorer-...` change IDs, never a release ID.
3. Describe observable effects and failure/recovery boundaries in `details`.
   Add only necessary, concrete `operator_actions` and conservative
   compatibility flags. Formatting/tests/internal maintenance normally use
   `Release-Note: none` instead of inventing a user-facing change.
4. Never modify or delete a fragment already included in a published release.
5. Validate with `python3 scripts/release_notes.py validate-fragments` and run
   affected tests. Report the fragment path and recommended
   `Release-Note: <change_id>` trailer when summarizing work.
6. Before freezing a tag, run `scripts/prepare_release.py` in its default
   preflight mode. Review every unclassified commit, consolidate fragments,
   and compare the generated compatibility evidence with the intended upgrade.
   Missing trailers remain report-only; never silently label them exempt.

Keep fragments current during development, then repeat the range review before
tagging. The source revision and previous published boundary are frozen in the
generated change record; Publish revalidates that record and its rendered body.

Never commit, tag, push, or alter release state without user authorization.
