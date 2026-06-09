# Security Policy

GYANAM collects out-of-band telemetry from GPU server BMCs and handles
sensitive material such as BMC credentials. We take security reports
seriously and appreciate responsible disclosure.

## Reporting a vulnerability

**Do not report security vulnerabilities through public GitHub issues,
pull requests, or discussions.**

Instead, report them privately using GitHub's private vulnerability
reporting:

➡️ **[Open a private security advisory](https://github.com/amd/Gyanam-GPU-Observability-Assistant/security/advisories/new)**

(From the repository, go to the **Security** tab → **Report a
vulnerability**.)

Please include as much of the following as you can:

- A description of the vulnerability and its impact.
- Steps to reproduce or a proof of concept.
- Affected version or commit (`git rev-parse --short HEAD`).
- Any suggested remediation.

Do **not** include live credentials, tokens, or real BMC hostnames in your
report — redact or use placeholders.

## What to expect

- We will acknowledge your report and begin investigating.
- We will keep you informed of progress toward a fix.
- We will credit you in the disclosure unless you prefer to remain
  anonymous.

## Scope

Security-relevant areas include, but are not limited to: credential
storage and handling, authentication/authorization, SSRF/CSRF protections,
path traversal, and the export and log-collection pipelines. For the
current security posture and accepted-risk audit, see
[`docs/CODEQL_REPORT.md`](docs/CODEQL_REPORT.md).
