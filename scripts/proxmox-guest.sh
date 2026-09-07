#!/usr/bin/env bash
# Internal installer, invoked by proxmox-install.sh in a NEW container only.
set +x
set -Eeuo pipefail
umask 022

network_ready() {
    [[ -n $(ip -4 -o address show dev eth0 scope global) ]] || return 1
    [[ -n $(ip -4 route show default) ]] || return 1
    timeout 3 getent ahostsv4 archive.ubuntu.com >/dev/null 2>&1
}

wait_for_network() {
    local attempt
    echo 'Ожидание IPv4, шлюза и DNS (до минуты)…'
    for ((attempt=0; attempt<12; attempt++)); do
        if network_ready; then
            return 0
        fi
        sleep 2
    done
    echo 'Сеть не готова: проверьте IP, шлюз, мост/VLAN, firewall и DNS контейнера в Proxmox.' >&2
    ip -4 -brief address >&2 || true
    ip -4 route >&2 || true
    cat /etc/resolv.conf >&2 || true
    echo 'Пакеты не устанавливались. После исправления сети повторите внутренний установщик; см. README.' >&2
    return 1
}

install_packages() {
    # apt update normally returns success even when all indexes fail to download.
    if ! apt-get -o APT::Update::Error-Mode=any -o Acquire::Retries=2 \
        -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 update; then
        echo 'Не удалось обновить индексы APT. Проверьте DNS и доступ к репозиториям; установка остановлена.' >&2
        return 1
    fi
    apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=15 \
        -o Acquire::https::Timeout=15 install -y --no-install-recommends \
        ca-certificates git curl python3.12 python3.12-venv
}

main() {
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
wait_for_network
install_packages

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
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
main "$@"
fi
