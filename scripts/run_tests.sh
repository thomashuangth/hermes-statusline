#!/usr/bin/env bash
# scripts/run_tests.sh
# Isolates HOME and HERMES_HOME before any Python process or test imports start.

set -euo pipefail

PYTHON="${PYTHON:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Create an isolated temporary test sandbox directory within TMPDIR
BASE_TMPDIR="${TMPDIR:-/tmp}"
ISOLATED_HOME="$(mktemp -d "${BASE_TMPDIR%/}/hermes-test-sandbox.XXXXXX")"
ISOLATED_HERMES_HOME="${ISOLATED_HOME}/.hermes"
mkdir -p "${ISOLATED_HERMES_HOME}/state"

# Ensure isolation is set in shell environment BEFORE Python process/imports start
export HOME="${ISOLATED_HOME}"
export HERMES_HOME="${ISOLATED_HERMES_HOME}"

cleanup() {
    rm -rf "${ISOLATED_HOME}"
}
trap cleanup EXIT INT TERM

echo "================================================================="
echo "Running hermes-statusline test suite with full HOME isolation"
echo "Python binary:        ${PYTHON}"
echo "Isolated HOME:        ${HOME}"
echo "Isolated HERMES_HOME: ${HERMES_HOME}"
echo "================================================================="

cd "${REPO_DIR}"
"${PYTHON}" -m unittest discover -s tests -p "test_*.py" -v
