# -*- coding: utf-8 -*-
"""一键启动：双击即拉起全部服务，并自动打开控制台页面。

桌面快捷方式指向本文件（用 pythonw.exe 运行 → 无黑窗）：
    3 个刷课实例  →  统一网关  →  内网穿透隧道  →  打开浏览器

任何一步失败都会弹窗提示原因，不会静默失败。
"""
import os
import socket
import sys
import time
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


def notify(title: str, msg: str, error: bool = False):
    """弹窗提示。启动失败时用，避免双击后毫无反应。"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        (messagebox.showerror if error else messagebox.showinfo)(title, msg)
        root.destroy()
    except Exception:
        print(f"[{title}] {msg}")


def port_alive(port: int) -> bool:
    try:
        with socket.socket() as s:
            s.settimeout(1.5)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def existing_count() -> int:
    """数数据目录里已有哪些实例，决定这次要拉起几个。

    这样在网关页面点「+ 新增账号」加了 acc4 之后，下次双击图标也会
    把 acc4 一起拉起来，不会又退回 3 个。数据目录是实例的唯一凭据
    （acc1..accN），所以直接按目录名推断。
    """
    root = os.path.join(os.path.dirname(BASE), "chaoxing_data")
    names = []
    try:
        for d in os.listdir(root):
            if d.startswith("acc") and d[3:].isdigit():
                if os.path.isdir(os.path.join(root, d)):
                    names.append(int(d[3:]))
    except Exception:
        pass
    # 至少 3 个（首次运行还没有数据目录时）
    return max(3, max(names) if names else 0)


def main():
    # 1) 拉起实例 + 网关 + 隧道（复用 start_multi 的逻辑，避免重复实现）
    count = existing_count()
    env_count = os.environ.get("CK_INSTANCES")
    if env_count and env_count.isdigit():
        count = max(count, int(env_count))
    print(f"将启动 {count} 个实例")
    sys.argv = ["start_multi.py", str(count)]
    try:
        import start_multi
    except Exception as e:
        notify("启动失败", f"导入启动模块出错：\n{e}", True)
        return
    try:
        start_multi.main()
    except Exception as e:
        notify("启动失败", f"启动过程出错：\n{e}", True)
        return

    # 2) 等网关就绪（最多 30 秒）
    gw = int(os.environ.get("CK_GW_PORT") or 8888)
    for _ in range(30):
        if port_alive(gw):
            break
        time.sleep(1)

    # 3) 打开控制台
    url = f"http://127.0.0.1:{gw}/"
    if port_alive(gw):
        webbrowser.open(url)
    else:
        notify("网关未就绪",
               f"实例已启动，但网关端口 {gw} 没起来。\n\n"
               f"可以手动打开：{url}\n"
               f"若仍打不开，多半是端口被占用，请重启电脑后再试。",
               True)


if __name__ == "__main__":
    main()
