#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Install dependencies if needed
pip3 install -q -r requirements.txt

# Copy .env.example → .env if .env doesn't exist
if [ ! -f .env ]; then
  cp .env.example .env
  echo "⚠️  Created .env from .env.example — fill in your API keys before using Meta OAuth."
fi

# Init DB and start
python3 -c "from app import app, db; app.app_context().__enter__(); db.create_all()"
echo "✓ Database ready"
echo ""
echo "🚀 MetaInsights running at http://localhost:5000"
echo ""
python3 app.py
