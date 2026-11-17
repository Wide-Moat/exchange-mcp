#!/usr/bin/env bash
# Run the server on this machine against a real Exchange, for the live check (docs/live-findings.md).
#
#   EXCHANGE_MCP_EWS_URL=https://<exchange host>/EWS/Exchange.asmx \
#   EXCHANGE_MCP_LOGIN_FORMAT=upn EXCHANGE_MCP_LOGIN_DOMAIN=<upn suffix> \
#   [EXCHANGE_MCP_CA_BUNDLE=/abs/path/internal-ca.pem] \
#   scripts/run-local.sh
#
# The key lives in memory only (one process, everyone signs in again after a restart). The person types
# their password on the sign-in page in their browser; it never passes through this script or a shell.
set -euo pipefail
: "${EXCHANGE_MCP_EWS_URL:?set the EWS URL}"
: "${EXCHANGE_MCP_LOGIN_DOMAIN:?set the login domain}"
export EXCHANGE_MCP_PUBLIC_URL="${EXCHANGE_MCP_PUBLIC_URL:-http://localhost:8765}"
export EXCHANGE_MCP_AUTH_MODE=password
export EXCHANGE_MCP_KEY_PROVIDER=memory
export PORT="${PORT:-8765}"
cd "$(dirname "$0")/.."
echo "MCP endpoint: ${EXCHANGE_MCP_PUBLIC_URL}/mcp"
echo "Add it to a client, e.g.: claude mcp add --transport http exchange ${EXCHANGE_MCP_PUBLIC_URL}/mcp"
exec uv run python -m exchange_mcp.app
