#!/bin/bash

cd "$(dirname "$0")" || exit

if [ -n "$CONDA_PREFIX" ]; then
    echo "It looks like you're in a conda environment. Skipping venv check."
else
    if [ ! -d ".venv" ]; then
        echo "Venv doesn't exist! Creating one for you."
        # python3 -m venv venv
        uv venv .venv

        if [ -f "start_options.json" ]; then
            echo "Removing old start_options.json"
            rm -rf start_options.json
        fi
    fi

    echo "Activating venv"

    # shellcheck source=/dev/null
    # source venv/bin/activate
    source .venv/bin/activate
fi

echo "Installing dependencies with uv"
uv pip install -e ".[cu121]"

python3 start_uv.py "$@"
