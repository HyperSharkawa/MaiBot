#!/bin/bash

# ==============================================
# Environment Initialization
# ==============================================

# Step 1: Locate project root directory
SCRIPTS_DIR="scripts"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
source "$SCRIPT_DIR/python_runtime.sh"
require_uv || exit 1

# Step 2: Verify scripts directory exists
if [ ! -d "$PROJECT_ROOT/$SCRIPTS_DIR" ]; then
    echo "❌ Error: scripts directory not found in project root" >&2
    echo "Current path: $PROJECT_ROOT" >&2
    exit 1
fi

# Step 3: Set up Python environment
export PYTHONPATH="$PROJECT_ROOT:$PYTHONPATH"
cd "$PROJECT_ROOT" || {
    echo "❌ Failed to cd to project root: $PROJECT_ROOT" >&2
    exit 1
}

# Debug info
echo "============================"
echo "Project Root: $PROJECT_ROOT"
echo "Python Path: $PYTHONPATH"
echo "Working Dir: $(pwd)"
echo "============================"

# ==============================================
# Python Script Execution
# ==============================================

run_python_script() {
    local script_name=$1
    echo "🔄 Running $script_name"
    if ! uv run --locked --no-dev --extra lpmm --python 3.15.2 python "$SCRIPTS_DIR/$script_name"; then
        echo "❌ $script_name failed" >&2
        exit 1
    fi
}

# Execute scripts in order
run_python_script "raw_data_preprocessor.py"
run_python_script "info_extraction.py"
run_python_script "import_openie.py"

echo "✅ All scripts completed successfully"
