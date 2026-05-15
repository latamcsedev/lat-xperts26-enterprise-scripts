#!/bin/sh
# deploy.sh — Web App deployment script.
# Lives in the root of your GitHub repo. Update here whenever deployment
# steps change — no need to touch the VM's init script.

set -e

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${INSTALL_DIR}/py-env"
SERVICE_NAME="latam-portal"
SERVICE_SRC="${INSTALL_DIR}/etc/${SERVICE_NAME}.service"
SERVICE_DEST="/etc/systemd/system/${SERVICE_NAME}.service"
CREDENTIALS_FILE="/fabric/credentials.env"   # lives on VM, never in the repo

echo "================================================================"
echo " LAT Scripts — Web App Deployment"
echo " Install dir : ${INSTALL_DIR}"
echo "================================================================"

# ── 1. Validate credentials file ─────────────────────────────────────────────
echo "--> Checking credentials"
if [ ! -f "${CREDENTIALS_FILE}" ]; then
    echo "ERROR: Credentials file not found at ${CREDENTIALS_FILE}" >&2
    echo "       Create it on the VM with the following contents:" >&2
    echo "         FABRIC_HOST=<host-ip>" >&2
    echo "         CREDENTIAL=<your-credential>" >&2
    exit 1
fi
# Ensure it's not world-readable
chmod 600 "${CREDENTIALS_FILE}"
echo "--> Credentials file found"

# ── 2. System packages ────────────────────────────────────────────────────────
echo "--> Installing system packages"
apt update -y
apt install -y vim expect yq sshpass rsync \
               python3-paramiko python3-pexpect python3-pip python3-venv

echo "StrictHostKeyChecking no" >> /etc/ssh/ssh_config

# ── 2a. Authorized SSH key ────────────────────────────────────────────────────
echo "--> Adding authorized SSH key"
mkdir -p /root/.ssh
chmod 700 /root/.ssh
AUTHKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIInLb2f6rETwAhCC/lPtebTn0+CD35F2O2tEy9/EuceH jmartin@jmartin-mac"
if ! grep -qF "${AUTHKEY}" /root/.ssh/authorized_keys 2>/dev/null; then
    echo "${AUTHKEY}" >> /root/.ssh/authorized_keys
fi
chmod 600 /root/.ssh/authorized_keys

# ── 3. Disable nginx ──────────────────────────────────────────────────────────
echo "--> Disabling nginx"
systemctl stop nginx    || true
systemctl disable nginx || true

# ── 4. Python virtual environment ────────────────────────────────────────────
echo "--> Setting up Python virtual environment at ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"
. "${VENV_DIR}/bin/activate"
pip install --upgrade pip
pip install \
    fastapi \
    uvicorn \
    paramiko \
    pyyaml \
    paramiko_expect \
    python-multipart \
    requests \
    jinja2 \
    scp

# ── 5. Inject EnvironmentFile into systemd service ───────────────────────────
echo "--> Installing systemd service (${SERVICE_NAME})"

if [ ! -f "${SERVICE_SRC}" ]; then
    echo "ERROR: Service file not found: ${SERVICE_SRC}" >&2
    exit 1
fi

# Copy service file and inject EnvironmentFile directive if not already present
cp "${SERVICE_SRC}" "${SERVICE_DEST}"
if ! grep -q "EnvironmentFile" "${SERVICE_DEST}"; then
    sed -i "/^\[Service\]/a EnvironmentFile=${CREDENTIALS_FILE}" "${SERVICE_DEST}"
fi

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl start  "${SERVICE_NAME}"

# ── 7. Post-deploy health check ───────────────────────────────────────────────
sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    echo "================================================================"
    echo " Deployment complete — ${SERVICE_NAME} is running"
    echo "================================================================"
else
    echo "ERROR: ${SERVICE_NAME} failed to start." >&2
    echo "       Check logs with: journalctl -u ${SERVICE_NAME}" >&2
    exit 1
fi

# ── 8. Post-deploy configurations ───────────────────────────────────────────────

# venv already activated
#python3 check_all_devices_online.py
python3 ${INSTALL_DIR}/postdeploy_configuration.py
