"""Smoke tests for the v2 tool expansion (19 -> 92).

All tests stay strictly STATIC:

  * Only ``Tool.validate()`` + ``Tool.build_argv()`` are exercised — no
    subprocess, no network, no filesystem write.
  * ``default_registry()`` is loaded once per session via a fixture so module
    import stays under 1 s (no heavy work at import time).
  * Negative paths assert ``ToolError`` for the kinds of inputs a malicious
    or hallucinating LLM is most likely to emit: shell metacharacters in
    user-supplied strings, relative paths, etc.

The goal is to lock in the contract for the 73 newly-added tools so future
edits cannot regress argv shape, JSON-Schema strictness, or registry
membership without a loud test failure.
"""
from __future__ import annotations

import pytest

from kyagent.mcp.tools import default_registry
from kyagent.mcp.tools.base import Tool, ToolError, TrendTool
from kyagent.mcp.tools.disk import DiskIoStatsTool
from kyagent.mcp.tools.pkg_family import PkgFamily, set_pkg_family_for_tests
from kyagent.mcp.tools import (
    process as process_mod,
    service as service_mod,
    network as network_mod,
    logs as logs_mod,
    package as package_mod,
    disk as disk_mod,
    system as system_mod,
    security as security_mod,
    compliance as compliance_mod,
    loongarch as loongarch_mod,
)
from kyagent.safety.write_preflight import (
    PathMetadata,
    WritePreflightDecision,
    WritePreflightResult,
)


# ---- fixtures --------------------------------------------------------------


@pytest.fixture(scope="session")
def registry():
    return default_registry()


@pytest.fixture
def rpm_family(monkeypatch):
    """Pin pkg_family() to RPM for the duration of one test."""
    monkeypatch.setattr(package_mod, "_detect_rpm_frontend", lambda: "dnf")
    set_pkg_family_for_tests(PkgFamily.RPM)
    yield PkgFamily.RPM
    set_pkg_family_for_tests(None)


@pytest.fixture
def dpkg_family():
    set_pkg_family_for_tests(PkgFamily.DPKG)
    yield PkgFamily.DPKG
    set_pkg_family_for_tests(None)


@pytest.fixture
def unknown_family():
    set_pkg_family_for_tests(PkgFamily.UNKNOWN)
    yield PkgFamily.UNKNOWN
    set_pkg_family_for_tests(None)


def _argv(tool: Tool, args: dict) -> list[str]:
    """Validate + build_argv helper to mirror the real pipeline."""
    return tool.build_argv(tool.validate(args))


# ---- 1. process extensions (new in v2: +5) --------------------------------


class TestProcessExtensions:
    def test_process_zombies_argv_default(self):
        t = process_mod.ProcessZombiesTool()
        argv = _argv(t, {})
        assert argv[0] == "ps"
        assert "stat,pid,ppid,user,comm" in argv

    def test_process_tree_argv_default(self):
        t = process_mod.ProcessTreeTool()
        argv = _argv(t, {})
        assert "--forest" in argv

    def test_process_tree_argv_with_user(self):
        t = process_mod.ProcessTreeTool()
        argv = _argv(t, {"user": "kyagent"})
        assert "-u" in argv and "kyagent" in argv

    def test_process_fd_count_argv(self):
        t = process_mod.ProcessFdCountTool()
        argv = _argv(t, {"pid": 4321})
        assert argv == ["ls", "-1", "/proc/4321/fd"]

    def test_process_fd_count_rejects_zero_pid(self):
        t = process_mod.ProcessFdCountTool()
        with pytest.raises(ToolError):
            t.validate({"pid": 0})

    def test_process_resource_argv(self):
        t = process_mod.ProcessResourceTool()
        argv = _argv(t, {"pid": 1})
        assert argv == ["cat", "/proc/1/status"]

    def test_process_resource_rejects_missing_pid(self):
        t = process_mod.ProcessResourceTool()
        with pytest.raises(ToolError):
            t.validate({})

    def test_top_cpu_snapshot_argv(self):
        t = process_mod.TopCpuSnapshotTool()
        argv = _argv(t, {})
        assert argv[0] == "top"
        assert "-bn1" in argv


# ---- 2. service extensions (new in v2: +8) --------------------------------


class TestServiceExtensions:
    def test_svc_is_active_argv(self):
        t = service_mod.SvcIsActiveTool()
        argv = _argv(t, {"unit": "sshd.service"})
        assert argv == ["systemctl", "is-active", "--", "sshd.service"]

    def test_svc_is_active_rejects_metachar(self):
        t = service_mod.SvcIsActiveTool()
        with pytest.raises(ToolError):
            t.validate({"unit": "sshd;rm -rf /"})

    def test_svc_is_active_rejects_space(self):
        t = service_mod.SvcIsActiveTool()
        with pytest.raises(ToolError):
            # Pattern forbids spaces
            t.validate({"unit": "sshd nginx"})

    def test_svc_is_enabled_argv(self):
        t = service_mod.SvcIsEnabledTool()
        argv = _argv(t, {"unit": "nginx.service"})
        assert "is-enabled" in argv and "nginx.service" in argv

    def test_svc_show_argv(self):
        t = service_mod.SvcShowTool()
        argv = _argv(t, {"unit": "nginx.service"})
        assert "show" in argv and "nginx.service" in argv

    def test_svc_cat_argv(self):
        t = service_mod.SvcCatTool()
        argv = _argv(t, {"unit": "nginx.service"})
        assert "cat" in argv and "nginx.service" in argv

    def test_svc_failed_argv(self):
        t = service_mod.SvcFailedTool()
        argv = _argv(t, {})
        assert "--failed" in argv

    def test_svc_timers_argv(self):
        t = service_mod.SvcTimersTool()
        argv = _argv(t, {})
        assert "list-timers" in argv and "--all" in argv

    def test_boot_analyze_argv(self):
        t = service_mod.BootAnalyzeTool()
        argv = _argv(t, {})
        assert argv[:2] == ["systemd-analyze", "blame"]

    def test_boot_logs_argv_default(self):
        t = service_mod.BootLogsTool()
        argv = _argv(t, {})
        assert "journalctl" in argv and "-b" in argv

    def test_boot_logs_argv_with_offset(self):
        t = service_mod.BootLogsTool()
        argv = _argv(t, {"boot_offset": -2})
        assert "-2" in argv

    def test_boot_logs_rejects_positive_offset(self):
        t = service_mod.BootLogsTool()
        with pytest.raises(ToolError):
            t.validate({"boot_offset": 1})

    def test_svc_show_rejects_forbidden_unit(self):
        t = service_mod.SvcShowTool()
        with pytest.raises(ToolError):
            _argv(t, {"unit": "systemd-logind"})


# ---- 3. network extensions (new in v2: +9) --------------------------------


class TestNetworkExtensions:
    def test_net_routes_argv(self):
        argv = _argv(network_mod.NetRoutesTool(), {})
        assert argv == ["ip", "-j", "route"]

    def test_net_arp_argv(self):
        argv = _argv(network_mod.NetArpTool(), {})
        assert argv == ["ip", "-j", "neigh"]

    def test_net_link_stats_argv_no_iface(self):
        argv = _argv(network_mod.NetLinkStatsTool(), {})
        assert argv == ["ip", "-s", "-j", "link"]

    def test_net_link_stats_argv_with_iface(self):
        argv = _argv(network_mod.NetLinkStatsTool(), {"iface": "eth0"})
        assert "show" in argv and "dev" in argv and "eth0" in argv

    def test_net_link_stats_rejects_metachar_iface(self):
        t = network_mod.NetLinkStatsTool()
        with pytest.raises(ToolError):
            t.validate({"iface": "eth0;ls"})

    def test_net_addr_argv(self):
        argv = _argv(network_mod.NetAddrTool(), {})
        assert argv == ["ip", "-j", "addr"]

    def test_net_firewall_iptables_argv(self):
        argv = _argv(network_mod.NetFirewallIptablesTool(), {})
        assert argv[:2] == ["iptables", "-L"]

    def test_net_firewall_nft_argv(self):
        argv = _argv(network_mod.NetFirewallNftTool(), {})
        assert argv == ["nft", "list", "ruleset"]

    def test_net_conn_state_summary_argv(self):
        argv = _argv(network_mod.NetConnStateSummaryTool(), {})
        assert argv == ["ss", "-ant"]

    def test_net_dns_resolve_argv(self):
        argv = _argv(network_mod.NetDnsResolveTool(), {"name": "kylinos.cn"})
        assert argv == ["getent", "hosts", "--", "kylinos.cn"]

    def test_net_dns_resolve_rejects_dollar(self):
        t = network_mod.NetDnsResolveTool()
        with pytest.raises(ToolError):
            t.validate({"name": "foo$bar"})

    def test_net_dns_resolve_rejects_semicolon(self):
        t = network_mod.NetDnsResolveTool()
        with pytest.raises(ToolError):
            t.validate({"name": "foo;rm"})

    def test_net_dns_resolve_rejects_missing_name(self):
        t = network_mod.NetDnsResolveTool()
        with pytest.raises(ToolError):
            t.validate({})

    def test_net_tcp_stats_argv(self):
        argv = _argv(network_mod.NetTcpStatsTool(), {})
        assert argv == ["ss", "-s"]


# ---- 4. logs extensions (new in v2: +7) -----------------------------------


class TestLogsExtensions:
    def test_log_files_top_argv(self):
        argv = _argv(logs_mod.LogFilesTopTool(), {})
        assert argv[0] == "find" and "/var/log" in argv and "+1M" in argv

    def test_log_size_sample_argv(self):
        argv = _argv(logs_mod.LogSizeSampleTool(), {"paths": ["/var/log/messages"]})
        assert argv[:3] == ["du", "-sb", "--"]
        assert "/var/log/messages" in argv

    def test_log_size_sample_rejects_empty_array(self):
        t = logs_mod.LogSizeSampleTool()
        with pytest.raises(ToolError):
            t.validate({"paths": []})

    def test_log_size_sample_rejects_non_array(self):
        t = logs_mod.LogSizeSampleTool()
        with pytest.raises(ToolError):
            t.validate({"paths": "/var/log/messages"})

    def test_log_grep_recent_argv(self):
        t = logs_mod.LogGrepRecentTool()
        argv = _argv(t, {"pattern": "Failed password"})
        assert "journalctl" in argv and "--grep" in argv and "Failed password" in argv

    def test_log_grep_recent_rejects_backtick(self):
        t = logs_mod.LogGrepRecentTool()
        with pytest.raises(ToolError):
            t.validate({"pattern": "evil `id`"})

    def test_log_grep_recent_rejects_dollar(self):
        t = logs_mod.LogGrepRecentTool()
        with pytest.raises(ToolError):
            t.validate({"pattern": "$(rm -rf /)"})

    def test_log_grep_recent_rejects_semicolon(self):
        t = logs_mod.LogGrepRecentTool()
        with pytest.raises(ToolError):
            t.validate({"pattern": "ok;evil"})

    def test_log_ssh_audit_argv(self):
        argv = _argv(logs_mod.LogSshAuditTool(), {})
        assert "-u" in argv and "sshd.service" in argv

    def test_log_auth_failed_argv(self):
        argv = _argv(logs_mod.LogAuthFailedTool(), {})
        assert "--grep" in argv
        assert any("Failed password" in tok for tok in argv)

    def test_log_audit_summary_argv(self):
        argv = _argv(logs_mod.LogAuditSummaryTool(), {})
        assert argv == ["aureport", "--summary"]

    def test_log_rotated_count_argv(self):
        argv = _argv(logs_mod.LogRotatedCountTool(), {})
        assert argv[0] == "find" and "*.gz" in argv


# ---- 5. package family mixin (new in v2: +6 PkgFamily tools) --------------


class TestPackageMixin:
    def test_pkg_verify_rpm_argv(self, rpm_family):
        t = package_mod.PkgVerifyTool()
        argv = _argv(t, {"name": "openssh-server"})
        assert argv == ["rpm", "-V", "--", "openssh-server"]

    def test_pkg_verify_dpkg_argv(self, dpkg_family):
        t = package_mod.PkgVerifyTool()
        argv = _argv(t, {"name": "openssh-server"})
        assert argv == ["debsums", "-c", "--", "openssh-server"]

    def test_pkg_verify_unknown_raises(self, unknown_family):
        t = package_mod.PkgVerifyTool()
        with pytest.raises(ToolError):
            _argv(t, {"name": "openssh-server"})

    def test_pkg_verify_rejects_metachar(self, rpm_family):
        t = package_mod.PkgVerifyTool()
        with pytest.raises(ToolError):
            t.validate({"name": "evil;rm"})

    def test_pkg_updates_rpm_argv(self, rpm_family):
        argv = _argv(package_mod.PkgUpdatesTool(), {})
        assert argv == ["dnf", "check-update", "--quiet"]

    def test_pkg_updates_dpkg_argv(self, dpkg_family):
        argv = _argv(package_mod.PkgUpdatesTool(), {})
        assert argv == ["apt", "list", "--upgradable"]

    def test_pkg_security_updates_rpm_argv(self, rpm_family):
        argv = _argv(package_mod.PkgSecurityUpdatesTool(), {})
        assert argv == ["dnf", "updateinfo", "list", "security"]

    def test_pkg_security_updates_dpkg_argv(self, dpkg_family):
        argv = _argv(package_mod.PkgSecurityUpdatesTool(), {})
        # dpkg has no native security flag; falls back to upgradable list
        assert "apt" in argv and "--upgradable" in argv

    def test_pkg_owns_file_rpm_argv(self, rpm_family):
        argv = _argv(package_mod.PkgOwnsFileTool(), {"path": "/usr/bin/ssh"})
        assert argv == ["rpm", "-qf", "--", "/usr/bin/ssh"]

    def test_pkg_owns_file_dpkg_argv(self, dpkg_family):
        argv = _argv(package_mod.PkgOwnsFileTool(), {"path": "/usr/bin/ssh"})
        assert argv == ["dpkg", "-S", "--", "/usr/bin/ssh"]

    def test_pkg_owns_file_rejects_relative(self, rpm_family):
        t = package_mod.PkgOwnsFileTool()
        with pytest.raises(ToolError):
            t.validate({"path": "usr/bin/ssh"})

    def test_pkg_repo_list_rpm_argv(self, rpm_family):
        argv = _argv(package_mod.PkgRepoListTool(), {})
        assert argv == ["dnf", "repolist"]

    def test_pkg_repo_list_dpkg_argv(self, dpkg_family):
        argv = _argv(package_mod.PkgRepoListTool(), {})
        assert argv == ["apt-cache", "policy"]

    def test_pkg_history_rpm_argv(self, rpm_family):
        argv = _argv(package_mod.PkgHistoryTool(), {"limit": 5})
        assert argv == ["dnf", "history", "list"]

    def test_pkg_history_dpkg_argv(self, dpkg_family):
        argv = _argv(package_mod.PkgHistoryTool(), {"limit": 12})
        assert argv == ["tail", "-n", "12", "/var/log/apt/history.log"]


# ---- 6. disk tools (new in v2: +7) ----------------------------------------


class TestDiskTools:
    def test_disk_io_stats_is_trend_tool(self):
        assert issubclass(DiskIoStatsTool, TrendTool)

    def test_disk_io_stats_argv_default(self):
        argv = _argv(DiskIoStatsTool(), {})
        # iostat -dx 1 2 by default
        assert argv == ["iostat", "-dx", "1", "2"]

    def test_disk_io_stats_argv_custom(self):
        argv = _argv(DiskIoStatsTool(), {"interval": 2, "count": 3})
        assert argv == ["iostat", "-dx", "2", "3"]

    def test_disk_io_stats_rejects_zero_count(self):
        t = DiskIoStatsTool()
        with pytest.raises(ToolError):
            t.validate({"count": 1})  # min is 2

    def test_disk_io_diskstats_argv(self):
        argv = _argv(disk_mod.DiskIoDiskstatsTool(), {})
        assert argv == ["cat", "/proc/diskstats"]

    def test_disk_inode_usage_argv_default(self):
        argv = _argv(disk_mod.DiskInodeUsageTool(), {})
        assert argv == ["df", "-i"]

    def test_disk_inode_usage_argv_path(self):
        argv = _argv(disk_mod.DiskInodeUsageTool(), {"path": "/var"})
        assert argv == ["df", "-i", "--", "/var"]

    def test_disk_inode_usage_rejects_relative(self):
        t = disk_mod.DiskInodeUsageTool()
        with pytest.raises(ToolError):
            t.validate({"path": "var"})

    def test_disk_open_deleted_argv(self):
        argv = _argv(disk_mod.DiskOpenDeletedTool(), {})
        assert argv == ["lsof", "-nP", "+L1"]

    def test_disk_mount_argv_default(self):
        argv = _argv(disk_mod.DiskMountTool(), {})
        assert argv == ["findmnt", "-J"]

    def test_disk_smart_argv(self):
        argv = _argv(disk_mod.DiskSmartTool(), {"device": "/dev/sda"})
        assert argv == ["smartctl", "-H", "-A", "--", "/dev/sda"]

    def test_disk_smart_rejects_non_dev_path(self):
        t = disk_mod.DiskSmartTool()
        with pytest.raises(ToolError):
            t.validate({"device": "/tmp/fake"})

    def test_dir_largest_files_argv(self):
        t = disk_mod.DirLargestFilesTool()
        argv = _argv(t, {"path": "/var/lib"})
        assert argv[0] == "find" and "/var/lib" in argv and "+100M" in argv


# ---- 7. system tools (new in v2: +9) --------------------------------------


class TestSystemTools:
    def test_sys_uptime_argv(self):
        assert _argv(system_mod.SysUptimeTool(), {}) == ["uptime"]

    def test_sys_loadavg_argv(self):
        assert _argv(system_mod.SysLoadavgTool(), {}) == ["cat", "/proc/loadavg"]

    def test_sys_memory_argv(self):
        assert _argv(system_mod.SysMemoryTool(), {}) == ["free", "-h"]

    def test_sys_swap_argv(self):
        assert _argv(system_mod.SysSwapTool(), {}) == ["swapon", "--show"]

    def test_sys_kernel_argv(self):
        assert _argv(system_mod.SysKernelTool(), {}) == ["uname", "-a"]

    def test_sys_cpu_info_argv(self):
        assert _argv(system_mod.SysCpuInfoTool(), {}) == ["lscpu"]

    def test_sys_dmi_argv(self):
        argv = _argv(system_mod.SysDmiTool(), {})
        assert argv[0] == "dmidecode"

    def test_sys_dmi_requires_root(self):
        assert system_mod.SysDmiTool().requires_root is True

    def test_sys_time_sync_argv(self):
        assert _argv(system_mod.SysTimeSyncTool(), {}) == ["timedatectl"]

    def test_sys_block_devices_argv(self):
        assert _argv(system_mod.SysBlockDevicesTool(), {}) == ["lsblk", "-J"]


# ---- 8. security tools (new in v2: +13) -----------------------------------


class TestSecurityTools:
    def test_sec_selinux_status_argv(self):
        assert _argv(security_mod.SecSelinuxStatusTool(), {}) == ["sestatus"]

    def test_sec_apparmor_status_argv(self):
        assert _argv(security_mod.SecApparmorStatusTool(), {}) == ["aa-status"]

    def test_sec_kysec_status_argv(self):
        argv = _argv(security_mod.SecKysecStatusTool(), {})
        assert argv == ["cat", "/sys/kernel/security/kysec/state"]

    def test_sec_kysec_status_is_low_risk_no_root(self):
        t = security_mod.SecKysecStatusTool()
        assert t.requires_root is False
        assert t.read_only is True

    def test_sec_setuid_files_argv(self):
        argv = _argv(security_mod.SecSetuidFilesTool(), {})
        assert "-perm" in argv and "-4000" in argv

    def test_sec_world_writable_argv(self):
        argv = _argv(security_mod.SecWorldWritableTool(), {})
        assert "-perm" in argv and "-0002" in argv

    def test_sec_capabilities_argv_default(self):
        argv = _argv(security_mod.SecCapabilitiesTool(), {})
        assert argv == ["getcap", "-r", "/usr"]

    def test_sec_capabilities_argv_custom_path(self):
        argv = _argv(security_mod.SecCapabilitiesTool(), {"path": "/opt"})
        assert argv == ["getcap", "-r", "/opt"]

    def test_sec_passwd_audit_argv(self):
        argv = _argv(security_mod.SecPasswdAuditTool(), {})
        assert argv == ["getent", "passwd"]

    def test_sec_sudoers_audit_argv(self):
        argv = _argv(security_mod.SecSudoersAuditTool(), {"user": "kyagent"})
        assert argv == ["sudo", "-l", "-U", "kyagent"]

    def test_sec_sudoers_audit_rejects_slash_in_user(self):
        t = security_mod.SecSudoersAuditTool()
        with pytest.raises(ToolError):
            t.validate({"user": "ky/agent"})

    def test_sec_sudoers_audit_rejects_uppercase_user(self):
        t = security_mod.SecSudoersAuditTool()
        with pytest.raises(ToolError):
            t.validate({"user": "Root"})

    def test_sec_ssh_config_argv(self):
        argv = _argv(security_mod.SecSshConfigTool(), {})
        assert argv == ["cat", "/etc/ssh/sshd_config"]

    def test_sec_kernel_taints_argv(self):
        argv = _argv(security_mod.SecKernelTaintsTool(), {})
        assert argv == ["cat", "/proc/sys/kernel/tainted"]

    def test_sec_kernel_modules_argv(self):
        argv = _argv(security_mod.SecKernelModulesTool(), {})
        assert argv == ["lsmod"]

    def test_sec_listening_external_argv(self):
        argv = _argv(security_mod.SecListeningExternalTool(), {})
        assert argv == ["ss", "-tlnp"]

    def test_sec_audit_status_argv(self):
        argv = _argv(security_mod.SecAuditStatusTool(), {})
        assert argv == ["systemctl", "is-active", "auditd"]


# ---- 9. compliance tools (new in v2: +6) ----------------------------------


class TestComplianceTools:
    def test_compl_aide_check_argv(self):
        argv = _argv(compliance_mod.ComplAideCheckTool(), {})
        assert argv == ["aide", "--check", "--config", "/etc/aide.conf"]

    def test_compl_file_attr_argv(self):
        argv = _argv(compliance_mod.ComplFileAttrTool(), {"path": "/etc/passwd"})
        assert argv == ["lsattr", "--", "/etc/passwd"]

    def test_compl_file_attr_rejects_relative(self):
        t = compliance_mod.ComplFileAttrTool()
        with pytest.raises(ToolError):
            t.validate({"path": "etc/passwd"})

    def test_compl_file_hash_argv(self):
        argv = _argv(compliance_mod.ComplFileHashTool(), {"path": "/etc/passwd"})
        assert argv == ["sha256sum", "--", "/etc/passwd"]

    def test_compl_file_hash_rejects_shell_metachar(self):
        t = compliance_mod.ComplFileHashTool()
        with pytest.raises(ToolError):
            t.validate({"path": "/etc/passwd;rm"})

    def test_compl_timestamp_audit_argv(self):
        argv = _argv(
            compliance_mod.ComplTimestampAuditTool(),
            {"paths": ["/etc/passwd", "/etc/shadow"]},
        )
        assert argv[0] == "stat"
        assert "/etc/passwd" in argv and "/etc/shadow" in argv

    def test_compl_timestamp_audit_rejects_relative(self):
        t = compliance_mod.ComplTimestampAuditTool()
        with pytest.raises(ToolError):
            # build_argv re-validates list elements
            t.build_argv(t.validate({"paths": ["etc/passwd"]}))

    def test_compl_hosts_argv(self):
        assert _argv(compliance_mod.ComplHostsTool(), {}) == ["cat", "/etc/hosts"]

    def test_compl_cron_dump_argv_system(self):
        argv = _argv(compliance_mod.ComplCronDumpTool(), {})
        assert argv == ["cat", "/etc/crontab"]

    def test_compl_user_cron_dump_argv(self):
        argv = _argv(compliance_mod.ComplUserCronDumpTool(), {"user": "kyagent"})
        assert argv == ["crontab", "-l", "-u", "kyagent"]

    def test_compl_cron_dump_rejects_user(self):
        t = compliance_mod.ComplCronDumpTool()
        with pytest.raises(ToolError):
            t.validate({"user": "kyagent"})

    def test_compl_user_cron_dump_rejects_bad_user(self):
        t = compliance_mod.ComplUserCronDumpTool()
        with pytest.raises(ToolError):
            t.validate({"user": "bad;user"})


# ---- 10. LoongArch专属 (+3) -----------------------------------------------


class TestLoongArchTools:
    def test_la_arch_info_argv(self):
        argv = _argv(loongarch_mod.LaArchInfoTool(), {})
        assert argv == ["cat", "/proc/cpuinfo"]

    def test_la_world_check_argv_contains_ld_linux_loongarch(self):
        argv = _argv(loongarch_mod.LaWorldCheckTool(), {})
        joined = " ".join(argv)
        assert "ld-linux-loongarch" in joined
        assert argv[0] == "ls"

    def test_la_binary_compat_argv(self):
        argv = _argv(loongarch_mod.LaBinaryCompatTool(), {"path": "/bin/ls"})
        assert argv == ["file", "--", "/bin/ls"]

    def test_la_binary_compat_rejects_relative(self):
        t = loongarch_mod.LaBinaryCompatTool()
        with pytest.raises(ToolError):
            t.validate({"path": "bin/ls"})


# ---- 11. registry-wide invariants -----------------------------------------


class TestRegistry:
    def test_default_registry_has_133_tools(self, registry):
        assert len(registry.names()) == 133

    def test_all_tools_have_description(self, registry):
        bad = [
            t.name for t in registry.all()
            if not (t.description and len(t.description) >= 5)
        ]
        assert not bad, f"tools without good description: {bad}"

    def test_all_tools_have_schema_type_object(self, registry):
        bad = [
            t.name for t in registry.all()
            if t.input_schema.get("type") != "object"
        ]
        assert not bad, f"tools with non-object schema: {bad}"

    def test_no_tool_argv_starts_with_shell(self, registry):
        """Sample many no-arg tools and confirm none of them invoke a shell.

        Some tools require args; skipping those keeps this static. The point
        is: nothing should be dispatched through ``sh -c`` / ``bash -c``.
        """
        forbidden = {"sh", "bash", "/bin/sh", "/bin/bash", "/usr/bin/sh", "/usr/bin/bash"}
        sampled = 0
        for t in registry.all():
            # Skip interactive ask_user_choice (no real argv) and any tool with required args.
            if t.name == "ask_user_choice":
                continue
            required = t.input_schema.get("required") or []
            if required:
                continue
            try:
                argv = t.build_argv(t.validate({}))
            except Exception:
                continue
            assert argv, f"{t.name} produced empty argv"
            assert argv[0] not in forbidden, f"{t.name} dispatches via shell: {argv[0]}"
            sampled += 1
        # Make sure we actually exercised a reasonable chunk of the registry.
        assert sampled >= 30, f"too few argv-checkable tools sampled ({sampled})"

    def test_registry_names_are_unique(self, registry):
        names = registry.names()
        assert len(names) == len(set(names))

    def test_new_domain_tools_are_registered(self, registry):
        """Spot-check that representative tools from each new domain are present."""
        expected = {
            "process_zombies",      # process ext
            "svc_show", "svc_cat",  # service ext
            "net_dns_resolve",      # network ext
            "log_grep_recent", "log_files_top",  # logs ext
            "pkg_verify",           # package mixin
            "disk_io_stats", "disk_inode_usage",  # disk
            "sys_uptime", "sys_kernel",           # system
            "sec_kysec_status", "sec_sudoers_audit",  # security
            "compl_file_hash", "compl_aide_check",    # compliance
            "la_arch_info", "la_world_check", "la_binary_compat",  # loongarch
            "log_vacuum", "log_delete_file", "process_kill",
            "pkg_install", "pkg_update", "pkg_reinstall", "pkg_update_all",
            "pkg_security_upgrade", "pkg_clean_cache", "pkg_rebuild_db",
            "pkg_remove", "fs_truncate", "fs_delete_file",  # write ops
            "git_status", "git_diff", "git_show",
            "web_fetch_url", "osv_query_package", "github_issue_search",
            "verify_pytest", "verify_ruff", "verify_script_syntax",
            "docx_extract_text", "xlsx_list_sheets", "pdf_extract_text", "ocr_image_text",
            "plan_list", "plan_get",
            "lock_inspect", "lock_remove_stale",
            "unix_socket_inspect", "unix_socket_remove_stale",
            "cron_d_list", "cron_d_read", "cron_entry_trace", "cron_d_disable",
            "log_dir_repair_permissions",
        }
        missing = expected - set(registry.names())
        assert not missing, f"missing tools: {missing}"


# ---- P1 regression: params declared in schema must actually take effect ----


def _make_exec_result(stdout: str, returncode: int = 0):
    from kyagent.executor.proxy import ExecutionResult
    return ExecutionResult(
        argv=[],
        returncode=returncode,
        stdout=stdout,
        stderr="",
        truncated=False,
        duration=0.0,
    )


class TestP1ParamEffect:
    """P1 regressions: declared input params must visibly affect output."""

    def test_process_list_limit_is_respected(self):
        t = process_mod.PsListTool()
        # 50 data rows + 1 header
        rows = ["USER  PID  CPU"] + [f"root  {i}   0.1" for i in range(50)]
        t.validate({"limit": 5})
        result = t.format_result(_make_exec_result("\n".join(rows)))
        assert result.ok
        body_lines = result.content.splitlines()[1:]  # skip header
        assert len(body_lines) == 5, f"expected 5 rows, got {len(body_lines)}"

    def test_process_list_default_limit_is_20(self):
        t = process_mod.PsListTool()
        rows = ["USER  PID  CPU"] + [f"root  {i}   0.1" for i in range(50)]
        t.validate({})
        result = t.format_result(_make_exec_result("\n".join(rows)))
        body_lines = result.content.splitlines()[1:]
        assert len(body_lines) == 20

    def test_process_zombies_limit_is_respected(self):
        t = process_mod.ProcessZombiesTool()
        rows = ["STAT PID PPID USER COMM"] + [f"Zs  {i}  1  root  defunct" for i in range(40)]
        t.validate({"limit": 10})
        result = t.format_result(_make_exec_result("\n".join(rows)))
        body_lines = [ln for ln in result.content.splitlines() if ln.lstrip().startswith("Z")]
        assert len(body_lines) == 10

    def test_top_cpu_snapshot_limit_is_respected(self):
        t = process_mod.TopCpuSnapshotTool()
        rows = [f"line{i}" for i in range(50)]
        t.validate({"limit": 8})
        result = t.format_result(_make_exec_result("\n".join(rows)))
        assert len(result.content.splitlines()) == 8

    def test_log_dmesg_lines_is_respected(self):
        t = logs_mod.DmesgTool()
        content = "\n".join(f"[  {i}.0] kernel msg" for i in range(300))
        t.validate({"lines": 50})
        result = t.format_result(_make_exec_result(content))
        assert result.ok
        assert len(result.content.splitlines()) == 50

    def test_log_dmesg_default_lines_is_200(self):
        t = logs_mod.DmesgTool()
        content = "\n".join(f"[  {i}.0] kernel msg" for i in range(300))
        t.validate({})
        result = t.format_result(_make_exec_result(content))
        assert len(result.content.splitlines()) == 200

    def test_log_journal_priority_enum_rejects_invalid(self):
        t = logs_mod.JournalctlTool()
        with pytest.raises(ToolError):
            t.validate({"priority": "critical"})  # not in enum (correct is "crit")

    def test_log_journal_priority_enum_rejects_random(self):
        t = logs_mod.JournalctlTool()
        with pytest.raises(ToolError):
            t.validate({"priority": "high"})

    def test_log_journal_priority_valid_passes(self):
        t = logs_mod.JournalctlTool()
        for p in ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"):
            cleaned = t.validate({"priority": p})
            assert cleaned["priority"] == p

    def test_log_journal_priority_in_argv(self):
        t = logs_mod.JournalctlTool()
        argv = _argv(t, {"priority": "err"})
        assert "-p" in argv
        idx = argv.index("-p")
        assert argv[idx + 1] == "err"

    def test_log_journal_no_priority_flag_when_omitted(self):
        t = logs_mod.JournalctlTool()
        argv = _argv(t, {})
        assert "-p" not in argv


# ---- 12. write operation tools (new: +5) ------------------------------------

from kyagent.mcp.tools import filesystem as filesystem_mod


class TestWriteOperationTools:
    # ---- LogVacuumTool ----

    def test_log_vacuum_argv_max_size(self):
        t = logs_mod.LogVacuumTool()
        argv = _argv(t, {"max_size": "500M"})
        assert argv == ["journalctl", "--vacuum-size=500M"]

    def test_log_vacuum_argv_max_age(self):
        t = logs_mod.LogVacuumTool()
        argv = _argv(t, {"max_age": "2weeks"})
        assert argv == ["journalctl", "--vacuum-time=2weeks"]

    def test_log_vacuum_rejects_no_params(self):
        t = logs_mod.LogVacuumTool()
        with pytest.raises(ToolError):
            t.validate({})

    def test_log_vacuum_rejects_both_params(self):
        t = logs_mod.LogVacuumTool()
        with pytest.raises(ToolError):
            t.validate({"max_size": "500M", "max_age": "2weeks"})

    def test_log_vacuum_rejects_bad_size_pattern(self):
        t = logs_mod.LogVacuumTool()
        with pytest.raises(ToolError):
            t.validate({"max_size": "500MB"})  # not matching ^[0-9]+[KMGT]$

    def test_log_vacuum_read_only_is_false(self):
        assert logs_mod.LogVacuumTool().read_only is False

    def test_log_vacuum_requires_root(self):
        assert logs_mod.LogVacuumTool().requires_root is True

    # ---- ProcessKillTool ----

    def test_process_kill_argv_default_signal(self):
        t = process_mod.ProcessKillTool()
        argv = _argv(t, {"pid": 1234})
        assert argv == ["kill", "-TERM", "1234"]

    def test_process_kill_argv_kill_signal(self):
        t = process_mod.ProcessKillTool()
        argv = _argv(t, {"pid": 999, "signal": "KILL"})
        assert argv == ["kill", "-KILL", "999"]

    def test_process_kill_argv_hup_signal(self):
        t = process_mod.ProcessKillTool()
        argv = _argv(t, {"pid": 500, "signal": "HUP"})
        assert argv == ["kill", "-HUP", "500"]

    def test_process_kill_rejects_pid_less_than_2(self):
        t = process_mod.ProcessKillTool()
        with pytest.raises(ToolError):
            t.validate({"pid": 1})

    def test_process_kill_rejects_pid_zero(self):
        t = process_mod.ProcessKillTool()
        with pytest.raises(ToolError):
            t.validate({"pid": 0})

    def test_process_kill_rejects_invalid_signal(self):
        t = process_mod.ProcessKillTool()
        with pytest.raises(ToolError):
            t.validate({"pid": 100, "signal": "SIGKILL"})

    def test_process_kill_read_only_is_false(self):
        assert process_mod.ProcessKillTool().read_only is False

    def test_process_kill_requires_root(self):
        assert process_mod.ProcessKillTool().requires_root is True

    # ---- PkgInstallTool ----

    def test_pkg_install_argv(self, rpm_family):
        t = package_mod.PkgInstallTool()
        argv = _argv(t, {"name": "htop"})
        assert argv == ["dnf", "-y", "install", "htop"]

    def test_pkg_install_rejects_illegal_name(self):
        t = package_mod.PkgInstallTool()
        with pytest.raises(ToolError):
            t.validate({"name": "htop; rm -rf /"})

    def test_pkg_install_rejects_too_long_name(self):
        t = package_mod.PkgInstallTool()
        with pytest.raises(ToolError):
            t.validate({"name": "a" * 101})

    def test_pkg_install_read_only_is_false(self):
        assert package_mod.PkgInstallTool().read_only is False

    def test_pkg_install_requires_root(self):
        assert package_mod.PkgInstallTool().requires_root is True

    # ---- PkgUpdateTool ----

    def test_pkg_update_argv(self, rpm_family):
        t = package_mod.PkgUpdateTool()
        argv = _argv(t, {"name": "openssl"})
        assert argv == ["dnf", "-y", "update", "openssl"]

    def test_pkg_update_rejects_illegal_name(self):
        t = package_mod.PkgUpdateTool()
        with pytest.raises(ToolError):
            t.validate({"name": "openssl; rm -rf /"})

    def test_pkg_update_rejects_space(self):
        t = package_mod.PkgUpdateTool()
        with pytest.raises(ToolError):
            t.validate({"name": "open ssl"})

    def test_pkg_update_read_only_is_false(self):
        assert package_mod.PkgUpdateTool().read_only is False

    def test_pkg_update_requires_root(self):
        assert package_mod.PkgUpdateTool().requires_root is True

    # ---- PkgReinstallTool ----

    def test_pkg_reinstall_argv(self, rpm_family):
        t = package_mod.PkgReinstallTool()
        argv = _argv(t, {"name": "openssl"})
        assert argv == ["dnf", "-y", "reinstall", "openssl"]

    def test_pkg_reinstall_rejects_illegal_name(self):
        t = package_mod.PkgReinstallTool()
        with pytest.raises(ToolError):
            t.validate({"name": "openssl; rm -rf /"})

    def test_pkg_reinstall_rejects_space(self):
        t = package_mod.PkgReinstallTool()
        with pytest.raises(ToolError):
            t.validate({"name": "open ssl"})

    def test_pkg_reinstall_read_only_is_false(self):
        assert package_mod.PkgReinstallTool().read_only is False

    def test_pkg_reinstall_requires_root(self):
        assert package_mod.PkgReinstallTool().requires_root is True

    # ---- PkgUpdateAllTool ----

    def test_pkg_update_all_argv(self, rpm_family):
        t = package_mod.PkgUpdateAllTool()
        assert _argv(t, {}) == ["dnf", "-y", "update"]

    def test_pkg_update_all_rejects_extra_args(self):
        t = package_mod.PkgUpdateAllTool()
        with pytest.raises(ToolError):
            t.validate({"name": "openssl"})

    def test_pkg_update_all_read_only_is_false(self):
        assert package_mod.PkgUpdateAllTool().read_only is False

    def test_pkg_update_all_requires_root(self):
        assert package_mod.PkgUpdateAllTool().requires_root is True

    # ---- PkgSecurityUpgradeTool ----

    def test_pkg_security_upgrade_argv(self, rpm_family):
        t = package_mod.PkgSecurityUpgradeTool()
        assert _argv(t, {}) == ["dnf", "-y", "update", "--security"]

    def test_pkg_security_upgrade_rejects_extra_args(self):
        t = package_mod.PkgSecurityUpgradeTool()
        with pytest.raises(ToolError):
            t.validate({"name": "openssl"})

    def test_pkg_security_upgrade_read_only_is_false(self):
        assert package_mod.PkgSecurityUpgradeTool().read_only is False

    def test_pkg_security_upgrade_requires_root(self):
        assert package_mod.PkgSecurityUpgradeTool().requires_root is True

    # ---- PkgCleanCacheTool ----

    def test_pkg_clean_cache_argv(self, rpm_family):
        t = package_mod.PkgCleanCacheTool()
        assert _argv(t, {}) == ["dnf", "clean", "all"]

    def test_pkg_clean_cache_rejects_extra_args(self):
        t = package_mod.PkgCleanCacheTool()
        with pytest.raises(ToolError):
            t.validate({"name": "openssl"})

    def test_pkg_clean_cache_read_only_is_false(self):
        assert package_mod.PkgCleanCacheTool().read_only is False

    def test_pkg_clean_cache_requires_root(self):
        assert package_mod.PkgCleanCacheTool().requires_root is True

    # ---- PkgRebuildDbTool ----

    def test_pkg_rebuild_db_argv(self, rpm_family):
        t = package_mod.PkgRebuildDbTool()
        assert _argv(t, {}) == ["rpm", "--rebuilddb"]

    def test_pkg_rebuild_db_rejects_extra_args(self, rpm_family):
        t = package_mod.PkgRebuildDbTool()
        with pytest.raises(ToolError):
            t.validate({"name": "rpm"})

    def test_pkg_rebuild_db_dpkg_raises(self, dpkg_family):
        t = package_mod.PkgRebuildDbTool()
        with pytest.raises(ToolError):
            _argv(t, {})

    def test_pkg_rebuild_db_read_only_is_false(self):
        assert package_mod.PkgRebuildDbTool().read_only is False

    def test_pkg_rebuild_db_requires_root(self):
        assert package_mod.PkgRebuildDbTool().requires_root is True

    # ---- PkgRemoveTool ----

    def test_pkg_remove_argv(self, rpm_family):
        t = package_mod.PkgRemoveTool()
        argv = _argv(t, {"name": "telnet"})
        assert argv == ["dnf", "-y", "remove", "telnet"]

    def test_pkg_remove_rejects_illegal_name(self):
        t = package_mod.PkgRemoveTool()
        with pytest.raises(ToolError):
            t.validate({"name": "telnet|evil"})

    def test_pkg_remove_rejects_critical_package_names(self):
        t = package_mod.PkgRemoveTool()
        for name in ("kernel", "systemd", "glibc", "openssh-server", "sudo"):
            with pytest.raises(ToolError):
                t.validate({"name": name})

    def test_pkg_remove_read_only_is_false(self):
        assert package_mod.PkgRemoveTool().read_only is False

    def test_pkg_remove_requires_root(self):
        assert package_mod.PkgRemoveTool().requires_root is True

    # ---- FsTruncateTool ----

    def test_fs_truncate_argv(self, monkeypatch):
        monkeypatch.setattr(
            filesystem_mod,
            "classify_write_preflight",
            lambda path, *, operation: WritePreflightResult(
                WritePreflightDecision.ALLOW_CONFIRM,
                "old-rotated-log",
                "test",
            ),
        )
        t = filesystem_mod.FsTruncateTool()
        argv = _argv(t, {"path": "/var/log/kyagent-test-old.1"})
        assert argv == ["kyagent-log-clean", "/var/log/kyagent-test-old.1"]

    def test_fs_truncate_argv_tmp(self, monkeypatch):
        monkeypatch.setattr(
            filesystem_mod,
            "classify_write_preflight",
            lambda path, *, operation: WritePreflightResult(
                WritePreflightDecision.ALLOW_CONFIRM,
                "temp-build-residual",
                "test",
            ),
        )
        t = filesystem_mod.FsTruncateTool()
        argv = _argv(t, {"path": "/tmp/build-myapp/output.tmp"})
        assert argv == ["kyagent-log-clean", "/tmp/build-myapp/output.tmp"]

    def test_fs_truncate_rejects_out_of_bounds_path(self):
        t = filesystem_mod.FsTruncateTool()
        with pytest.raises(ToolError):
            t.validate({"path": "/etc/passwd"})

    def test_fs_truncate_rejects_home_path(self):
        t = filesystem_mod.FsTruncateTool()
        with pytest.raises(ToolError):
            t.validate({"path": "/home/user/app.log"})

    def test_fs_truncate_rejects_metachar_path(self):
        t = filesystem_mod.FsTruncateTool()
        with pytest.raises(ToolError):
            t.validate({"path": "/var/log/app;rm"})

    def test_fs_truncate_rejects_active_log(self):
        t = filesystem_mod.FsTruncateTool()
        with pytest.raises(ToolError, match="active-log"):
            t.validate({"path": "/var/log/app.log"})

    def test_fs_truncate_rejects_unclassified_log_name(self, monkeypatch):
        monkeypatch.setattr(
            filesystem_mod,
            "classify_write_preflight",
            lambda path, *, operation: WritePreflightResult(
                WritePreflightDecision.DENY,
                "unclassified-cleanup-target",
                "test",
            ),
        )
        t = filesystem_mod.FsTruncateTool()
        with pytest.raises(ToolError, match="unclassified-cleanup-target"):
            t.validate({"path": "/var/log/messages"})

    def test_fs_truncate_read_only_is_false(self):
        assert filesystem_mod.FsTruncateTool().read_only is False

    def test_fs_truncate_requires_root(self):
        assert filesystem_mod.FsTruncateTool().requires_root is True

    # ---- FsDeleteFileTool ----

    def test_fs_delete_file_argv(self, monkeypatch):
        monkeypatch.setattr(
            filesystem_mod,
            "classify_write_preflight",
            lambda path, *, operation: WritePreflightResult(
                WritePreflightDecision.ALLOW_CONFIRM,
                "cache-target",
                "test",
            ),
        )
        t = filesystem_mod.FsDeleteFileTool()
        argv = _argv(t, {"path": "/var/cache/app.tmp"})
        assert argv == ["kyagent-file-delete", "/var/cache/app.tmp"]

    def test_fs_delete_file_argv_tmp(self, monkeypatch):
        monkeypatch.setattr(
            filesystem_mod,
            "classify_write_preflight",
            lambda path, *, operation: WritePreflightResult(
                WritePreflightDecision.ALLOW_CONFIRM,
                "temp-build-residual",
                "test",
            ),
        )
        t = filesystem_mod.FsDeleteFileTool()
        argv = _argv(t, {"path": "/tmp/kyagent-build/output.tmp"})
        assert argv == ["kyagent-file-delete", "/tmp/kyagent-build/output.tmp"]

    def test_fs_delete_file_rejects_out_of_bounds_path(self):
        t = filesystem_mod.FsDeleteFileTool()
        with pytest.raises(ToolError):
            t.validate({"path": "/etc/passwd"})

    def test_fs_delete_file_rejects_metachar_path(self):
        t = filesystem_mod.FsDeleteFileTool()
        with pytest.raises(ToolError):
            t.validate({"path": "/var/log/app;rm"})

    def test_fs_delete_file_rejects_audit_log(self):
        t = filesystem_mod.FsDeleteFileTool()
        with pytest.raises(ToolError, match="audit-log"):
            t.validate({"path": "/var/log/audit/audit.log.1"})

    def test_fs_delete_file_read_only_is_false(self):
        assert filesystem_mod.FsDeleteFileTool().read_only is False

    def test_fs_delete_file_requires_root(self):
        assert filesystem_mod.FsDeleteFileTool().requires_root is True

    # ---- LogDeleteFileTool ----

    def test_log_delete_file_argv(self, monkeypatch):
        monkeypatch.setattr(
            logs_mod,
            "classify_write_preflight",
            lambda path, *, operation: WritePreflightResult(
                WritePreflightDecision.ALLOW_CONFIRM,
                "old-rotated-log",
                "test",
            ),
        )
        t = logs_mod.LogDeleteFileTool()
        argv = _argv(t, {"path": "/var/log/kyagent-test-old.1"})
        assert argv == ["kyagent-file-delete", "/var/log/kyagent-test-old.1"]

    def test_log_delete_file_rejects_plain_active_log(self):
        t = logs_mod.LogDeleteFileTool()
        with pytest.raises(ToolError, match="active-log"):
            t.validate({"path": "/var/log/myapp.log"})

    def test_log_delete_file_rejects_auth_log(self):
        t = logs_mod.LogDeleteFileTool()
        with pytest.raises(ToolError, match="auth-log"):
            t.validate({"path": "/var/log/auth.log.1"})

    def test_log_delete_file_rejects_database_log(self):
        t = logs_mod.LogDeleteFileTool()
        with pytest.raises(ToolError, match="database-log"):
            t.validate({"path": "/var/log/mysql-bin.000001"})

    def test_log_delete_file_rejects_non_log_suffix(self):
        t = logs_mod.LogDeleteFileTool()
        with pytest.raises(ToolError):
            t.validate({"path": "/var/log/messages"})

    def test_log_delete_file_rejects_tmp_path(self):
        t = logs_mod.LogDeleteFileTool()
        with pytest.raises(ToolError):
            t.validate({"path": "/tmp/myapp.log"})

    def test_log_delete_file_rejects_metachar_path(self):
        t = logs_mod.LogDeleteFileTool()
        with pytest.raises(ToolError):
            t.validate({"path": "/var/log/app.log;rm"})

    def test_log_delete_file_read_only_is_false(self):
        assert logs_mod.LogDeleteFileTool().read_only is False

    def test_log_delete_file_requires_root(self):
        assert logs_mod.LogDeleteFileTool().requires_root is True
