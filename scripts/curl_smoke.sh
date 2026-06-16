#!/bin/sh
set -eu
BASE="${BASE_URL:-http://api:8000}"
PASS=0
FAIL=0

check() {
  name="$1"
  cond="$2"
  if [ "$cond" = "1" ]; then
    echo "PASS - $name"
    PASS=$((PASS + 1))
  else
    echo "FAIL - $name"
    FAIL=$((FAIL + 1))
  fi
}

code() { curl -s -o /tmp/body -w '%{http_code}' "$@"; }
body() { cat /tmp/body; }
jget() { body | grep -o "\"$1\":[^,}]*" | head -1 | sed 's/.*://; s/"//g; s/ //g'; }

# Health
c=$(code "$BASE/health"); check "GET /health" "$([ "$c" = "200" ] && echo 1 || echo 0)"

# Register user
c=$(code -X POST "$BASE/auth/register" -H 'Content-Type: application/json' \
  -d '{"email":"curl-user@example.com","password":"password123"}')
check "POST /auth/register user" "$([ "$c" = "201" ] && echo 1 || echo 0)"
[ "$c" != "201" ] && echo "  body: $(body)"

# Block admin without token
c=$(code -X POST "$BASE/auth/register" -H 'Content-Type: application/json' \
  -d '{"email":"curl-bad-admin@example.com","password":"password123","role":"admin"}')
check "POST /auth/register admin blocked" "$([ "$c" = "403" ] && echo 1 || echo 0)"

# Register admin
c=$(code -X POST "$BASE/auth/register" -H 'Content-Type: application/json' \
  -H 'X-Admin-Setup-Token: local-admin-setup-token' \
  -d '{"email":"curl-admin@example.com","password":"password123","role":"admin"}')
check "POST /auth/register admin" "$([ "$c" = "201" ] && echo 1 || echo 0)"
[ "$c" != "201" ] && echo "  body: $(body)"

# Login user
c=$(code -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d '{"email":"curl-user@example.com","password":"password123"}')
USER_TOKEN=$(body | grep -o '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)
check "POST /auth/login user" "$([ "$c" = "200" ] && [ -n "$USER_TOKEN" ] && echo 1 || echo 0)"
[ "$c" != "200" ] && echo "  body: $(body)"

# Login admin
c=$(code -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d '{"email":"curl-admin@example.com","password":"password123"}')
ADMIN_TOKEN=$(body | grep -o '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)
check "POST /auth/login admin" "$([ "$c" = "200" ] && [ -n "$ADMIN_TOKEN" ] && echo 1 || echo 0)"

AUTH="Authorization: Bearer $USER_TOKEN"

# Create job
c=$(code -X POST "$BASE/jobs" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"payload":{"task_type":"demo","duration_seconds":3,"should_fail":false}}')
JOB_ID=$(jget id)
check "POST /jobs create" "$([ "$c" = "201" ] && [ -n "$JOB_ID" ] && echo 1 || echo 0)"
[ "$c" != "201" ] && echo "  body: $(body)"

# Idempotency
code -X POST "$BASE/jobs" -H "$AUTH" -H 'Content-Type: application/json' -H 'Idempotency-Key: curl-key-1' \
  -d '{"payload":{"task_type":"demo","duration_seconds":1}}' >/dev/null
ID1=$(jget id)
code -X POST "$BASE/jobs" -H "$AUTH" -H 'Content-Type: application/json' -H 'Idempotency-Key: curl-key-1' \
  -d '{"payload":{"task_type":"demo","duration_seconds":1}}' >/dev/null
ID2=$(jget id)
check "POST /jobs idempotency" "$([ "$ID1" = "$ID2" ] && echo 1 || echo 0)"

# List jobs
c=$(code -H "$AUTH" "$BASE/jobs?limit=10")
check "GET /jobs list" "$([ "$c" = "200" ] && echo 1 || echo 0)"
[ "$c" != "200" ] && echo "  body: $(body)"

# Get job
c=$(code -H "$AUTH" "$BASE/jobs/$JOB_ID")
check "GET /jobs/{id}" "$([ "$c" = "200" ] && echo 1 || echo 0)"

# Logs
c=$(code -H "$AUTH" "$BASE/jobs/$JOB_ID/logs")
check "GET /jobs/{id}/logs" "$([ "$c" = "200" ] && echo 1 || echo 0)"

# Register user2 + authz
curl -s -X POST "$BASE/auth/register" -H 'Content-Type: application/json' \
  -d '{"email":"curl-user2@example.com","password":"password123"}' >/dev/null
code -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d '{"email":"curl-user2@example.com","password":"password123"}' >/dev/null
USER2_TOKEN=$(body | grep -o '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)
c=$(code -H "Authorization: Bearer $USER2_TOKEN" "$BASE/jobs/$JOB_ID")
check "GET /jobs/{id} forbidden" "$([ "$c" = "403" ] && echo 1 || echo 0)"

# Admin access
c=$(code -H "Authorization: Bearer $ADMIN_TOKEN" "$BASE/jobs/$JOB_ID")
check "GET /jobs/{id} admin access" "$([ "$c" = "200" ] && echo 1 || echo 0)"

# Cancel
code -X POST "$BASE/jobs" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"payload":{"task_type":"demo","duration_seconds":30}}' >/dev/null
CANCEL_ID=$(jget id)
c=$(code -X POST -H "$AUTH" "$BASE/jobs/$CANCEL_ID/cancel")
STATUS=$(jget status)
check "POST /jobs/{id}/cancel" "$([ "$c" = "200" ] && [ "$STATUS" = "cancelled" ] && echo 1 || echo 0)"

# SSE
SSE=$(curl -s -N -m 3 -H "$AUTH" "$BASE/jobs/$JOB_ID/events" 2>/dev/null || true)
case "$SSE" in *snapshot*|*poll*|*data:*) ok=1;; *) ok=0;; esac
check "GET /jobs/{id}/events SSE" "$ok"

# Job completion (before rate-limit spam)
code -X POST "$BASE/jobs" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"payload":{"task_type":"demo","duration_seconds":2,"should_fail":false}}' >/dev/null
RUN_ID=$(jget id)
COMPLETED=0
i=0
while [ "$i" -lt 20 ]; do
  sleep 2
  code -H "$AUTH" "$BASE/jobs/$RUN_ID" >/dev/null
  STATUS=$(jget status)
  if [ "$STATUS" = "completed" ]; then COMPLETED=1; break; fi
  i=$((i + 1))
done
check "Job completes via worker" "$COMPLETED"
[ "$COMPLETED" = "0" ] && echo "  last status: $STATUS body: $(body)"

# Rate limit
GOT429=0
i=1
while [ "$i" -le 12 ]; do
  c=$(code -X POST "$BASE/jobs" -H "$AUTH" -H 'Content-Type: application/json' \
    -d '{"payload":{"task_type":"demo","duration_seconds":1}}')
  if [ "$c" = "429" ]; then GOT429=1; break; fi
  i=$((i + 1))
done
check "POST /jobs rate limit 429" "$GOT429"

echo ""
echo "=== SUMMARY: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
