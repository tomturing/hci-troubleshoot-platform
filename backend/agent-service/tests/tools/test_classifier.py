"""
RiskClassifier 单元测试。

测试 classifier.py 中的 classify_acli / classify_bash / risk_to_policy 函数。
"""
from app.tools.acli.classifier import (
    classify_acli,
    classify_bash,
    risk_to_policy,
)


class TestClassifyAcli:
    """acli 命令风险分级测试"""

    # risk=1：只读操作
    def test_read_only_list(self):
        assert classify_acli("acli vm list --formatter json") == 1

    def test_read_only_get(self):
        assert classify_acli("acli platform info get") == 1

    def test_read_only_status(self):
        assert classify_acli("acli vm status get abc-123") == 1

    def test_read_only_check(self):
        assert classify_acli("acli vm disk check abc-123") == 1

    def test_read_only_help(self):
        assert classify_acli("acli vm --help") == 1

    # risk=2：写操作
    def test_service_restart(self):
        assert classify_acli("acli service asv redis restart") == 2

    def test_vm_start(self):
        assert classify_acli("acli vm start abc-123") == 2

    def test_vm_stop(self):
        assert classify_acli("acli vm stop abc-123") == 2

    def test_vm_shutdown(self):
        assert classify_acli("acli vm shutdown abc-123") == 2

    def test_vm_restart(self):
        assert classify_acli("acli vm restart abc-123") == 2

    def test_vm_suspend(self):
        assert classify_acli("acli vm suspend abc-123") == 2

    def test_vm_resume(self):
        assert classify_acli("acli vm resume abc-123") == 2

    def test_network_nic_up(self):
        assert classify_acli("acli network nic up eth0") == 2

    def test_network_nic_down(self):
        assert classify_acli("acli network nic down eth0") == 2

    def test_network_nic_set(self):
        assert classify_acli("acli network nic set eth0") == 2

    def test_vm_migrate(self):
        assert classify_acli("acli vm migrate abc-123") == 2

    def test_vm_clone(self):
        assert classify_acli("acli vm clone abc-123") == 2

    def test_vm_snapshot(self):
        assert classify_acli("acli vm snapshot abc-123") == 2

    def test_platform_start(self):
        assert classify_acli("acli platform node start") == 2

    def test_platform_stop(self):
        assert classify_acli("acli platform node stop") == 2

    # risk=3：破坏性操作
    def test_vm_delete(self):
        assert classify_acli("acli vm delete abc-123") == 3

    def test_storage_delete(self):
        assert classify_acli("acli storage asan volume delete vol-123") == 3

    def test_storage_remove(self):
        assert classify_acli("acli storage asan disk remove disk-123") == 3

    def test_storage_wipe(self):
        assert classify_acli("acli storage asan disk wipe disk-123") == 3

    def test_storage_format(self):
        assert classify_acli("acli storage asan disk format disk-123") == 3

    def test_storage_destroy(self):
        assert classify_acli("acli storage asan volume destroy vol-123") == 3

    def test_network_delete(self):
        assert classify_acli("acli network bond delete bond0") == 3

    def test_network_remove(self):
        assert classify_acli("acli network vrouter remove vrouter-123") == 3

    def test_system_rm(self):
        assert classify_acli("acli system rm /tmp/test") == 3

    # 边界测试
    def test_empty_string(self):
        assert classify_acli("") == 1

    def test_whitespace_only(self):
        assert classify_acli("   ") == 1

    def test_non_acli_command(self):
        # 不以 acli 开头的命令，视为只读
        assert classify_acli("ls -la") == 1

    def test_long_command(self):
        long_cmd = "acli vm list --formatter json --cluster cluster-1 --limit 1000 --offset 0 --filter 'name==test*'"
        assert classify_acli(long_cmd) == 1


class TestClassifyBash:
    """bash 命令风险分级测试"""

    # risk=1：只读操作
    def test_df(self):
        assert classify_bash("df -h /") == 1

    def test_free(self):
        assert classify_bash("free -h") == 1

    def test_ps(self):
        assert classify_bash("ps aux") == 1

    def test_ip_addr(self):
        assert classify_bash("ip addr") == 1

    def test_cat_log(self):
        assert classify_bash("cat /sf/log/vtpdaemon.log") == 1

    def test_grep(self):
        assert classify_bash("grep ERROR /sf/log/vtpdaemon.log | tail -50") == 1

    def test_tail(self):
        assert classify_bash("tail -n 100 /var/log/syslog") == 1

    def test_ls(self):
        assert classify_bash("ls -la /etc") == 1

    # risk=2：写操作
    def test_systemctl_restart(self):
        assert classify_bash("systemctl restart nginx") == 2

    def test_systemctl_stop(self):
        assert classify_bash("systemctl stop nginx") == 2

    def test_systemctl_start(self):
        assert classify_bash("systemctl start nginx") == 2

    def test_kill(self):
        assert classify_bash("kill -9 1234") == 2

    def test_killall(self):
        assert classify_bash("killall nginx") == 2

    def test_pkill(self):
        assert classify_bash("pkill -f nginx") == 2

    def test_chmod_777(self):
        assert classify_bash("chmod 777 /tmp/test") == 2

    def test_chmod_777_variant(self):
        assert classify_bash("chmod 0777 /tmp/test") == 2

    def test_append_write(self):
        assert classify_bash("echo 'test' >> /tmp/test.txt") == 2

    def test_redirect_write(self):
        assert classify_bash("echo 'test' > /tmp/test.txt") == 2

    def test_sed_inplace(self):
        assert classify_bash("sed -i 's/old/new/g' /tmp/test.txt") == 2

    def test_tee(self):
        assert classify_bash("echo 'test' | tee /tmp/test.txt") == 2

    # risk=3：破坏性操作
    def test_rm_rf(self):
        assert classify_bash("rm -rf /tmp/test") == 3

    def test_rm_rf_root(self):
        assert classify_bash("rm -rf /") == 3

    def test_rm_with_options(self):
        assert classify_bash("rm -rf --no-preserve-root /") == 3

    def test_mkfs(self):
        assert classify_bash("mkfs.ext4 /dev/sda") == 3

    def test_fdisk(self):
        assert classify_bash("fdisk /dev/sda") == 3

    def test_parted(self):
        assert classify_bash("parted /dev/sda") == 3

    def test_dd(self):
        assert classify_bash("dd if=/dev/zero of=/dev/sda") == 3

    def test_format(self):
        assert classify_bash("format /dev/sda") == 3

    def test_reboot(self):
        assert classify_bash("reboot") == 3

    def test_shutdown(self):
        assert classify_bash("shutdown") == 3

    def test_halt(self):
        assert classify_bash("halt") == 3

    def test_poweroff(self):
        assert classify_bash("poweroff") == 3

    def test_passwd(self):
        assert classify_bash("passwd") == 3

    def test_useradd(self):
        assert classify_bash("useradd test") == 3

    def test_userdel(self):
        assert classify_bash("userdel test") == 3

    def test_usermod(self):
        assert classify_bash("usermod -aG sudo test") == 3

    def test_visudo(self):
        assert classify_bash("visudo") == 3

    def test_sudoers_edit(self):
        assert classify_bash("echo 'test ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers") == 3

    # 边界测试
    def test_empty_string(self):
        assert classify_bash("") == 1

    def test_whitespace_only(self):
        assert classify_bash("   ") == 1

    def test_none_handling(self):
        # None 输入应返回 1（安全兜底）
        assert classify_bash(None) == 1

    def test_long_command(self):
        long_cmd = "grep 'ERROR' /sf/log/vtpdaemon.log | awk '{print $1,$2,$3}' | sort | uniq -c | sort -rn | head -20"
        assert classify_bash(long_cmd) == 1


class TestRiskToPolicy:
    """风险等级到策略映射测试"""

    def test_auto(self):
        assert risk_to_policy(1) == "auto"

    def test_confirm(self):
        assert risk_to_policy(2) == "confirm"

    def test_block(self):
        assert risk_to_policy(3) == "block"

    def test_invalid_risk(self):
        # 无效风险等级返回 block（安全兜底）
        assert risk_to_policy(0) == "block"
        assert risk_to_policy(4) == "block"
        assert risk_to_policy(-1) == "block"


class TestEdgeCases:
    """边界与特殊情况测试"""

    def test_acli_empty(self):
        assert classify_acli("") == 1

    def test_acli_whitespace(self):
        assert classify_acli("   ") == 1

    def test_acli_newlines(self):
        assert classify_acli("\n\n") == 1

    def test_bash_empty(self):
        assert classify_bash("") == 1

    def test_bash_whitespace(self):
        assert classify_bash("   ") == 1

    def test_bash_newlines(self):
        assert classify_bash("\n\n") == 1

    def test_acli_long_command(self):
        # 超长命令（1000 字符）
        long_cmd = "acli vm list " + "--option=value " * 100
        assert classify_acli(long_cmd) == 1

    def test_bash_long_command(self):
        # 超长命令（1000 字符）
        long_cmd = "grep 'pattern' /sf/log/test.log " + "| grep 'sub' " * 100
        assert classify_bash(long_cmd) == 1

    def test_acli_mixed_keywords(self):
        # 包含多个关键词，匹配第一个
        assert classify_acli("acli vm delete restart") == 3

    def test_bash_mixed_keywords(self):
        # 包含多个关键词，匹配第一个
        assert classify_bash("rm -rf && systemctl restart nginx") == 3

    def test_acli_case_insensitive_partial(self):
        # 正则匹配对大小写敏感（acli 命令通常小写）
        assert classify_acli("ACLI VM DELETE abc-123") == 1  # 大写不匹配

    def test_bash_case_insensitive(self):
        # bash 命令通常小写
        assert classify_bash("RM -RF /") == 1  # 大写不匹配
