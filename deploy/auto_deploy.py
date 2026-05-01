"""
OpenChart Pro 全自动部署脚本（paramiko）。
跑一次完成：本地打包 → scp 上传 → 远程解压 → setup.sh → 验证服务。
"""
import os
import sys
import io
import time
import tarfile
import getpass

import paramiko

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HOST = "43.165.185.243"
USER = "ubuntu"
# 临时密码（部署后会建议改）
PASSWORD = "XU8V7;Ln+-KGC(e"

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PKG_LOCAL = os.path.join(os.environ.get("TEMP", "/tmp"), "openchart-pkg.tar.gz")
PKG_REMOTE = "/home/ubuntu/openchart-pkg.tar.gz"

# 排除的目录/文件 pattern
EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules", "archive", ".venv", "venv"}
# 这些只在「项目根」下排除，避免误伤 backend/data 等同名子目录
EXCLUDE_TOP_DIRS = {"data", "logs"}
EXCLUDE_EXT = {".pyc", ".pyo", ".png", ".jpg", ".gif"}
EXCLUDE_NAMES_PREFIX = ("debug_", "diag_")
# 关键：永远不打包 .env（上面有 API Key、密码等，每次部署不能动远程的 .env）
# 2026-04-29 事故复盘：rsync --delete 同步代码时如果本地有空 .env，会覆盖远程真实 key
EXCLUDE_NAMES_EXACT = {".env", ".env.local", ".env.production"}


def banner(text):
    print()
    print("━" * 70)
    print(f"  {text}")
    print("━" * 70)


def step(text):
    print(f"\n▶ {text}")


def _excludes(name):
    base = os.path.basename(name)
    if base in EXCLUDE_DIRS:
        return True
    if base in EXCLUDE_NAMES_EXACT:
        return True
    for ext in EXCLUDE_EXT:
        if base.endswith(ext):
            return True
    for pre in EXCLUDE_NAMES_PREFIX:
        if base.startswith(pre):
            return True
    return False


def make_tarball():
    step(f"打包项目 → {PKG_LOCAL}")
    if os.path.exists(PKG_LOCAL):
        os.remove(PKG_LOCAL)

    def filter_(tarinfo):
        rel = tarinfo.name.lstrip("./")
        parts = rel.split("/") if rel else []
        # 全局排除（任意层级）
        for p in parts:
            if p in EXCLUDE_DIRS:
                return None
            if p.startswith("debug_") or p.startswith("diag_"):
                return None
        # 仅排除「项目根」下的同名目录（防止误伤 backend/data 等）
        if parts and parts[0] in EXCLUDE_TOP_DIRS:
            return None
        # 排除根目录的 png/jpg
        if len(parts) == 1:
            for ext in EXCLUDE_EXT:
                if rel.endswith(ext):
                    return None
        return tarinfo

    with tarfile.open(PKG_LOCAL, "w:gz", compresslevel=6) as tar:
        tar.add(PROJECT_ROOT, arcname=".", filter=filter_)
    size_mb = os.path.getsize(PKG_LOCAL) / 1024 / 1024
    print(f"  ✓ 打包完成 {size_mb:.1f} MB")


def ssh_connect(retry=4):
    step(f"SSH 连接 {USER}@{HOST}")
    last_err = None
    for i in range(retry):
        try:
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cli.connect(HOST, username=USER, password=PASSWORD, timeout=30,
                        allow_agent=False, look_for_keys=False, banner_timeout=30,
                        auth_timeout=30)
            tr = cli.get_transport()
            tr.set_keepalive(15)
            print("  ✓ 已连接")
            return cli
        except Exception as e:
            last_err = e
            wait = 8 * (i + 1)
            print(f"  ✗ 第{i+1}次失败：{e}；等待 {wait}s 重试")
            time.sleep(wait)
    raise last_err


def _upload_sftp(cli, file_size):
    """SFTP 上传（首选）。"""
    sent_box = [0]
    last_box = [0]

    def _cb(sent, total):
        sent_box[0] = sent
        if sent - last_box[0] > 2 * 1024 * 1024 or sent == total:
            pct = sent * 100 / total if total else 0
            print(f"  上传进度(SFTP): {sent/1024/1024:.1f}/{total/1024/1024:.1f} MB ({pct:.0f}%)")
            last_box[0] = sent

    sftp = cli.open_sftp()
    try:
        sftp.put(PKG_LOCAL, PKG_REMOTE, callback=_cb, confirm=True)
    finally:
        sftp.close()


def _upload_chunked(cli, file_size):
    """分片备用：每片 1MB，每片独立 exec/cat 通道，失败重试。"""
    print("  ⚠ SFTP 失败，回退分片 exec/cat 模式")
    CHUNK = 1024 * 1024  # 1MB
    n_chunks = (file_size + CHUNK - 1) // CHUNK
    # 远端先清空目标文件
    rc, _ = run_remote(cli, f": > {PKG_REMOTE}")
    if rc != 0:
        raise IOError("无法清空远端文件")
    sent = 0
    with open(PKG_LOCAL, "rb") as f:
        for i in range(n_chunks):
            data = f.read(CHUNK)
            for attempt in range(3):
                try:
                    chan = cli.get_transport().open_session()
                    chan.settimeout(120)
                    chan.exec_command(f"cat >> {PKG_REMOTE}")
                    view = memoryview(data)
                    while view:
                        n = chan.send(view)
                        if n == 0:
                            raise IOError("Channel send 0")
                        view = view[n:]
                    chan.shutdown_write()
                    rc = chan.recv_exit_status()
                    if rc != 0:
                        raise IOError(f"远端 cat 退出 {rc}")
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    print(f"    片 {i+1}/{n_chunks} 失败 ({e})，重试 {attempt+1}")
                    time.sleep(1.5)
            sent += len(data)
            if (i + 1) % 4 == 0 or i == n_chunks - 1:
                pct = sent * 100 / file_size
                print(f"  上传进度(分片): {sent/1024/1024:.1f}/{file_size/1024/1024:.1f} MB ({pct:.0f}%)")


def upload(cli):
    step(f"上传 tar.gz → {PKG_REMOTE}")
    file_size = os.path.getsize(PKG_LOCAL)
    try:
        _upload_sftp(cli, file_size)
    except Exception as e:
        print(f"  SFTP 异常: {e}")
        _upload_chunked(cli, file_size)
    print("  ✓ 上传完成")
    rc, out = run_remote(cli, f"stat -c %s {PKG_REMOTE}")
    remote_size = int(out.strip().split()[-1] if out.strip() else 0)
    if remote_size != file_size:
        raise IOError(f"远程文件大小不匹配 local={file_size} remote={remote_size}")
    print(f"  ✓ 远程文件校验通过 ({remote_size} bytes)")


def run_remote(cli, cmd, timeout=900, sudo_password=None):
    """执行远程命令，流式输出 stdout/stderr。返回 (rc, out, err)。"""
    print(f"  $ {cmd[:120]}{'...' if len(cmd) > 120 else ''}")
    chan = cli.get_transport().open_session()
    chan.settimeout(timeout)
    chan.get_pty()
    chan.exec_command(cmd)
    if sudo_password:
        chan.send(sudo_password + "\n")
    out_chunks = []
    while True:
        if chan.recv_ready():
            data = chan.recv(4096).decode("utf-8", errors="replace")
            if data:
                # 实时打印
                sys.stdout.write(data)
                sys.stdout.flush()
                out_chunks.append(data)
        if chan.recv_stderr_ready():
            data = chan.recv_stderr(4096).decode("utf-8", errors="replace")
            if data:
                sys.stdout.write(data)
                sys.stdout.flush()
                out_chunks.append(data)
        if chan.exit_status_ready():
            # 把剩余的读完
            while chan.recv_ready():
                out_chunks.append(chan.recv(4096).decode("utf-8", errors="replace"))
            while chan.recv_stderr_ready():
                out_chunks.append(chan.recv_stderr(4096).decode("utf-8", errors="replace"))
            break
        time.sleep(0.05)
    rc = chan.recv_exit_status()
    return rc, "".join(out_chunks)


def deploy(cli):
    # 1) 解压
    step("远程解压 + 准备目录")
    rc, _ = run_remote(cli, "rm -rf ~/openchart-pkg && mkdir -p ~/openchart-pkg && tar -xzf ~/openchart-pkg.tar.gz -C ~/openchart-pkg && ls ~/openchart-pkg/run.py")
    if rc != 0:
        print("  ✗ 解压失败")
        return False
    print("  ✓ 解压完成")

    # 2) 跑 setup.sh（需要 sudo 密码 = ssh 密码）
    step("执行 setup.sh（apt + Python venv + systemd + 防火墙 + cron，5-10 分钟）")
    # 用 echo password | sudo -S 传递 sudo 密码
    cmd = f"echo '{PASSWORD}' | sudo -S bash ~/openchart-pkg/deploy/setup.sh 2>&1"
    rc, out = run_remote(cli, cmd, timeout=1200)
    if rc != 0:
        print(f"\n  ✗ setup.sh 失败 rc={rc}")
        return False
    print("\n  ✓ setup.sh 完成")
    return True


def verify(cli):
    step("验证服务状态")
    rc, _ = run_remote(cli, "systemctl is-active openchart && curl -s -o /dev/null -w 'HTTP %{http_code}\\n' http://127.0.0.1:8000/api/health")
    return rc == 0


def main():
    banner("OpenChart Pro 自动部署到腾讯云轻量服务器")
    print(f"  目标: {USER}@{HOST}")
    print(f"  项目: {PROJECT_ROOT}")

    make_tarball()
    cli = ssh_connect()
    try:
        upload(cli)
        ok = deploy(cli)
        if not ok:
            print("\n✗ 部署失败")
            sys.exit(1)
        verify(cli)
    finally:
        cli.close()

    banner("✅ 部署完成")
    print(f"  访问: http://{HOST}:8000")
    print()
    print("  下一步：")
    print(f"  1) ssh {USER}@{HOST}")
    print(f"  2) sudo vim /opt/openchart/.env  填 DEEPSEEK_API_KEY=xxx")
    print(f"  3) sudo systemctl restart openchart")
    print(f"  4) journalctl -u openchart -f")


if __name__ == "__main__":
    main()
