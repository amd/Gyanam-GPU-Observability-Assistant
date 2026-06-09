# CodeQL Analysis Report

**Last run:** 2026-05-30
**CodeQL version:** 2.25.5
**Suites:** `python-security-extended` + `python-code-quality`
**Files scanned:** 39 Python files (~14 000 LOC) under `collector/src/` and `scripts/`
**Raw SARIF:** [`../.codeql-results/results.sarif`](../.codeql-results/results.sarif)
**Config:** [`../.github/codeql/codeql-config.yml`](../.github/codeql/codeql-config.yml)

## Result

| Stage | Count |
|---|---|
| Initial findings | **39** |
| After code-level fixes | **10** |
| After path-based suppression (`paths-ignore`) | **3** |
| Accepted-risk (documented below) | **3** |
| **Actionable remaining** | **0** |

✅ Clean. All actionable findings have been addressed; the remaining
three are accepted architectural decisions documented below.

---

## Remaining findings (accepted risk)

### `py/request-without-cert-validation` × 2  (sev 7.5)

**Locations:**
- `collector/src/redfish/sse_capability_check.py:89`
- `collector/src/redfish/sse_capability_check.py:178`

**What CodeQL flags:**
> This request may run without certificate validation because it is
> disabled by this value.

**Why it's intentional:**

Gyanam's deployment context is a fleet of AMD Instinct BMCs, which ship
with **self-signed TLS certificates by default**. The codebase exposes a
**per-target `verify_ssl` field** in the `Target` model — operators can
enable certificate validation for any target that has a real CA-signed
certificate, but the default is `False` to make the out-of-the-box
experience work.

The two flagged call sites in `sse_capability_check.py` honor that
per-target flag — they pass `verify_ssl=target.verify_ssl` through. The
"finding" is essentially CodeQL noting that the architecture *allows*
certificate validation to be disabled, which is the explicit design.

**What we do instead of fixing:**
- The rule stays **enabled** at the suite level so any NEW occurrence
  (in a different file) is still surfaced for review.
- These two locations are documented here as accepted risk.
- Operators who deploy with proper CA-signed BMC certs can set
  `verify_ssl: true` per-target and the runtime path actually validates.

### `py/stack-trace-exposure` × 1  (sev 5.4)

**Location:**
- `collector/src/api/routes/targets.py:676` (bulk-create CSV import endpoint)

**What CodeQL flags:**
> Stack trace information flows to this location and may be exposed to
> an external user.

**Why it's accepted:**

The bulk-create endpoint imports targets from an operator-supplied CSV
file. Per-row failures are reported in the response so the operator
knows *which* rows failed and *why* — almost always a validation error
("invalid IP", "duplicate host", "encryption key mismatch") that the
operator needs to see in order to fix their CSV. Returning only the
exception class name (e.g. `ValidationError`) would force the operator
to dig through server logs for every failed row, which is hostile UX
for what is, by design, an admin tool.

Mitigations in place:
- Only the **first line** of the message is taken (no multi-line stack
  output).
- Non-printable characters are stripped (no log-injection / JSON-break
  surface).
- Length is capped at 200 chars.
- The full exception with traceback is logged server-side via
  `exc_info=True` for auditing.
- The endpoint is behind admin authentication (`Depends(get_current_user)`).

If you adopt a stricter posture, change the error message construction
in `targets.py` to `f"{type(e).__name__} (see server logs)"` and update
the docs.

---


## Re-running

### Manual (local) re-run

After installing the CodeQL bundle once to `~/.codeql/codeql/` (see
[`scripts/run-codeql.sh`](../scripts/run-codeql.sh)):

```bash
./scripts/run-codeql.sh
```

This rebuilds the Python database, runs both suites, applies the
path-based filter, writes the SARIF to `.codeql-results/results.sarif`,
and prints a per-rule summary.

### CI re-run

Add `.github/workflows/codeql.yml` using `github/codeql-action/init`
and `analyze` with `config-file: .github/codeql/codeql-config.yml`.
Findings will appear in the repo's Security tab automatically.

### Updating this report

1. Re-run the analysis.
2. If the **Actionable remaining** count is anything other than 0, fix
   the new findings.
3. If a new genuinely-accepted-risk finding appears, add it to the
   "Remaining findings" section above with the same structure (location,
   what CodeQL flags, why it's intentional, what we do instead).
4. Update the "Last run" date at the top.

---

## What this report does NOT cover

CodeQL's Python analyzer is strong but not exhaustive. The following
categories of issues are out of scope for this report and should be
chased with the appropriate tool:

- **Dependency vulnerabilities** → `pip-audit`, GitHub Dependabot
- **Container image CVEs** → `trivy`, GitHub Container Scanning
- **Secrets in git history** → `gitleaks`, `truffleHog`
- **Runtime behaviour issues** (DoS, resource exhaustion, deadlocks) →
  the load-test results in `docs/SCALABILITY.md`
- **Configuration security** (TLS posture of the deployed services,
  network segmentation) → manual review against `docs/DEPLOYMENT.md`
- **Shell scripts** (`gyanam.sh` and friends) — CodeQL Python doesn't
  cover these. Use `shellcheck` (already in CI via `setup-linters.sh`).
