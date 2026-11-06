#!/usr/bin/env bash
# Sets up the DOH-CHD CALABARZON Recruitment Portal DEMO server on Ubuntu 24.04.
# Run as root from the droplet console:  bash setup-demo-server.sh
#
# Safe to run again. A second run keeps the secrets and the data, pulls the latest
# code, applies migrations and restarts the service. It never reseeds.
# Demo use only: the first run loads synthetic demo data.
set -euo pipefail

APP_USER=portal
APP_DIR=/opt/recruitment-portal
DB_NAME=recruitment_portal
SERVICE=recruitment-portal
REPO=https://github.com/cybertechiesph/RecruitGuard-CHD.git
CONF=/root/portal-setup.conf
LOGINS=/root/staff-logins.txt
export DEBIAN_FRONTEND=noninteractive
cd /tmp  # the limited account cannot read /root, which breaks git and pip there

step() { printf '\n=== %s ===\n' "$1"; }
as_app() { runuser -u "$APP_USER" -- "$@"; }
manage() { (cd "$APP_DIR" && as_app .venv/bin/python manage.py "$@"); }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root." >&2
  exit 1
fi

# ---- Questions (first run only) -------------------------------------------------
if [ -f "$CONF" ]; then
  # shellcheck disable=SC1090
  . "$CONF"
else
  step "A few questions"
  read -rp "Domain, without www (example: example.com): " DOMAIN
  DOMAIN=$(printf '%s' "$DOMAIN" | tr '[:upper:]' '[:lower:]' | sed -E 's#^https?://##; s#^www\.##; s#/.*$##')
  if ! printf '%s' "$DOMAIN" | grep -Eq '^[a-z0-9-]+(\.[a-z0-9-]+)+$'; then
    echo "That does not look like a domain." >&2
    exit 1
  fi
  read -rp "Email server [smtp.gmail.com]: " EMAIL_HOST
  EMAIL_HOST=${EMAIL_HOST:-smtp.gmail.com}
  read -rp "Email port [587]: " EMAIL_PORT
  EMAIL_PORT=${EMAIL_PORT:-587}
  read -rp "Email account that sends the codes: " EMAIL_USER
  read -rp "Sender address, if different [$EMAIL_USER]: " EMAIL_FROM
  EMAIL_FROM=${EMAIL_FROM:-$EMAIL_USER}
  printf 'Each staff login needs its own address. name+sec@gmail.com style\n'
  printf 'addresses are fine and arrive in the same inbox.\n'
  read -rp "Secretariat email: " STAFF_SECRETARIAT
  read -rp "HRM Chief email: " STAFF_CHIEF
  read -rp "HRMPSB Member email: " STAFF_PANEL
  read -rp "Appointing Authority email: " STAFF_AA
  read -rp "System Administrator email: " STAFF_ADMIN
  umask 077
  {
    printf 'DOMAIN=%q\nEMAIL_HOST=%q\nEMAIL_PORT=%q\nEMAIL_USER=%q\nEMAIL_FROM=%q\n' \
      "$DOMAIN" "$EMAIL_HOST" "$EMAIL_PORT" "$EMAIL_USER" "$EMAIL_FROM"
    printf 'STAFF_SECRETARIAT=%q\nSTAFF_CHIEF=%q\nSTAFF_PANEL=%q\nSTAFF_AA=%q\nSTAFF_ADMIN=%q\n' \
      "$STAFF_SECRETARIAT" "$STAFF_CHIEF" "$STAFF_PANEL" "$STAFF_AA" "$STAFF_ADMIN"
  } > "$CONF"
  umask 022
fi

# ---- Memory safety net ----------------------------------------------------------
step "Swap file"
if [ ! -f /swapfile ] && [ "$(free -m | awk '/^Mem:/{print $2}')" -lt 3000 ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---- Packages -------------------------------------------------------------------
step "Installing packages (a few minutes)"
timedatectl set-timezone Asia/Manila
apt-get update -y
apt-get install -y python3-venv python3-pip postgresql nginx certbot python3-certbot-nginx \
  git ufw dnsutils netcat-openbsd curl

# ---- Limited account and code ---------------------------------------------------
step "Getting the code"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$APP_USER"
if [ -d "$APP_DIR/.git" ]; then
  as_app git -C "$APP_DIR" pull --ff-only
else
  mkdir -p "$APP_DIR"
  chown "$APP_USER:$APP_USER" "$APP_DIR"
  as_app git clone --depth 1 "$REPO" "$APP_DIR"
fi
[ -d "$APP_DIR/.venv" ] || as_app python3 -m venv "$APP_DIR/.venv"
as_app "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
as_app "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# ---- Secrets (made here, never shown) -------------------------------------------
if [ ! -f "$APP_DIR/.env" ]; then
  step "Creating secrets"
  read -rsp "Email app password (hidden while typing): " EMAIL_PASSWORD
  echo
  EMAIL_PASSWORD=${EMAIL_PASSWORD// /}
  gen() { python3 -c 'import secrets; print(secrets.token_hex(32))'; }
  DB_PASSWORD=$(gen)
  umask 077
  cat > "$APP_DIR/.env" <<EOF
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=$(gen)
DJANGO_ALLOWED_HOSTS=$DOMAIN,www.$DOMAIN
DJANGO_CSRF_TRUSTED_ORIGINS=https://$DOMAIN,https://www.$DOMAIN
APPLICANT_PORTAL_BASE_URL=https://$DOMAIN
DJANGO_TIME_ZONE=Asia/Manila
POSTGRES_DB=$DB_NAME
POSTGRES_USER=$APP_USER
POSTGRES_PASSWORD=$DB_PASSWORD
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=$EMAIL_HOST
EMAIL_PORT=$EMAIL_PORT
EMAIL_HOST_USER=$EMAIL_USER
EMAIL_HOST_PASSWORD=$EMAIL_PASSWORD
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL="DOH-CHD CALABARZON Recruitment Portal <$EMAIL_FROM>"
APPLICATION_OTP_HASH_SECRET=$(gen)
INTERNAL_MFA_ENABLED=True
INTERNAL_MFA_OTP_HASH_SECRET=$(gen)
EVIDENCE_ENCRYPTION_SECRET=$(gen)
CAPTCHA_ENABLED=True
CAPTCHA_PROVIDER=local
RATE_LIMIT_TRUST_X_FORWARDED_FOR=True
EOF
  umask 022
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  unset EMAIL_PASSWORD
else
  DB_PASSWORD=$(grep '^POSTGRES_PASSWORD=' "$APP_DIR/.env" | cut -d= -f2-)
fi

# ---- Database -------------------------------------------------------------------
step "Database"
systemctl enable --now postgresql
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$APP_USER'" | grep -q 1; then
  runuser -u postgres -- psql -c "CREATE ROLE $APP_USER LOGIN PASSWORD '$DB_PASSWORD'"
fi
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
  runuser -u postgres -- createdb -O "$APP_USER" "$DB_NAME"
fi
manage migrate --noinput
manage collectstatic --noinput

# ---- Staff logins and demo data (first run only) --------------------------------
if [ ! -f "$APP_DIR/.staff-done" ]; then
  step "Staff logins"
  STAFF_PY='
import os, secrets
from django.contrib.auth import get_user_model
User = get_user_model()
rows = [
    ("secretariat", "secretariat", "Demo", "Secretariat", "STAFF_SECRETARIAT"),
    ("hrm_chief", "hrm_chief", "Demo", "HRM Chief", "STAFF_CHIEF"),
    ("hrmpsb_member", "hrmpsb_member", "Demo", "Panel Member", "STAFF_PANEL"),
    ("appointing_authority", "appointing_authority", "Demo", "Appointing Authority", "STAFF_AA"),
    ("system_admin", "system_admin", "Demo", "System Administrator", "STAFF_ADMIN"),
]
for username, role, first, last, key in rows:
    password = secrets.token_urlsafe(14)
    user, _ = User.objects.get_or_create(username=username)
    user.role, user.first_name, user.last_name = role, first, last
    user.email, user.is_active = os.environ[key], True
    user.set_password(password)
    user.save()
    print(f"LOGIN {username}  {password}  ({user.email})")
'
  umask 077
  (cd "$APP_DIR" && runuser -u "$APP_USER" -- env \
    STAFF_SECRETARIAT="$STAFF_SECRETARIAT" STAFF_CHIEF="$STAFF_CHIEF" STAFF_PANEL="$STAFF_PANEL" \
    STAFF_AA="$STAFF_AA" STAFF_ADMIN="$STAFF_ADMIN" \
    .venv/bin/python manage.py shell -c "$STAFF_PY") | grep '^LOGIN ' | sed 's/^LOGIN //' > "$LOGINS"
  umask 022
  [ "$(wc -l < "$LOGINS")" -eq 5 ] || { echo "Staff logins were not all created." >&2; exit 1; }
  as_app touch "$APP_DIR/.staff-done"
fi
if [ ! -f "$APP_DIR/.seed-done" ]; then
  step "Demo data"
  manage seed_demo_data --force
  as_app touch "$APP_DIR/.seed-done"
fi

# ---- App service ----------------------------------------------------------------
step "App service"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Recruitment Portal demo
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 2 --timeout 120
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

# ---- Web server -----------------------------------------------------------------
step "Web server"
echo 'server_names_hash_bucket_size 128;' > /etc/nginx/conf.d/portal-names.conf
if [ ! -f "/etc/nginx/sites-available/$SERVICE" ]; then
  cat > "/etc/nginx/sites-available/$SERVICE" <<EOF
server {
    listen 80;
    server_name $DOMAIN www.$DOMAIN;
    client_max_body_size 30m;

    location /static/ {
        alias $APP_DIR/staticfiles/;
    }
    location /admin/ {
        return 404;
    }
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
EOF
fi
ln -sf "/etc/nginx/sites-available/$SERVICE" "/etc/nginx/sites-enabled/$SERVICE"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# ---- Firewall, daily jobs, SSH --------------------------------------------------
step "Firewall and daily jobs"
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
cat > "/etc/cron.d/$SERVICE" <<EOF
0 8 * * * $APP_USER cd $APP_DIR && .venv/bin/python manage.py emit_deadline_notifications
5 8 * * * $APP_USER cd $APP_DIR && .venv/bin/python manage.py send_draft_reminders
10 8 * * * $APP_USER cd $APP_DIR && .venv/bin/python manage.py send_resubmission_reminders
15 8 * * * $APP_USER cd $APP_DIR && .venv/bin/python manage.py purge_abandoned_drafts
EOF
# 00- so it wins over the 50-cloud-init file: the first value sshd reads is the one it keeps.
echo 'PasswordAuthentication no' > /etc/ssh/sshd_config.d/00-no-password.conf
systemctl reload ssh || systemctl restart ssh || true

printf '#!/usr/bin/env bash
exec bash %s/deploy/reset-demo.sh "$@"
' "$APP_DIR" > /usr/local/bin/demo-reset
chmod 755 /usr/local/bin/demo-reset

# ---- HTTPS ----------------------------------------------------------------------
step "HTTPS"
SERVER_IP=$(curl -fsS -m 5 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address || true)
DOMAIN_IP=$(dig +short @8.8.8.8 "$DOMAIN" A | tail -1)
WWW_IP=$(dig +short @8.8.8.8 "www.$DOMAIN" A | tail -1)
echo "Server: ${SERVER_IP:-unknown}   $DOMAIN: ${DOMAIN_IP:-none}   www: ${WWW_IP:-none}"
if [ -z "$SERVER_IP" ] || [ "$DOMAIN_IP" != "$SERVER_IP" ]; then
  echo
  echo "NOT FINISHED: $DOMAIN does not point to this server yet."
  echo "Fix the A record, wait a few minutes, then run this script again."
  exit 2
fi
NAMES=(-d "$DOMAIN")
[ "$WWW_IP" = "$SERVER_IP" ] && NAMES+=(-d "www.$DOMAIN")
certbot --nginx "${NAMES[@]}" --non-interactive --agree-tos \
  --register-unsafely-without-email --redirect --keep-until-expiring

# ---- Checks ---------------------------------------------------------------------
step "Checks"
systemctl restart "$SERVICE"
sleep 3
if curl -fsS -o /dev/null -m 20 "https://$DOMAIN/apply/"; then
  echo "OK: https://$DOMAIN/apply/ opens."
else
  echo "PROBLEM: the site did not open. Run: journalctl -u $SERVICE -n 50 --no-pager"
fi
if nc -z -w 10 "$EMAIL_HOST" "$EMAIL_PORT"; then
  echo "OK: the email port is open."
  manage sendtestemail "$STAFF_ADMIN" && echo "Test email sent to $STAFF_ADMIN. Check that inbox." \
    || echo "PROBLEM: the test email failed. Check the app password."
else
  echo "PROBLEM: $EMAIL_HOST port $EMAIL_PORT is blocked from this server."
fi

step "Done"
echo "Site: https://$DOMAIN/apply/"
echo "Staff sign-in: https://$DOMAIN/internal/"
echo "Staff logins are in $LOGINS (read them with: cat $LOGINS)"
