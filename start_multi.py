# -*- coding: utf-8 -*-
"""多账号实例启动器：一份代码，按账号起多个互相独立的实例。

每个实例 = 独立进程 + 独立端口 + 独立数据目录。
数据目录里各有一份 cookies.txt / cache.json / learned_answers.json /
ui_config.json，所以账号之间完全不共享登录态、题库与界面配置。

用法:
    python start_multi.py          # 起 3 个实例（端口 5000/5001/5002）
    python start_multi.py 2        # 起 2 个
    python start_multi.py 3 5100   # 起 3 个，从 5100 端口开始
    python start_multi.py --one 4  # 只起 acc4（网关的「新增账号」用这个）

实例数也可用环境变量 CK_INSTANCES 指定（命令行参数优先）。
网关总览页的「+ 新增账号」按钮就是调用 `--one <n>` 来加实例的。

注意：多个实例同时跑时，每个实例的"并发章节数"建议设 1，
否则同一 IP 的并发请求数叠加上去容易触发平台进度回溯。
"""
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.join(os.path.dirname(BASE), "chaoxing_data")
DEFAULT_COUNT = 3
BASE_PORT = 5000

# 让实例彻底脱离父进程：父窗口关掉/被回收时实例仍继续跑
#   · DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP：脱离控制台与进程组
#   · CREATE_BREAKAWAY_FROM_JOB：从父进程所在的 Windows Job 里脱离
#     （某些宿主会给子进程套 Job Object，父进程被清理时子进程会被一起干掉，
#      只靠 DETACHED 逃不掉，必须 breakaway）
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_CREATE_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def _python():
    """子进程优先用 pythonw.exe（不弹黑窗），没有就退回当前解释器。"""
    exe = sys.executable or "python"
    pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return pw if os.path.exists(pw) else exe


def _port_busy(port: int) -> bool:
    """端口是否已被监听。自己解析 netstat，不引第三方库。"""
    try:
        raw = subprocess.run(["netstat", "-ano"], capture_output=True).stdout
    except Exception:
        return False
    text = raw.decode("gbk", errors="ignore")
    return any(f":{port} " in ln and "LISTENING" in ln for ln in text.splitlines())


GATEWAY_PORT = 8888


def start_gateway():
    """网关是外网访问的唯一入口，一起拉起来，省得用户记两条命令。"""
    if _port_busy(GATEWAY_PORT):
        print(f"  [跳过] 网关：端口 {GATEWAY_PORT} 已在监听（大概已在运行）")
        return
    os.makedirs(DATA_ROOT, exist_ok=True)
    env = dict(os.environ)
    # 网关是对外入口：强制要求登录（内网穿透的 frpc 在本机转发，
    # 若允许"本机免登录"，任何拿到公网地址的人都会变成管理员）
    env.update({"CK_NO_BROWSER": "1", "PYTHONIOENCODING": "utf-8",
                "CK_GW_STRICT": "1"})
    log = open(os.path.join(DATA_ROOT, "gateway.log"), "a", encoding="utf-8")
    args = [_python(), os.path.join(BASE, "gateway.py")]
    common = dict(cwd=BASE, env=env, stdout=log, stderr=subprocess.STDOUT,
                  close_fds=True)
    try:
        proc = subprocess.Popen(
            args, creationflags=_CREATE_FLAGS | CREATE_BREAKAWAY_FROM_JOB,
            **common)
    except OSError:
        proc = subprocess.Popen(args, creationflags=_CREATE_FLAGS, **common)
    print(f"  [启动] 网关：http://127.0.0.1:{GATEWAY_PORT}/   (pid={proc.pid})")


SAKURA_SERVICE = os.environ.get(
    "CK_SAKURA_SERVICE",
    r"C:\Program Files\SakuraFrpLauncher\SakuraFrpService.exe")


def _frpc_running() -> bool:
    try:
        raw = subprocess.run(["tasklist", "/FI", "IMAGENAME eq frpc.exe"],
                             capture_output=True).stdout
        return "frpc.exe" in raw.decode("gbk", errors="ignore")
    except Exception:
        return False


def ensure_tunnel():
    """让外网能访问网关：确保内网穿透隧道在跑。

    SakuraFrp 服务端配置（C:\\ProgramData\\SakuraFrpService\\config.json）里
    已经有 auto_start_tunnels，所以**只要把服务进程拉起来**，隧道就会自动连上，
    不必再去点启动器。没有装 SakuraFrp 就只提示，不影响本地使用。
    """
    if _frpc_running():
        print("  [跳过] 内网穿透隧道已在运行（frpc 进程存在）")
        return
    if not os.path.exists(SAKURA_SERVICE):
        print("  [提示] 没找到 SakuraFrpService.exe；如需外网访问请手动开启内网穿透")
        return
    try:
        subprocess.Popen([SAKURA_SERVICE],
                         cwd=os.path.dirname(SAKURA_SERVICE),
                         creationflags=_CREATE_FLAGS, close_fds=True)
        print("  [启动] SakuraFrp 服务（会自动连上已配置好的隧道）")
        time.sleep(7)
        print("         隧道已连上 ✓" if _frpc_running()
              else "         暂未连上，可打开启动器确认隧道状态")
    except Exception as e:
        print(f"  [提示] 拉起 SakuraFrp 失败：{e}（不影响本地使用）")


def spawn_instance(n: int, base_port: int = BASE_PORT, quiet: bool = False):
    """启动第 n 个实例（acc{n} → base_port+n-1）。返回 pid 或 None。

    抽成独立函数是为了让网关的「+ 新增账号」按钮能只加一个实例，
    而不用把已存在的实例再拉一遍。
    """
    name = f"acc{n}"
    port = base_port + n - 1
    data_dir = os.path.join(DATA_ROOT, name)
    if _port_busy(port):
        # 端口被占不等于"本实例已在运行"——可能是别的（甚至没设 CK_DATA_DIR 的）
        # 进程占着。实测踩过：野进程占 5000 导致 acc1 被静默跳过，
        # 177 账号的登录与选课记录被写进了项目根目录的 ui_config.json。
        if not quiet:
            print(f"  [跳过] {name}：端口 {port} 已被占用。")
            print(f"         若该端口上是别的程序/别的数据目录的实例，"
                  f"请先用界面上的「退出服务」或重启电脑清掉，否则本次刷课"
                  f"不会跑在 {data_dir} 上。")
        return None
    os.makedirs(data_dir, exist_ok=True)
    env = dict(os.environ)
    env.update({"CK_PORT": str(port), "CK_DATA_DIR": data_dir,
                "CK_NO_BROWSER": "1", "PYTHONIOENCODING": "utf-8"})
    log = open(os.path.join(data_dir, "instance.log"), "a", encoding="utf-8")
    args = [_python(), os.path.join(BASE, "web_ui.py")]
    common = dict(cwd=BASE, env=env, stdout=log, stderr=subprocess.STDOUT,
                  close_fds=True)
    try:
        proc = subprocess.Popen(
            args, creationflags=_CREATE_FLAGS | CREATE_BREAKAWAY_FROM_JOB,
            **common)
    except OSError:
        # 父进程没给 breakaway 权限时退回普通脱离方式
        proc = subprocess.Popen(args, creationflags=_CREATE_FLAGS, **common)
    print(f"  [启动] {name}：http://127.0.0.1:{port}/   (pid={proc.pid})")
    return proc.pid


def existing_count() -> int:
    """数据目录里已有的实例数（acc1..accN 的最大编号），至少 DEFAULT_COUNT。

    网关页面点「+ 新增账号」会新建 acc4、acc5…，这里跟着走，
    保证下次启动不会把多加的账号漏掉。
    """
    nums = []
    try:
        for d in os.listdir(DATA_ROOT):
            if d.startswith("acc") and d[3:].isdigit():
                if os.path.isdir(os.path.join(DATA_ROOT, d)):
                    nums.append(int(d[3:]))
    except Exception:
        pass
    return max(DEFAULT_COUNT, max(nums) if nums else 0)


def main():
    # --one <n>：只起第 n 个实例（供网关「新增账号」调用）
    if len(sys.argv) > 2 and sys.argv[1] == "--one":
        try:
            n = int(sys.argv[2])
        except ValueError:
            print("用法: python start_multi.py --one 4")
            return
        base_port = int(sys.argv[3]) if len(sys.argv) > 3 else BASE_PORT
        print(f"单实例启动：acc{n} → 端口 {base_port + n - 1}")
        sys.stdout.flush()
        pid = spawn_instance(n, base_port)
        print("done" if pid else "skip")
        return

    raw = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CK_INSTANCES")
    if raw:
        try:
            count = int(raw)
        except ValueError:
            count = existing_count()
    else:
        count = existing_count()
    base_port = int(sys.argv[2]) if len(sys.argv) > 2 else BASE_PORT
    print(f"启动器：共 {count} 个实例，端口 {base_port} ~ {base_port + count - 1}")
    print(f"数据根目录：{DATA_ROOT}\n")

    for i in range(count):
        spawn_instance(i + 1, base_port)
        time.sleep(0.8)

    time.sleep(1.5)
    start_gateway()
    time.sleep(1.5)
    ensure_tunnel()
    print(f"\n网关只监听 127.0.0.1:{GATEWAY_PORT}，对外靠内网穿透暴露。")
    print("每个实例的「并发章节数」建议设 1~2；多开时别三个同时全速跑。")


if __name__ == "__main__":
    main()
