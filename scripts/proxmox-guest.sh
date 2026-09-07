#!/usr/bin/env bash
# Internal installer, invoked by proxmox-install.sh in a NEW container only.
set +x
set -Eeuo pipefail
umask 022

[[ $EUID == 0 ]] || { echo 'Требуется root.' >&2; exit 1; }
[[ $(systemd-detect-virt --container) == lxc ]] || { echo 'Требуется LXC.' >&2; exit 1; }
# shellcheck source=/dev/null
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || { echo 'Требуется Ubuntu 24.04.' >&2; exit 1; }
[[ ! -e /opt/urumi && ! -e /etc/urumi && ! -e /var/lib/urumi && ! -e /etc/systemd/system/urumi.service ]] || {
    echo 'Установка уже существует. Скрипт не перезаписывает данные; см. README.' >&2
    exit 1
}
[[ -f /root/urumi.env ]] || { echo 'Отсутствует конфигурация /root/urumi.env.' >&2; exit 1; }
ref=${1:?Не указана версия}
[[ $ref =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ && $ref != *..* ]] || exit 1

export DEBIAN_FRONTEND=noninteractive
echo 'Установка Python и зависимостей…'
# apt retries allow DHCP/DNS to come up after the first container boot.
apt-get -o Acquire::Retries=5 update
apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    ca-certificates git curl python3.12 python3.12-venv

useradd --system --home-dir /var/lib/urumi --shell /usr/sbin/nologin urumi
install -d -o urumi -g urumi -m 0750 /var/lib/urumi
install -d -o root -g urumi -m 0750 /etc/urumi
install -o root -g urumi -m 0640 /root/urumi.env /etc/urumi/urumi.env
rm -- /root/urumi.env

git init -q /opt/urumi
git -C /opt/urumi remote add origin https://github.com/scatari69/urumi.git
git -C /opt/urumi fetch --depth 1 origin "$ref"
git -C /opt/urumi checkout --detach FETCH_HEAD
python3.12 -m venv /opt/urumi/.venv
/opt/urumi/.venv/bin/pip install --no-cache-dir -r /opt/urumi/requirements.txt
ln -s /etc/urumi/urumi.env /opt/urumi/.env
# Validate config without printing credentials or contacting external APIs.
cd /opt/urumi
port=$(.venv/bin/python -c '
try:
    from core.config import settings
    assert 1024 <= settings.ADMIN_PORT <= 65535
    print(settings.ADMIN_PORT)
except Exception:
    raise SystemExit("Некорректный конфиг: проверьте /etc/urumi/urumi.env")
')

cat > /etc/systemd/system/urumi.service <<'UNIT'
[Unit]
Description=Urumi Telegram bot and admin panel
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=urumi
Group=urumi
WorkingDirectory=/opt/urumi
ExecStart=/opt/urumi/.venv/bin/python /opt/urumi/main.py
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/urumi

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now urumi.service
echo 'Ожидание запуска админки…'
for ((attempt=0; attempt<60; attempt++)); do
    if systemctl is-active --quiet urumi.service && curl -fsS --max-time 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
        echo 'Сервис и HTTP /health доступны. Обмен сообщениями проверьте в Telegram.'
        exit 0
    fi
    sleep 2
done
echo 'Админка не запустилась. Проверьте: journalctl -u urumi -n 50' >&2
exit 1
