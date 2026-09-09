# USDB Explorer engineering guidance

- Communicate with the operator in Chinese; keep code comments and logs in English.
- Keep changes scoped and preserve existing operator configuration, deployment IDs, credentials and volumes.
- This repository owns explorer deployment and public RPC gateway code. Access USDB through RPC and checked-in network contracts; do not import sibling node repositories during ordinary builds or tests.
- Preserve the `usdb-public` compatibility command, config/deployment schemas and storage defaults when evolving the `usdb-explorer` command.
- Put integration tests in `tests/` and shared fixtures in `tests/common/`; keep Go unit tests beside gateway code.
- Run the affected Python suites and `go test -race ./...` in `gateway/` when modifying the gateway. Run workflow lint after CI edits.
- Do not create commits, push, publish releases, or operate real services unless the user has authorized the corresponding action.
