#!/bin/bash
# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# setup-linters.sh - Install and configure linting tools
#
# This script sets up pre-commit hooks and development tools for code quality.
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_info() {
    echo -e "${BLUE}[INFO] $1${NC}"
}

print_success() {
    echo -e "${GREEN}[OK] $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}[WARN] $1${NC}"
}

print_error() {
    echo -e "${RED}[ERROR] $1${NC}"
}

# Check if Python 3 is available
check_python() {
    if ! command -v python3 &> /dev/null; then
        print_error "Python 3 is not installed or not in PATH"
        exit 1
    fi

    local python_version
    python_version=$(python3 --version | cut -d' ' -f2)
    print_info "Found Python ${python_version}"
}

# Check if we're in a git repository
check_git() {
    if ! git rev-parse --git-dir &> /dev/null; then
        print_error "Not in a git repository"
        exit 1
    fi
    print_success "Git repository detected"
}

# Create virtual environment if needed
setup_venv() {
    local venv_dir=".venv-linters"

    # Check if venv exists and is valid
    if [[ -d "${venv_dir}" ]] && [[ ! -f "${venv_dir}/bin/activate" ]]; then
        print_warning "Incomplete virtual environment found, recreating..."
        rm -rf "${venv_dir}"
    fi

    if [[ ! -d "${venv_dir}" ]]; then
        print_info "Creating virtual environment..."
        if ! python3 -m venv "${venv_dir}"; then
            print_error "Failed to create virtual environment"
            print_error "Make sure python3-venv is installed: sudo apt install python3-venv"
            exit 1
        fi
        print_success "Virtual environment created at ${venv_dir}"
    else
        print_info "Using existing virtual environment"
    fi

    # Activate virtual environment
    if [[ ! -f "${venv_dir}/bin/activate" ]]; then
        print_error "Virtual environment activation script not found"
        exit 1
    fi
    source "${venv_dir}/bin/activate"
}

# Install development dependencies
install_deps() {
    print_info "Installing development dependencies..."

    # Install requirements in virtual environment
    pip install --upgrade pip
    pip install -r requirements-dev.txt

    print_success "Development dependencies installed"
}

# Install pre-commit hooks
setup_precommit() {
    print_info "Setting up pre-commit hooks..."

    # Install pre-commit hooks
    pre-commit install

    print_success "Pre-commit hooks installed"
    print_info "Hooks will run automatically on git commit"
}

# Run initial check (optional)
run_check() {
    print_info "Running initial pre-commit check on all files..."
    print_warning "This may take a while and show many issues on first run"
    echo ""

    if pre-commit run --all-files; then
        print_success "All checks passed!"
    else
        print_warning "Some checks failed. This is normal for the first run."
        echo ""
        print_info "Pre-commit auto-fixed many issues. Files have been modified."
        print_info "Review the changes and commit them."
    fi
}

# Main setup
main() {
    echo "=== Gyanam Linter Setup ==="
    echo ""

    check_python
    check_git
    echo ""

    setup_venv
    echo ""

    install_deps
    echo ""

    setup_precommit
    echo ""

    # Ask if user wants to run initial check
    read -p "Run initial lint check on all files? (y/N): " run_initial
    if [[ "${run_initial}" == "y" || "${run_initial}" == "Y" ]]; then
        echo ""
        run_check
    else
        print_info "Skipping initial check"
        print_info "Run 'pre-commit run --all-files' manually when ready"
    fi

    echo ""
    print_success "Linter setup complete!"
    echo ""
    print_info "Usage:"
    echo "  - Hooks run automatically on git commit"
    echo "  - Run manually: source .venv-linters/bin/activate && pre-commit run --all-files"
    echo "  - Run on specific file: pre-commit run --files <file>"
    echo "  - Skip hooks once: git commit --no-verify"
    echo ""
    print_info "Note: Pre-commit hooks use the virtual environment automatically"
    echo ""
}

main "$@"
