#!/bin/bash
# Verifier entrypoint. All logic lives in verify.py, which runs with the reference
# interpreter and never imports code from /app.
set -u

mkdir -p /logs/verifier
chmod 700 /logs /logs/verifier
rm -f /logs/verifier/reward.txt
echo 0 > /logs/verifier/reward.txt

# The harness copies tests/ in again at run time; keep it unreadable for unprivileged users.
chmod -R go-rwx /tests /opt/ref

cd /tests
/opt/ref/bin/python -I -B /tests/verify.py 2>&1 | tee /logs/verifier/verify.log
exit 0
