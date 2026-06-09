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
# Quick linting check without full setup

echo "=== Linting Check Summary ==="
echo ""
echo "To install linters, first run:"
echo "  sudo apt install python3-venv"
echo "  ./setup-linters.sh"
echo ""
echo "For now, here's what we can check without installation:"
echo ""

# Check Python syntax
echo "1. Python Syntax Check:"
find collector -name "*.py" -exec python3 -m py_compile {} + 2>&1 | head -20
if [ $? -eq 0 ]; then
    echo "   ✓ No syntax errors found"
else
    echo "   ✗ Syntax errors detected"
fi
echo ""

# Check for common issues
echo "2. Common Issues:"
echo "   - Trailing whitespace:"
git ls-files | grep -E '\.(py|sh|yaml|yml)$' | xargs grep -l ' $' | head -5
echo ""

echo "   - Long lines (>100 chars) in Python:"
find collector -name "*.py" -exec awk 'length>100 {print FILENAME":"NR; exit}' {} + | head -5
echo ""

echo "3. Shell Scripts:"
if command -v shellcheck &> /dev/null; then
    echo "   Running shellcheck on *.sh files..."
    shellcheck *.sh 2>&1 | head -20
else
    echo "   shellcheck not installed (will be installed via pre-commit)"
fi
echo ""

echo "=== Next Steps ==="
echo "1. Install python3-venv: sudo apt install python3-venv"
echo "2. Run setup: ./setup-linters.sh"
echo "3. The setup will show all linting issues and auto-fix most"
