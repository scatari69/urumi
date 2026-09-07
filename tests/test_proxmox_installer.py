"""Offline checks for the installer's interactive input; no Proxmox commands run.

Run: python3 -m unittest discover -s tests -p 'test_proxmox_installer.py'
"""
import errno
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "proxmox-install.sh"


def terminal_run(commands, answers, expected_code=0):
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("bash", ["bash", "-c", 'source "$1"; ' + commands, "test", str(SCRIPT)])
    output = b""
    pending = list(answers)
    search_from = 0
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            if not select.select([fd], [], [], 0.1)[0]:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output += chunk
            if pending:
                prompt, answer = pending[0]
                marker = prompt.encode()
                found = output.find(marker, search_from)
                if found >= 0:
                    os.write(fd, (answer + "\n").encode())
                    search_from = found + len(marker)
                    pending.pop(0)
        else:
            raise AssertionError("Interactive installer timed out")
        _, status = os.waitpid(pid, 0)
        pid = None
        if os.waitstatus_to_exitcode(status) != expected_code or pending:
            raise AssertionError(f"Installer failed: {output.decode()}")
        return output.decode()
    finally:
        os.close(fd)
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


class InstallerInputTests(unittest.TestCase):
    def test_defaults_reach_callers_variable(self):
        result = terminal_run('ask value "Name" urumi; printf "RESULT:%s" "$value"', [("Name [urumi]: ", "")])
        self.assertIn("RESULT:urumi", result)

    def test_number_retries_and_normalizes_leading_zero(self):
        result = terminal_run(
            'number ctid "ID" 100 100 999; printf "RESULT:%s" "$ctid"',
            [("ID [100]: ", "0"), ("ID [100]: ", "$(false)"), ("ID [100]: ", "0101")],
        )
        self.assertIn("RESULT:101", result)

    def test_secrets_are_hidden_and_interpolation_rejected(self):
        result = terminal_run(
            'secret password "Password"; [[ $password == "accepted-secret" ]]',
            [("Password: ", ""), ("Password: ", "${HOME}"), ("Password: ", "accepted-secret")],
        )
        self.assertNotIn("accepted-secret", result)
        self.assertNotIn("${HOME}", result)

    def test_storage_must_be_active_and_in_list(self):
        result = terminal_run(
            "pvesm() { printf 'Name Type Status\nlocal dir active\noff dir inactive\n'; }; "
            'storage selected rootdir "Storage"; printf "RESULT:%s" "$selected"',
            [("Storage: ", "off"), ("Storage: ", "unknown"), ("Storage: ", "local")],
        )
        self.assertIn("RESULT:local", result)

    def test_full_install_with_mock_proxmox(self):
        result = terminal_run(MOCK_HOST + 'main', install_answers("yes"))
        self.assertIn("<create> <101>", result)
        self.assertIn("<--unprivileged> <1>", result)
        self.assertIn("<--features> <nesting=1>", result)
        self.assertIn("<--rootfs> <local:4>", result)
        self.assertIn("ip=dhcp", result)
        self.assertIn("http://192.0.2.10:8080", result)
        self.assertNotIn("<destroy>", result)
        self.assertNotIn("fake-secret", result)
        self.assertIn("CREATE_UMASK:0022", result)
        self.assertIn("SECRET_MODE:600", result)

    def test_restrictive_caller_umask_does_not_close_container_etc(self):
        result = terminal_run(MOCK_HOST + 'umask 077; main', install_answers("yes"))
        self.assertIn("ETC_MODE:755", result)
        self.assertIn("SECRET_DIR_MODE:700", result)
        self.assertIn("SECRET_MODE:600", result)

    def test_explicit_dns_is_passed_to_container(self):
        answers = install_answers("yes")
        answers = [(prompt, "192.0.2.53" if prompt.startswith("DNS IPv4") else answer) for prompt, answer in answers]
        result = terminal_run(MOCK_HOST + 'main', answers)
        self.assertIn("<--nameserver> <192.0.2.53>", result)

    def test_cancel_does_not_create_container(self):
        result = terminal_run(MOCK_HOST + 'main', install_answers("no"))
        self.assertIn("Отменено", result)
        self.assertNotIn("<create>", result)

    def test_failed_start_preserves_container(self):
        commands = MOCK_HOST + '''
pct() {
    printf 'PCT'; printf ' <%s>' "$@"; printf '\n'
    [[ $1 != start ]]
}
main
'''
        result = terminal_run(commands, install_answers("yes"), expected_code=1)
        self.assertIn("Контейнер 101 сохранён для диагностики", result)
        self.assertNotIn("<destroy>", result)


# No host/container mutations: only the real mktemp/config serialization/cleanup
# and interactive flow run. All Proxmox and network commands are replaced.
MOCK_HOST = r'''
check_host() { :; }
bridge_exists() { :; }
pveversion() { echo pve-test; }
ip() { :; }
pvesh() { echo 101; }
pvesm() { printf 'Name Type Status\nlocal dir active\n'; }
pveam() {
    if [[ $1 == available ]]; then
        echo 'system ubuntu-24.04-standard_test_amd64.tar.zst'
    fi
}
curl() { printf '#!/bin/bash\n' > "${@: -1}"; }
pct() {
    if [[ $1 == create ]]; then
        printf 'CREATE_UMASK:%s\n' "$(umask)"
        mock_root=$(mktemp -d)
        mkdir "$mock_root/etc"
        printf 'ETC_MODE:%s\n' "$(stat -c %a "$mock_root/etc")"
        rmdir "$mock_root/etc" "$mock_root"
    elif [[ $1 == push && $4 == /root/urumi.env ]]; then
        printf 'SECRET_MODE:%s\n' "$(stat -c %a "$3")"
        printf 'SECRET_DIR_MODE:%s\n' "$(stat -c %a "${3%/*}")"
    fi
    if [[ $1 == exec && $4 == hostname ]]; then
        echo '192.0.2.10 '
    else
        printf 'PCT'; printf ' <%s>' "$@"; printf '\n'
    fi
}
'''


def install_answers(confirmation):
    return [
        ("ID нового контейнера [101]: ", ""),
        ("Имя контейнера [urumi]: ", ""),
        ("CPU (ядра) [1]: ", ""),
        ("RAM (МБ) [1024]: ", ""),
        ("Диск (ГБ) [4]: ", ""),
        ("Хранилище шаблонов: ", "local"),
        ("Хранилище диска контейнера: ", "local"),
        ("Сетевой мост [vmbr0]: ", ""),
        ("IPv4: dhcp или адрес/маска [dhcp]: ", ""),
        ("VLAN (0 — без тега) [0]: ", ""),
        ("DNS IPv4 (пусто — наследовать от узла): ", ""),
        ("Порт админки [8080]: ", ""),
        ("Ветка, тег или commit репозитория scatari69/urumi [main]: ", ""),
        ("BOT_TOKEN: ", "fake-secret"),
        ("OPENROUTER_API_KEY: ", "fake-secret"),
        ("Пароль админки: ", "fake-secret"),
        ("ID администраторов бота, JSON-массив [[]]: ", ""),
        ("Создать контейнер и запустить бота? Введите yes: ", confirmation),
    ]


class GuestNetworkTests(unittest.TestCase):
    def run_guest(self, commands):
        return subprocess.run(
            ["bash", "-c", 'source "$1"; ' + commands, "test", str(SCRIPT.with_name("proxmox-guest.sh"))],
            capture_output=True, text=True, timeout=5,
        )

    def test_failed_apt_update_never_installs_packages(self):
        result = self.run_guest('apt-get() { printf "%s\\n" "$*"; return 100; }; install_packages')
        self.assertEqual(result.returncode, 1)
        self.assertIn("APT::Update::Error-Mode=any", result.stdout)
        self.assertNotIn("install -y", result.stdout)

    def test_network_failure_stops_before_packages(self):
        result = self.run_guest('''
network_ready() { return 1; }
sleep() { :; }
ip() { :; }
cat() { :; }
wait_for_network
echo PACKAGES
''')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("PACKAGES", result.stdout)
        self.assertIn("Сеть не готова", result.stderr)

    def test_network_can_become_ready_after_dhcp_delay(self):
        result = self.run_guest('''
tries=0
network_ready() { tries=$((tries+1)); [[ $tries == 3 ]]; }
sleep() { :; }
wait_for_network
echo "READY:$tries"
''')
        self.assertEqual(result.returncode, 0)
        self.assertIn("READY:3", result.stdout)


if __name__ == "__main__":
    unittest.main()
