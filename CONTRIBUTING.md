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
- **Add or refine metric schemas** in
  [`collector/config/metrics_schema.yaml`](collector/config/metrics_schema.yaml).
- **Contribute Grafana dashboards** under
  [`grafana/provisioning/dashboards/`](grafana/provisioning/dashboards/).
- **Improve transports / collection paths** (Redfish, SSH-proxy, SSE).
- **Improve documentation** under [`docs/`](docs/).
- **Help land roadmap items** (in-band telemetry via AMD Device Metrics
  Exporter, VM-level telemetry, broader log collection, security
  hardening).

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
  (Redfish / SSH-proxy / SSE).
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

4. If tests exist for the area you touch, run them:

   ```bash
   cd collector && python -m pytest tests/ -v
   ```

## Commit message convention

```
<type>: <short description>

<optional body>

Signed-off-by: Your Name <your.email@example.com>
```

Use a clear `<type>` prefix such as `feat`, `fix`, `docs`, `refactor`,
`perf`, `test`, or `chore`. Keep the subject line concise and in the
imperative mood.

## Pull request checklist

Before opening a PR, confirm:

- [ ] Every commit has a valid `Signed-off-by` line (DCO).
- [ ] Linters / pre-commit hooks pass (see [`LINTING.md`](LINTING.md)).
- [ ] Changes are scoped and the description explains the *why*.
- [ ] Documentation is updated when behavior or configuration changes.
- [ ] Any new dashboards or schemas follow the existing structure.

## Code of conduct

Be respectful and constructive. We want GYANAM to be a welcoming project
for contributors at every level of experience.

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
