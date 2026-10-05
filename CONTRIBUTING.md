# Contributing to GYANAM

Thanks for your interest in GYANAM — the open debug and observability
reference for AMD GPU products. Contributions are welcome from everyone:
hyperscaler operators, neocloud engineers, established software teams,
and individuals debugging a single node alike.

This guide covers how to get changes accepted. For project background and
architecture, start with the [README](README.md).

## Ways to contribute

- **Report bugs** and request features via GitHub Issues — see
  [Reporting issues](#reporting-issues) below.
- **Contribute Grafana dashboards** under
  [`grafana/provisioning/dashboards/`](grafana/provisioning/dashboards/).
- **Improve transports / collection paths** (Redfish Aggregation / proxy, SSE).
- **Improve documentation** under [`docs/`](docs/).

## Reporting issues

All bugs, feature requests, and questions are tracked through the standard
**GitHub issue process** at
[github.com/amd/Gyanam-GPU-Observability-Assistant/issues](https://github.com/amd/Gyanam-GPU-Observability-Assistant/issues).

Before opening an issue:

1. **Search existing issues** (open and closed) to avoid duplicates — if
   one already exists, add your details as a comment instead.
2. **Pick the right template** when you click *New Issue* — a structured
   form for bug reports or feature requests will guide you through the
   required details.

A good **bug report** includes:

- GYANAM version or git commit (`git rev-parse --short HEAD`).
- Deployment details — OS, Docker version, fleet size, and which transport
  (Redfish Aggregation / proxy / SSE).
- Exact steps to reproduce.
- What you expected vs. what actually happened.
- Relevant logs (`./gyanam.sh logs <service>`) and any error output, with
  credentials and BMC hostnames redacted.

A good **feature request** explains the use case and the problem it solves,
not just the proposed implementation.

> **Security vulnerabilities — do not open a public issue.** Report them
> privately following [SECURITY.md](SECURITY.md).

## Developer Certificate of Origin (DCO)

All contributions to GYANAM require a **DCO sign-off**. The DCO is a
lightweight statement that you have the right to submit your contribution
under the project's MIT license. Read the full text at
[developercertificate.org](https://developercertificate.org/).

You certify the DCO by adding a `Signed-off-by` line to every commit:

```
Signed-off-by: Your Name <your.email@example.com>
```

Git adds this automatically when you commit with `-s`:

```bash
git commit -s -m "your message"
```

The name and email **must match** your Git author identity. Pull requests
whose commits are missing a valid `Signed-off-by` line cannot be merged.
If you forget, you can amend the most recent commit:

```bash
git commit --amend -s --no-edit
```

For multiple commits, rebase and re-sign:

```bash
git rebase --signoff main
```

## Coding assistants and AI tools

AI coding assistants are welcome under one principle: **an assistant is a
tool, and you — the human contributor — are fully responsible for every
line you submit.**

1. **AI agents MUST NOT add a `Signed-off-by` line.** The DCO sign-off is a
   legal certification only a human can make. If you used an assistant, you
   still sign off by hand, and thereby take full responsibility.
2. **Review and understand every AI-generated change before submitting.**
   If you can't explain why the code is correct, it isn't ready.
3. **You own licensing and provenance.** Don't submit output you suspect was
   copied from an incompatible-licensed source; everything must be
   compatible with the project's [MIT License](LICENSE).
4. **AI code meets the same bar as hand-written code** — linters, tests, and
   scope. Don't submit code you haven't built and run.

### Attribution

If an assistant materially shaped a change, credit it with an `Assisted-by:`
trailer below your sign-off:

```
Signed-off-by: Your Name <your.email@example.com>
Assisted-by: Claude (claude-opus-4)
```

List only assistants that generated or substantially shaped the work — not
ordinary tooling (editors, `git`, linters). Attribution flags where extra
scrutiny may help; it does not shift responsibility away from you.

### Assistant-found bugs

Assistants are good at surfacing bugs, but verify independently before you
trust the diagnosis — ideally with a reproducer. Reference the offending
commit (`Fixes: <short-sha> ("<subject>")`) where you can, and state the
limits honestly: if you couldn't reproduce or fully test it, say so in the
PR. For anything security-relevant, follow [SECURITY.md](SECURITY.md)
instead of opening a public issue or PR.

> These follow the spirit of the Linux kernel's
> [AI coding assistants](https://docs.kernel.org/process/coding-assistants.html)
> policy, adapted to GYANAM's MIT + DCO workflow.

## Development setup

1. Fork the repository and create a feature branch off `main`.
2. Install and run the linters / pre-commit hooks documented in
   [`LINTING.md`](LINTING.md) (ruff, mypy, shellcheck).
3. Build and run locally with the management script:

   ```bash
   ./gyanam.sh init
   ./gyanam.sh build
   ./gyanam.sh start
   ```

## Testing

GYANAM has **two test tiers**, and a PR must pass **both**:

### 1. Unit tests (fast, fully mocked) — run on every change

Runs the whole suite in the collector image with coverage enforced. No running
stack required.

```bash
./scripts/run-tests.sh
```

- All tests must pass.
- **Coverage gate: ≥ 95%** (`--cov-fail-under=95`). The run fails if coverage
  drops below that — so new code needs tests, and dead code should be removed
  rather than left uncovered.

### 2. Live smoke tests (against a running stack) — run before you open the PR

These hit the *actual* running services and catch the regressions mocks can't:
image/dependency drift, cross-service wiring, template/route 500s, and real
SQLite/PostgreSQL/InfluxDB connectivity.

```bash
./gyanam.sh init     # one-time: generates .env with secrets
./gyanam.sh build
./gyanam.sh start    # wait until `./gyanam.sh status` shows everything healthy
./scripts/smoke-test.sh
```

- **Read-only by default** — safe to run against any stack, including a live
  one. It only reads health, renders every page, lists targets, exports CSV.
- The create/update/delete round-trip (which *writes* to the database) is
  **gated** behind `GYANAM_LIVE_MUTATE=1` — only enable it on a throwaway/CI
  stack, never a real fleet:

  ```bash
  GYANAM_LIVE_MUTATE=1 ./scripts/smoke-test.sh
  ```

CI runs both tiers on a disposable stack. **To be approved and merged, a PR
should include the output of both runs in its description** (the tail of
`./scripts/run-tests.sh` showing the pass count + coverage %, and the
`./scripts/smoke-test.sh` summary line). Sharing both results helps every
contribution keep the codebase healthy and the deployment solid as the project
grows.

## Commit message convention

```
<type>: <short description>

<body summarizing the essence of the change>

Signed-off-by: Your Name <your.email@example.com>
```

Use a clear `<type>` prefix such as `feat`, `fix`, `docs`, `refactor`,
`perf`, `test`, or `chore`. Keep the subject line concise and in the
imperative mood. If an assistant helped, add an `Assisted-by:` trailer
below the sign-off (see [Coding assistants and AI tools](#coding-assistants-and-ai-tools)).

## Pull request checklist

Before opening a PR, confirm:

- [ ] Every commit has a valid `Signed-off-by` line (DCO).
- [ ] Linters / pre-commit hooks pass (see [`LINTING.md`](LINTING.md)).
- [ ] **Both test tiers pass and their output is pasted in the PR description**
      — `./scripts/run-tests.sh` (pass count + coverage ≥ 95%) and
      `./scripts/smoke-test.sh` (against a running stack). See [Testing](#testing).
- [ ] Changes are scoped and the description explains the *why*.
- [ ] Documentation is updated when behavior or configuration changes.
- [ ] Any new dashboards or schemas follow the existing structure.
- [ ] AI-assisted changes were reviewed and understood; commits credit the
      assistant with `Assisted-by:`, never `Signed-off-by:`.

## Code of conduct

Be respectful and constructive. We want GYANAM to be a welcoming project
for contributors at every level of experience.

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
