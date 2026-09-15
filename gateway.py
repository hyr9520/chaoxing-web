# -*- coding: utf-8 -*-
"""统一网关：一个入口访问多个账号实例。

为什么需要它
    SakuraFrp 免费用户只有 **2 条隧道**，而账号有 3 个。
    用 1 条隧道映射到本网关，再由网关路由到各账号实例。

职责
    1. 认证：本机免登录；远程分「管理员 / 普通用户 / 只读访客」
    2. 总览页：一屏看所有账号实例的状态、进度、最近日志
    3. 反向代理：/acc/<n>/... → http://127.0.0.1:<port>/...
    4. 访客白名单：只读角色仅能访问只读路径

用法
    python gateway.py                 # 默认监听 127.0.0.1:8888
    CK_GW_PORT=9000 python gateway.py # 换端口
    CK_GW_HOST=0.0.0.0 python gateway.py   # 允许局域网直连（一般不需要）
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time

import requests
from flask import (Flask, Response, g, jsonify, redirect, request, session)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.join(os.path.dirname(BASE), "chaoxing_data")
GW_CONFIG = os.path.join(DATA_ROOT, "gateway_config.json")
INSTANCE_BASE_PORT = 5000
HOST = os.environ.get("CK_GW_HOST") or "127.0.0.1"
try:
    PORT = int(os.environ.get("CK_GW_PORT") or 8888)
except ValueError:
    PORT = 8888

ROLE_ADMIN, ROLE_USER, ROLE_VIEWER = "admin", "user", "viewer"
# 访客（只读）允许访问的路径前缀。
# ⚠️ 绝对不能包含 "/acc/"（2026-09-14 实测踩坑）：反代过去的请求对实例而言
# 来自 127.0.0.1（网关转发），实例会当成本机而**完全放行** —— 于是只读访客
# 能拿到实例的明文账号密码、AI Key，甚至直接启动任务。
# 权限必须在网关这一层卡死，不能指望下游实例再拦一次。
VIEWER_OK = ("/", "/api/overview", "/api/status", "/api/logs",
             "/api/courses", "/api/ping", "/api/whoami")
# 反代时不透传的 hop-by-hop 头
_SKIP_HEADERS = {"host", "content-length", "connection", "keep-alive",
                 "transfer-encoding", "upgrade", "accept-encoding"}
_SKIP_RESP_HEADERS = {"content-encoding", "content-length", "transfer-encoding",
                      "connection", "keep-alive"}

app = Flask(__name__)
app.json.ensure_ascii = False
# secret_key 在下面配置读写函数定义之后设置（需要持久化到配置文件）


# ---------------- 配置与实例发现 ----------------

def _load_gw_config() -> dict:
    try:
        with open(GW_CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_gw_config(cfg: dict) -> None:
    os.makedirs(DATA_ROOT, exist_ok=True)
    tmp = GW_CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, GW_CONFIG)


def _load_secret() -> str:
    """持久化的会话密钥。

    原实现每次进程启动都随机生成 —— 于是**网关一重启，所有已登录用户立刻
    失效被弹回登录页**（用户实测的"登录一次被弹回、第二次才好"就是这个：
    那期间我为修 bug 重启过几次网关）。改成存进配置文件，只生成一次，
    以后重启不再把已登录的人踢下线。
    """
    cfg = _load_gw_config()
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(32)
        _save_gw_config(cfg)
    return cfg["secret_key"]


app.secret_key = _load_secret()


def gateway_tokens() -> dict:
    """网关自己的管理/只读口令，首次运行生成并打印。"""
    cfg = _load_gw_config()
    changed = False
    for key, n in (("admin_token", 9), ("viewer_token", 6)):
        if not cfg.get(key):
            cfg[key] = secrets.token_urlsafe(n)
            changed = True
    if changed:
        _save_gw_config(cfg)
        print(f"[网关口令] 管理口令 : {cfg['admin_token']}")
        print(f"[网关口令] 只读口令 : {cfg['viewer_token']}")
        print(f"           （写在 {GW_CONFIG}，可自行修改）")
    return cfg


# 判断一个 acc 目录是否"真实存在过的实例"需要看的痕迹文件。
# 空壳目录（只有 0 字节 instance.log）是 spawn 失败/回归测试点了一下
# "新增账号"留下的残渣，不该出现在实例列表里（2026-09-14）。
_INSTANCE_MARKERS = ("ui_config.json", "cookies.txt",
                     "learned_answers.json", "cache.json")


def _is_real_instance(path: str) -> bool:
    """目录里有任何一份真实数据文件，才算一个实例。

    只看 instance.log 不算 —— 它是 spawn_instance 在 Popen 之前就创建的空文件，
    实例没起来也会留下，之前因此冒出过 acc6/acc7 幽灵实例。
    """
    return any(os.path.isfile(os.path.join(path, m))
               for m in _INSTANCE_MARKERS)


def instances() -> list:
    """扫描数据目录得出实例列表（acc1 → 5000，acc2 → 5001 …）。

    会跳过空壳目录（见 _is_real_instance），避免幽灵实例。
    """
    out = []
    if not os.path.isdir(DATA_ROOT):
        return out
    for name in sorted(os.listdir(DATA_ROOT)):
        m = re.fullmatch(r"acc(\d+)", name)
        if not m:
            continue
        n = int(m.group(1))
        d = os.path.join(DATA_ROOT, name)
        if not _is_real_instance(d):
            continue
        out.append({"n": n, "name": name,
                    "port": INSTANCE_BASE_PORT + n - 1,
                    "dir": d})
    return out


def _read_instance_cfg(path: str) -> dict:
    try:
        with open(os.path.join(path, "ui_config.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def instance_account(path: str) -> str:
    """实例绑定的账号（用于总览页显示）。"""
    cfg = _read_instance_cfg(path)
    profs = cfg.get("profiles") or []
    if profs:
        return str(profs[0].get("username") or "")
    return str((cfg.get("last_params") or {}).get("username") or "")


def all_profiles() -> list:
    """汇总各实例的账号档案（登录校验用）。"""
    out = []
    for it in instances():
        for p in (_read_instance_cfg(it["dir"]).get("profiles") or []):
            p = dict(p)
            p["_inst"] = it["n"]
            out.append(p)
    return out


# ---------------- 认证 ----------------

# 严格模式：一律要求登录，不认"本机免登录"。
# 网关是对外入口，而内网穿透的 frpc 就在本机转发请求——
# 隧道过来的 remote_addr 同样是 127.0.0.1，只按它判断会让任何公网访客
# 直接拿到管理员权限。所以网关默认走严格模式（start_multi.py 会设这个变量）。
_FORCE_AUTH = os.environ.get("CK_GW_STRICT", "1") != "0"


def _is_local_request() -> bool:
    """是否来自本机浏览器。

    ⚠️ 不能只看 remote_addr：frpc 转发时源地址也是 127.0.0.1，
    于是公网访客会被误判成"本机=管理员"而跳过登录。必须同时要求 Host
    也是本机地址（隧道访问时 Host 是 frp-put.com:28288 之类）。
    更彻底的做法是开启 CK_GW_STRICT=1 完全禁用免登录。
    """
    if _FORCE_AUTH:
        return False
    if (request.remote_addr or "").strip() not in ("127.0.0.1", "::1"):
        return False
    host = (request.host or "").split(":")[0].strip().lower()
    return host in ("127.0.0.1", "localhost", "[::1]", "::1")


def _role() -> str:
    return getattr(g, "role", ROLE_ADMIN)


def _viewer_allowed(path: str) -> bool:
    """访客白名单校验。

    ⚠️ 首页 "/" 必须**精确匹配**（2026-09-14 实测踩坑）：早先把 "/" 放进白名单
    后用 startswith 判断，而任何路径都以 "/" 开头 —— 等于全部放行，
    只读访客照样能拿到实例的明文密码。
    """
    if path == "/":
        return True
    return any(path.startswith(p) for p in VIEWER_OK if p != "/")


@app.before_request
def _auth_gate():
    if request.path in ("/login", "/auth", "/favicon.ico"):
        return None
    if _is_local_request():
        g.role, g.who = ROLE_ADMIN, "本机"
        return None
    role = session.get("role")
    if role:
        g.role, g.who = role, session.get("who") or role
        if role == ROLE_VIEWER and not _viewer_allowed(request.path):
            return jsonify(ok=False, msg="只读访问：该操作用不了"), 403
        return None
    if request.path.startswith("/api/"):
        return jsonify(ok=False, need_login=True, msg="未登录或登录已过期"), 401
    return redirect("/login")


@app.get("/api/whoami")
def api_whoami():
    return jsonify(role=_role(), who=getattr(g, "who", ""),
                   local=_is_local_request())


@app.post("/auth")
def do_auth():
    data = request.get_json(silent=True) or request.form or {}
    user = str(data.get("user") or "").strip()
    pwd = str(data.get("pwd") or "").strip()
    if not (user and pwd):
        return jsonify(ok=False, msg="请输入账号和密码")
    tk = gateway_tokens()
    if pwd == tk.get("admin_token"):
        session["role"], session["who"] = ROLE_ADMIN, "管理口令"
        return jsonify(ok=True, role=ROLE_ADMIN)
    if pwd == tk.get("viewer_token"):
        session["role"], session["who"] = ROLE_VIEWER, "只读口令"
        return jsonify(ok=True, role=ROLE_VIEWER)
    for p in all_profiles():
        if (str(p.get("username") or "") == user
                and str(p.get("password") or "") == pwd):
            session["role"], session["who"] = ROLE_USER, user
            session["inst"] = p.get("_inst")
            return jsonify(ok=True, role=ROLE_USER, inst=p.get("_inst"))
    return jsonify(ok=False, msg="账号或密码不正确（需与本机已保存的学习通账号一致）")


@app.post("/auth/logout")
def do_auth_logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/quit")
def api_quit():
    """停掉网关进程（认证已在上游拦截：本机或已登录）。"""
    # 关掉网关 = 全体实例失去入口，属于最高级别的管理动作。
    # /api/stop_all 已要求 ADMIN，这里也必须一致，否则普通账号用户
    # 可以把整台机器上的服务一次性关停。
    guard = _admin_only("关闭网关服务")
    if guard:
        return guard

    def _bye():
        os._exit(0)
    threading.Timer(0.5, _bye).start()
    return jsonify(ok=True)


@app.get("/login")
def login_page():
    return Response(LOGIN_PAGE, mimetype="text/html; charset=utf-8")


# ---------------- 总览 ----------------

def _instance_state(it: dict) -> dict:
    """读一个实例的状态与进度。实例都在本机，直接打它的 API。"""
    out = {"n": it["n"], "name": it["name"], "port": it["port"],
           "account": instance_account(it["dir"]),
           "online": False, "running": False, "paused": False,
           "progress": None, "last_log": ""}
    try:
        st = requests.get(f"http://127.0.0.1:{it['port']}/api/status",
                          timeout=4).json()
        out["online"] = True
        out["running"] = bool(st.get("running"))
        out["paused"] = bool(st.get("paused"))
        acc = st.get("account") or {}
        if acc.get("name") or acc.get("user"):
            out["account"] = f"{acc.get('name') or ''} {acc.get('user') or ''}".strip()
    except Exception:
        return out
    try:
        lg = requests.get(f"http://127.0.0.1:{it['port']}/api/logs",
                          params={"after": 10 ** 9}, timeout=6).json()
        out["progress"] = lg.get("progress")
        total = int(lg.get("total") or 0)
        if total:
            tail = requests.get(f"http://127.0.0.1:{it['port']}/api/logs",
                                params={"after": max(0, total - 30)},
                                timeout=6).json()
            lines = [str(x) for x in (tail.get("lines") or [])]
            if lines:
                out["last_log"] = lines[-1][:160]
            # 风控联动：最近日志出现熔断/回溯关键字就在总览页告警，
            # 提醒用户把另外两个实例也停一停（同一 IP 的并发是共同风险）
            joined = " ".join(lines)
            out["risk"] = any(k in joined for k in
                              ("RiskControl", "风控", "进度回溯", "熔断"))
    except Exception:
        pass
    return out


@app.get("/api/overview")
def api_overview():
    role = _role()
    me = session.get("inst")
    data = []
    for it in instances():
        if role == ROLE_USER and me and it["n"] != me:
            continue          # 普通用户只看自己那个实例
        data.append(_instance_state(it))
    return jsonify(ok=True, role=role, who=getattr(g, "who", ""),
                   instances=data, stagger=dict(_stagger))


@app.get("/")
def index():
    return Response(GW_PAGE, mimetype="text/html; charset=utf-8")


# ---------------- 错峰调度 ----------------
# 目的：避免三个账号同时全速跑把请求密度翻三倍（实测那会触发平台进度回溯）。
# 做法：按用户给的间隔，依次让各实例"用上次的选课"开跑。
# 前提：每个实例至少手动点过一次「开始」，这样才有 last_pick 记录。
_stagger = {"running": False, "current": None, "gap": 0, "log": [],
            "started_at": 0, "finished_at": 0}


def _stagger_worker(queue, gap):
    _stagger.update({"running": True, "gap": gap, "log": [], "current": None,
                     "started_at": time.time(), "finished_at": 0})
    for i, n in enumerate(queue):
        if not _stagger["running"]:
            _stagger["log"].append("已手动停止调度")
            break
        if i and gap:
            _stagger["log"].append(f"等待 {gap // 60} 分 {gap % 60} 秒后启动实例 {n}")
            for _ in range(gap):
                if not _stagger["running"]:
                    break
                time.sleep(1)
        if not _stagger["running"]:
            _stagger["log"].append("已手动停止调度")
            break
        it = next((x for x in instances() if x["n"] == n), None)
        if not it:
            _stagger["log"].append(f"实例 {n} 不存在，跳过")
            continue
        # 实例被用户在总览页用开关关掉了：不要硬戳（会得到"连接拒绝"），
        # 直接跳过并说明原因。轮询期间用户随时可能关掉实例。
        if not _port_listeners(it["port"]):
            _stagger["log"].append(f"实例 {n} 已停止（开关关闭），跳过")
            continue
        _stagger["current"] = n
        try:
            r = requests.post(f"http://127.0.0.1:{it['port']}/api/start_last",
                              timeout=120).json()
            _stagger["log"].append(f"实例 {n}：{r.get('msg') or r.get('ok')}")
        except Exception as e:
            _stagger["log"].append(f"实例 {n} 启动失败：{e}")
    _stagger["current"] = None
    _stagger["running"] = False
    _stagger["finished_at"] = time.time()


@app.post("/api/stagger")
def api_stagger():
    """开始错峰启动。gap 单位秒，默认 20 分钟。"""
    # 与其它实例级管理动作统一：只读访客和普通账号用户都不许触发调度。
    # 原先只挡了 viewer，普通登录用户仍可发起 —— 语义不一致，一并收口。
    guard = _admin_only("启动错峰调度")
    if guard:
        return guard
    if _stagger["running"]:
        return jsonify(ok=False, msg="调度已经在跑了")
    data = request.get_json(silent=True) or {}
    # 同样不能用 `or` —— gap=0（不等待、立即依次启动）是合法用法
    raw_gap = data.get("gap")
    try:
        gap = 1200 if raw_gap is None else max(0, int(raw_gap))
    except (TypeError, ValueError):
        gap = 1200
    # 注意：不能用 `data.get("instances") or 默认值` —— 空列表是 falsy，
    # 会把"显式传空"误判成"没传"而退化成全部实例（实测踩过）
    raw = data.get("instances")
    if raw is None:
        queue = [it["n"] for it in instances()]
    else:
        queue = []
        for x in raw:
            try:
                queue.append(int(x))
            except (TypeError, ValueError):
                continue
    if not queue:
        return jsonify(ok=False, msg="没有可调度的实例")
    threading.Thread(target=_stagger_worker, args=(queue, gap),
                     daemon=True).start()
    return jsonify(ok=True,
                   msg=f"已开始错峰启动：{len(queue)} 个实例，间隔 {gap} 秒")


@app.post("/api/stagger/stop")
def api_stagger_stop():
    # 与 /api/stagger 配对：原先只挡 viewer，普通用户能单方面停掉别人的调度
    guard = _admin_only("停止错峰调度")
    if guard:
        return guard
    _stagger["running"] = False
    return jsonify(ok=True, msg="已请求停止调度（已启动的实例不会被暂停）")


# ---------------- 实例级启停 / 新增账号 ----------------

def _admin_only(what: str):
    """管理动作的统一权限闸门：只读访客与普通账号用户都不许动。"""
    if _role() != ROLE_ADMIN:
        return jsonify(ok=False, msg=f"只有管理员能{what}"), 403
    return None


# 实例创建/删除的互斥锁：编号计算与目录创建必须原子，否则并发加号会撞车
_SPAWN_LOCK = threading.Lock()
MAX_INSTANCES = 20


def _port_listeners(port: int) -> list:
    """监听指定端口的 PID 列表（中文系统 netstat 输出是 GBK）。"""
    try:
        raw = subprocess.run(["netstat", "-ano"], capture_output=True).stdout
    except Exception:
        return []
    text = raw.decode("gbk", errors="ignore")
    pids = []
    for line in text.splitlines():
        if f":{port} " in line and "LISTENING" in line:
            try:
                pids.append(int(line.split()[-1]))
            except ValueError:
                continue
    return pids


def _instance_port(n: int) -> int:
    return INSTANCE_BASE_PORT + n - 1


@app.post("/api/instance/<int:n>/start")
def api_instance_start(n):
    denied = _admin_only("启动实例")
    if denied:
        return denied
    if not any(it["n"] == n for it in instances()):
        return jsonify(ok=False, msg=f"没有 acc{n} 这个实例")

    port = _instance_port(n)
    if _port_listeners(port):
        return jsonify(ok=True, msg=f"实例 {n} 已经在运行了")

    # 后台起，避免 HTTP 请求等 1~2 秒超时；前端靠轮询刷新状态
    def _spawn():
        try:
            import start_multi
            start_multi.spawn_instance(n, INSTANCE_BASE_PORT)
        except Exception as e:
            print(f"[实例启动失败] acc{n}: {e}")

    threading.Thread(target=_spawn, daemon=True).start()
    return jsonify(ok=True, msg=f"正在启动实例 {n}…")


@app.post("/api/instance/<int:n>/stop")
def api_instance_stop(n):
    denied = _admin_only("停止实例")
    if denied:
        return denied
    if not any(it["n"] == n for it in instances()):
        return jsonify(ok=False, msg=f"没有 acc{n} 这个实例")
    port = _instance_port(n)
    pids = _port_listeners(port)
    if not pids:
        return jsonify(ok=True, msg=f"实例 {n} 本来就没在跑")

    # 先让实例自己体面退出（它会清掉"自动续刷"标记，避免下次莫名自己跑起来），
    # 超时或不可达再强杀。直接 taskkill 会留下 resume 标记。
    try:
        requests.post(f"http://127.0.0.1:{port}/api/shutdown", timeout=4)
    except Exception:
        pass
    time.sleep(2.0)

    still = _port_listeners(port)
    for pid in still:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True)
    time.sleep(0.6)
    left = _port_listeners(port)
    if left:
        return jsonify(ok=False, msg=f"实例 {n} 未能停止（PID {left}）")
    return jsonify(ok=True, msg=f"实例 {n} 已停止")


@app.post("/api/instance/add")
def api_instance_add():
    """新增一个账号实例：找到当前最大编号 +1，起一个新实例。

    编号不回收（停掉 acc2 后再加会得到 acc4），避免新账号继承旧账号的
    数据目录 —— 数据目录是按 acc{n} 命名的，编号复用会让新账号看到别人的
    登录态和题库缓存。

    ⚠️ 必须加锁（2026-09-14 实测）：编号是"读目录 → 算 max+1"，而实例创建
    是异步的（makedirs 在后台线程）。不加锁时并发调用会同时看到 max=3 而
    都返回 n=4，两个实例撞同一个目录和端口。
    """
    denied = _admin_only("新增账号")
    if denied:
        return denied
    with _SPAWN_LOCK:
        nums = [it["n"] for it in instances()]
        n = (max(nums) + 1) if nums else 1
        if n > MAX_INSTANCES:
            return jsonify(ok=False,
                           msg=f"实例数已达上限（{MAX_INSTANCES}），请先停用不用的账号")
        # 先占位建目录，让下一次请求立刻能算到新编号（消除 TOCTOU 窗口）
        try:
            os.makedirs(os.path.join(DATA_ROOT, f"acc{n}"), exist_ok=True)
        except Exception as e:
            return jsonify(ok=False, msg=f"创建数据目录失败：{e}")

    def _spawn():
        try:
            import start_multi
            start_multi.spawn_instance(n, INSTANCE_BASE_PORT)
        except Exception as e:
            print(f"[新增账号失败] acc{n}: {e}")
        # 起不来就清掉空壳目录，别在实例列表里留幽灵（2026-09-14）
        d = os.path.join(DATA_ROOT, f"acc{n}")
        if not _is_real_instance(d):
            try:
                shutil.rmtree(d, ignore_errors=True)
                print(f"[新增账号] acc{n} 启动失败，已清理空壳目录")
            except Exception:
                pass

    threading.Thread(target=_spawn, daemon=True).start()
    return jsonify(ok=True, msg=f"正在创建账号 {n}…",
                   n=n, port=_instance_port(n))


@app.post("/api/stop_all")
def api_stop_all():
    """一键全停：所有实例 + 网关自己。

    页面先收到响应再断开（延迟 1.5 秒退出），否则前端拿不到结果。
    """
    denied = _admin_only("停止全部服务")
    if denied:
        return denied
    _stagger["running"] = False

    def _bye():
        time.sleep(1.5)          # 留时间让响应回到浏览器
        for it in instances():
            port = it["port"]
            try:
                requests.post(f"http://127.0.0.1:{port}/api/shutdown",
                              timeout=3)
            except Exception:
                pass
            time.sleep(0.4)
            for pid in _port_listeners(port):
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True)
        os._exit(0)

    threading.Thread(target=_bye, daemon=True).start()
    return jsonify(ok=True, msg="正在停止全部服务…（本页稍后会断开）")


# ---------------- 反向代理 ----------------

def _forward(n: int, sub: str):
    port = INSTANCE_BASE_PORT + n - 1
    url = f"http://127.0.0.1:{port}/{sub}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _SKIP_HEADERS}
    headers["X-Base-Path"] = f"/acc/{n}"      # 让实例知道自己的前缀
    try:
        resp = requests.request(
            request.method, url, params=request.args,
            data=request.get_data(), headers=headers, cookies=request.cookies,
            allow_redirects=False, timeout=120)
    except Exception as e:
        return Response(f"实例 {n} 不可达：{e}", status=502,
                        mimetype="text/plain; charset=utf-8")
    out = Response(resp.content, resp.status_code)
    for k, v in resp.headers.items():
        if k.lower() in _SKIP_RESP_HEADERS:
            continue
        if k.lower() == "location" and v.startswith("/"):
            v = f"/acc/{n}{v}"          # 重定向也要带上前缀
        out.headers[k] = v
    return out


@app.route("/acc/<int:n>/", defaults={"sub": ""}, methods=["GET", "POST"])
@app.route("/acc/<int:n>/<path:sub>", methods=["GET", "POST"])
def proxy(n, sub):
    if not any(it["n"] == n for it in instances()):
        return Response("没有这个实例", status=404,
                        mimetype="text/plain; charset=utf-8")
    role = _role()
    # 第二道保险：即使白名单被人改错，这里也挡住访客进实例详情
    if role == ROLE_VIEWER:
        return jsonify(ok=False, msg="只读访问：不能进入实例详情页"), 403
    # 普通用户只能进自己那个实例，不能操作别人的账号
    if role == ROLE_USER and session.get("inst") != n:
        return jsonify(ok=False, msg="无权访问该实例"), 403
    return _forward(n, sub)


# ---------------- 页面 ----------------

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · 刷课控制台</title>
<style>
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#f4f6f8;color:#1f2328;
       font:14px/1.6 "Microsoft YaHei UI","PingFang SC",sans-serif}
  .box{width:min(92vw,380px);background:#fff;border:1px solid #e5e7eb;border-radius:14px;
       padding:26px 24px;box-shadow:0 6px 24px rgba(0,0,0,.06)}
  h1{margin:0 0 6px;font-size:19px}
  p.sub{margin:0 0 18px;color:#6b7280;font-size:12.5px}
  label{display:block;margin:12px 0 6px;font-size:13px;color:#374151}
  input{width:100%;padding:10px 12px;border:1px solid #d8dee4;border-radius:9px;font-size:14px}
  button{width:100%;margin-top:18px;padding:11px;border:0;border-radius:9px;
         background:#2563eb;color:#fff;font-size:15px;cursor:pointer}
  button:disabled{opacity:.6}
  .msg{margin-top:12px;font-size:13px;color:#dc2626;min-height:18px}
  .msg.ok{color:#067647}
  .tip{margin-top:16px;padding-top:14px;border-top:1px dashed #e5e7eb;
       font-size:12px;color:#9ca3af;line-height:1.7}
  .pwrow{position:relative}
  .pwrow input{padding-right:52px}
  .eye{position:absolute;right:6px;top:50%;transform:translateY(-50%);
       width:auto;margin:0;padding:6px 9px;background:none;border:0;
       color:#6b7280;font-size:12px;cursor:pointer}
  .eye:hover{color:#2563eb}
  .spin{display:inline-block;width:11px;height:11px;margin-right:6px;
        border:2px solid rgba(255,255,255,.4);border-top-color:#fff;
        border-radius:50%;animation:spin .7s linear infinite;vertical-align:-1px}
  @keyframes spin{to{transform:rotate(360deg)}}
  input:focus{outline:2px solid #93b4f5;outline-offset:1px;border-color:#2563eb}
</style></head><body>
<div class="box">
  <h1>刷课控制台</h1>
  <p class="sub">用学习通账号登录，查看该账号的进度</p>
  <label for="user">学习通账号</label>
  <input id="user" autocomplete="username" placeholder="手机号" autofocus>
  <label for="pwd">密码</label>
  <div class="pwrow">
    <input id="pwd" type="password" autocomplete="current-password" placeholder="密码">
    <button class="eye" id="eye" type="button" aria-label="显示密码">显示</button>
  </div>
  <button id="btn" type="submit">登录</button>
  <div class="msg" id="msg" role="status" aria-live="polite"></div>
  <div class="tip">账号密码只与本机已保存的档案比对，不会发送给学习通。<br>
    也可直接输入管理员口令或只读口令进入。</div>
</div>
<script>
const $ = id => document.getElementById(id);
let going = false;
$('eye').onclick = () => {
  const i = $('pwd'), on = i.type === 'password';
  i.type = on ? 'text' : 'password';
  $('eye').textContent = on ? '隐藏' : '显示';
  $('eye').setAttribute('aria-label', on ? '隐藏密码' : '显示密码');
  i.focus();
};
async function go() {
  if (going) return;
  const user = $('user').value.trim(), pwd = $('pwd').value.trim();
  if (!user) { $('msg').className = 'msg'; $('msg').textContent = '请填写账号或口令'; $('user').focus(); return; }
  if (!pwd) { $('msg').className = 'msg'; $('msg').textContent = '请填写密码'; $('pwd').focus(); return; }
  going = true;
  $('btn').disabled = true;
  $('msg').className = 'msg';
  $('msg').innerHTML = '<span class="spin"></span>校验中…';
  try {
    const r = await fetch('/auth', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({user, pwd})}).then(r => r.json());
    if (!r.ok) {
      $('msg').textContent = r.msg || '登录失败';
      $('btn').disabled = false; going = false;
      $('pwd').select();
      return;
    }
    $('msg').className = 'msg ok';
    $('msg').textContent = '登录成功，正在进入…';
    location.href = '/';
  } catch (e) {
    $('msg').textContent = '请求失败（服务可能未启动）: ' + e;
    $('btn').disabled = false; going = false;
  }
}
$('btn').onclick = go;
$('pwd').onkeydown = e => { if (e.key === 'Enter') go(); };
$('user').onkeydown = e => { if (e.key === 'Enter') $('pwd').focus(); };
</script></body></html>"""


GW_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>多账号刷课控制台</title>
<style>
  *{box-sizing:border-box}
  body{margin:0;padding:26px 18px 60px;background:#f4f6f8;color:#1f2328;
       font:14px/1.6 "Microsoft YaHei UI","PingFang SC",sans-serif}
  .wrap{max-width:1000px;margin:0 auto}
  h1{margin:0 0 4px;font-size:20px}
  .sub{color:#6b7280;font-size:12.5px;margin-bottom:18px}
  .bar{display:flex;align-items:center;gap:8px;margin:0 0 14px;flex-wrap:wrap}
  .sp{flex:1}
  button{padding:7px 14px;border:0;border-radius:9px;background:#2563eb;color:#fff;
         font-size:13px;cursor:pointer}
  button.ghost{background:#fff;color:#374151;border:1px solid #d8dee4}
  button:disabled{opacity:.55;cursor:default}
  .tag{padding:3px 9px;border-radius:999px;font-size:12px;background:#eef2f7;color:#374151}
  .tag.ok{background:#e7f7ee;color:#067647}
  .tag.warn{background:#fff7ed;color:#b45309}
  .tag.off{background:#f3f4f6;color:#9ca3af}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
  .card{background:#fff;border:1px solid #e5e7eb;border-radius:13px;padding:16px 16px 14px;
        box-shadow:0 2px 10px rgba(0,0,0,.03)}
  .card h3{margin:0 0 2px;font-size:15px;display:flex;align-items:center;gap:8px}
  .card .acc{color:#6b7280;font-size:12px;margin-bottom:12px}
  /* ---- 移动端优化（2026-09-14）---- */
  .head h1{margin:0 0 2px;font-size:19px;line-height:1.35}
  .head .sub{color:#6b7280;font-size:12.5px;margin-bottom:14px}
  .statrow{display:flex;align-items:center;gap:8px;margin-bottom:10px}
  .btnrow{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
  .chead{display:flex;align-items:center;gap:8px;margin-bottom:5px}
  .cno{font-size:15px;font-weight:600}
  .cport{font-size:11.5px;color:#b6bcc4}
  .cacc{color:#6b7280;font-size:12.5px;margin-bottom:10px;
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .pv .big i{font-style:normal;color:#b6bcc4;font-weight:400;font-size:16px}
  .pv{display:flex;align-items:baseline;gap:6px;margin:8px 0 6px}
  .pv .big{font-size:24px;font-weight:600}
  .pbar{height:8px;background:#eef2f7;border-radius:99px;overflow:hidden;
        box-shadow:inset 0 0 0 1px #e7ebf0}
  .pfill{height:100%;background:#22c55e;width:0;transition:width .4s;
         min-width:0;border-radius:99px}
  .cur{font-size:12px;color:#6b7280;margin-top:8px;min-height:18px;
       overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  /* 未登录/未启动时的下一步提示，比空着的 0/0 好懂 */
  .hint{margin-top:8px;padding:7px 10px;border-radius:8px;background:#f6f8fb;
        color:#5f6b7a;font-size:12px;line-height:1.5}
  .log{margin-top:10px;padding-top:9px;border-top:1px dashed #e5e7eb;
       font:11.5px/1.5 ui-monospace,Consolas,monospace;color:#6b7280;
       max-height:44px;overflow:hidden}
  .acts{margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .acts a{padding:6px 12px;border:1px solid #d8dee4;border-radius:8px;color:#2563eb;
          text-decoration:none;font-size:12.5px;background:#fff;display:inline-block}
  /* ---- 实例开关（iOS 风格）+ 危险按钮 ---- */
  /* 开关用 40x22 的滑块承载视觉；外层 label 加 padding 把可点区域放大到
     ~56x34，手指也点得中（原来只有滑块本身，手机上好几次点不中）。 */
  .sw{position:relative;display:inline-block;width:40px;height:22px;flex:none;
      padding:6px 8px;margin:-6px -8px;box-sizing:content-box;cursor:pointer}
  .sw input{opacity:0;width:0;height:0;position:absolute}
  .sw .sl{position:absolute;left:8px;top:6px;width:40px;height:22px;
          background:#cbd5e1;border-radius:99px;transition:background .2s}
  .sw .sl:before{content:"";position:absolute;width:18px;height:18px;left:2px;top:2px;
          background:#fff;border-radius:50%;transition:transform .2s}
  .sw input:checked + .sl{background:#22c55e}
  .sw input:checked + .sl:before{transform:translateX(18px)}
  .sw input:disabled + .sl{opacity:.5;cursor:default}
  .sw input:focus-visible + .sl{outline:2px solid #2563eb;outline-offset:3px}
  .acts .sw{margin-left:auto}
  /* 关着的实例：滑块加一圈淡蓝，明确"这里可以点开" */
  .card[data-on="0"] .sw .sl{background:#cbd5e1;box-shadow:0 0 0 3px rgba(37,99,235,.10)}
  .card[data-on="0"] .sw:hover .sl{background:#b8c2cf}
  button.danger{background:#dc2626}
  .fab{position:fixed;right:24px;bottom:26px;width:52px;height:52px;border-radius:50%;
       background:#2563eb;color:#fff;font-size:28px;line-height:1;border:0;cursor:pointer;
       box-shadow:0 4px 16px rgba(37,99,235,.35);display:flex;align-items:center;
       justify-content:center;padding:0}
  .fab:active{transform:scale(.95)}
  .empty{border:1px dashed #d8dee4;border-radius:12px;padding:26px;text-align:center;
         color:#6b7280;font-size:13px;background:#fff}
  .riskcard{border-color:#fecaca}
  .risk{margin-top:8px;padding:6px 9px;border-radius:8px;background:#fef2f2;
        color:#b91c1c;font-size:12px}
  /* ⚠️ 必须定义：页面里用 class="hide" / classList.toggle('hide') 控制显隐，
     但早先漏了这条规则 —— 于是 #staggerBox 和「停止调度」按钮根本藏不住
     （实测表现为顶部多出一个空白卡片）*/
  .hide{display:none !important}
  /* ---- 消息提示：按语义分色，并支持醒目的错误态 ---- */
  .msg{margin-top:14px;font-size:13px;color:#b45309;min-height:20px}
  .msg.err{color:#b91c1c}
  .msg.ok{color:#067647}
  .msg .spin{display:inline-block;width:11px;height:11px;margin-right:6px;
        border:2px solid #cbd5e1;border-top-color:#2563eb;border-radius:50%;
        animation:spin .7s linear infinite;vertical-align:-1px}
  @keyframes spin{to{transform:rotate(360deg)}}
  @media (prefers-reduced-motion: reduce){.msg .spin{animation:none}}
  /* 顶栏按钮的忙碌态 */
  button[data-busy]::after{content:"";display:inline-block;width:9px;height:9px;
        margin-left:6px;border:2px solid rgba(255,255,255,.45);
        border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;
        vertical-align:0}
  button.ghost[data-busy]::after{border-color:#cbd5e1;border-top-color:#374151}
  /* Toast：错误/成功短提示，自动消失，不占布局 */
  .toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);
        background:#1f2328;color:#fff;padding:9px 16px;border-radius:10px;
        font-size:13px;max-width:min(86vw,520px);z-index:50;
        box-shadow:0 6px 22px rgba(0,0,0,.22);opacity:0;transition:opacity .2s}
  .toast.show{opacity:1}
  .toast.err{background:#b91c1c}
  .toast.ok{background:#067647}
  /* 刷新中的骨架感：整页轻微变淡，表示数据在重取 */
  .grid.loading{opacity:.55;transition:opacity .15s}
  /* 无障碍：聚焦可见 */
  button:focus-visible,.acts a:focus-visible,.sw .sl:focus-visible{
        outline:2px solid #2563eb;outline-offset:2px}
  .sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
</style></head><body>
<div class="wrap">
  <div class="head">
    <h1>多账号刷课控制台</h1>
    <div class="sub">一个入口管理所有账号实例 · <span id="who"></span></div>
  </div>
  <div class="statrow">
    <span id="sum" class="tag">加载中…</span>
  </div>
  <div class="btnrow">
    <button id="btnStagger">错峰启动</button>
    <button class="ghost hide" id="btnStop">停止调度</button>
    <button class="ghost" id="btnRefresh">刷新</button>
    <button class="ghost danger" id="btnStopAll">停止所有服务</button>
    <button class="ghost" id="btnLogout">退出登录</button>
  </div>
  <div class="card hide" id="staggerBox" style="margin-bottom:14px"></div>
  <div class="grid" id="grid"></div>
  <div class="msg" id="msg" role="status" aria-live="polite"></div>
</div>
<button class="fab" id="btnAdd" title="新增账号" aria-label="新增账号">+</button>
<div class="toast" id="toast" role="alert" aria-live="assertive"></div>
<script>
const $ = id => document.getElementById(id);
let overview = null;
let isAdmin = false;
let loadTimer = null;      // 自动刷新句柄（保存下来，便于出错时暂停）
let busy = false;          // 有管理动作在进行（暂停自动刷新，避免打断）

// ---- 统一反馈：状态行 + Toast ----
function say(text, kind) {
  const el = $('msg');
  el.className = 'msg' + (kind ? ' ' + kind : '');
  el.innerHTML = (kind === 'busy' ? '<span class="spin"></span>' : '')
    + String(text == null ? '' : text);
}

let toastTimer = null;
function toast(text, kind) {
  const el = $('toast');
  el.textContent = text;
  el.className = 'toast show' + (kind ? ' ' + kind : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 4200);
}

// 按钮忙碌态：禁用 + 转圈，避免连点造成重复请求
function setBusy(btn, on) {
  if (!btn) return;
  if (on) { btn.dataset.busy = '1'; btn.disabled = true; }
  else { delete btn.dataset.busy; btn.disabled = false; }
}

function fmtPct(p) {
  if (!p || !p.total) return 0;
  return Math.round((p.done || 0) / p.total * 100);
}

function card(it) {
  const on = it.online;
  const p = it.progress || {};
  const pct = fmtPct(p);
  const cur = p.current ? (p.current.name + '  ' +
      Math.round((p.current.pos || 0) / (p.current.total || 1) * 100) + '%') : '';
  // 四态：未启动（进程不在）/ 空闲（进程在但没刷课）/ 运行中 / 已暂停
  let tag = '<span class="tag off">未启动</span>';
  if (!on) tag = '<span class="tag off">未启动</span>';
  else if (it.paused) tag = '<span class="tag warn">已暂停</span>';
  else if (it.running) tag = '<span class="tag ok">运行中</span>';
  else tag = '<span class="tag">空闲</span>';
  const risk = it.risk
    ? '<div class="risk">检测到风控/回溯提示，建议把其他实例也停一停</div>' : '';
  // 账号显示成「姓名 · 手机号」更好读（服务端给的是「姓名 手机号」）。
  // 2026-09-15 修 bug：原正则缺 g 标志，只替换第一处空白。
  // 注意本页是普通三引号字符串（非 raw）：源码里写两个反斜杠加 s，
  // 输出到页面才是正则的空白符转义；写四个反斜杠会让页面拿到两个反斜杠、
  // 匹配失效（此处不要多加转义）。
  //
  // 2026-09-15 补强：姓名来自实例侧 cx.get_name()，该调用失败时服务端只给
  // 手机号（实测有的实例能取到姓名、有的取不到），于是卡片上既有
  // 「姓名 · 手机号」又有裸手机号，看着像两个系统的数据。
  // 这里统一形态：只要有手机号就固定成「姓名 · 手机号」，无姓名则原样显示。
  const _rawAcc = (it.account || '').trim();
  let acc;
  if (!_rawAcc) {
    acc = '未登录';
  } else {
    const _m = _rawAcc.match(/^(.*?)[\\s·]*?(1[3-9]\\d{9})$/);
    acc = _m
      ? ((_m[1] || '').trim() ? (_m[1].trim() + ' · ' + _m[2]) : _m[2])
      : _rawAcc.replace(/\\s+/g, ' · ');
  }
  // 没登录时给一句明确的下一步，比空着 0/0 好懂
  const hint = (!on || !(it.account || '').trim())
    ? '<div class="hint">' + (on ? '未登录账号，点「打开该实例」登录后再选课'
                                  : '实例已关闭，打开右侧开关即可启动') + '</div>'
    : '';
  // 实例开关：控制实例进程的启停（暂停/继续刷课在实例页里）
  const sw = isAdmin
    ? '<label class="sw" title="开启/关闭这个实例"><input type="checkbox" '
      + 'data-inst="' + it.n + '"' + (on ? ' checked' : '')
      + '><span class="sl"></span></label>'
    : '';
  // data-on 是"服务端说的状态"，只用于差量签名（checked 属性会被用户改，
  // 不能拿它当签名依据，否则手动切换后永远判定为"没变化"）
  return '<div class="card' + (it.risk ? ' riskcard' : '') + '" data-on="'
    + (on ? '1' : '0') + '">'
    + '<div class="chead"><span class="cno">实例 ' + it.n + '</span>' + tag
    + '<span class="sp"></span><span class="cport">:' + it.port + '</span></div>'
    + '<div class="cacc">' + esc(acc) + '</div>'
    + '<div class="pv"><span class="big">' + (p.done || 0)
    + '<i>/' + (p.total || 0) + '</i></span>'
    + '<span class="tag">' + pct + '%</span></div>'
    + '<div class="pbar"><div class="pfill" style="width:' + pct + '%"></div></div>'
    + '<div class="cur">' + esc(cur || (on ? '暂无进行中的任务' : '—')) + '</div>'
    + hint
    + risk
    + '<div class="log">' + esc(it.last_log || '暂无日志') + '</div>'
    + '<div class="acts"><a href="/acc/' + it.n + '/">打开该实例 →</a>'
    + sw + '</div></div>';
}

async function load(silent) {
  if (busy) return;                 // 管理动作进行中，别打断状态显示
  const grid = $('grid');
  if (!silent) grid.classList.add('loading');
  try {
    const r = await fetch('/api/overview').then(r => r.json());
    if (r.need_login) { location.href = '/login'; return; }
    overview = r;
    isAdmin = r.role === 'admin';
    $('who').textContent = isAdmin
      ? ('管理员 · ' + r.who) : (r.role === 'viewer' ? '只读访问' : ('账号 ' + r.who));
    const list = r.instances || [];
    // 差量重绘：只在内容真的变化时替换 innerHTML，避免打断用户选中文字。
    // ⚠️ 但开关的 checked 必须每次都同步 —— innerHTML 里的 checked 只是
    // 「初始值」，用户手动拨过之后 DOM 的 checked 与实际状态会脱节，
    // 而差量重绘恰恰会跳过这次同步（实测导致开关视觉与真实状态不一致）。
    const html = list.length ? list.map(card).join('')
      : '<div class="empty">还没有实例。<br>点右下角的 <b>+</b> 新增第一个账号。</div>';
    if (grid.dataset.sig !== html) {
      grid.dataset.sig = html;
      grid.innerHTML = html;
    }
    // 无条件把每个开关同步到服务端的真实状态
    list.forEach(it => {
      const cb = grid.querySelector('.sw input[data-inst="' + it.n + '"]');
      if (cb && !cb.disabled && cb.checked !== !!it.online) {
        cb.checked = !!it.online;
      }
    });
    bindSwitches();
    const started = list.filter(x => x.online).length;
    const active = list.filter(x => x.running).length;
    const paused = list.filter(x => x.paused).length;
    $('sum').textContent = '共 ' + list.length + ' 个实例 · 已启动 ' + started
      + ' · 刷课中 ' + active + (paused ? ' · 已暂停 ' + paused : '');

    // 管理动作只对管理员显示
    $('btnAdd').classList.toggle('hide', !isAdmin);
    $('btnStopAll').classList.toggle('hide', !isAdmin);
    $('btnStagger').classList.toggle('hide', !isAdmin);

    // 错峰调度状态
    const sg = r.stagger || {};
    const logs = sg.log || [];
    const box = $('staggerBox');
    if (sg.running || logs.length) {
      box.classList.remove('hide');
      box.innerHTML = '<div style="font-size:13px"><b>'
        + (sg.running ? '错峰调度进行中' : '上次调度记录') + '</b>'
        + '（间隔 ' + Math.round((sg.gap || 0) / 60) + ' 分钟）</div>'
        + '<div style="margin-top:6px;font:12px/1.7 ui-monospace,Consolas,monospace;'
        + 'color:#6b7280">' + logs.map(esc).join('<br>') + '</div>';
    } else {
      box.classList.add('hide');
    }
    $('btnStop').classList.toggle('hide', !sg.running);
  } catch (e) {
    say('加载失败（网络或服务未就绪），5 秒后自动重试：' + e, 'err');
    // 出错时不要静默死掉：自动重试一次
    clearTimeout(loadTimer);
    loadTimer = setTimeout(() => load(true), 5000);
  } finally {
    grid.classList.remove('loading');
  }
}

// 日志里的内容来自后端，插进 innerHTML 前必须转义（防注入/防破版）
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function bindSwitches() {
  document.querySelectorAll('.sw input').forEach(cb => {
    cb.onchange = async () => {
      const n = cb.dataset.inst;
      const wantOn = cb.checked;
      cb.disabled = true;
      busy = true;                       // 暂停自动刷新，别把中间态闪掉
      say((wantOn ? '正在启动' : '正在停止') + ' 实例 ' + n + '…', 'busy');
      try {
        const r = await fetch('/api/instance/' + n
          + (wantOn ? '/start' : '/stop'), {method: 'POST'})
          .then(r => r.json());
        if (r.ok) {
          say(r.msg || '完成', 'ok');
          toast(r.msg || '完成', 'ok');
        } else {
          say(r.msg || '操作失败', 'err');
          toast(r.msg || '操作失败', 'err');
          cb.checked = !wantOn;          // 失败就把开关拨回去，别留假状态
        }
      } catch (e) {
        say('请求失败: ' + e, 'err');
        toast('请求失败，请检查服务是否还在运行', 'err');
        cb.checked = !wantOn;
      } finally {
        cb.disabled = false;
        busy = false;
        setTimeout(() => load(true), wantOn ? 2500 : 800);
      }
    };
  });
}

$('btnRefresh').onclick = () => {
  const b = $('btnRefresh');
  setBusy(b, true);
  load().finally(() => setBusy(b, false));
};
$('btnAdd').onclick = async () => {
  if (!confirm('新增一个账号实例？\\n\\n会创建一个新的空白实例，'
      + '你需要在它自己的页面里登录新的学习通账号。')) return;
  const b = $('btnAdd');
  setBusy(b, true); busy = true;
  say('正在创建新账号…', 'busy');
  try {
    const r = await fetch('/api/instance/add', {method: 'POST'})
      .then(r => r.json());
    if (r.ok) {
      say(r.msg + ' 建好后请到它的页面登录账号。', 'ok');
      toast(r.msg || '已创建', 'ok');
      setTimeout(() => load(true), 3000);
    } else {
      say(r.msg || '创建失败', 'err');
      toast(r.msg || '创建失败', 'err');
    }
  } catch (e) {
    say('请求失败: ' + e, 'err');
    toast('创建失败，请检查服务状态', 'err');
  } finally {
    setBusy(b, false); busy = false;
  }
};
$('btnStopAll').onclick = async () => {
  if (!confirm('停止所有服务？\\n\\n所有实例的刷课会被中断（已上报进度不丢），'
      + '网关也会一起关闭。下次需要重新双击桌面图标启动。')) return;
  const b = $('btnStopAll');
  setBusy(b, true); busy = true;
  say('正在停止全部服务…', 'busy');
  try {
    await fetch('/api/stop_all', {method: 'POST'});
  } catch (e) {}
  clearInterval(loadTimer);
  document.body.innerHTML =
    '<div class="wrap" style="padding-top:80px;text-align:center">'
    + '<h1>服务已停止</h1>'
    + '<div class="sub">所有实例与网关都已关闭，可以关闭此页面了。<br>'
    + '需要时双击桌面的「超星刷课」重新启动。</div></div>';
};
$('btnStagger').onclick = async () => {
  if (!confirm('错峰启动：按 20 分钟间隔，依次让各实例用「上次的选课」开跑。\\n\\n'
      + '注意：刷课会真实提交章节测验且不可重做，确认现在开始？')) return;
  const b = $('btnStagger');
  setBusy(b, true); busy = true;
  say('正在下发错峰调度…', 'busy');
  try {
    const r = await fetch('/api/stagger', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({gap: 1200})}).then(r => r.json());
    say(r.msg || (r.ok ? '已开始' : '启动失败'), r.ok ? 'ok' : 'err');
    if (r.ok) toast(r.msg || '调度已开始', 'ok');
  } catch (e) {
    say('请求失败: ' + e, 'err');
    toast('下发调度失败', 'err');
  } finally {
    setBusy(b, false); busy = false;
    load(true);
  }
};
$('btnStop').onclick = async () => {
  const b = $('btnStop');
  setBusy(b, true); busy = true;
  try {
    const r = await fetch('/api/stagger/stop', {method: 'POST'})
      .then(r => r.json());
    say(r.msg || '已停止调度', 'ok');
    toast('已停止错峰调度', 'ok');
  } catch (e) {
    say('请求失败: ' + e, 'err');
  } finally {
    setBusy(b, false); busy = false;
    load(true);
  }
};
$('btnLogout').onclick = async () => {
  if (!confirm('退出登录？下次需要重新输入口令。')) return;
  const b = $('btnLogout');
  setBusy(b, true);
  try { await fetch('/auth/logout', {method:'POST'}); } catch (e) {}
  location.href = '/login';
};
// 输入口令的页面用回车也能提交
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.activeElement && document.activeElement.blur) {
    document.activeElement.blur();
  }
});
load();
// 每 5 秒静默刷新；管理动作进行中会被 load() 内部跳过
loadTimer = setInterval(() => load(true), 5000);
// 切回标签页立刻刷新一次（手机端息屏回来最常见）
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) load(true);
});
</script></body></html>"""


if __name__ == "__main__":
    gateway_tokens()
    inst = instances()
    print(f"网关已就绪：http://{HOST}:{PORT}/")
    print(f"发现 {len(inst)} 个实例：" +
          (", ".join(f"acc{i['n']}@{i['port']}" for i in inst) if inst else "无"))
    print("把 SakuraFrp 的 1 条隧道映射到本端口即可外网访问。")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
