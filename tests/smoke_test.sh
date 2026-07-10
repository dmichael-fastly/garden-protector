#!/bin/bash
set -e

# Colors for pretty logs
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

# Ensure rustup shims take absolute precedence over standalone compilers
export PATH="$HOME/.cargo/bin:$PATH"

echo -e "${BLUE}[SMOKE TEST] 1. Ensuring wasm32-wasip1 target is installed...${NC}"
rustup target add wasm32-wasip1 2>/dev/null || true

echo -e "${BLUE}[SMOKE TEST] 2. Starting local Fastly Compute development server...${NC}"
# Start fastly compute serve in the background, targeting our backend directory and port 7878
fastly compute serve --dir=backend --addr="127.0.0.1:7878" &
FASTLY_PID=$!

# Ensure the local server is killed on exit, even if there are script errors
trap "echo -e '${BLUE}[SMOKE TEST] Stopping Fastly development server...${NC}'; kill -9 $FASTLY_PID 2>/dev/null || true" EXIT

echo -e "${BLUE}[SMOKE TEST] Waiting dynamically for development server to compile and boot...${NC}"
TIMEOUT=90
while [ $TIMEOUT -gt 0 ]; do
  # Check if server responds with 200 or 404 (any valid HTTP response indicates server is listening)
  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:7878/api/status || true)
  if [[ "$HTTP_STATUS" == "200" || "$HTTP_STATUS" == "404" ]]; then
    echo -e "${GREEN}[SMOKE TEST] Fastly local development server is online and responding!${NC}"
    break
  fi
  sleep 1
  TIMEOUT=$((TIMEOUT - 1))
done

if [ $TIMEOUT -eq 0 ]; then
  echo -e "\033[0;31m[SMOKE TEST] ERROR: Fastly server failed to start within 90 seconds.\033[0m"
  exit 1
fi

echo -e "${BLUE}[SMOKE TEST] 4. Triggering emulated Raspberry Pi Client...${NC}"
# Run our client with --trigger. It will:
# - Detect movement, load tests/fixtures/raccoon.jpg, POST to Viceroy
# - Receive "mitigate" action, start mock sprinkler/strobe
# - Heartbeat check Viceroy /api/status, receive override_stop: false (continue), then naturally finish.
# (Since mock_sleep is not active here, it will run real-time with shorter intervals)
export PYTHONPATH=.
python3 hardware/client.py --trigger

echo -e "${GREEN}[SMOKE TEST] SUCCESS! End-to-end edge-IoT communication verified flawlessly!${NC}"
