#!/bin/bash

cd "$(dirname "$0")/.." || exit

if [ -n "$CONDA_PREFIX" ]; then
    echo "It looks like you're in a conda environment. Skipping venv check."
else
    # if [ ! -d "venv" ]; then
    if [ ! -d ".venv" ]; then
        echo "Venv doesn't exist! Please run start.sh instead."
        exit 0
    fi

    echo "Activating venv"

    # shellcheck source=/dev/null
    # source venv/bin/activate
    source .venv/bin/activate
fi

echo "Updating dependencies with uv"
uv pip install -e ".[cu121]" --upgrade

python3 start_uv.py --update-deps "$@"
