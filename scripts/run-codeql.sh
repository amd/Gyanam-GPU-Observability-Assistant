#!/usr/bin/env bash
# Reproducible local CodeQL run for the gyanam Python codebase.
#
# Usage:    ./scripts/run-codeql.sh
# Output:   .codeql-results/results.sarif  (filtered, canonical)
# Report:   docs/CODEQL_REPORT.md          (human-readable, hand-curated)
#
# Prerequisites (first run only):
#   1. Download the CodeQL bundle:
#      mkdir -p ~/.codeql && cd ~/.codeql && \
#        curl -L -o bundle.tar.zst \
#          https://github.com/github/codeql-action/releases/latest/download/codeql-bundle-linux64.tar.zst && \
#        tar --use-compress-program=unzstd -xf bundle.tar.zst
#   2. The codeql binary will then be at ~/.codeql/codeql/codeql

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CODEQL_DIR="${CODEQL_DIR:-${HOME}/.codeql/codeql}"
DB_DIR="${DB_DIR:-/tmp/codeql-db}"
RESULTS_DIR="${REPO_ROOT}/.codeql-results"
RAW_SARIF="${RESULTS_DIR}/results-raw.sarif"
SARIF="${RESULTS_DIR}/results.sarif"

if [[ ! -x "${CODEQL_DIR}/codeql" ]]; then
    echo "ERROR: CodeQL CLI not found at ${CODEQL_DIR}/codeql" >&2
    echo "See script header for install instructions." >&2
    exit 1
fi

export PATH="${CODEQL_DIR}:${PATH}"
mkdir -p "${RESULTS_DIR}"

echo "==> Building Python database at ${DB_DIR}..."
rm -rf "${DB_DIR}"
codeql database create "${DB_DIR}" \
    --language=python \
    --source-root="${REPO_ROOT}" \
    --overwrite \
    >/dev/null

echo "==> Running security-extended + code-quality suites..."
codeql database analyze "${DB_DIR}" \
    codeql/python-queries:codeql-suites/python-security-extended.qls \
    codeql/python-queries:codeql-suites/python-code-quality.qls \
    --format=sarif-latest \
    --output="${RAW_SARIF}" \
    --threads=0 \
    >/dev/null

echo "==> Applying path-based filter from .github/codeql/codeql-config.yml..."
python3 - "${RAW_SARIF}" "${SARIF}" << 'PYEOF'
import json, fnmatch, sys
# Mirror the paths-ignore list from .github/codeql/codeql-config.yml.
EXCLUDE = [
    "scripts/test_bmc_alerts.py",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/build/**",
    "**/dist/**",
    ".codeql-results/**",
]
src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    s = json.load(f)
run = s["runs"][0]
original = run.get("results", [])
kept = []
for r in original:
    keep = True
    for loc in r.get("locations", []):
        uri = loc["physicalLocation"]["artifactLocation"]["uri"]
        for pat in EXCLUDE:
            if fnmatch.fnmatch(uri, pat) or uri == pat:
                keep = False
                break
        if not keep:
            break
    if keep:
        kept.append(r)
run["results"] = kept
with open(dst, "w") as f:
    json.dump(s, f, indent=2)
print(f"   raw findings: {len(original)}")
print(f"   after filter: {len(kept)}")

# Print per-rule summary
rules = {r["id"]: r for r in run["tool"]["driver"].get("rules", [])}
by_rule = {}
for r in kept:
    by_rule.setdefault(r["ruleId"], []).append(r)
def sev(rid):
    try:
        return float(rules.get(rid, {}).get("properties", {}).get("security-severity", "0"))
    except Exception:
        return 0.0
if not kept:
    print("\n✅ No remaining findings.")
else:
    print()
    for rid in sorted(by_rule, key=lambda r: -sev(r)):
        s_v = rules.get(rid, {}).get("properties", {}).get("security-severity", "-")
        items = by_rule[rid]
        print(f"   sev={s_v:<5} n={len(items):>2}  {rid}")
        for it in items:
            for loc in it.get("locations", []):
                p = loc["physicalLocation"]
                print(f"                  {p['artifactLocation']['uri']}:{p['region']['startLine']}")
PYEOF

rm -f "${RAW_SARIF}"
echo "==> Done. Canonical SARIF: ${SARIF}"
echo "==> See docs/CODEQL_REPORT.md for accepted-risk context."
