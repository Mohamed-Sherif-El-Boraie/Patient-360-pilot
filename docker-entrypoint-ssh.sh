#!/bin/sh
# Troubleshooting entrypoint (debug image only): make the container's settings visible to SSH sessions, start the SSH
# server, then run the normal entrypoint. SSH logins do not inherit the container environment, so it is copied into
# /etc/profile; the same file is readable by anyone who can open the SSH session. Never used in production.
set -eu

export -p >> /etc/profile
/usr/sbin/sshd

exec sh /app/docker-entrypoint.sh "$@"
