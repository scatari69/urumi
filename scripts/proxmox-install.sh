#!/usr/bin/env bash
# Run on the Proxmox VE host. No community-scripts runtime dependencies.
set +x
set -Eeuo pipefail

die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

ask() {
    local target=$1 label=$2 default=${3:-} reply
    read -r -p "$label${default:+ [$default]}: " reply < /dev/tty
    printf -v "$target" '%s' "${reply:-$default}"
}

secret() {
    local target=$1 label=$2 value
    while true; do
        read -r -s -p "$label: " value < /dev/tty
        printf '\n' > /dev/tty
        # python-dotenv interpolates ${...}, even inside quoted values.
        # shellcheck disable=SC2016
        if [[ -n $value && $value != *'${'* && $value != *$'\r'* ]]; then
            printf -v "$target" '%s' "$value"
            return
        fi
        # shellcheck disable=SC2016
        printf 'Введите непустое значение без ${ и перевода строки.\n' >&2
    done
}

number() {
    local target=$1 label=$2 default=$3 minimum=$4 maximum=$5 value
    while true; do
        ask value "$label" "$default"
        if [[ $value =~ ^[0-9]{1,9}$ ]] && ((10#$value >= minimum && 10#$value <= maximum)); then
            printf -v "$target" '%d' "$((10#$value))"
            return
        fi
        printf 'Допустимо целое число от %s до %s.\n' "$minimum" "$maximum" >&2
    done
}

storage() {
    local target=$1 content=$2 label=$3 value listing
    listing=$(pvesm status --content "$content")
    printf '%s\n' "$listing"
    while true; do
        ask value "$label"
        if awk -v selected="$value" 'NR>1 && $1==selected && $3=="active" {ok=1} END {exit !ok}' <<< "$listing"; then
            printf -v "$target" '%s' "$value"
            return
        fi
        printf 'Выберите активное хранилище из списка.\n' >&2
    done
}

cleanup() {
    local result=$?
    trap - EXIT
    [[ -z ${urumi_work_dir:-} ]] || rm -rf -- "$urumi_work_dir"
    if ((result != 0)) && [[ ${urumi_created:-0} == 1 ]]; then
        printf '\nУстановка прервана. Контейнер %s сохранён для диагностики.\n' "$urumi_ctid" >&2
        printf 'Консоль: pct enter %s\nЛоги: pct exec %s -- journalctl -u urumi -n 50\n' "$urumi_ctid" "$urumi_ctid" >&2
    fi
    exit "$result"
}

check_host() {
    [[ $EUID == 0 && -d /etc/pve ]] || die 'Запустите скрипт от root в Shell узла Proxmox VE.'
    [[ $(uname -m) == x86_64 ]] || die 'Поддерживается только amd64/x86_64.'
    local command_name
    for command_name in pct pvesm pveam pvesh pveversion curl python3 ip; do
        command -v "$command_name" >/dev/null || die "Не найдена команда $command_name"
    done
}

bridge_exists() { [[ -d /sys/class/net/$1/bridge ]]; }

main() {
    [[ ${1:-} != --help ]] || { printf 'Запуск: bash scripts/proxmox-install.sh (root на узле Proxmox VE).\n'; return; }
    check_host
    exec 3<> /dev/tty
    printf 'Установка Urumi: Ubuntu 24.04 LXC, Python 3.12, systemd.\n'
    pveversion

    local ct_name cores memory disk template_storage root_storage bridge address gateway vlan admin_port dns
    local -a dns_options=()
    local ref bot_token router_key admin_password admin_ids confirmation template net0 installer ip_address
    # EXIT traps run after function locals have gone out of scope on errors.
    urumi_ctid='' urumi_work_dir='' urumi_created=0
    number urumi_ctid 'ID нового контейнера' "$(pvesh get /cluster/nextid)" 100 999999999
    # Cluster-wide check also covers VM IDs and containers on other nodes.
    pvesh get /cluster/nextid --vmid "$urumi_ctid" >/dev/null || die 'Этот ID уже занят.'
    ask ct_name 'Имя контейнера' urumi
    [[ ${#ct_name} -le 63 && $ct_name =~ ^[a-z]([a-z0-9-]*[a-z0-9])?$ ]] || die 'Некорректное имя контейнера.'
    number cores 'CPU (ядра)' 1 1 128
    number memory 'RAM (МБ)' 1024 512 1048576
    number disk 'Диск (ГБ)' 4 4 1048576
    storage template_storage vztmpl 'Хранилище шаблонов'
    storage root_storage rootdir 'Хранилище диска контейнера'
    ip -brief link show type bridge
    ask bridge 'Сетевой мост' vmbr0
    if [[ ! $bridge =~ ^[a-zA-Z0-9_.-]+$ ]] || ! bridge_exists "$bridge"; then
        die 'Сетевой мост не найден.'
    fi
    ask address 'IPv4: dhcp или адрес/маска' dhcp
    gateway=''
    if [[ $address != dhcp ]]; then
        python3 -c 'import ipaddress,sys; ipaddress.IPv4Interface(sys.argv[1])' "$address" || die 'Некорректный IPv4/CIDR.'
        [[ $address == */* ]] || die 'Укажите маску, например 192.168.1.50/24.'
        ask gateway 'Шлюз IPv4'
        python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "$gateway" || die 'Некорректный шлюз.'
    fi
    number vlan 'VLAN (0 — без тега)' 0 0 4094
    ask dns 'DNS IPv4 (пусто — наследовать от узла)'
    if [[ -n $dns ]]; then
        python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "$dns" || die 'Некорректный DNS IPv4.'
        dns_options=(--nameserver "$dns")
    fi
    number admin_port 'Порт админки' 8080 1024 65535
    ask ref 'Ветка, тег или commit репозитория scatari69/urumi' main
    [[ $ref =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ && $ref != *..* ]] || die 'Используйте имя без /, пробелов и .. либо commit SHA.'
    secret bot_token 'BOT_TOKEN'
    secret router_key 'OPENROUTER_API_KEY'
    secret admin_password 'Пароль админки'
    ask admin_ids 'ID администраторов бота, JSON-массив' '[]'
    python3 -c 'import json,sys; a=json.loads(sys.argv[1]); assert isinstance(a,list) and all(type(x) is int and x>0 for x in a)' "$admin_ids" || die 'Пример: [123456789, 987654321].'

    printf '\nКонтейнер %s (%s): %s CPU, %s МБ RAM, %s ГБ на %s.\n' "$urumi_ctid" "$ct_name" "$cores" "$memory" "$disk" "$root_storage"
    printf 'Сеть: %s, %s, VLAN %s; админка: порт %s; версия: %s.\n' "$bridge" "$address" "$vlan" "$admin_port" "$ref"
    printf 'DNS: %s; nesting включён для systemd в Ubuntu 24.04.\n' "${dns:-наследуется от узла}"
    printf 'После установки бот начнёт polling. Другой экземпляр с этим токеном должен быть остановлен.\n'
    ask confirmation 'Создать контейнер и запустить бота? Введите yes'
    [[ $confirmation == yes ]] || { printf 'Отменено.\n'; return; }

    umask 077
    urumi_work_dir=$(mktemp -d /tmp/urumi-install.XXXXXXXX)
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    # Download the in-container installer before allocating any container resources.
    installer="$urumi_work_dir/install.sh"
    curl --fail --show-error --silent --location --connect-timeout 15 --max-time 120 \
        "https://raw.githubusercontent.com/scatari69/urumi/$ref/scripts/proxmox-guest.sh" -o "$installer"
    bash -n "$installer"
    printf '%s\n' "$bot_token" "$router_key" "$admin_password" "$admin_ids" "$admin_port" |
        python3 -c '
import sys
keys = ["BOT_TOKEN", "OPENROUTER_API_KEY", "ADMIN_PASSWORD", "ADMIN_USER_IDS", "ADMIN_PORT"]
for key in keys:
    value = sys.stdin.readline().rstrip("\n")
    value = value.replace("\\", "\\\\").replace("\x27", "\\\x27")
    print(key + "=\x27" + value + "\x27")
print("DB_PATH=/var/lib/urumi/urumi.db")
' > "$urumi_work_dir/urumi.env"
    unset bot_token router_key admin_password

    pveam update
    template=$(pveam available --section system | awk '$2 ~ /^ubuntu-24\.04-standard_.*_amd64\.tar\./ {print $2}' | sort -V | tail -n 1)
    [[ -n $template ]] || die 'Шаблон Ubuntu 24.04 не найден в каталоге pveam.'
    pveam download "$template_storage" "$template"
    net0="name=eth0,bridge=$bridge,ip=$address,ip6=manual,type=veth,firewall=1"
    [[ -z $gateway ]] || net0+=",gw=$gateway"
    ((vlan == 0)) || net0+=",tag=$vlan"
    pct create "$urumi_ctid" "$template_storage:vztmpl/$template" \
        --hostname "$ct_name" --ostype ubuntu --arch amd64 --unprivileged 1 --features nesting=1 \
        --cores "$cores" --memory "$memory" --swap 512 --rootfs "$root_storage:$disk" \
        --net0 "$net0" "${dns_options[@]}" --onboot 1 --description 'Urumi Telegram bot (systemd)'
    urumi_created=1
    pct start "$urumi_ctid"
    pct push "$urumi_ctid" "$installer" /root/urumi-install.sh --perms 0700
    pct push "$urumi_ctid" "$urumi_work_dir/urumi.env" /root/urumi.env --perms 0600
    pct exec "$urumi_ctid" -- bash /root/urumi-install.sh "$ref"
    ip_address=$(pct exec "$urumi_ctid" -- hostname -I)
    ip_address=${ip_address%% *}
    printf '\nУстановка завершена. Админка: http://%s:%s\n' "${ip_address:-IP-контейнера}" "$admin_port"
    printf 'Активируйте группу на /chats. В BotFather отключите privacy mode и заново добавьте бота в группу.\n'
    printf 'Консоль: pct enter %s\nЛоги: pct exec %s -- journalctl -u urumi -f\n' "$urumi_ctid" "$urumi_ctid"
    cleanup
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
