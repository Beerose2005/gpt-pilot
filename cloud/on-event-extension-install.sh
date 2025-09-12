#!/bin/bash

set -e

VSCODE_SERVER_PORT=8080

# Create workspace directory and settings
mkdir -p /pythagora/pythagora-core/workspace/.vscode
printf '{\n  "gptPilot.isRemoteWs": true,\n  "gptPilot.useRemoteWs": false,\n  "workbench.colorTheme": "Default Dark+",\n  "remote.autoForwardPorts": false\n}' > /pythagora/pythagora-core/workspace/.vscode/settings.json

# Start code-server and direct to our workspace
echo "Starting code-server..."
code-server --disable-proxy --disable-workspace-trust --config /etc/code-server/config.yaml /pythagora/pythagora-core/workspace &
CODE_SERVER_PID=$!
echo $CODE_SERVER_PID > /tmp/vscode-http-server.pid

# Wait for code-server to open the port (e.g., 8080)
for ((i=0; i<15*2; i++)); do
  if curl -s "http://localhost:$VSCODE_SERVER_PORT/healthz" > /dev/null; then
    echo "TASK: VS Code server started"
    echo "VS Code HTTP server started with PID $CODE_SERVER_PID. Access at http://localhost:$VSCODE_SERVER_PORT"
    break
  fi
  sleep 0.5
done
