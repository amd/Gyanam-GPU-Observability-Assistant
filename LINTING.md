# Linting and Code Quality

This project uses pre-commit hooks to enforce code quality standards before commits.

## Prerequisites

Install Python venv package (one-time setup):

```bash
sudo apt install python3-venv
```

## Quick Setup

Run the automated setup script:

```bash
./setup-linters.sh
```

This will:
1. Create a virtual environment (`.venv-linters`)
2. Install all development dependencies
3. Set up pre-commit hooks
4. Optionally run an initial check on all files

## Manual Setup

If you prefer manual setup:

```bash
# Create virtual environment
python3 -m venv .venv-linters

# Activate virtual environment
source .venv-linters/bin/activate

# Install dependencies
pip install -r requirements-dev.txt

# Install pre-commit hooks
pre-commit install

# Run initial check (optional)
pre-commit run --all-files
```

## What Gets Checked

### Python Files
- **ruff**: Linting and formatting (replaces black, isort, flake8)
- **mypy**: Static type checking

### Shell Scripts
- **shellcheck**: Shell script analysis

### YAML Files
- **yamllint**: YAML linting and formatting

### General
- Trailing whitespace
- End of file fixes
- JSON syntax validation
- Large file detection
- Merge conflict detection

## Usage

### Automatic (Recommended)

Hooks run automatically on `git commit`. If any check fails:
- Auto-fixable issues are corrected automatically
- Review the changes with `git diff`
- Stage the fixes with `git add`
- Commit again

### Manual

Run checks manually without committing:

```bash
# Check all files
source .venv-linters/bin/activate
pre-commit run --all-files

# Check specific files
pre-commit run --files collector/src/api/app.py

# Check only staged files
pre-commit run
```

### Skip Hooks (Use Sparingly)

In rare cases where you need to bypass hooks:

```bash
git commit --no-verify
```

## Configuration Files

- `.pre-commit-config.yaml` - Pre-commit hooks configuration
- `pyproject.toml` - Python linting rules (ruff, mypy)
- `.yamllint.yaml` - YAML linting rules
- `.shellcheckrc` - Shellcheck configuration
- `requirements-dev.txt` - Development dependencies

## Excluded Directories

The following are excluded from linting:
- `grafana/` - Generated Grafana dashboards
- `.git/`, `.venv*/` - System directories
- `__pycache__/`, `*.egg-info/` - Build artifacts
