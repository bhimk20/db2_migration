#!/bin/bash
# Run the DB2 Migration app from source.
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$DIR"

if [ ! -x ".venv/bin/streamlit" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi

echo "Starting DB2 Migration UI..."
.venv/bin/streamlit run app.py
