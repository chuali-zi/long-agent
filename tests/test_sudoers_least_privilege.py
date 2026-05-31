import subprocess
from pathlib import Path


SUDOERS = Path(__file__).parents[1] / "configs" / "sudoers.kyagent"
SETUP_SUDOERS = Path(__file__).parents[1] / "scripts" / "setup-sudoers.sh"
ROOT = Path(__file__).parents[1]


def _sudoers() -> str:
    return SUDOERS.read_text(encoding="utf-8")


def test_non_root_readonly_commands_are_not_granted_via_sudo() -> None:
    text = _sudoers()
    forbidden = [
        "/usr/bin/systemctl status *",
        "/usr/bin/systemctl is-active *",
        "/usr/bin/systemctl is-enabled *",
        "/usr/bin/systemctl list-units *",
        "/usr/bin/systemctl list-unit-files *",
        "/usr/bin/systemctl list-timers *",
        "/usr/bin/systemctl show *",
        "/usr/bin/systemctl cat *",
        "/usr/bin/systemctl restart *",
        "/usr/bin/systemctl reload *",
        "/usr/bin/systemctl start *",
        "/usr/bin/systemctl stop *",
        "/usr/bin/find / -xdev",
        "/usr/bin/find /var/log",
        "/usr/bin/findmnt -J -- *",
        "/usr/bin/lsattr -- *",
        "/usr/bin/sha256sum -- *",
        "/usr/bin/crontab -l -u -- *",
        "/usr/bin/sudo -l -U -- *",
        "/usr/bin/smartctl -H -A -- /dev/sd*",
        "/usr/bin/smartctl -H -A -- /dev/nvme*",
    ]

    for pattern in forbidden:
        assert pattern not in text, f"sudoers still grants broad pattern: {pattern}"


def test_dynamic_root_commands_use_anchored_argument_regexes() -> None:
    text = _sudoers()
    required = [
        r"/usr/bin/systemctl ^restart [A-Za-z0-9@._+:][-A-Za-z0-9@._+:]*$",
        r"/usr/bin/systemctl ^reload [A-Za-z0-9@._+:][-A-Za-z0-9@._+:]*$",
        r"/usr/bin/smartctl ^-H -A -- /dev/sd[a-z][0-9]*$",
        r"/usr/bin/smartctl ^-H -A -- /dev/nvme[0-9]+n[0-9]+(p[0-9]+)?$",
        r"/usr/bin/crontab ^-l -u [a-z_][a-z0-9_-]{0,31}$",
    ]

    for rule in required:
        assert rule in text, f"missing anchored sudoers rule: {rule}"


def test_required_fixed_root_queries_remain_granted() -> None:
    text = _sudoers()
    required = [
        "/usr/sbin/aa-status",
        "/usr/sbin/aureport --summary",
        "/usr/sbin/iptables -L -n -v --line-numbers",
        "/usr/sbin/nft list ruleset",
        "/usr/bin/aide --check --config /etc/aide.conf",
        "/usr/sbin/dmidecode -s system-product-name",
        "/usr/sbin/dmidecode -s system-manufacturer",
        "/usr/bin/sudo -l -U kyagent",
    ]

    for rule in required:
        assert rule in text, f"missing required root query: {rule}"


def test_custom_runtime_account_rewrites_self_audit_target() -> None:
    script = SETUP_SUDOERS.read_text(encoding="utf-8")
    assert (
        's#/usr/bin/sudo -l -U kyagent#/usr/bin/sudo -l -U ${USER_NAME}#'
        in script
    )


def test_setup_rejects_sudo_without_regex_support() -> None:
    script = SETUP_SUDOERS.read_text(encoding="utf-8")
    assert "LC_ALL=C sudo -V" in script
    assert "sort -V -C" in script
    assert "1.9.10" in script


def test_setup_parses_sudo_version_with_c_locale() -> None:
    result = subprocess.run(
        ["bash", "tests/fixtures/sudo_version_locale.sh"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1.9.13p3"


def test_setup_validates_custom_runtime_account_before_sed() -> None:
    script = SETUP_SUDOERS.read_text(encoding="utf-8")
    validation = '[[ ! "$USER_NAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]'
    assert validation in script
    assert script.index(validation) < script.index('if [[ "$USER_NAME" == "kyagent" ]]')


def test_service_mutation_regex_accepts_same_unit_characters_as_tool_schema() -> None:
    text = _sudoers()
    assert (
        r"/usr/bin/systemctl ^restart [A-Za-z0-9@._+:][-A-Za-z0-9@._+:]*$"
        in text
    )
    assert (
        r"/usr/bin/systemctl ^reload [A-Za-z0-9@._+:][-A-Za-z0-9@._+:]*$"
        in text
    )


def test_service_mutation_sudoers_denies_tool_forbidden_core_units() -> None:
    text = _sudoers()
    assert (
        r"!/usr/bin/systemctl ^(restart|reload) (systemd-logind|systemd-journald|systemd-udevd|dbus|polkit)([.]service)?$"
        in text
    )


def test_manual_install_instructions_validate_before_install() -> None:
    text = _sudoers()
    assert "sudo >= 1.9.10" in text
    assert text.index("sudo visudo -cf configs/sudoers.kyagent") < text.index(
        "sudo install -m 0440 configs/sudoers.kyagent"
    )
