from __future__ import annotations

import datetime
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path


class Sandbox:
    """基于linux写时拷贝沙箱实现，本质上是实现了不需要镜像的 docker 子集，只适用于 linux

    - OverlayFS: linux的分层文件系统，分为三层，借此实现写时拷贝
    - Mount Namespace: 挂载隔离，让沙箱拥有独立挂载树
    - Copy on Write: 写时拷贝
    - Pivot Root: 切换根目录，让每个沙箱都有独立的根目录
    """

    def __init__(
        self,
        sandbox_id: str | None = None,
        rw_paths: list[str] | None = None,
        ro_paths: list[str] | None = None,
        hide_paths: list[str] | None = None,
        base_tmp_dir: str = "/tmp/sandbox",
    ):
        self.sandbox_id = sandbox_id or str(uuid.uuid4())[:8]

        self.rw_paths = [
            os.path.abspath(os.path.expanduser(p)) for p in (rw_paths or [])
        ]
        self.ro_paths = [
            os.path.abspath(os.path.expanduser(p)) for p in (ro_paths or [])
        ]
        self.hide_paths = [
            os.path.abspath(os.path.expanduser(p)) for p in (hide_paths or [])
        ]

        self.sandbox_root = Path(base_tmp_dir) / self.sandbox_id
        self.upper_path = str(self.sandbox_root / "upper")
        self.work_path = str(self.sandbox_root / "work")
        self.merged_path = str(self.sandbox_root / "merged")

        self._memory_history: list[dict[str, str]] = []

        self._unshare_proc: subprocess.Popen | None = None
        self.target_pid: int | None = None

    def _build_init_script(self) -> str:
        lines = ["set -e", ""]
        lines.append(
            f"mount -t overlay overlay -o lowerdir=/,upperdir={shlex.quote(self.upper_path)},workdir={shlex.quote(self.work_path)} {shlex.quote(self.merged_path)}"
        )

        ops = (
            [(len(Path(p).parts), "hide", p) for p in self.hide_paths]
            + [(len(Path(p).parts), "rw", p) for p in self.rw_paths]
            + [(len(Path(p).parts), "ro", p) for p in self.ro_paths]
        )
        ops.sort(key=lambda o: o[0])

        for _, kind, p in ops:
            target = self.merged_path + p
            qt = shlex.quote(target)
            if kind == "hide":
                lines.append(
                    f"if [ -d {shlex.quote(p)} ]; then mkdir -p {qt} && mount -t tmpfs tmpfs {qt}; elif [ -f {shlex.quote(p)} ]; then mount --bind /dev/null {qt}; fi"
                )
            elif kind == "rw":
                lines.append(f"mkdir -p {qt} && mount --bind {shlex.quote(p)} {qt}")
            elif kind == "ro":
                lines.append(
                    f"mkdir -p {qt} && mount --bind {shlex.quote(p)} {qt} && mount -o remount,bind,ro {qt}"
                )

        for mnt in ["/proc", "/sys", "/dev", "/run"]:
            lines.append(
                f"mount --rbind {mnt} {shlex.quote(self.merged_path + mnt)} && mount --make-rslave {shlex.quote(self.merged_path + mnt)}"
            )

        lines.append(
            f"cd {shlex.quote(self.merged_path)} && mkdir -p .pivot_old && pivot_root . .pivot_old && umount -l /.pivot_old && rmdir /.pivot_old"
        )

        lines.append("exec sleep infinity")
        return "\n".join(lines)

    def start(self) -> None:
        for d in (self.upper_path, self.work_path, self.merged_path):
            os.makedirs(d, exist_ok=True)

        script = self._build_init_script()

        unshare_cmd = ["unshare", "--mount", "--fork", "--", "/bin/bash", "-c", script]
        self._unshare_proc = subprocess.Popen(
            unshare_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        time.sleep(0.5)
        try:
            output = subprocess.check_output(
                ["pgrep", "-P", str(self._unshare_proc.pid)], text=True
            )
            self.target_pid = int(output.strip().split()[0])
        except Exception:
            self.target_pid = self._unshare_proc.pid

        print(f"沙箱 [{self.sandbox_id}] 初始化成功！")
        print(f"沙箱基础设施守护 PID: {self.target_pid}")

    def exec_command(
        self, command: list[str], run_as_user: str | None = None
    ) -> subprocess.CompletedProcess:
        if not self.target_pid:
            raise RuntimeError("沙箱尚未成功启动！")

        nsenter_cmd = ["nsenter", "-t", str(self.target_pid), "-m"]
        user_script = "cd / && " + " ".join(command)

        if run_as_user and run_as_user != "root":
            final_cmd = nsenter_cmd + [
                "runuser",
                "-u",
                run_as_user,
                "--",
                "sh",
                "-c",
                user_script,
            ]
        else:
            final_cmd = nsenter_cmd + ["sh", "-c", user_script]

        result = subprocess.run(
            final_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

        self._record_to_memory(command, run_as_user or "root", result)

        return result

    def _record_to_memory(
        self, command: list[str], user: str, result: subprocess.CompletedProcess
    ) -> None:
        """内部方法：格式化并记录每次命令的输出快照到内存"""
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 拼接出该命令产生的完整标准输出/错误内容
        output_buffer = []
        if result.stdout:
            output_buffer.append(result.stdout)
        if result.stderr:
            output_buffer.append(f"[STDERR]: {result.stderr}")
        if not result.stdout and not result.stderr:
            output_buffer.append("[No Output]\n")

        full_output = "".join(output_buffer)

        # 存入内存字典结构，极大方便后续 view 的多维度筛选
        self._memory_history.append(
            {
                "timestamp": timestamp,
                "user": user,
                "command": " ".join(command),
                "output": full_output,
            }
        )

    def view(self, keyword: str | None = None) -> str:
        if not self._memory_history:
            return "暂无任何命令执行记录。"

        output_lines = []

        for record in self._memory_history:
            # 格式化每条历史记录的基本头信息
            header = f"[{record['timestamp']}] [USER: {record['user']}] [CMD]: {record['command']}\n"
            body = record["output"]
            footer = "-" * 50 + "\n"

            # 如果没有关键词，直接合并整块输出
            if not keyword:
                output_lines.append(header + body + footer)
            else:
                # 如果指定了关键词，对这一块的所有内容（包括头、命令、输出内容）做过滤
                full_block = header + body
                if keyword.lower() in full_block.lower():
                    # 进阶筛选：如果你只想保留匹配的那几行，也可以在这里对 body.splitlines() 进行过滤
                    output_lines.append(header + body + footer)

        if not output_lines:
            return f"--- 内存中未找到包含关键词 '{keyword}' 的日志 ---"

        return "".join(output_lines)

    def stop(self) -> None:
        if self._unshare_proc:
            self._unshare_proc.terminate()
            self._unshare_proc.wait()

        subprocess.run(["umount", "-R", self.merged_path], stderr=subprocess.DEVNULL)
        shutil.rmtree(self.sandbox_root, ignore_errors=True)
        # 显式清空内存，防止内存泄漏
        self._memory_history.clear()
        print(f"沙箱 [{self.sandbox_id}] 已安全销毁并清理。")


if __name__ == "__main__":
    if os.getuid() != 0:
        print("错误：必须使用 root 权限运行", file=sys.stderr)
        sys.exit(1)

    box = Sandbox()
    box.start()

    try:
        print("\n--- 1. 执行命令（输出全部进入 Python 内存） ---")
        box.exec_command(
            ["echo 'Error: high database temperature detected!'"], run_as_user="root"
        )
        box.exec_command(
            ["echo 'System status: running smoothly'"], run_as_user="nobody"
        )
        box.exec_command(
            ["echo 'Error: out of disk space on /dev/sda1'"], run_as_user="www-data"
        )

        print("\n--- 2. 测试 view()：全内存读取全部输出 ---")
        print("=" * 60)
        print(box.view())
        print("=" * 60)

        print("\n--- 3. 测试 view(keyword='Error')：纯内存快速过滤筛选 ---")
        print("=" * 60)
        print(box.view(keyword="Error"))
        print("=" * 60)

    finally:
        print("\n--- 4. 销毁沙箱 ---")
        box.stop()
