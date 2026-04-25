#!/usr/bin/env bash
# x-sensai setup wizard.
#
# Slice 0: stub. Slice 6 fills out the full 11-step wizard from the spec:
#   1. Preflight  (Python, QMD, gh, security CLI, git remote)
#   2. Install dependencies (hash-locked)
#   3. Register an X dev app
#   4. Buy X API credits
#   5. OAuth 2.0 PKCE flow
#   6. LLM + transcription API keys
#   7. Register MCP server with Claude Desktop
#   8. GitHub Actions secrets
#   9. v1 -> v2 migration (--dry-run then --apply)
#  10. First /xsync
#  11. Schedule the cron

set -euo pipefail

cat <<'EOF'
x-sensai setup wizard - Slice 0 stub.

The full 11-step wizard ships in Slice 6. Until then, do this manually:

  1. brew install uv               # if not already installed
  2. uv venv                        # creates .venv/
  3. uv pip install -r requirements.txt
  4. uv pip install -e .
  5. pytest                         # confirm tests pass
  6. Add to ~/Library/Application Support/Claude/claude_desktop_config.json:
       {
         "mcpServers": {
           "xsensai": {
             "command": "$(pwd)/.venv/bin/xsensai-mcp"
           }
         }
       }
  7. Restart Claude Desktop. Try "use the xsensai server's ping tool with echo=hello".

EOF
