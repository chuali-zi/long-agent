import subprocess
import sys
from pathlib import Path

import pytest


SUDOERS = Path(__file__).parents[1] / "configs" / "sudoers.kyagent"
SETUP_SUDOERS = Path(__file__).parents[1] / "scripts" / "setup-sudoers.sh"
LOG_CLEAN_WRAPPER = Path(__file__).parents[1] / "scripts" / "kyagent-log-clean"
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


# ---------------------------------------------------------------------------
# opt-in write operations: render_log_clean
# ---------------------------------------------------------------------------

def test_render_log_clean_enabled_outputs_correct_rules() -> None:
    require_usable_bash()
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source scripts/setup-sudoers.sh; "
            "KYAGENT_ENABLE_LOG_CLEAN=1 render_log_clean kyagent",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "/usr/bin/journalctl ^--vacuum-size=[0-9]+[KMGT]$" in out
    assert "/usr/bin/journalctl ^--vacuum-time=[0-9]+(s|min|h|days|weeks|months|years)$" in out
    # truncate 不再裸授权：改授权 OS 层包装器 + 单个绝对路径参数。
    assert "/usr/local/bin/kyagent-log-clean ^/[A-Za-z0-9._/@-]+$" in out
    assert "/usr/local/bin/kyagent-file-delete ^/[A-Za-z0-9._/@-]+$" in out
    # 旧的、允许 .. 越界的裸 truncate 正则必须已彻底消失。
    assert "/usr/bin/truncate" not in out
    assert "Cmnd_Alias KY_LOG_CLEAN" in out
    assert "kyagent  ALL=(root)  NOPASSWD: KY_LOG_CLEAN" in out


def test_render_log_clean_disabled_by_default_outputs_nothing() -> None:
    require_usable_bash()
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source scripts/setup-sudoers.sh; "
            "render_log_clean kyagent",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_render_log_clean_non1_value_outputs_nothing() -> None:
    require_usable_bash()
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source scripts/setup-sudoers.sh; "
            "KYAGENT_ENABLE_LOG_CLEAN=0 render_log_clean kyagent",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# opt-in write operations: render_pkg_mgmt
# ---------------------------------------------------------------------------

def test_render_pkg_mgmt_enabled_outputs_correct_rules() -> None:
    require_usable_bash()
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source scripts/setup-sudoers.sh; "
            "KYAGENT_ENABLE_PKG_MGMT=1 render_pkg_mgmt kyagent",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "/usr/bin/dnf ^-y install [A-Za-z0-9._+-]+$" in out
    assert "/usr/bin/yum ^-y install [A-Za-z0-9._+-]+$" in out
    assert "/usr/bin/dnf ^-y update [A-Za-z0-9._+-]+$" in out
    assert "/usr/bin/yum ^-y update [A-Za-z0-9._+-]+$" in out
    assert "/usr/bin/dnf -y update" in out
    assert "/usr/bin/yum -y update" in out
    assert "/usr/bin/dnf -y update --security" in out
    assert "/usr/bin/yum -y update --security" in out
    assert "/usr/bin/dnf clean all" in out
    assert "/usr/bin/yum clean all" in out
    assert "^-y remove [A-Za-z0-9._+-]+$" not in out
    assert "^-y update .*$" not in out
    assert "Cmnd_Alias KY_PKG_MUTATE" in out
    assert "kyagent  ALL=(root)  NOPASSWD: KY_PKG_MUTATE" in out


def test_render_pkg_remove_allowlist_outputs_fixed_package_rules() -> None:
    require_usable_bash()
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source scripts/setup-sudoers.sh; "
            "KYAGENT_PKG_REMOVE_ALLOWLIST='telnet,ftp' render_pkg_remove_allowlist kyagent",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "/usr/bin/dnf -y remove telnet" in out
    assert "/usr/bin/yum -y remove ftp" in out
    assert "^-y remove [A-Za-z0-9._+-]+$" not in out
    assert "kyagent  ALL=(root)  NOPASSWD: KY_PKG_REMOVE" in out


def test_render_pkg_remove_allowlist_rejects_critical_packages() -> None:
    require_usable_bash()
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source scripts/setup-sudoers.sh; "
            "KYAGENT_PKG_REMOVE_ALLOWLIST='telnet,systemd' render_pkg_remove_allowlist kyagent",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )
    assert result.returncode != 0
    assert "systemd" in result.stderr


def test_render_pkg_mgmt_disabled_by_default_outputs_nothing() -> None:
    require_usable_bash()
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source scripts/setup-sudoers.sh; "
            "render_pkg_mgmt kyagent",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_setup_never_grants_wildcard_package_remove() -> None:
    script = SETUP_SUDOERS.read_text(encoding="utf-8")
    assert "^-y remove [A-Za-z0-9._+-]+$" not in script
    assert "KYAGENT_PKG_REMOVE_ALLOWLIST" in script


# ---------------------------------------------------------------------------
# opt-in write operations: render_proc_kill
# ---------------------------------------------------------------------------

def test_render_proc_kill_enabled_outputs_correct_rules() -> None:
    require_usable_bash()
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source scripts/setup-sudoers.sh; "
            "KYAGENT_ENABLE_PROC_KILL=1 render_proc_kill kyagent",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "/usr/bin/kill ^-(TERM|KILL|HUP|INT) [2-9][0-9]*$" in out
    assert "[0-9]+$" not in out
    assert "Cmnd_Alias KY_PROC_KILL" in out
    assert "kyagent  ALL=(root)  NOPASSWD: KY_PROC_KILL" in out


def test_render_proc_kill_disabled_by_default_outputs_nothing() -> None:
    require_usable_bash()
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source scripts/setup-sudoers.sh; "
            "render_proc_kill kyagent",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_setup_proc_kill_sudoers_excludes_pid_zero_and_one() -> None:
    script = SETUP_SUDOERS.read_text(encoding="utf-8")
    assert "/usr/bin/kill ^-(TERM|KILL|HUP|INT) [2-9][0-9]*$" in script
    assert "/usr/bin/kill ^-(TERM|KILL|HUP|INT) [0-9]+$" not in script


# ---------------------------------------------------------------------------
# default sudoers template must not contain any write-operation aliases
# ---------------------------------------------------------------------------

def test_default_sudoers_does_not_grant_write_operations() -> None:
    text = _sudoers()
    forbidden_strings = [
        "KY_LOG_CLEAN",
        "KY_PKG_MUTATE",
        "KY_PROC_KILL",
        "journalctl --vacuum",
        "dnf -y install",
        "dnf -y update",
        "yum -y update",
        "kyagent-file-delete",
        "/usr/bin/kill",
    ]
    for s in forbidden_strings:
        assert s not in text, (
            f"Default sudoers template must not contain write-operation string: {s!r}"
        )


# ---- 生产预设一键脚本 setup-sudoers-prod.sh -------------------------------

PROD_WRAPPER = Path(__file__).parents[1] / "scripts" / "setup-sudoers-prod.sh"
KYAGENT_SH = Path(__file__).parents[1] / "scripts" / "kyagent.sh"


def test_prod_wrapper_defaults_enable_all_write_switches() -> None:
    text = PROD_WRAPPER.read_text(encoding="utf-8")
    # 三个开关默认开（:-1），但允许已存在的环境变量覆盖
    assert 'KYAGENT_ENABLE_LOG_CLEAN="${KYAGENT_ENABLE_LOG_CLEAN:-1}"' in text
    assert 'KYAGENT_ENABLE_PKG_MGMT="${KYAGENT_ENABLE_PKG_MGMT:-1}"' in text
    assert 'KYAGENT_ENABLE_PROC_KILL="${KYAGENT_ENABLE_PROC_KILL:-1}"' in text
    assert 'KYAGENT_PKG_REMOVE_ALLOWLIST="${KYAGENT_PKG_REMOVE_ALLOWLIST:-}"' in text
    # 服务白名单默认值可被覆盖
    assert 'KYAGENT_SERVICE_ALLOWLIST="${KYAGENT_SERVICE_ALLOWLIST:-$DEFAULT_SERVICE_ALLOWLIST}"' in text


def test_prod_wrapper_does_not_enable_wildcard_package_remove_by_default() -> None:
    text = PROD_WRAPPER.read_text(encoding="utf-8")
    assert "install|remove <pkg>" not in text
    assert "dnf/yum install/update/security/clean-cache" in text
    assert "KYAGENT_PKG_REMOVE_ALLOWLIST" in text


def test_prod_wrapper_default_allowlist_has_common_services() -> None:
    text = PROD_WRAPPER.read_text(encoding="utf-8")
    for unit in ("nginx.service", "sshd.service", "firewalld.service",
                 "mariadb.service", "docker.service", "crond.service"):
        assert unit in text, f"production preset missing common service: {unit}"
    # 绝不预设核心服务（核心脚本会拒，但预设也不该列）
    for unit in ("systemd-logind", "dbus.service", "polkit.service"):
        assert unit not in text


def test_prod_wrapper_blocks_non_interactive_without_yes() -> None:
    require_usable_bash()
    result = subprocess.run(
        ["bash", "scripts/setup-sudoers-prod.sh"],
        check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=ROOT, stdin=subprocess.DEVNULL,
    )
    # 非交互且未加 --yes：守卫退出 1，绝不落地。
    assert result.returncode == 1
    # summary 横幅应在守卫之前打印（断言 ASCII token，避免跨平台子进程编码差异）。
    assert "sudoers" in result.stdout
    assert "systemctl restart/reload nginx.service" in result.stdout


def test_prod_wrapper_summary_reflects_switch_override() -> None:
    require_usable_bash()
    # 在 bash 内联设置变量（比 subprocess env= 在 git-bash 上更可靠）。
    result = subprocess.run(
        ["bash", "-c",
         "KYAGENT_ENABLE_PROC_KILL=0 "
         "KYAGENT_SERVICE_ALLOWLIST=nginx.service,redis.service "
         "bash scripts/setup-sudoers-prod.sh </dev/null"],
        check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=ROOT,
    )
    # 只断言 ASCII 稳定标记：proc_kill 关 → 摘要里不出现 kill 命令行；
    # 自定义白名单生效 → nginx/redis 在、被覆盖掉的默认 mariadb 不在。
    assert "kill -TERM" not in result.stdout
    assert "systemctl restart/reload nginx.service" in result.stdout
    assert "systemctl restart/reload redis.service" in result.stdout
    assert "mariadb.service" not in result.stdout


def test_prod_wrapper_summary_reflects_package_remove_allowlist() -> None:
    require_usable_bash()
    result = subprocess.run(
        ["bash", "-c",
         "KYAGENT_PKG_REMOVE_ALLOWLIST=telnet,ftp "
         "bash scripts/setup-sudoers-prod.sh </dev/null"],
        check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=ROOT,
    )
    assert "包卸载 allowlist: telnet,ftp" in result.stdout
    assert "install|remove <pkg>" not in result.stdout


def test_kyagent_sh_exposes_permissions_prod_subcommand() -> None:
    text = KYAGENT_SH.read_text(encoding="utf-8")
    assert "permissions-prod)" in text
    assert "setup-sudoers-prod.sh" in text


# ---------------------------------------------------------------------------
# OS 层日志清空包装器 kyagent-log-clean：路径穿越 / 符号链接语义校验
# ---------------------------------------------------------------------------

def _run_wrapper(path: str):
    return subprocess.run(
        [sys.executable, str(LOG_CLEAN_WRAPPER), path],
        check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def test_setup_installs_log_clean_wrapper_root_owned() -> None:
    script = SETUP_SUDOERS.read_text(encoding="utf-8")
    # 包装器以 root:root 0755 安装到 PATH 白名单内的 /usr/local/bin。
    assert 'install -m 0755 -o root -g root "$LOG_CLEAN_WRAPPER_SRC" "$LOG_CLEAN_WRAPPER_DST"' in script
    assert 'install -m 0755 -o root -g root "$FILE_DELETE_WRAPPER_SRC" "$FILE_DELETE_WRAPPER_DST"' in script
    assert 'LOG_CLEAN_WRAPPER_DST="/usr/local/bin/kyagent-log-clean"' in script
    assert 'FILE_DELETE_WRAPPER_DST="/usr/local/bin/kyagent-file-delete"' in script
    # 仅在日志清理开关打开时安装。
    assert '[[ "${KYAGENT_ENABLE_LOG_CLEAN:-}" != "1" ]] && return 0' in script


FILE_DELETE_WRAPPER = Path(__file__).parents[1] / "scripts" / "kyagent-file-delete"


def _run_delete_wrapper(path: str):
    return subprocess.run(
        [sys.executable, str(FILE_DELETE_WRAPPER), path],
        check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def test_delete_wrapper_rejects_traversal_out_of_allowed_roots() -> None:
    if sys.platform == "win32":
        pytest.skip("wrapper enforces POSIX realpath/O_NOFOLLOW semantics")
    result = _run_delete_wrapper("/tmp/../../etc/shadow")
    assert result.returncode != 0
    assert "越界" in result.stderr


def test_delete_wrapper_deletes_regular_file_in_allowed_root() -> None:
    if sys.platform == "win32":
        pytest.skip("wrapper enforces POSIX realpath/O_NOFOLLOW semantics")
    import tempfile

    fd, name = tempfile.mkstemp(prefix="kyagent_delete_", suffix=".log", dir="/tmp")
    try:
        with open(fd, "w") as fh:
            fh.write("noise")
        result = _run_delete_wrapper(name)
        assert result.returncode == 0, result.stderr
        assert not Path(name).exists()
    finally:
        Path(name).unlink(missing_ok=True)


def test_delete_wrapper_rejects_symlink_escaping_allowed_root() -> None:
    if sys.platform == "win32":
        pytest.skip("wrapper enforces POSIX realpath/O_NOFOLLOW semantics")
    import os
    import tempfile

    link = tempfile.mktemp(prefix="kyagent_delete_link_", dir="/tmp")
    os.symlink("/etc/passwd", link)
    try:
        result = _run_delete_wrapper(link)
        assert result.returncode != 0
        assert "越界" in result.stderr
    finally:
        Path(link).unlink(missing_ok=True)


def test_delete_wrapper_rejects_symlink_even_within_allowed_root() -> None:
    if sys.platform == "win32":
        pytest.skip("wrapper enforces POSIX realpath/O_NOFOLLOW semantics")
    import os
    import tempfile

    fd, target = tempfile.mkstemp(prefix="kyagent_delete_tgt_", dir="/tmp")
    os.close(fd)
    link = tempfile.mktemp(prefix="kyagent_delete_lnk_", dir="/tmp")
    os.symlink(target, link)
    try:
        result = _run_delete_wrapper(link)
        assert result.returncode != 0
        assert "打开失败" in result.stderr
    finally:
        Path(link).unlink(missing_ok=True)
        Path(target).unlink(missing_ok=True)


def test_wrapper_rejects_traversal_out_of_allowed_roots() -> None:
    if sys.platform == "win32":
        pytest.skip("wrapper enforces POSIX realpath/O_NOFOLLOW semantics")
    # 旧 sudoers 正则会放行的越界写：解析后落在 /etc，必须拒绝。
    result = _run_wrapper("/tmp/../../etc/shadow")
    assert result.returncode != 0
    assert "越界" in result.stderr


def test_wrapper_rejects_path_outside_allowed_roots() -> None:
    if sys.platform == "win32":
        pytest.skip("wrapper enforces POSIX realpath/O_NOFOLLOW semantics")
    result = _run_wrapper("/etc/passwd")
    assert result.returncode != 0
    assert "越界" in result.stderr


def test_wrapper_rejects_relative_path() -> None:
    if sys.platform == "win32":
        pytest.skip("wrapper enforces POSIX realpath/O_NOFOLLOW semantics")
    result = _run_wrapper("var/log/x")
    assert result.returncode != 0
    assert "绝对路径" in result.stderr


def test_wrapper_truncates_regular_file_in_allowed_root() -> None:
    if sys.platform == "win32":
        pytest.skip("wrapper enforces POSIX realpath/O_NOFOLLOW semantics")
    import tempfile
    # /tmp 是白名单根目录且 CI 上可写。
    fd, name = tempfile.mkstemp(prefix="kyagent_logclean_", suffix=".log", dir="/tmp")
    try:
        with open(fd, "w") as fh:
            fh.write("noise" * 100)
        assert Path(name).stat().st_size > 0
        result = _run_wrapper(name)
        assert result.returncode == 0, result.stderr
        assert Path(name).stat().st_size == 0
    finally:
        Path(name).unlink(missing_ok=True)


def test_wrapper_rejects_symlink_escaping_allowed_root() -> None:
    if sys.platform == "win32":
        pytest.skip("wrapper enforces POSIX realpath/O_NOFOLLOW semantics")
    import os
    import tempfile
    # /tmp 下的软链指向 allowed root 之外：realpath 解析后越界，拒绝。
    link = tempfile.mktemp(prefix="kyagent_logclean_link_", dir="/tmp")
    os.symlink("/etc/passwd", link)
    try:
        result = _run_wrapper(link)
        assert result.returncode != 0
        assert "越界" in result.stderr
    finally:
        Path(link).unlink(missing_ok=True)


def test_wrapper_rejects_symlink_even_within_allowed_root() -> None:
    if sys.platform == "win32":
        pytest.skip("wrapper enforces POSIX realpath/O_NOFOLLOW semantics")
    import os
    import tempfile
    # 末端组件是软链（即使指向 /tmp 内合法文件），O_NOFOLLOW 也必须拒绝，挡 TOCTOU。
    fd, target = tempfile.mkstemp(prefix="kyagent_logclean_tgt_", dir="/tmp")
    os.close(fd)
    link = tempfile.mktemp(prefix="kyagent_logclean_lnk_", dir="/tmp")
    os.symlink(target, link)
    try:
        result = _run_wrapper(link)
        assert result.returncode != 0
        assert "打开失败" in result.stderr
    finally:
        Path(link).unlink(missing_ok=True)
        Path(target).unlink(missing_ok=True)
