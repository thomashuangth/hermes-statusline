#!/usr/bin/env bash
# scripts/run_tests.sh
# Isolates HOME and HERMES_HOME before any Python process or test imports start.
# Runs unit tests and real-core integration tests per core in dedicated subprocesses.

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
export HERMES_DISABLE_LAZY_INSTALLS="1"

cleanup() {
    rm -rf "${ISOLATED_HOME}"
}
trap cleanup EXIT INT TERM

echo "================================================================="
echo "Running hermes-statusline test suite with full HOME isolation"
echo "Python binary:        ${PYTHON}"
echo "Isolated HOME:        ${HOME}"
echo "Isolated HERMES_HOME: ${HERMES_HOME}"
echo "HERMES_DISABLE_LAZY_INSTALLS: 1"
echo "================================================================="

cd "${REPO_DIR}"

echo ""
echo "=== Phase 1: Unit Test Discovery ==="
"${PYTHON}" -m unittest discover -s tests -p "test_*.py" -v

echo ""
echo "=== Phase 2: Integration Tests Against Hermes Core Archives ==="

run_core_integration() {
    local core_path="$1"
    local core_name="$2"
    echo ""
    echo "--- Running Integration Tests for ${core_name} (${core_path}) ---"
    (
        export HERMES_TEST_CORE="${core_path}"
        "${PYTHON}" -m unittest discover -s tests_integration -p "test_*.py" -v
    )
}

if [ -n "${HERMES_TEST_CORE:-}" ]; then
    run_core_integration "${HERMES_TEST_CORE}" "Explicit HERMES_TEST_CORE"
else
    # Auto-detect sibling pinned core archives if present
    SIBLING_DIR="$(cd "${REPO_DIR}/.." && pwd)"
    if [ -d "${SIBLING_DIR}/core_4f9b741a" ]; then
        run_core_integration "${SIBLING_DIR}/core_4f9b741a" "Raspberry Pi Core (4f9b741a)"
    fi
    if [ -d "${SIBLING_DIR}/core_bf0203a7" ]; then
        run_core_integration "${SIBLING_DIR}/core_bf0203a7" "Modern Core (bf0203a7)"
    fi
    if [ ! -d "${SIBLING_DIR}/core_4f9b741a" ] && [ ! -d "${SIBLING_DIR}/core_bf0203a7" ]; then
        echo "No pinned core archive found and HERMES_TEST_CORE unset: real-core integration SKIPPED."
        echo "This is not integration coverage. Provide a core checkout or use the CI integration job."
    fi
fi

echo ""
echo "================================================================="
echo "All hermes-statusline tests passed successfully!"
echo "================================================================="
