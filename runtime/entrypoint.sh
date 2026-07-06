#!/bin/sh
set -e

# Managed session storage (/mnt/agent-state) only mounts at invocation time,
# NOT at container init. Do not touch it here — server.js prepares it lazily
# on the first invoke.

exec node /app/server.js
