import subprocess
from pathlib import Path

import pytest


SUDOERS = Path(__file__).parents[1] / "configs" / "sudoers.kyagent"
SETUP_SUDOERS = Path(__file__).parents[1] / "scripts" / "setup-sudoers.sh"
ROOT = Path(__file__).parents[1]


def _sudoers() -> str:
    return SUDOERS.read_text(encoding="utf-8")


def require_usable_bash() -> None:
    result = subprocess.run(
        ["bash", "--version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        pytest.skip("bash is not usable in this test environment")


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
    required = [r"/usr/bin/crontab ^-l -u [a-z_][a-z0-9_-]{0,31}$"]
    for path in ("/usr/bin", "/usr/sbin", "/sbin"):
        required.extend(
            [
                rf"{path}/smartctl ^-H -A -- /dev/sd[a-z][0-9]*$",
                rf"{path}/smartctl ^-H -A -- /dev/nvme[0-9]+n[0-9]+(p[0-9]+)?$",
            ]
        )

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


def test_sudoers_uses_boolean_defaults_without_assignment() -> None:
    text = _sudoers()
    assert "requiretty=false" not in text
    assert "!requiretty" in text


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
    require_usable_bash()
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


def test_default_sudoers_does_not_grant_service_mutation() -> None:
    text = _sudoers()
    assert "KY_SVC_MUTATE" not in text
    assert "/usr/bin/systemctl restart" not in text
    assert "/usr/bin/systemctl reload" not in text


def test_setup_renders_only_explicit_service_allowlist() -> None:
    require_usable_bash()
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source scripts/setup-sudoers.sh; "
            "render_service_allowlist opsagent 'nginx.service,sshd.service'",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert "/usr/bin/systemctl restart nginx.service" in result.stdout
    assert "/usr/bin/systemctl reload sshd.service" in result.stdout
    assert "opsagent  ALL=(root)  NOPASSWD: KY_SVC_MUTATE" in result.stdout


def test_setup_rejects_non_service_allowlist_unit() -> None:
    require_usable_bash()
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source scripts/setup-sudoers.sh; "
            "render_service_allowlist kyagent 'rescue.target'",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )

    assert result.returncode != 0
    assert "非法服务 allowlist unit" in result.stderr


def test_setup_creates_audit_directories_with_exact_0700_mode_without_recursive_chown() -> None:
    script = SETUP_SUDOERS.read_text(encoding="utf-8")

    assert "chmod 0700 /var/log/sudo-io" in script
    assert 'install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "/var/lib/kyagent"' in script
    assert 'install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" /var/log/kyagent' in script
    assert "chown -R" not in script


def test_manual_install_instructions_validate_before_install() -> None:
    text = _sudoers()
    assert "sudo >= 1.9.10" in text
    assert text.index("sudo visudo -cf configs/sudoers.kyagent") < text.index(
        "sudo install -m 0440 configs/sudoers.kyagent"
    )
