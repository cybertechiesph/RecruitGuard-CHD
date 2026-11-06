#!/usr/bin/env bash
# Wipes the DEMO server's database and rebuilds it with the exhibit story.
# Run as root from the droplet console, after setup-demo-server.sh has finished:
#   bash /opt/recruitment-portal/deploy/reset-demo.sh
#
# THIS DELETES EVERYTHING IN THE DATABASE. Demo servers only; never use it on real records.
# Secrets, HTTPS and the email settings stay as they are.
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
read -rp "Type RESET to continue: " ANSWER
[ "$ANSWER" = "RESET" ] || { echo "Nothing was changed."; exit 1; }
read -rsp "Shared password for all staff accounts (hidden): " STAFF_PASSWORD
echo
[ -n "$STAFF_PASSWORD" ] || { echo "The password cannot be empty."; exit 1; }

echo "=== Getting the latest code ==="
runuser -u "$APP_USER" -- git -C "$APP_DIR" pull --ff-only
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "=== Rebuilding the database ==="
systemctl stop "$SERVICE"
runuser -u postgres -- dropdb --force --if-exists "$DB_NAME"
runuser -u postgres -- createdb -O "$APP_USER" "$DB_NAME"
manage migrate --noinput
manage collectstatic --noinput

echo "=== Staff accounts ==="
STAFF_PY='
import os
from django.contrib.auth import get_user_model
User = get_user_model()
rows = [
    ("secretariat", "STAFF_SECRETARIAT"),
    ("hrm_chief", "STAFF_CHIEF"),
    ("hrmpsb_member", "STAFF_PANEL"),
    ("appointing_authority", "STAFF_AA"),
    ("system_admin", "STAFF_ADMIN"),
]
for role, key in rows:
    user, _ = User.objects.get_or_create(username=role)
    user.role, user.email, user.is_active = role, os.environ[key], True
    user.set_password(os.environ["STAFF_PASSWORD"])
    user.save()
    print("Ready:", role)
'
(cd "$APP_DIR" && runuser -u "$APP_USER" -- env \
  STAFF_PASSWORD="$STAFF_PASSWORD" STAFF_SECRETARIAT="$STAFF_SECRETARIAT" STAFF_CHIEF="$STAFF_CHIEF" \
  STAFF_PANEL="$STAFF_PANEL" STAFF_AA="$STAFF_AA" STAFF_ADMIN="$STAFF_ADMIN" \
  .venv/bin/python manage.py shell -c "$STAFF_PY") | grep '^Ready:'
unset STAFF_PASSWORD
rm -f /root/staff-logins.txt

echo "=== Demo data ==="
manage seed_exhibit_demo --force

echo "=== Starting the site ==="
systemctl start "$SERVICE"
sleep 3
if curl -fsS -o /dev/null -m 20 "https://$DOMAIN/apply/"; then
  echo "OK: https://$DOMAIN/apply/ opens."
else
  echo "PROBLEM: the site did not open. Run: journalctl -u $SERVICE -n 50 --no-pager"
fi
echo "=== Done ==="
echo "Staff usernames: secretariat, hrm_chief, hrmpsb_member, appointing_authority, system_admin"
