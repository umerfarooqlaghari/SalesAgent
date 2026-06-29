#!/bin/bash
# Start script to run both backend and frontend development servers.

# Get absolute path of project root
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🚀 Starting B2B Sales SDR Agent Platform..."

# Function to kill child processes on exit
cleanup() {
    echo -e "\n🛑 Shutting down backend and frontend..."
    kill "$BACKEND_PID" 2>/dev/null
    kill "$FRONTEND_PID" 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

# 1. Start Backend FastAPI
echo "📡 Starting backend server..."
cd "$ROOT_DIR"
"$ROOT_DIR/.venv/bin/python" -m uvicorn backend.main:app --host 0.0.0.0 --port 8765 --reload &
BACKEND_PID=$!

# 2. Start Frontend Next.js
echo "💻 Starting frontend server..."
cd "$ROOT_DIR/frontend"
npm run dev &
FRONTEND_PID=$!

# Wait for both processes
wait "$BACKEND_PID" "$FRONTEND_PID"
