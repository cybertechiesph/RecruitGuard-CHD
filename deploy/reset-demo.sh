#!/usr/bin/env bash
# Wipes the DEMO server's database and rebuilds it with the exhibit story, dated from today.
# Run as root from the droplet console:  demo-reset
#
# THIS DELETES EVERYTHING IN THE DATABASE. Demo servers only; never use it on real records.
# Staff logins and passwords, secrets, HTTPS and the email settings stay as they are.
# It does not download new code. To update the code, run setup-demo-server.sh instead.
set -euo pipefail

APP_USER=portal
APP_DIR=/opt/recruitment-portal
DB_NAME=recruitment_portal
SERVICE=recruitment-portal
CONF=/root/portal-setup.conf
cd /tmp

manage() { (cd "$APP_DIR" && runuser -u "$APP_USER" -- .venv/bin/python manage.py "$@"); }

if [ "$(id -u)" -ne 0 ] || [ ! -f "$CONF" ]; then
  echo "Run this as root on a server built by setup-demo-server.sh." >&2
  exit 1
fi
# shellcheck disable=SC1090
. "$CONF"

echo "This deletes every record on $DOMAIN and loads fresh demo data."
echo "The site is down for a minute or two while it runs."
read -rp "Type RESET to continue: " ANSWER
[ "$ANSWER" = "RESET" ] || { echo "Nothing was changed."; exit 1; }

# DUMP_PY_START
DUMP_PY='
import json
from django.contrib.auth import get_user_model
names = ["secretariat", "hrm_chief", "hrmpsb_member", "appointing_authority", "system_admin"]
rows = get_user_model().objects.filter(username__in=names).values("username", "role", "email", "password")
print("STAFF_JSON " + json.dumps(list(rows)))
'
# DUMP_PY_END
# RESTORE_PY_START
RESTORE_PY='
import json, os
from django.contrib.auth import get_user_model
User = get_user_model()
for row in json.loads(os.environ["STAFF_JSON"]):
    user, _ = User.objects.get_or_create(username=row["username"])
    user.role, user.email, user.password, user.is_active = row["role"], row["email"], row["password"], True
    user.save()
    print("Ready:", user.username)
'
# RESTORE_PY_END

echo "=== Saving the staff logins ==="
STAFF_JSON=$(manage shell -c "$DUMP_PY" | sed -n 's/^STAFF_JSON //p')
COUNT=$(printf '%s' "$STAFF_JSON" | python3 -c 'import json, sys; print(len(json.load(sys.stdin)))')
[ "$COUNT" -eq 5 ] || { echo "Expected 5 staff accounts, found $COUNT. Nothing was deleted." >&2; exit 1; }

echo "=== Rebuilding the database ==="
systemctl stop "$SERVICE"
trap 'systemctl start "$SERVICE"' EXIT
runuser -u postgres -- dropdb --force --if-exists "$DB_NAME"
runuser -u postgres -- createdb -O "$APP_USER" "$DB_NAME"
manage migrate --noinput | tail -1

echo "=== Staff accounts ==="
(cd "$APP_DIR" && runuser -u "$APP_USER" -- env STAFF_JSON="$STAFF_JSON" \
  .venv/bin/python manage.py shell -c "$RESTORE_PY") | grep '^Ready:'
unset STAFF_JSON

echo "=== Demo data ==="
manage seed_exhibit_demo --force | sed -n 1p

echo "=== Starting the site ==="
trap - EXIT
systemctl start "$SERVICE"
sleep 3
if curl -fsS -o /dev/null -m 20 "https://$DOMAIN/apply/"; then
  echo "OK: https://$DOMAIN/apply/ opens."
else
  echo "PROBLEM: the site did not open. Run: journalctl -u $SERVICE -n 50 --no-pager"
fi
echo "=== Done. Staff passwords are unchanged. ==="
