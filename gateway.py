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
# 安全微伴专用角色（2026-09-16）：从登录页选「安全微伴」并用微伴账号进来的用户，
# **只能**访问微伴控制台 —— 碰不到任何学习通实例、首页和实例接口。
ROLE_WB = "wb"
WB_OK = ("/wb", "/api/whoami", "/auth/logout")
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


# 🔴 2026-09-26：全局防缓存。网关是被浏览器直接访问的入口，它缓存的 HTML
# 会让实例页面的更新"看起来没生效"（点按钮无反应、后端零日志）。
@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0")
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ---------------- 自动回灌状态（学习库 → 全局题库） ----------------
# 2026-09-15 新增。定义放在这里而不是线程函数旁边，是为了让 /api/overview
# （定义在本节下方）能直接引用；虽然 Python 运行时查找能跨顺序访问，但显式
# 靠前更不容易误判成"未定义"。
_SYNC = {
    "every": 600,       # 间隔（秒）。10 分钟——回灌本身很轻（纯 SQLite 读+比对）
    "last_at": None,    # 上次跑完的时间戳（字符串，便于直接展示）
    "last_res": None,   # 上次返回的统计 dict
    "runs": 0,          # 累计跑了几轮
}


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


# ---------------- 新实例默认参数（题库/AI）----------------
# 目标（2026-09-26 用户要求）：任何实例 —— 已存在的和新开的 —— 都预置好
# Agent 的 API Key，用户只需输入账号密码即可开刷，不用再碰题库设置。
#
# 取值来源：现有实例 ui_config.json 里的 AI 配置（单一真源）。
# 不硬编码 key —— 硬编码会让 key 明文进 git；同时用户换 key 后只需改任
# 一实例，其余实例重启网关即跟着换。
# 若所有实例都没有 AI 配置，再退回 config.ini 的 [tiku] 段（key/endpoint/model）。
_TIKU_DEFAULTS = {
    "tiku_type": "adapter",
    "adapter_url": ("http://127.0.0.1:8060/adapter-service/search"
                    "?use=local,icodef,buguake,wanneng,tikuhai"),
    "token": "",
    "ai_fallback": True,
    "auto_submit": True,
    "cover_rate": 0.9,
    "speed": 2,
    "jobs": 1,
}
_AI_FIELDS = ("ai_endpoint", "ai_key", "ai_model")


def _ai_from_instances() -> dict:
    """从已有实例里取一份完整的 AI 配置（last_params 优先，其次 profiles）。"""
    for it in instances():
        cfg = _read_instance_cfg(it["dir"])
        for src in (cfg.get("last_params") or {},
                    *(cfg.get("profiles") or [])):
            if all((src.get(k) or "").strip() for k in _AI_FIELDS):
                return {k: src[k] for k in _AI_FIELDS}
    return {}


def _ai_from_ini() -> dict:
    """退路：读 config.ini 的 [tiku] 段（key / endpoint / model）。"""
    import configparser
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(os.path.join(BASE, "config.ini"), encoding="utf-8")
        sec = cp["tiku"]
    except Exception:
        return {}
    out = {"ai_endpoint": (sec.get("endpoint") or "").strip(),
           "ai_key": (sec.get("key") or "").strip(),
           "ai_model": (sec.get("model") or "").strip()}
    return out if all(out.values()) else {}


def default_params() -> dict:
    """新实例/缺配置实例的默认参数（题库 + AI Key）。"""
    p = dict(_TIKU_DEFAULTS)
    p.update(_ai_from_instances() or _ai_from_ini())
    return p


def apply_defaults(instance_dir: str, force: bool = False) -> bool:
    """把默认参数写进实例的 ui_config.json。返回是否发生了写入。

    - 账号密码、last_pick、resume 一律不动，只补题库/AI 参数
    - force=False（默认）：已有 ai_key 的实例跳过，不覆盖用户在界面上改过的值
    - force=True：无条件重置为默认（用于"统一为我的 Key"）
    """
    path = os.path.join(instance_dir, "ui_config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        return False
    # 兼容旧版「平铺格式」：整份就是一份 last_params
    if "profiles" not in cfg and "last_params" not in cfg and cfg.get("username"):
        cfg = {"profiles": [dict(cfg)], "last_params": dict(cfg)}
    lp = dict(cfg.get("last_params") or {})
    if not force and (lp.get("ai_key") or "").strip():
        return False
    dp = {k: v for k, v in default_params().items() if v not in (None, "")}
    lp.update(dp)
    # profiles 键必须显式写出：只有 last_params 的配置会被 web_ui.load_ui_config
    # 的旧版兼容分支判为"残缺格式"（2026-09-26 已修该分支，但保持格式完整更稳）
    profs = cfg.get("profiles")
    if not isinstance(profs, list):
        profs = []
    for p in profs:
        if isinstance(p, dict):
            p.update(dp)
    cfg["profiles"] = profs
    cfg["last_params"] = lp
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        return True
    except OSError as e:
        print(f"[默认配置] 写入 {path} 失败：{e}")
        return False


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


def _user_allowed(path: str) -> bool:
    """普通用户（查看 + 暂停）能碰的路径。

    ⚠️ 实例反代（/acc/N/...）必须按**子路径**细分：整个 /acc/ 放行的话，
    用户能直接调 /acc/N/api/start 把任务跑起来 —— 等于没设权限。
    这里只放行"看"和"暂停"。
    """
    if path == "/":
        return True
    if any(path.startswith(p) for p in VIEWER_OK if p != "/"):
        return True
    m = re.match(r"^/acc/\d+/(.*)$", path)
    if m:
        sub = m.group(1)
        if sub == "":
            return True
        return any(sub.startswith(k) for k in (
            "api/status", "api/logs", "api/whoami", "api/stop",
            "api/courses", "api/saved-config"))
    return False


@app.before_request
def _auth_gate():
    if request.path in ("/login", "/auth", "/auth/weiban",
                        "/auth/weiban/add", "/favicon.ico"):
        return None
    if _is_local_request():
        g.role, g.who = ROLE_ADMIN, "本机"
        return None
    role = session.get("role")
    if role:
        g.role, g.who = role, session.get("who") or role
        if role == ROLE_USER and not _user_allowed(request.path):
            return jsonify(ok=False, msg="普通用户只能查看和暂停，不能执行该操作"), 403
        # 微伴账号：只放行安全微伴控制台，其余（首页 / 实例 / 实例接口）一律拒绝
        if role == ROLE_WB and not any(
                request.path == p or request.path.startswith(p + "/")
                for p in WB_OK):
            return jsonify(ok=False, msg="微伴账号仅能访问安全微伴控制台"), 403
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
    # 只读口令与账号登录都归入「用户」：能看、能暂停，但不能启动/新增
    # （2026-09-16 按用户要求把原来的三级权限收敛成两级）
    if pwd == tk.get("viewer_token"):
        session["role"], session["who"] = ROLE_USER, "只读口令"
        return jsonify(ok=True, role=ROLE_USER)
    for p in all_profiles():
        if (str(p.get("username") or "") == user
                and str(p.get("password") or "") == pwd):
            session["role"], session["who"] = ROLE_USER, user
            session["inst"] = p.get("_inst")
            inst = p.get("_inst")
            # 🔴 2026-09-28（用户要求）：**账号登录的人直接落到自己的刷课进度页**，
            #    不再停在"多账号刷课控制台"那个实例列表 —— 那是管理员视角，
            #    普通用户看了只会去点别人的实例。管理员 / 口令登录仍回首页。
            return jsonify(ok=True, role=ROLE_USER, inst=inst,
                           redirect=(f"/acc/{inst}/" if inst is not None else "/"))
    return jsonify(ok=False, msg="账号或密码不正确（需与本机已保存的学习通账号一致）")


# 安全微伴：独立虚拟环境 + 校验脚本（都在上游源码目录旁边）
WB_SRC_DIR = os.path.join(os.path.dirname(DATA_ROOT), "weiban_src")
WB_VERIFY_PY = os.path.join(os.path.dirname(DATA_ROOT), "weiban", "verify_login.py")
WB_VENV_PY = os.path.join(WB_SRC_DIR, ".venv", "Scripts", "python.exe")


def _verify_weiban(tenant: str, username: str, password: str, timeout: int = 150):
    """真去登录一次安全微伴，确认账号可用。返回 (ok, errmsg)。

    慢（要过验证码识别），但这是"认证"该有的代价 —— 不能让乱输就进来。
    """
    if not os.path.exists(WB_VENV_PY) or not os.path.exists(WB_VERIFY_PY):
        return False, "安全微伴组件未就绪（缺 weiban_src/.venv 或 verify_login.py）"
    env = os.environ.copy()
    env.update({"WB_TENANT_NAME": tenant, "WB_USERNAME": username,
                "WB_PASSWORD": password, "PYTHONIOENCODING": "utf-8"})
    try:
        r = subprocess.run([WB_VENV_PY, WB_VERIFY_PY], cwd=WB_SRC_DIR, env=env,
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, "认证超时（平台响应慢或验证码识别卡住），请重试"
    except Exception as e:
        return False, f"认证失败：{e}"
    out = f"{r.stdout or ''}\n{r.stderr or ''}"
    if "AUTH_OK" in out:
        return True, ""
    for line in out.splitlines():
        if "AUTH_FAIL" in line:
            return False, line.split("AUTH_FAIL:", 1)[-1].strip()[:180]
    return False, "账号或密码不正确"


# 安全微伴：已加入的账号清单（管理员添加，登录时用来比对；不再去平台验证学校）
WB_ACCOUNTS = os.path.join(DATA_ROOT, "weiban", "accounts.json")


def _wb_accounts() -> dict:
    try:
        with open(WB_ACCOUNTS, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _wb_save_accounts(d: dict) -> None:
    os.makedirs(os.path.dirname(WB_ACCOUNTS), exist_ok=True)
    with open(WB_ACCOUNTS, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


@app.post("/auth/weiban")
def do_auth_weiban():
    """安全微伴入口。

    验证方式（2026-09-16 按用户要求改）：**不再连平台校验学校**，直接拿输入
    去比对管理员添加的本地账号清单 —— 快、且不受平台波动影响。
      ① 密码 = 管理员口令  → 管理员身份进入微伴控制台（不受限）
      ② 学号在已加入清单里 → 学校/密码一致则放行，否则打回
    """
    data = request.get_json(silent=True) or {}
    tenant = str(data.get("tenant") or "").strip()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")

    # ① 管理员口令：直接进（微伴控制台对管理员不设限）
    if password and password == gateway_tokens().get("admin_token"):
        session["role"], session["who"] = ROLE_ADMIN, "管理口令"
        session.pop("wb_user", None)
        return jsonify(ok=True, redirect="/wb/")

    if not (tenant and username):
        return jsonify(ok=False, msg="学校全称与学号都要填")

    # ② 比对已加入的账号
    acct = _wb_accounts().get(username)
    if not acct:
        return jsonify(ok=False,
                       msg="该账号还没加入。请让管理员先在「安全微伴」页把它添加进来")
    if acct.get("tenant") and acct["tenant"] != tenant:
        return jsonify(ok=False,
                       msg=f"学校全称与登记的不一致，应为：{acct['tenant']}")
    if acct.get("password") and password and acct["password"] != password:
        return jsonify(ok=False, msg="密码不正确")

    wb_dir = os.path.join(DATA_ROOT, "weiban")
    try:
        os.makedirs(wb_dir, exist_ok=True)
        with open(os.path.join(wb_dir, "_pending.json"), "w", encoding="utf-8") as f:
            json.dump({"tenant": acct.get("tenant") or tenant, "username": username,
                       "password": password or acct.get("password") or username,
                       "at": time.time()}, f)
    except Exception as e:
        return jsonify(ok=False, msg=f"保存账号失败：{e}")
    session["role"], session["who"], session["wb_user"] = ROLE_WB, f"微伴 · {username}", username
    return jsonify(ok=True, redirect="/wb/")


@app.post("/auth/weiban/add")
def do_weiban_add_account():
    """登录页专用的「管理员添加账号」入口。

    为什么单独开一个：加账号的入口原本只在微伴控制台里，但账号不在清单里就
    进不去控制台 —— 鸡生蛋。这里让管理员在**登录页**直接用口令把账号加上，
    绕开这个循环。凭据是管理员口令，与本页其它入口同一安全级别。
    """
    data = request.get_json(silent=True) or {}
    if str(data.get("admin_token") or "") != gateway_tokens().get("admin_token"):
        return jsonify(ok=False, msg="管理员口令不正确")
    tenant = str(data.get("tenant") or "").strip()
    username = str(data.get("username") or "").strip()
    if not (tenant and username):
        return jsonify(ok=False, msg="学校全称和学号都要填")
    accounts = _wb_accounts()
    if username in accounts:
        return jsonify(ok=False, msg=f"{username} 已经在清单里了")
    accounts[username] = {
        "tenant": tenant,
        "password": str(data.get("password") or "").strip(),
        "added_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    _wb_save_accounts(accounts)
    return jsonify(ok=True, msg=f"已添加 {username}，切回上面就能用它登录了")


@app.route("/api/wb/accounts", methods=["GET", "POST", "DELETE"])
def api_wb_accounts():
    """安全微伴账号清单管理 —— **只有管理员**能增删（用户明确要求）。"""
    if _role() != ROLE_ADMIN:
        return jsonify(ok=False, msg="只有管理员能管理微伴账号"), 403
    accounts = _wb_accounts()

    if request.method == "GET":
        # 不回传密码明文，只说明"有没有设密码"
        return jsonify(ok=True, accounts=[
            {"username": k, "tenant": v.get("tenant", ""),
             "has_password": bool(v.get("password")),
             "added_at": v.get("added_at", "")}
            for k, v in sorted(accounts.items())])

    d = request.get_json(silent=True) or {}
    username = str(d.get("username") or "").strip()
    if not username:
        return jsonify(ok=False, msg="学号必填")

    if request.method == "DELETE":
        accounts.pop(username, None)
        _wb_save_accounts(accounts)
        return jsonify(ok=True, msg=f"已移除 {username}")

    tenant = str(d.get("tenant") or "").strip()
    if not tenant:
        return jsonify(ok=False, msg="学校全称必填")
    accounts[username] = {
        "tenant": tenant,
        "password": str(d.get("password") or "").strip(),
        "added_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    _wb_save_accounts(accounts)
    return jsonify(ok=True, msg=f"已添加 {username}")


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
                   instances=data, stagger=dict(_stagger),
                   sync=dict(_SYNC))


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
            # 新实例预置好题库/AI 默认参数（2026-09-26）：用户只需输账密。
            # 必须在实例进程启动**之前**写，避免它的界面先读到空配置。
            apply_defaults(os.path.join(DATA_ROOT, f"acc{n}"))
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
        its = instances()
        for idx, it in enumerate(its):
            port = it["port"]
            # 2026-09-15：只有"停止全部"才连题库一起关，且放在最后一个实例上
            # 关（避免先关的实例把题库杀了、后面实例的收尾逻辑受影响）。
            allq = "?all=1" if idx == len(its) - 1 else ""
            try:
                requests.post(f"http://127.0.0.1:{port}/api/shutdown{allq}",
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


# ---------------- 自动回灌（学习库 → TikuAdapter 题库） ----------------

# 2026-09-15 新增。用户需求原话："程序如果是能自动的只是会有延迟的话，
# 问题不大，只要确定能把答案关回去就行了。"
#
# 背景：程序有两条"记住答案"的通道 —— 每实例的学习库 learned_answers.json
# 和全局共用的 tikuAdapter/tiku.db。以前两者**没有打通**：某个账号把题做对、
# 平台确认全对，学习库记下了，题库却还是空的 → 换个账号遇到同一道题照样搜不到。
# 修法是靠 sync_learned_to_tiku.py 手工回灌，但那要人记得去跑。
#
# 这里挂一个后台线程定时自动汇总，彻底免掉手工。
# 状态容器 _SYNC 定义在模块顶部（app 之后），此处只放线程逻辑。


def _sync_once():
    """跑一轮回灌，异常全部吞掉（定时任务绝不能因一次失败就死掉）。"""
    try:
        # 延迟导入：sync 脚本会 import 一堆东西，放模块顶层会拖慢网关冷启动；
        # 而且万一脚本有语法问题，也不该连带网关起不来。
        from sync_learned_to_tiku import sync
        r = sync(quiet=True)          # 不带 accs = 汇总全部实例
        _SYNC["last_res"] = r
        _SYNC["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _SYNC["runs"] += 1
        if r and r.get("added"):
            print(f"[自动回灌] 新增 {r['added']} 条 → {r.get('msg')}")
        elif r and not r.get("ok"):
            print(f"[自动回灌] 本轮未写入：{r.get('msg')}")
        elif r and (r.get("fixed_pollute") or r.get("fixed_clean")
                    or r.get("fixed_update") or r.get("conflicts")):
            # 🔴 2026-09-28：矫正结果必须可见。
            #    原先只在"有新增"或"失败"时打印，而题库跑满后 added 恒为 0
            #    → 矫正做完也没任何输出，等于是个黑盒（排查时完全看不到）。
            print(f"[自动回灌] 矫正：清洗污染 {r.get('fixed_pollute', 0)} 条 / "
                  f"更正错答案 {r.get('fixed_update', 0)} 条 / "
                  f"待人工核对 {r.get('conflicts', 0)} 条")
        return r
    except Exception as e:
        _SYNC["last_res"] = {"ok": False, "msg": f"异常：{e}"}
        _SYNC["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[自动回灌] 异常：{e}")
        return None


def _sync_worker():
    """后台定时器。启动后先等一轮（服务刚起来时别抢资源）。

    2026-09-15 加固：整个循环体再包一层 try。`_sync_once` 内部已经全兜底，
    但时间戳格式化、状态字典写入等外围代码万一抛异常，会让**线程静默死亡**——
    之后再也不会自动回灌，而且表面看不出任何症状（这正是最危险的一种故障）。
    包一层后任何意外都只跳过本轮。

    首轮只等 30 秒（原来 60 秒）：改 gateway.py 后要重启网关才生效，而重启会让
    runs 归零、重新进入缓冲期。缓冲期越长，"改完重启后迟迟看不到回灌生效"的
    窗口就越长，排查时容易误以为功能没生效。回灌本身极轻（几百条内存比对 +
    最多几十条 INSERT），30 秒足够让实例和日志稳定下来。
    """
    time.sleep(30)                    # 冷启动缓冲：让实例先起来、日志先落盘
    while True:
        try:
            _sync_once()
        except Exception as e:        # 理论上到不了这里，留着是保险
            print(f"[自动回灌] 线程级异常（已跳过本轮）：{e}")
        time.sleep(_SYNC["every"])


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
    # 普通用户只能进自己那个实例，不能操作别人的账号
    # （原 ROLE_VIEWER 分支已随"权限收敛为两级"移除）
    if role == ROLE_USER and session.get("inst") != n:
        return jsonify(ok=False, msg="无权访问该实例"), 403
    return _forward(n, sub)


# ---------------- 安全微伴反向代理（完全独立的进程 / 端口）----------------
# 安全微伴跑在自己的进程里（weiban/web_ui.py，默认 5011），这里只做一层转发，
# 让远程用户也能访问它 —— 两个平台在进程、端口、数据目录上都是分开的：
# 这边崩了不会影响学习通，反之亦然。
WB_PORT = int(os.environ.get("WB_PORT", "5011"))


def _forward_wb(sub: str):
    url = f"http://127.0.0.1:{WB_PORT}/{sub}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _SKIP_HEADERS}
    headers["X-Base-Path"] = "/wb"
    # 把"当前是哪种身份"告诉微伴控制台（跨进程，它读不到网关的 session）：
    #   X-WB-Isolation=1 → 是微伴账号进来的：只能操作自己那个账号，
    #                      且页面上不该出现"学习通"入口
    #   X-WB-User        → 绑定到哪个账号
    if _role() == ROLE_WB:
        headers["X-WB-Isolation"] = "1"
        headers["X-WB-User"] = str(session.get("wb_user") or "")
    try:
        resp = requests.request(
            request.method, url, params=request.args,
            data=request.get_data(), headers=headers, cookies=request.cookies,
            allow_redirects=False, timeout=120)
    except Exception:
        return Response(
            "安全微伴控制台未启动（端口 %d）。请在项目根目录运行："
            "python weiban/web_ui.py" % WB_PORT,
            status=502, mimetype="text/plain; charset=utf-8")
    out = Response(resp.content, resp.status_code)
    for k, v in resp.headers.items():
        if k.lower() in _SKIP_RESP_HEADERS:
            continue
        if k.lower() == "location" and v.startswith("/"):
            v = f"/wb{v}"
        out.headers[k] = v
    return out


@app.route("/wb/", defaults={"sub": ""}, methods=["GET", "POST"])
@app.route("/wb/<path:sub>", methods=["GET", "POST"])
def proxy_wb(sub):
    # 普通用户（只读口令 / 账号）可以进微伴控制台做"查看 + 暂停"；
    # 微伴账号只能看自己的（由 _forward_wb 注入身份头，控制台端再校验一次）。
    return _forward_wb(sub)


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
  .box{width:min(92vw,380px);background:rgba(255,255,255,.58);
       border:1px solid rgba(255,255,255,.68);border-radius:14px;
       padding:26px 24px;
       box-shadow:0 6px 24px rgba(0,0,0,.06),inset 0 1px 0 rgba(255,255,255,.60);
       backdrop-filter:blur(12px) saturate(160%);
       -webkit-backdrop-filter:blur(12px) saturate(160%)}
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
  /* ---- 平台选择：登录页就分平台（2026-09-16）---- */
  .tabs{display:flex;gap:8px;margin:0 0 16px}
  .tab{flex:1;padding:9px 6px;border:1px solid #d8dee4;border-radius:9px;background:#fff;
       color:#374151;font-size:14px;cursor:pointer;margin:0;width:auto}
  .tab.on{background:#2563eb;color:#fff;border-color:#2563eb}
  .hide{display:none !important}
/* ---- 视觉增强·第三轮（2026-09-24）：设计系统 + 极光背景 + 克制动效 ----
   约束：只加/覆盖**外观**属性，不动任何尺寸、布局、结构与 JS 行为。
   所有动画只作用于 transform / opacity / box-shadow，保证合成层 60fps。 */
:root{
  --ink:#161b22; --ink-2:#3d4757; --ink-3:#6b7686; --ink-4:#9aa3b0;
  --line:#e3e8f0; --line-2:#eef2f8;
  --brand:#2563eb; --brand-2:#3b82f6; --brand-3:#60a5fa;
  --blue-ink:#1d4ed8;
  --card:#ffffff;
  --sh-1:0 1px 2px rgba(16,24,40,.05);
  --sh-2:0 1px 2px rgba(16,24,40,.05), 0 10px 28px -12px rgba(16,24,40,.14);
  --sh-3:0 2px 6px rgba(16,24,40,.07), 0 20px 44px -16px rgba(16,24,40,.20);
  --ease-out-quart:cubic-bezier(.25,1,.5,1);
  --ease-out-quint:cubic-bezier(.22,1,.36,1);
  --ease-out-expo:cubic-bezier(.16,1,.3,1);
  --ease-in-out:cubic-bezier(.65,0,.35,1);
  --ease-spring:cubic-bezier(.34,1.56,.64,1);   /* 回弹：按下松开用 */
  --mx:50vw; --my:50vh;                          /* 光标位置，由 JS 写入 */
}
/* ---- 2026-09-24 第四轮：把"看不太出来"的静态背景改成真正会动的环境 ---- */
body{
  color:var(--ink);
  background:
    radial-gradient(1100px 700px at 8% -6%,   rgba(96,165,250,.42), transparent 60%),
    radial-gradient(900px 580px at 92% 2%,    rgba(34,211,238,.32), transparent 58%),
    radial-gradient(820px 640px at 78% 106%,  rgba(56,189,248,.30), transparent 60%),
    radial-gradient(700px 520px at 16% 98%,   rgba(45,212,191,.26), transparent 60%),
    linear-gradient(168deg,#f4f8fe 0%,#e8f0fb 48%,#e3ecf9 100%);
  background-attachment:fixed;
}
/* 极光：三层大光斑缓慢漂移。幅度从 ±3% 提到 ±14%，肉眼可见但不晃眼 */
body::before{
  content:"";position:fixed;inset:-40%;z-index:-2;pointer-events:none;
  background:
    radial-gradient(62% 52% at 24% 26%, rgba(56,189,248,.55) 0%, rgba(56,189,248,.55) 44%, rgba(56,189,248,0.18) 64%, transparent 72%),
    radial-gradient(58% 48% at 76% 60%, rgba(96,165,250,.47) 0%, rgba(96,165,250,.47) 44%, rgba(96,165,250,0.16) 64%, transparent 72%),
    radial-gradient(54% 46% at 46% 90%, rgba(34,211,238,.42) 0%, rgba(34,211,238,.42) 44%, rgba(34,211,238,0.14) 64%, transparent 73%),
    radial-gradient(46% 40% at 88% 22%, rgba(125,211,252,.38) 0%, rgba(125,211,252,.38) 44%, rgba(125,211,252,0.13) 64%, transparent 74%);
  animation:auroraA 14s ease-in-out infinite;
  will-change:transform;
}
@keyframes auroraA{
  0%  {transform:translate3d(-24%,-16%,0) scale(1.10) rotate(-6deg)}
  25% {transform:translate3d(18%,-6%,0)   scale(1.26) rotate(5deg)}
  50% {transform:translate3d(-4%,20%,0)   scale(1.15) rotate(-2deg)}
  75% {transform:translate3d(22%,10%,0)   scale(1.30) rotate(7deg)}
  100%{transform:translate3d(-24%,-16%,0) scale(1.10) rotate(-6deg)}
}
/* 光带扫过：斜向高光带 9s 横穿全屏 —— 最直观的"有东西在流动" */
body .sweep{
  position:fixed;top:-60%;left:-60%;width:220%;height:220%;
  z-index:-2;pointer-events:none;
  /* ⚠️ 光带亮度 2026-09-24 从 rgba(255,255,255,.55)/…54,.42 下调到 .20/.16。
     真因：`.sweep` 是一条 55% 白的高对比光带，扫过半透明卡片（alpha .40/.30）时
     会让**整张卡片底色大幅跳变** —— 用户录屏实测：关掉 sweep 后卡片内帧差
     下降 76%（1.770 → 0.433），是"整个框全部闪"的唯一主犯。
     压低对比后，光带在卡片外依然看得见（背景没被削弱），但在卡片下不再打脸。 */
  background:linear-gradient(102deg,
     transparent 28%,rgba(255,255,255,.10) 47%,rgba(191,219,254,.08) 50%,
     rgba(255,255,255,.10) 53%,transparent 72%);
  animation:sweepMove 9s cubic-bezier(.45,0,.55,1) infinite;
  will-change:transform;
}
@keyframes sweepMove{
  0%  {transform:translate3d(-38%,0,0)  rotate(-8deg);opacity:0}
  8%  {opacity:.85}
  50% {opacity:.85}
  58% {opacity:0}
  100%{transform:translate3d(38%,0,0)   rotate(-8deg);opacity:0}
}
/* 网格：6s 走一格（约 9px/秒），明显在爬。只动 background-position */
body .gridflow{
  content:"";position:fixed;inset:-10%;z-index:-3;pointer-events:none;opacity:.30;
  background-image:
    linear-gradient(to right, rgba(37,99,235,.055) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(37,99,235,.055) 1px, transparent 1px);
  background-size:72px 72px;
  animation:gridDrift 6s linear infinite;
  will-change:transform;
}
@keyframes gridDrift{
  from{transform:translate3d(0,0,0)}
  to  {transform:translate3d(72px,72px,0)}
}
/* 呼吸光晕：中心亮度 8s 起伏，整页像在"呼吸" */
body .breathe{
  position:fixed;inset:0;z-index:-2;pointer-events:none;
  background:radial-gradient(60% 50% at 50% 45%,
             rgba(96,165,250,.34), transparent 70%);
  animation:breathe 8s ease-in-out infinite;
  will-change:transform,opacity;
}
@keyframes breathe{
  0%  {transform:scale(.92);opacity:.42}
  50% {transform:scale(1.16);opacity:.92}
  100%{transform:scale(.92);opacity:.42}
}
/* 漂浮光点：各自独立漂移，营造"有东西在游"的生气 */
body .orbs{position:fixed;inset:0;z-index:-2;pointer-events:none}
body .orbs i{position:absolute;display:block;border-radius:50%;
  will-change:transform,opacity}
body .orbs i:nth-child(1){width:190px;height:190px;left:12%;top:18%;
  background:radial-gradient(circle,rgba(56,189,248,0.58) 0%,rgba(56,189,248,0.50) 40%,transparent 70%);
  animation:orbA 11s ease-in-out infinite}
body .orbs i:nth-child(2){width:260px;height:260px;left:74%;top:12%;
  background:radial-gradient(circle,rgba(34,211,238,0.50) 0%,rgba(34,211,238,0.43) 40%,transparent 70%);
  animation:orbB 17s ease-in-out infinite}
body .orbs i:nth-child(3){width:160px;height:160px;left:58%;top:72%;
  background:radial-gradient(circle,rgba(45,212,191,0.52) 0%,rgba(45,212,191,0.45) 40%,transparent 70%);
  animation:orbC 9.5s ease-in-out infinite}
body .orbs i:nth-child(4){width:210px;height:210px;left:22%;top:76%;
  background:radial-gradient(circle,rgba(244,114,182,0.42) 0%,rgba(244,114,182,0.36) 40%,transparent 70%);
  animation:orbD 13.5s ease-in-out infinite}
body .orbs i:nth-child(5){width:130px;height:130px;left:48%;top:38%;
  background:radial-gradient(circle,rgba(52,211,153,0.44) 0%,rgba(52,211,153,0.38) 40%,transparent 70%);
  animation:orbE 7.5s ease-in-out infinite}
@keyframes orbA{
  0%,100%{transform:translate3d(0,0,0)         scale(1)}
  33%    {transform:translate3d(90px,-70px,0)  scale(1.25)}
  66%    {transform:translate3d(-60px,50px,0)  scale(.85)}
}
@keyframes orbB{
  0%,100%{transform:translate3d(0,0,0)          scale(1)}
  40%    {transform:translate3d(-120px,80px,0)  scale(1.3)}
  70%    {transform:translate3d(60px,120px,0)   scale(.9)}
}
@keyframes orbC{
  0%,100%{transform:translate3d(0,0,0)           scale(.9)}
  35%    {transform:translate3d(-80px,-110px,0)  scale(1.35)}
  75%    {transform:translate3d(110px,-40px,0)   scale(1)}
}
@keyframes orbD{
  0%,100%{transform:translate3d(0,0,0)            scale(1.05)}
  45%    {transform:translate3d(130px,-90px,0)   scale(.8)}
  80%    {transform:translate3d(-40px,-140px,0)  scale(1.2)}
}
/* orbE：最短循环 + 纯位移，和其他几个的"缩放型"形成节奏差 */
@keyframes orbE{
  0%  {transform:translate3d(-30px,20px,0)    scale(.9)}
  50% {transform:translate3d(-110px,-60px,0)  scale(1.3)}
  100%{transform:translate3d(-30px,20px,0)    scale(.9)}
}
/* 顶部流动渐变条：色相循环，明确表示"系统在运行" */
body .flowbar{
  position:fixed;top:0;left:0;right:0;height:3px;z-index:50;
  pointer-events:none;
  background:linear-gradient(90deg,
    #38bdf8,#22d3ee,#5eead4,#7dd3fc,#38bdf8,#22d3ee,#7dd3fc,#38bdf8);
  background-size:300% 100%;
  animation:flowBar 5s linear infinite;
  box-shadow:0 0 10px rgba(59,130,246,.55);
}
@keyframes flowBar{
  from{background-position:0% 0}
  to  {background-position:300% 0}
}
/* 光标跟随光晕：跟随鼠标的柔和蓝光（叠加在自动流动之上，不替代） */
body .cursorGlow{
  position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:0;
  background:radial-gradient(360px 360px at var(--mx) var(--my),
             rgba(56,189,248,.22), transparent 70%);
  transition:opacity .5s var(--ease-out-quart);
  /* ⚠️ 2026-09-28 删掉 will-change:background —— background 无法 GPU 合成，
     它只会让浏览器持续为主线程重绘做准备，是全屏级别的性能陷阱。 */
}
body.pointerFine .cursorGlow{opacity:1}
/* 细噪点：仅 3.5% 不透明度，只为消除渐变的塑料感 */
body::after{
  content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.42;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)' opacity='.035'/%3E%3C/svg%3E");
}
/* 🔴 卡片整体「半融入背景」（2026-09-24 用户要求）：
   原来是 rgba(255,255,255,.94) ≈ 实心白，像一块白板贴在流动背景上；
   现在透明度降到 .58/.46 + 加强 backdrop-filter 做磨砂，
   让后面的极光/光带/网格透上来，卡片像"浮在流动背景上的玻璃"。
   ⚠️ 可读性靠两点兜住：① backdrop-filter blur 把背景糊匀；② 卡片内的
   input / 按钮本身不透明度更高（见下方 input 规则），文字底色始终够实。 */
.box{background:linear-gradient(180deg,rgba(255,255,255,.40) 0%,rgba(252,253,255,.30) 100%);
     border:1px solid rgba(255,255,255,.68);
     box-shadow:0 1px 2px rgba(16,24,40,.04),
                0 12px 30px -14px rgba(16,24,40,.13),
                0 36px 76px -34px rgba(37,99,235,.20),
                inset 0 1px 0 rgba(255,255,255,.60),
                inset 0 -1px 0 rgba(255,255,255,.28);
     backdrop-filter:blur(12px) saturate(160%);
     -webkit-backdrop-filter:blur(12px) saturate(160%)}
h1{letter-spacing:.2px;color:var(--ink)}
input{background:#fcfdff;color:var(--ink);
      transition:border-color .18s var(--ease-out-quart),
                 box-shadow .18s var(--ease-out-quart),
                 background .18s var(--ease-out-quart),
                 transform .18s var(--ease-out-quart)}
input:hover{border-color:#c3cfe2}
input:focus{outline:none;border-color:var(--brand);background-color:rgba(255,255,255,.90);
            box-shadow:0 0 0 3.5px rgba(37,99,235,.14);transform:translateY(-1px)}
/* ---- 主按钮：渐变底 + 光泽扫过 + 物理按压 + 松开回弹 ---- */
button{background:linear-gradient(180deg,var(--brand-2) 0%,var(--brand) 100%);
       box-shadow:0 1px 2px rgba(37,99,235,.28),0 8px 18px -6px rgba(37,99,235,.45);
       transition:transform .16s var(--ease-out-quart),
                  box-shadow .16s var(--ease-out-quart),
                  filter .16s var(--ease-out-quart);
       position:relative;overflow:hidden;isolation:isolate;
       transform:translateZ(0)}
button::after{                      /* 光泽：默认停在画布外，悬停时扫过 */
  content:"";position:absolute;inset:0;z-index:-1;pointer-events:none;
  background:linear-gradient(105deg,transparent 30%,rgba(255,255,255,.42) 50%,transparent 70%);
  transform:translate3d(-115%,0,0);transition:transform .62s var(--ease-out-expo)}
button:hover:not(:disabled)::after{transform:translate3d(115%,0,0)}
button:hover:not(:disabled){transform:translateY(-2px);
       box-shadow:0 3px 6px rgba(37,99,235,.3),0 14px 26px -8px rgba(37,99,235,.58)}
/* 按下：明确下沉 + 收紧阴影，手感"被压进去"。
   ⚠️ 必须用 transition 简写整体覆盖：只写 transition-duration 的话，
   timing-function 会沿用松开时的 --ease-spring（前 34% 几乎不动），
   实测按下要 250ms+ 才看得出下沉，手感是"点了没反应"。 */
button:active:not(:disabled){transform:translateY(1px) scale(.97);
       filter:brightness(.95);
       box-shadow:0 0 0 0 rgba(37,99,235,.3),0 2px 5px rgba(37,99,235,.32);
       transition:transform .06s linear,filter .06s linear,box-shadow .06s linear}
/* 松开：弹性回弹（:active 结束后 transform 回到 hover/rest，
   用 --ease-spring 让它"过冲一点再落定"） */
button:not(:active):not(:disabled){
       transition:transform .34s var(--ease-spring),
                  box-shadow .3s var(--ease-out-quart),
                  filter .2s var(--ease-out-quart)}
button:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
button:disabled{cursor:default}
/* 点一下的"波纹扩散"：由 JS 注入 span.ripple 后自动播放并移除 */
button .ripple{position:absolute;border-radius:50%;pointer-events:none;
       background:radial-gradient(circle,rgba(255,255,255,.55) 0%,rgba(255,255,255,0) 70%);
       transform:translate(-50%,-50%) scale(0);opacity:.9;
       animation:rippleOut .52s var(--ease-out-quart) forwards;z-index:-1}
@keyframes rippleOut{
  0%  {transform:translate(-50%,-50%) scale(0);opacity:.9}
  100%{transform:translate(-50%,-50%) scale(1);opacity:0}
}
/* 主按钮磁吸：JS 写入 --pull-x/--pull-y（上限 4px），悬停时轻微被"吸"过去 */
button.magnetic:hover:not(:disabled){
       transform:translate(calc(var(--pull-x,0px) + 0px),
                           calc(-2px + var(--pull-y,0px)))}
button.magnetic:active:not(:disabled){
       transform:translate(0,1px) scale(.97);
       filter:brightness(.95);
       box-shadow:0 0 0 0 rgba(37,99,235,.3),0 2px 5px rgba(37,99,235,.32);
       transition:transform .06s linear,filter .06s linear,box-shadow .06s linear}
.tab{transition:background .18s var(--ease-out-quart),color .18s var(--ease-out-quart),
                  border-color .18s var(--ease-out-quart),box-shadow .18s var(--ease-out-quart)}
.tab:hover:not(.on){border-color:#b9c6dc;background:#f8fafd}
.tab.on{background:linear-gradient(180deg,var(--brand-2) 0%,var(--brand) 100%);
        box-shadow:0 1px 2px rgba(37,99,235,.25),0 6px 14px -6px rgba(37,99,235,.5)}
.tab::after{display:none}          /* 标签页不要光泽，避免闪烁 */
summary{transition:color .16s var(--ease-out-quart)}
summary:hover{color:var(--brand)}
.eye::after{display:none}
.eye:hover{color:var(--brand);background:none;box-shadow:none;transform:none}
/* 眼睛按钮复位：它继承了全局 button 的白底/圆角/内边距，
   在输入框里会变成一个突兀的白方块（手机端还会被撑到 50px 高、贴出边框）。 */
.eye{background:none!important;border:0!important;box-shadow:none!important;
     min-height:0!important;width:auto!important;border-radius:8px;
     padding:8px 9px;margin:0;transform:translateY(-50%)}
.eye:active{transform:translateY(-50%) scale(.94)!important;filter:none!important;
     transition:transform .08s linear!important}
.eye:focus-visible{outline:2px solid var(--brand);outline-offset:1px}

/* ---- 视觉增强·第二轮：精致化细节，仍只加外观属性 ---- */
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(100,116,139,.28);border-radius:999px;
      border:2px solid transparent;background-clip:content-box}
::-webkit-scrollbar-thumb:hover{background:rgba(100,116,139,.45);background-clip:content-box}
button,.tab,.box,input{transition-timing-function:var(--ease-out-quart)}
body{animation:fadeIn .34s var(--ease-out-quart)}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
/* 卡片入场：轻微上浮，克制（6px，不要更大） */
.box{animation:cardIn .46s var(--ease-out-expo) both}
@keyframes cardIn{from{opacity:0;transform:translate3d(0,10px,0)}to{opacity:1;transform:none}}
::selection{background:rgba(37,99,235,.16)}
.box h1{position:relative;padding-left:11px}
.box h1::before{content:"";position:absolute;left:0;top:50%;transform:translateY(-50%);
      width:3px;height:17px;border-radius:2px;
      background:linear-gradient(180deg,var(--brand-3),var(--brand))}
input::placeholder{color:var(--ink-4)}
@keyframes spin{to{transform:rotate(360deg)}}
.spin{display:inline-block;width:11px;height:11px;margin-right:6px;
      border:2px solid rgba(255,255,255,.4);border-top-color:#fff;
      border-radius:50%;animation:spin .7s linear infinite;vertical-align:-1px}
.msg .spin,.msg .spin{border-color:rgba(37,99,235,.25);border-top-color:var(--brand)}
/* 按钮成功态：保存/登录成功后短暂变成对勾绿（三点式：忙碌→成功→复位） */
button.done{background:linear-gradient(180deg,#34d399 0%,#10b981 100%);
      box-shadow:0 1px 2px rgba(16,185,129,.3),0 8px 18px -6px rgba(16,185,129,.5);
      animation:donePop .42s var(--ease-spring)}
@keyframes donePop{
  0%  {transform:scale(1)}
  45% {transform:scale(1.045)}
  100%{transform:scale(1)}
}
button.done .lbl::before{content:"✓ "}
/* ===== 输入框浅特效（2026-09-24 手机端优化）=====
   设计原则：**"浅"不等于"没有"** —— 桌面端原有 hover/focus 在手机上根本触发不了
   （触屏没有 hover），所以这里补的是一套"触屏也能感知"的细腻反馈：
     ① 常驻一层极淡的内阴影，让白底输入框有"陷进去"的纸感
     ② 聚焦时边框走一圈渐变光（用 background-image 叠在 border 上，不改尺寸）
     ③ 底边一条高光从左铺到右（伪元素，纯 transform，GPU 合成零重排）
     ④ 抬升阴影分层：近处紧、远处散，比单层 box-shadow 更"高级"
   全部只动 transform / opacity / box-shadow / background —— 不碰 layout。 */
input{box-shadow:inset 0 1px 2px rgba(15,32,74,.045), 0 1px 2px rgba(15,32,74,.04)}
input:hover{border-color:#c3cfe2;
            box-shadow:inset 0 1px 2px rgba(15,32,74,.05),
                       0 2px 6px -1px rgba(37,99,235,.10)}
input:focus{border-color:var(--brand);background-color:rgba(255,255,255,.90);
            box-shadow:inset 0 1px 2px rgba(15,32,74,.03),
                       0 0 0 3.5px rgba(37,99,235,.14),
                       0 4px 12px -3px rgba(37,99,235,.26),
                       0 12px 28px -12px rgba(37,99,235,.30);
            transform:translateY(-1px)}
/* ⚠️ input 是替换元素，::before/::after **不生效**。
   想要"底边高光扫过"必须包一层容器（.field）或用 label 兜住。
   这里用 label 的 ::after 实现：聚焦时那条线从左铺到右。 */
label{position:relative;display:inline-block}
label:has(+ input:focus)::after,
label:has(+ .pwrow input:focus)::after{
  content:"";position:absolute;left:0;right:0;bottom:-6px;height:1.5px;
  background:linear-gradient(90deg,transparent,var(--brand-2),var(--brand),transparent);
  border-radius:2px;animation:ulSweep .5s var(--ease-out-expo) both}
@keyframes ulSweep{
  0%  {transform:scaleX(0);opacity:0;transform-origin:left}
  60% {opacity:1}
  100%{transform:scaleX(1);opacity:.85;transform-origin:left}
}
/* 聚焦时输入文字轻微"亮起"，强化"我在输入"的感觉 */
input:not(:placeholder-shown){color:var(--ink);font-weight:500}
input::placeholder{color:var(--ink-4);
      transition:opacity .2s var(--ease-out-quart)}
input:focus::placeholder{opacity:.5}
@media (prefers-reduced-motion: reduce){
  body{animation:none}
  body::before,.gridflow{animation:none}
  body.pointerFine .cursorGlow{opacity:0}
  .box{animation:none}
  button::after{display:none}
  button .ripple{display:none}
  button:hover:not(:disabled){transform:none}
  button:active:not(:disabled){transform:none;filter:none}
  button.magnetic:hover:not(:disabled),button.magnetic:active:not(:disabled){transform:none}
  button.done{animation:none}
  label::after{animation:none}
  label::after,input{transition:none}
}
/* ===== 手机端专项优化（2026-09-24）=====
   用户原话："手机端看这个界面就太普通了"。
   桌面端那套 hover/光标光晕/极光在手机上要么触发不了、要么太费电，
   所以手机端是**另一套**：更克制、更聚焦"手指够得着 + 看得清 + 不卡"。

   核心 5 条：
     ① 触控目标 ≥44px（iOS HIG）/ 48dp（Material），输入框和按钮都不能更小
     ② 字号 ≥16px —— **低于 16px iOS Safari 聚焦时会自动放大页面**（这是手机端
        最容易被忽略的体验灾难，页面突然被推大）
     ③ 减轻背景动效：手机上 GPU 弱，六层流动背景耗电且掉帧 → 只留两层
     ④ 去掉 hover 类效果（触屏没有 hover，留着会出现"点完还亮着"的粘滞态）
     ⑤ 卡片内边距收窄 + 圆角加大，贴合移动端审美
   ⚠️ 全部写在 @media 里，桌面端一个像素都不动。 */
@media (max-width: 640px){
  /* ---- ⓪ 先给窄屏把基础节奏拉开 ----
     ⚠️ 第一版教训：只做"等比放大"（输入框 48px + 16px）而说明文字仍是 12.5px，
     结果是"大控件 + 小文字"挤在一起，看着又闷又笨 —— 用户原话：
     "放大排在一起太拥挤了，反而没有那种灵活感"。
     正解是**拉开层级差**：标题更大、说明更清楚、字段间距更大、卡片更宽松。 */
  html{-webkit-text-size-adjust:100%}

  /* --- ① 输入框：够大够好点，但不要太"重" --- */
  input{
    min-height:50px;
    padding:14px 16px;
    font-size:16px;            /* ⚠️ 必须 ≥16px，否则 iOS 聚焦时整页被放大 */
    border-radius:14px;
    box-shadow:inset 0 1px 2px rgba(15,32,74,.05), 0 1px 2px rgba(15,32,74,.04);
  }
  .pwrow input{padding-right:60px}
  /* 触屏没有 hover：去掉位移，避免"点完还浮着"的粘滞感 */
  input:hover{border-color:#d8dee4;box-shadow:inset 0 1px 2px rgba(15,32,74,.045),0 1px 2px rgba(15,32,74,.04)}
  input:focus{transform:none;     /* 不位移，避免键盘弹起时页面跳动 */
              border-color:var(--brand);
              box-shadow:inset 0 1px 2px rgba(15,32,74,.03),
                         0 0 0 4px rgba(37,99,235,.15),
                         0 4px 14px -4px rgba(37,99,235,.28)}

  /* --- ② 字段节奏：label 与输入框之间、字段与字段之间都要透气 --- */
  label{display:block;font-size:14px;font-weight:500;color:#3d4657;
        margin:20px 0 9px;letter-spacing:.2px}
  /* 第一个 label 紧跟说明文字，不要再叠一层上边距 */
  p.sub + label{margin-top:16px}

  /* --- ③ 按钮：整宽 + 大触控区 --- */
  button{min-height:52px;font-size:16.5px;border-radius:14px;
         letter-spacing:.3px;font-weight:500}
  #btn,#btnWb{margin-top:28px}          /* 与上方输入框拉开，别贴着 */
  /* 触屏用 :active 代替 hover（按下即反馈，这才是手机该有的手感） */
  button:hover:not(:disabled){transform:none}
  button:active:not(:disabled){transform:scale(.972);filter:brightness(.96);
         transition:transform .08s linear,filter .08s linear}

  /* --- ④ 卡片：更宽松的留白 + 更大圆角，才有"轻盈"感 --- */
  .box{width:min(92vw,400px);
       padding:34px 26px 30px;border-radius:24px;
       box-shadow:0 1px 2px rgba(15,32,74,.04),
                  0 18px 44px -18px rgba(37,99,235,.28),
                  0 40px 80px -40px rgba(15,23,42,.22)}
  /* 头部三块（标题 / 分段控件 / 说明）之间要给足气口，
     贴太紧就会显得"挤成一坨"、没有灵活感 */
  h1{font-size:24px;font-weight:650;letter-spacing:.3px;margin:0 0 20px}
  .tabs{margin-bottom:0}
  .tabs,.tab{gap:0}
  .tab{min-height:50px;font-size:15.5px;border-radius:13px}
  p.sub{font-size:13.5px;line-height:1.7;color:#66707f;margin:18px 0 4px}
  .tip{font-size:12.5px;line-height:1.9;color:#949eae;
       margin-top:30px;padding-top:22px}

  /* --- ⑤ 背景动效：全保留，只把"贵"的部分降级 ---
     ⚠️ 第一版把 .sweep/.orbs/.flowbar 直接 display:none，用户立刻反馈
     "动态效果和流动效果还是完全没有"。手机上这几层全是 transform/opacity，
     走 GPU 合成、不触发重排，中端机完全跑得动 —— 不该为省电把设计语言砍掉。
     真正贵的只有 filter:blur（光点），所以**只减 blur 半径**，不动层数。
     ⚠️ 必须用 `body .orbs i:nth-child(n)` —— 基础定义写在这个特异性上，
        只写 `.orbs i` 会被它压住（静默失效）。 */
  body .orbs i:nth-child(n){width:170px;height:170px}
  body .orbs i:nth-child(2){width:210px;height:210px}
  .sweep{opacity:.85}
  .gridflow{opacity:.24}              /* 手机屏小、方格更容易显重，略淡一档 */
  .breathe{opacity:.7}

  /* --- ⑥ 触屏设备不显示光标光晕（本来就没有鼠标） --- */
  .cursorGlow{display:none}

  /* ===== ⑥ 流动感加强（2026-09-24 第三轮 · 用户第二次反馈）=====
     用户原话：「手机端的流动感还是没有，手机端的屏幕本身比较小，
               又不用加载那么大的地方，应该小区域的流动加大一点，
               要不然压根看不出流动感，就像一个死背景」

     根因（我算过账）：
       桌面参数是按 1440px 宽屏调的，照搬到 390px 屏上，
       **每秒可见位移 ÷ 屏宽** 被稀释了近 4 倍 →
         · 光点 orbA：90px/11s = 8.2px/s   （肉眼几乎静止）
         · 背景网格：72px/6s   = 12px/s    （勉强）
         · 极光 body::before：blur(48px) 在 390px 屏上把色斑糊成整屏单色，
           层次被抹平 —— 这是"死背景感"最大的元凶
       验收标准：**每秒可见位移 ≥ 15px**（比桌面翻倍，因为屏小、参照物近）。

     对策：「小区域、大位移、快周期」三件套 ——
       位移 → 放到 2.2 倍（光点 90→200px 级）
       周期 → 压到 0.62 倍（sweep 9→5.5s、grid 6→3.6s、breathe 8→5s）
       模糊 → 极光 48→26px（恢复层次，同时更省 GPU）
     ⚠️ 仍然只动 transform / opacity / background-position，
        不碰 layout，手机 GPU 合成完全扛得住。 */

  /* ① 极光：去糊、加幅、加速（"死背景"第一元凶） */
  body::before{
    animation-duration:9s;
  }
  @keyframes auroraA_m{
    0%  {transform:translate3d(-34%,-24%,0) scale(1.06) rotate(-7deg)}
    25% {transform:translate3d(26%,-10%,0)  scale(1.34) rotate(6deg)}
    50% {transform:translate3d(-8%,28%,0)   scale(1.18) rotate(-3deg)}
    75% {transform:translate3d(32%,14%,0)   scale(1.40) rotate(8deg)}
    100%{transform:translate3d(-34%,-24%,0) scale(1.06) rotate(-7deg)}
  }

  /* ② 光带：9s → 5.5s，位移 ±38% → ±52%（明显"刷"过去）
     ⚠️ 2026-09-24 追加：手机端**不再把 opacity 抬到 1**（桌面 .85）。
        卡片在窄屏上几乎占满整屏，光带越亮 → 卡片内明暗跳变越猛。
        用户录屏「整个框全部闪」的主犯就是它（关掉后卡片内帧差 -76%）。
        现在改成 .55 —— 背景里仍能看见光带扫过，卡片下不再打脸。 */
  body .sweep{
    animation-duration:5.5s;
    opacity:.55;
  }
  @keyframes sweepMove_m{
    0%  {transform:translate3d(-52%,0,0) rotate(-10deg);opacity:0}
    8%  {opacity:.55}
    50% {opacity:.55}
    58% {opacity:0}
    100%{transform:translate3d(52%,0,0)  rotate(-10deg);opacity:0}
  }

  /* ③ 网格：6s → 3.6s（走一格 72px ⇒ 20px/s，跨过 15px/s 门槛） */
  body .gridflow{
    animation-duration:3.6s;
    background-size:64px 64px;inset:-22%;      /* 屏小，格距也收一点，格数不变才不显空 */
    opacity:.22;
  }
  @keyframes gridDrift_m{
    from{transform:translate3d(0,0,0)}
    to  {transform:translate3d(64px,64px,0)}
  }

  /* ④ 呼吸：8s → 5s，幅度也拉开（.90↔1.22） */
  body .breathe{
    animation-duration:5s;
    opacity:.8;
  }
  @keyframes breathe_m{
    0%  {transform:scale(.90);opacity:.46}
    50% {transform:scale(1.22);opacity:1}
    100%{transform:scale(.90);opacity:.46}
  }

  /* ⑤ 光点：位移 ×2.2、周期压到 0.62 倍
        —— 这是手机端"有东西在游"的主力 */
  body .orbs i:nth-child(1){animation-duration:6.8s}
  body .orbs i:nth-child(2){animation-duration:10.5s}
  body .orbs i:nth-child(3){animation-duration:5.9s}
  body .orbs i:nth-child(4){animation-duration:8.4s}
  body .orbs i:nth-child(5){animation-duration:4.6s}
  @keyframes orbA_m{
    0%,100%{transform:translate3d(0,0,0)            scale(1)}
    33%    {transform:translate3d(190px,-150px,0)   scale(1.4)}
    66%    {transform:translate3d(-130px,110px,0)   scale(.78)}
  }
  @keyframes orbB_m{
    0%,100%{transform:translate3d(0,0,0)            scale(1)}
    40%    {transform:translate3d(-230px,160px,0)   scale(1.45)}
    70%    {transform:translate3d(120px,230px,0)    scale(.82)}
  }
  @keyframes orbC_m{
    0%,100%{transform:translate3d(0,0,0)            scale(.88)}
    35%    {transform:translate3d(-170px,-230px,0)  scale(1.5)}
    75%    {transform:translate3d(210px,-90px,0)    scale(1)}
  }
  @keyframes orbD_m{
    0%,100%{transform:translate3d(0,0,0)            scale(1.06)}
    45%    {transform:translate3d(250px,-180px,0)   scale(.72)}
    80%    {transform:translate3d(-90px,-270px,0)   scale(1.35)}
  }
  @keyframes orbE_m{
    0%  {transform:translate3d(-60px,40px,0)        scale(.86)}
    50% {transform:translate3d(-210px,-120px,0)     scale(1.4)}
    100%{transform:translate3d(-60px,40px,0)        scale(.86)}
  }

  /* ⑦ 输入框内部流动：手机端略微提速（屏小，慢了看不出）。
     ⚠️ 聚焦态**不再额外提速** —— 那是「一直在闪」的来源（2026-09-24 修）。 */
  input{
    animation-duration:3.4s, 6.5s, 1.7s;
  }

  /* 🔴 手机端专用 keyframes 绑定
     @keyframes 是全局作用域，同名会无条件覆盖 → 手机端必须用独立命名，
     再在这里把元素的 animation-name 指过去，否则会连带改坏桌面端。 */
  body::before{animation-name:auroraA_m}
  body .sweep{animation-name:sweepMove_m}
  body .gridflow{animation-name:gridDrift_m}
  body .breathe{animation-name:breathe_m}
  body .orbs i:nth-child(1){animation-name:orbA_m}
  body .orbs i:nth-child(2){animation-name:orbB_m}
  body .orbs i:nth-child(3){animation-name:orbC_m}
  body .orbs i:nth-child(4){animation-name:orbD_m}
  body .orbs i:nth-child(5){animation-name:orbE_m}
}
/* 更窄的屏（iPhone SE 等 375px 及以下）：只收尺寸，不动节奏 */
@media (max-width: 380px){
  .box{padding:28px 20px 26px;border-radius:22px}
  h1{font-size:22px}
  label{margin:17px 0 8px}
  input{font-size:16px;padding:13px 14px;min-height:48px}
  button{font-size:15.5px;min-height:50px}
  .tip{line-height:1.8}

  /* 更窄屏（≤380px）：再压一档模糊，位移不变（屏更小反而更要动得明显） */
}

/* ===== 输入框内部流动光（2026-09-24 第三轮）=====
   用户要求：「把整个输入栏也加入流动效果，不过颜色比起周围稍微淡一点，
             然后整体的流动是和后面的同步的」

   两个约束怎么满足：
   ① 「比周围淡」 —— 周围背景格线 alpha 是 .055，这里只用 .018~.030，约为它的 1/3；
      而且全部走浅色（白/淡蓝），不抢输入文字。
   ② 「和后面同步」 —— 周期取背景各层的公约节奏（sweep 9s / gridflow 6s /
      breathe 8s / flowbar 5s）→ 输入框用 4.5s / 9s / 2.25s 三档，与 sweep 同源，
      方向也一致（都往右下 + 横向掠光）。

   ⚠️ input 是**替换元素**，::before/::after 完全不生效（已踩过）。
      所以只能用 background-image 多铺几层渐变 + 动 background-position，
      纯绘制层，不触发重排，GPU 友好。
   ⚠️ 但 background-position 动画在主线程合成之外，仍属"重绘"：
      实测 4 层渐变 + 3 条动画在手机上帧率稳定，因为尺寸小（输入框 ~50px 高）。 */

input{
  background-color:#fcfdff;
  background-image:
    /* ① 横向掠光（最上层，最显眼）*/
    linear-gradient(100deg,
      transparent 32%, rgba(147,197,253,.22) 46%,
      rgba(255,255,255,.40) 50%, rgba(147,197,253,.22) 54%,
      transparent 68%),
    /* ② 斜向细纹（与背景网格同向，向下右爬）*/
    repeating-linear-gradient(115deg,
      rgba(96,165,250,.042) 0px, rgba(96,165,250,.042) 1px,
      transparent 1px, transparent 26px),
    /* ③ 底晕（最淡的一层，让上面两层有"承托"）*/
    radial-gradient(130% 200% at 10% 50%,
      rgba(191,219,254,.42), rgba(255,255,255,0) 64%);
  background-size:
    900px 100%,
    26px 26px,
    150% 100%;
  background-position:
    -900px 0,
    0 0,
    0 0;
  background-repeat:no-repeat,repeat,no-repeat;
  animation:inputFlow 4.5s linear infinite,
            inputWeave 9s linear infinite,
            inputPulse 2.25s ease-in-out infinite;
}
@keyframes inputFlow{
  from{background-position:-900px 0,   0 0,      0 0}
  to  {background-position: 900px 0,   0 0,      0 0}
}
@keyframes inputWeave{
  from{background-position:-900px 0,   0 0,      0 0}
  to  {background-position:-900px 0,  26px 26px, 0 0}
}
@keyframes inputPulse{
  0%,100%{background-position:-900px 0, 0 0,   0% 0}
  50%    {background-position:-900px 0, 0 0, 100% 0}
}
/* 聚焦态：**不再「更亮一档 + 提速」**（2026-09-24 修）
   那套 3.2s/6.5s/1.8s 提速 + 换亮的背景，是「手机端一直在闪」的真因；
   现在聚焦只做**静态**变化（底色略提），流动与常态同速同亮度。 */
input:focus{
  background-color:rgba(255,255,255,.90);
}
input:disabled{animation:none}


/* 2026-09-24 第二十轮 GPU 优化（CDP LayerTree 实测）：
   input/select 的 background 流动动画会让**整个父卡片层**每帧重绘
   （手机 DPR=3 时 520x810 = 14.5MB/帧）。加 will-change:transform 让它
   独立成合成层后，重绘范围缩到 input 自身（约 348x50），降低约 12 倍。 */
input,select,textarea{will-change:transform}
/* 2026-09-24 第二十轮：移动端去掉 background-attachment:fixed。
   它会让移动端浏览器在滚动/视口变化时重绘整个背景（桌面无此问题）；
   页面装饰层本来就是 position:fixed 的独立元素，body 底色不需要 fixed。 */
@media (max-width:640px){body{background-attachment:scroll}}


/* ===== 2026-09-24：「安全微伴」tab 专属绿色皮肤 =====
   切到微伴 tab 时 JS 给 body 加 .wb，这里整套覆盖成 emerald + teal 绿，
   与微伴控制台保持一致；切回「学习通」自动恢复蓝色。 */
body.wb{
  --brand:#10b981; --brand-2:#34d399;
  background:
    radial-gradient(1100px 700px at 8% -6%,   rgba(52,211,153,.36), transparent 60%),
    radial-gradient(900px 580px at 92% 2%,    rgba(45,212,191,.29), transparent 58%),
    radial-gradient(820px 640px at 78% 106%,  rgba(16,185,129,.27), transparent 60%),
    radial-gradient(700px 520px at 16% 98%,   rgba(110,231,183,.22), transparent 60%),
    linear-gradient(168deg,#f4fbf9 0%,#e8f4f0 48%,#e3f1ed 100%);
}
body.wb::before{
  background:
    radial-gradient(62% 52% at 24% 26%, rgba(16,185,129,.50) 0%, rgba(16,185,129,.50) 44%, rgba(16,185,129,0.17) 64%, transparent 72%),
    radial-gradient(58% 48% at 76% 60%, rgba(20,184,166,.44) 0%, rgba(20,184,166,.44) 44%, rgba(20,184,166,0.15) 64%, transparent 72%),
    radial-gradient(54% 46% at 46% 90%, rgba(52,211,153,.40) 0%, rgba(52,211,153,.40) 44%, rgba(52,211,153,0.13) 64%, transparent 73%),
    radial-gradient(46% 40% at 88% 22%, rgba(110,231,183,.36) 0%, rgba(110,231,183,.36) 44%, rgba(110,231,183,0.12) 64%, transparent 74%);
}
body.wb .flowbar{
  background:linear-gradient(90deg,#34d399,#14b8a6,#5eead4,#6ee7b7,#34d399,#2dd4bf,#6ee7b7,#34d399);
}
body.wb .orbs i:nth-child(1){background:radial-gradient(circle,rgba(52,211,153,.58) 0%,rgba(52,211,153,.50) 40%,transparent 70%)}
body.wb .orbs i:nth-child(2){background:radial-gradient(circle,rgba(20,184,166,.50) 0%,rgba(20,184,166,.43) 40%,transparent 70%)}
body.wb .orbs i:nth-child(3){background:radial-gradient(circle,rgba(110,231,183,.52) 0%,rgba(110,231,183,.45) 40%,transparent 70%)}
body.wb .orbs i:nth-child(4){background:radial-gradient(circle,rgba(45,212,191,.44) 0%,rgba(45,212,191,.38) 40%,transparent 70%)}
body.wb .orbs i:nth-child(5){background:radial-gradient(circle,rgba(163,230,53,.40) 0%,rgba(163,230,53,.34) 40%,transparent 70%)}
body.wb .cursorGlow{
  background:radial-gradient(360px 360px at var(--mx) var(--my), rgba(52,211,153,.22), transparent 70%);
}
</style></head><body>
<div class="gridflow" aria-hidden="true"></div>
<div class="breathe" aria-hidden="true"></div>
<div class="orbs" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div>
<div class="sweep" aria-hidden="true"></div>
<div class="cursorGlow" aria-hidden="true"></div>
<div class="flowbar" aria-hidden="true"></div>
<div class="box">
  <h1>刷课控制台</h1>
  <div class="tabs">
    <button class="tab on" id="tabCk" type="button">学习通</button>
    <button class="tab" id="tabWb" type="button">安全微伴</button>
  </div>

  <div id="paneCk">
    <p class="sub">用学习通账号登录，查看该账号的进度</p>
    <label for="user">学习通账号</label>
    <!-- ⚠️ 这里**故意不加 autofocus**（2026-09-24 用户报障"手机端一直在闪这个输入框"）：
         带 autofocus 时手机端一进页面就聚焦 → 聚焦态有 4px 蓝色光环 +
         （修复前）流动提速 → 看上去就是"输入框一直在闪"。
         现在聚焦态已改成静态变化，且默认不自动聚焦，等用户自己点。 -->
    <input id="user" autocomplete="username" placeholder="手机号">
    <label for="pwd">密码</label>
    <div class="pwrow">
      <input id="pwd" type="password" autocomplete="current-password" placeholder="密码">
      <button class="eye" id="eye" type="button" aria-label="显示密码">显示</button>
    </div>
    <button id="btn" type="submit">登录</button>
    <div class="tip">账号密码只与本机已保存的档案比对，不会发送给学习通。<br>
      也可直接输入管理员口令或只读口令进入。</div>
  </div>

  <div id="paneWb" class="hide">
    <p class="sub">用安全微伴账号进入（学校全称需与登录页显示完全一致）</p>
    <label for="wbTenant">学校全称</label>
    <input id="wbTenant" placeholder="例如：XX大学">
    <label for="wbUser">学号 / 用户名</label>
    <input id="wbUser" autocomplete="username" placeholder="学号">
    <label for="wbPwd">密码</label>
    <div class="pwrow">
      <input id="wbPwd" type="password" autocomplete="current-password" placeholder="默认与学号相同">
      <button class="eye" id="eye2" type="button" aria-label="显示密码">显示</button>
    </div>
    <button id="btnWb" type="submit">进入安全微伴</button>
    <div class="tip">账号要先由管理员加入清单，才能从这里进入。<br>
      <b>首次使用：把管理员口令填在「密码」框</b>，点「进入安全微伴」，进到
      「已加入的账号」卡片里添加学号。</div>

    <details id="adBox" style="margin-top:14px;border-top:1px dashed #d9d9d9;padding-top:10px">
      <summary style="cursor:pointer;font-size:13px;color:#555">管理员：在这里直接添加账号</summary>
      <label for="adTenant" style="margin-top:10px">学校全称</label>
      <input id="adTenant" placeholder="例如：XX大学">
      <label for="adUser">学号 / 用户名</label>
      <input id="adUser" placeholder="学号">
      <label for="adPwd">密码（可留空，留空则不校验）</label>
      <input id="adPwd" type="password" placeholder="留空则不校验密码">
      <label for="adToken">管理员口令</label>
      <input id="adToken" type="password" placeholder="管理员口令">
      <button id="btnAdd" type="button">添加账号</button>
      <div class="msg" id="adMsg" role="status" aria-live="polite"></div>
    </details>
  </div>

  <div class="msg" id="msg" role="status" aria-live="polite"></div>
</div>
<script>
const $ = id => document.getElementById(id);

/* ---- 2026-09-24：环境动效与指针反馈 ----
   全部只写 CSS 变量 / 加 class，不改任何既有逻辑与 DOM 结构。
   指针为「粗」（触屏）时不启用光标光晕与磁吸，避免手机端无谓开销。 */
(function(){
  const fine = window.matchMedia('(hover:hover) and (pointer:fine)').matches;
  if (!fine) return;
  document.body.classList.add('pointerFine');
  let raf = 0, px = 0, py = 0;
  window.addEventListener('pointermove', e => {
    px = e.clientX; py = e.clientY;
    if (raf) return;                       // 每帧最多写一次，避免抖动
    raf = requestAnimationFrame(() => {
      raf = 0;
      document.documentElement.style.setProperty('--mx', px + 'px');
      document.documentElement.style.setProperty('--my', py + 'px');
    });
  }, {passive:true});
})();

/* 点击波纹：任意按钮点下去从落点扩散一圈光，即时反馈「收到了」 */
let ptrDown = false;
document.addEventListener('pointerdown', e => {
  const b = e.target && e.target.closest && e.target.closest('button');
  if (!b || b.disabled) return;
  /* ⚠️ 先只做「不会引起重排」的事：插入波纹节点会触发布局重算，
     把 :active 伪类的生效推迟到 200ms 之后（实测），按下去像没反应。
     所以波纹和变量复位统统延到下一帧，让浏览器先把 :active 画出来。 */
  ptrDown = true;
  const cx = e.clientX, cy = e.clientY;
  requestAnimationFrame(() => {
    const r = b.getBoundingClientRect();
    const d = Math.max(r.width, r.height) * 1.8;
    const s = document.createElement('span');
    s.className = 'ripple';
    s.style.width = s.style.height = d + 'px';
    s.style.left = (cx - r.left) + 'px';
    s.style.top  = (cy - r.top)  + 'px';
    b.appendChild(s);
    setTimeout(() => s.remove(), 560);
    /* 按下的同时把磁吸位移归零：否则 pointermove 一直在改 --pull-x/--pull-y，
       会让 :active 的下沉视觉被 hover 位移盖住 */
    b.style.setProperty('--pull-x', '0px');
    b.style.setProperty('--pull-y', '0px');
  });
}, {passive:true});

/* 主按钮磁吸：悬停时被鼠标轻微吸引（上限 4px，克制不晃眼） */
document.addEventListener('pointerup', () => { ptrDown = false; }, {passive:true});
document.addEventListener('pointercancel', () => { ptrDown = false; }, {passive:true});
document.addEventListener('pointermove', e => {
  if (!document.body.classList.contains('pointerFine')) return;
  /* 按住不放时不做磁吸：让 :active 的下沉成为唯一视觉反馈，
     否则位移会被"吸回去"，按下去像没反应 */
  if (ptrDown || e.buttons & 1) return;
  const b = e.target && e.target.closest && e.target.closest('button');
  if (!b || b.disabled || b.classList.contains('eye') || b.classList.contains('tab')
      || b.classList.contains('ghost') || b.classList.contains('fab')) return;
  b.classList.add('magnetic');
  const r = b.getBoundingClientRect();
  const dx = (e.clientX - (r.left + r.width / 2)) / (r.width / 2);
  const dy = (e.clientY - (r.top + r.height / 2)) / (r.height / 2);
  b.style.setProperty('--pull-x', (dx * 4).toFixed(2) + 'px');
  b.style.setProperty('--pull-y', (dy * 4).toFixed(2) + 'px');
}, {passive:true});

let going = false;
function bindEye(btnId, inputId) {
  $(btnId).onclick = () => {
    const i = $(inputId), on = i.type === 'password';
    i.type = on ? 'text' : 'password';
    $(btnId).textContent = on ? '隐藏' : '显示';
    i.focus();
  };
}
bindEye('eye', 'pwd');
bindEye('eye2', 'wbPwd');

// 平台切换：登录页就分平台 —— 选哪个进哪个系统
let plat = 'ck';
function switchPlat(p) {
  plat = p;
  /* 2026-09-24：切到「安全微伴」时，整套背景换绿色（与微伴控制台同系） */
  document.body.classList.toggle('wb', p === 'wb');
  $('tabCk').classList.toggle('on', p === 'ck');
  $('tabWb').classList.toggle('on', p === 'wb');
  $('paneCk').classList.toggle('hide', p !== 'ck');
  $('paneWb').classList.toggle('hide', p !== 'wb');
  $('msg').className = 'msg';
  $('msg').textContent = '';
}
$('tabCk').onclick = () => switchPlat('ck');
$('tabWb').onclick = () => switchPlat('wb');

// 安全微伴入口
async function goWb() {
  if (going) return;
  const tenant = $('wbTenant').value.trim();
  const user = $('wbUser').value.trim();
  const pwd = $('wbPwd').value;
  // 管理员可以只填口令进来（后端第一分支直接放行），所以这里不强制学校/学号。
  // 只要求「至少填了东西」，具体校验与报错交给后端 —— 否则管理员会被前端先挡住。
  if (!pwd && !(tenant && user)) {
    $('msg').textContent = '请填密码，或填学校全称 + 学号';
    $('wbTenant').focus();
    return;
  }
  going = true; $('btnWb').disabled = true;
  $('msg').className = 'msg';
  $('msg').innerHTML = '<span class="spin"></span>正在校验…';
  try {
    const r = await fetch('auth/weiban', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tenant: tenant, username: user, password: pwd})});
    const j = await r.json();
    if (j.ok) { going = false; location.href = j.redirect || 'wb/'; return; }
    $('msg').className = 'msg';
    $('msg').textContent = j.msg || '进入失败';
  } catch (e) {
    $('msg').className = 'msg';
    $('msg').textContent = '网络错误：' + e;
  }
  going = false; $('btnWb').disabled = false;
}
$('btnWb').onclick = goWb;
async function addAccountFromLogin(){
  const btn = $('btnAdd');
  if(btn.disabled) return;
  const t = $('adTenant').value.trim(), u = $('adUser').value.trim(), tk = $('adToken').value;
  if(!t || !u){ $('adMsg').textContent = '学校全称和学号都要填'; return; }
  if(!tk){ $('adMsg').textContent = '请填管理员口令'; return; }
  btn.disabled = true;
  $('adMsg').textContent = '正在添加…';
  try{
    const r = await fetch('/auth/weiban/add', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({tenant:t, username:u, password:$('adPwd').value, admin_token:tk})});
    const j = await r.json();
    $('adMsg').textContent = j.msg || (j.ok ? '已添加' : '添加失败');
    if(j.ok){ $('adTenant').value=''; $('adUser').value=''; $('adPwd').value=''; }
  }catch(e){
    $('adMsg').textContent = '请求失败：' + e;
  }
  btn.disabled = false;
}
$('btnAdd').onclick = addAccountFromLogin;
$('wbPwd').addEventListener('keydown', e => { if (e.key === 'Enter') goWb(); });
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
    $('btn').classList.add('done');        // 短暂打勾再跳转
    $('btn').classList.remove('magnetic');
    going = false;      // 跳转前复位：否则浏览器「返回」时 bfcache 会把这个 true 带回来
    // 账号登录时后端会带 redirect（直接落到自己的实例进度页）；
    // 管理员 / 口令登录没有 redirect → 回首页（保持原样）
    location.href = r.redirect || '/';
  } catch (e) {
    $('msg').textContent = '请求失败（服务可能未启动）: ' + e;
    $('btn').disabled = false; going = false;
  }
}
$('btn').onclick = go;
// 浏览器「返回」会从 bfcache 恢复页面，而登录跳转前没清掉的 going 会一直卡在 true，
// 再点登录按钮就被 `if (going) return` 静默吞掉 —— 表现就是“点了一点反应都没有”。
// 恢复时把状态清干净。
window.addEventListener('pageshow', e => {
  if (!e.persisted) return;
  going = false;
  $('btn').disabled = false;
  $('btnWb').disabled = false;
  $('msg').textContent = '';
  $('msg').className = 'msg';
});
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
  /* 卡片整体半融入背景（2026-09-24）：与 .box/.card 主定义同一套参数 */
  .card{background:linear-gradient(180deg,rgba(255,255,255,.40) 0%,rgba(252,253,255,.30) 100%);
        border:1px solid rgba(255,255,255,.66);border-radius:13px;padding:16px 16px 14px;
        backdrop-filter:blur(12px) saturate(160%);
        -webkit-backdrop-filter:blur(12px) saturate(160%);
        box-shadow:0 2px 10px rgba(0,0,0,.03),inset 0 1px 0 rgba(255,255,255,.60)}
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
          background:#cbd5e1;border-radius:99px;
          transition:background .26s var(--ease-out-quart) .04s,
                     box-shadow .2s var(--ease-out-quart)}
  /* 滑块用弹性曲线：翻转时轻微过冲再落定，借一点物理感 */
  .sw .sl:before{content:"";position:absolute;width:18px;height:18px;left:2px;top:2px;
          background:#fff;border-radius:50%;
          box-shadow:0 1px 3px rgba(16,24,40,.28);
          transition:transform .34s var(--ease-spring)}
  .sw input:checked + .sl{background:#22c55e;
          box-shadow:0 0 0 3px rgba(34,197,94,.16)}
  .sw input:checked + .sl:before{transform:translateX(18px)}
  .sw:hover .sl:before{box-shadow:0 2px 6px rgba(16,24,40,.34)}
  .sw input:disabled + .sl{opacity:.5;cursor:default}
  .sw input:focus-visible + .sl{outline:2px solid #2563eb;outline-offset:3px}
  .acts .sw{margin-left:auto}
  /* 关着的实例：滑块加一圈淡蓝，明确"这里可以点开" */
  .card[data-on="0"] .sw .sl{background:#cbd5e1;box-shadow:0 0 0 3px rgba(37,99,235,.055)}
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
  /* ---- 平台切换（学习通 / 安全微伴）：两个平台是完全独立的进程与端口 ---- */
  .platbar{display:flex;gap:8px;margin:0 0 14px;flex-wrap:wrap}
  .plat{padding:6px 15px;border-radius:9px;background:#fff;color:#374151;
        border:1px solid #d8dee4;text-decoration:none;font-size:13px}
  .plat.on{background:#2563eb;color:#fff;border-color:#2563eb}
  .plat:not(.on):hover{background:#f3f6fb}
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
  /* 顶栏按钮的忙碌态。
     ⚠️ 第三轮给 button 加了光泽伪元素 ::after，忙碌转圈不能用同一个伪元素
     （会互相覆盖，转圈消失）。改用 ::before 承载转圈。 */
  button[data-busy]::before{content:"";display:inline-block;width:9px;height:9px;
        margin-right:6px;border:2px solid rgba(255,255,255,.45);
        border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;
        vertical-align:0}
  button.ghost[data-busy]::before{border-color:#cbd5e1;border-top-color:#374151}
  button[data-busy]::after{animation:none;transform:translate3d(-115%,0,0)}
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
  /* ---- 视觉增强·第三轮（2026-09-24）：设计系统 + 极光背景 + 克制动效 ----
     约束：只加/覆盖**外观**属性，不动尺寸、布局、结构与 JS 行为。 */
  :root{
    --ink:#161b22; --ink-2:#3d4757; --ink-3:#6b7686; --ink-4:#9aa3b0;
    --line:#e3e8f0; --line-2:#eef2f8;
    --brand:#2563eb; --brand-2:#3b82f6; --brand-3:#60a5fa;
    --ok:#22c55e; --warn:#b45309; --err:#dc2626;
    --sh-1:0 1px 2px rgba(16,24,40,.05);
    --sh-2:0 1px 2px rgba(16,24,40,.05), 0 10px 28px -12px rgba(16,24,40,.14);
    --sh-3:0 2px 6px rgba(16,24,40,.07), 0 20px 44px -16px rgba(16,24,40,.20);
    --ease-out-quart:cubic-bezier(.25,1,.5,1);
    --ease-out-quint:cubic-bezier(.22,1,.36,1);
    --ease-out-expo:cubic-bezier(.16,1,.3,1);
    --ease-in-out:cubic-bezier(.65,0,.35,1);
    --ease-spring:cubic-bezier(.34,1.56,.64,1);   /* 回弹：开关/按钮松开用 */
    --mx:50vw; --my:50vh;                          /* 光标位置，由 JS 写入 */
  }
  /* ---- 2026-09-24 第四轮：把静态背景改成真正会动的环境 ---- */
  body{color:var(--ink);
    background:
      radial-gradient(1100px 700px at 8% -6%,   rgba(59,130,246,.42), transparent 60%),
      radial-gradient(900px 580px at 92% 2%,    rgba(96,165,250,.32), transparent 58%),
      radial-gradient(820px 640px at 78% 106%,  rgba(56,189,248,.30), transparent 60%),
      radial-gradient(700px 520px at 16% 98%,   rgba(96,165,250,.26), transparent 60%),
      linear-gradient(168deg,#f4f8fe 0%,#e8f0fb 48%,#e3ecf9 100%);
    background-attachment:fixed;
  }
  /* ① 极光：14s 一圈、±26% 位移 + 旋转，肉眼明确可见在"游" */
  body::before{
    content:"";position:fixed;inset:-40%;z-index:-2;pointer-events:none;
    background:
      radial-gradient(62% 52% at 24% 26%, rgba(59,130,246,.55) 0%, rgba(59,130,246,.55) 44%, rgba(59,130,246,0.18) 64%, transparent 72%),
      radial-gradient(58% 48% at 76% 60%, rgba(96,165,250,.47) 0%, rgba(96,165,250,.47) 44%, rgba(96,165,250,0.16) 64%, transparent 72%),
      radial-gradient(54% 46% at 46% 90%, rgba(56,189,248,.42) 0%, rgba(56,189,248,.42) 44%, rgba(56,189,248,0.14) 64%, transparent 73%),
      radial-gradient(46% 40% at 88% 22%, rgba(34,211,238,.38) 0%, rgba(34,211,238,.38) 44%, rgba(34,211,238,0.13) 64%, transparent 74%);
    animation:auroraA 14s ease-in-out infinite;
    will-change:transform;
  }
  @keyframes auroraA{
    0%  {transform:translate3d(-24%,-16%,0) scale(1.10) rotate(-6deg)}
    25% {transform:translate3d(18%,-6%,0)   scale(1.26) rotate(5deg)}
    50% {transform:translate3d(-4%,20%,0)   scale(1.15) rotate(-2deg)}
    75% {transform:translate3d(22%,10%,0)   scale(1.30) rotate(7deg)}
    100%{transform:translate3d(-24%,-16%,0) scale(1.10) rotate(-6deg)}
  }
  /* ② 光带扫过：斜向高光带 9s 横穿全屏 —— 最直观的"有东西在流动" */
  .sweep{position:fixed;top:-60%;left:-60%;width:220%;height:220%;
    z-index:-2;pointer-events:none;
    background:linear-gradient(102deg,
       transparent 28%,
       rgba(255,255,255,.10) 47%,
       rgba(191,219,254,.08) 50%,
       rgba(255,255,255,.10) 53%,
       transparent 72%);
    animation:sweepMove 9s cubic-bezier(.45,0,.55,1) infinite;
    will-change:transform;
  }
  @keyframes sweepMove{
    0%  {transform:translate3d(-38%,0,0)  rotate(-8deg);opacity:0}
    8%  {opacity:.85}
    50% {opacity:.85}
    58% {opacity:0}
    100%{transform:translate3d(38%,0,0)   rotate(-8deg);opacity:0}
  }
  /* ③ 网格：6s 走一格（56px），约 9px/秒 —— 明显在爬 */
  .gridflow{position:fixed;inset:-10%;z-index:-3;pointer-events:none;opacity:.30;
    background-image:
      linear-gradient(to right, rgba(37,99,235,.055) 1px, transparent 1px),
      linear-gradient(to bottom, rgba(37,99,235,.055) 1px, transparent 1px);
    background-size:72px 72px;
    animation:gridDrift 6s linear infinite;
  will-change:transform;
  }
  @keyframes gridDrift{
    from{transform:translate3d(0,0,0)}
    to  {transform:translate3d(72px,72px,0)}
  }
  /* ④ 呼吸光晕：中心亮度 8s 起伏，整个页面像在"呼吸" */
  .breathe{position:fixed;inset:0;z-index:-2;pointer-events:none;
    background:radial-gradient(60% 50% at 50% 45%,
               rgba(96,165,250,.34), transparent 70%);
    animation:breathe 8s ease-in-out infinite;
    will-change:transform,opacity;
  }
  @keyframes breathe{
    0%  {transform:scale(.92);opacity:.42}
    50% {transform:scale(1.16);opacity:.92}
    100%{transform:scale(.92);opacity:.42}
  }
  /* ⑤ 漂浮光点 ×5：各自独立漂移，营造"有东西在游"的生气 */
  .orbs{position:fixed;inset:0;z-index:-2;pointer-events:none}
  .orbs i{position:absolute;display:block;border-radius:50%;
    will-change:transform,opacity}
  .orbs i:nth-child(1){width:190px;height:190px;left:12%;top:18%;
    background:radial-gradient(circle,rgba(56,189,248,0.58) 0%,rgba(56,189,248,0.50) 40%,transparent 70%);
    animation:orbA 11s ease-in-out infinite}
  .orbs i:nth-child(2){width:260px;height:260px;left:74%;top:12%;
    background:radial-gradient(circle,rgba(34,211,238,0.50) 0%,rgba(34,211,238,0.43) 40%,transparent 70%);
    animation:orbB 17s ease-in-out infinite}
  .orbs i:nth-child(3){width:160px;height:160px;left:58%;top:72%;
    background:radial-gradient(circle,rgba(45,212,191,0.52) 0%,rgba(45,212,191,0.45) 40%,transparent 70%);
    animation:orbC 9.5s ease-in-out infinite}
  .orbs i:nth-child(4){width:210px;height:210px;left:22%;top:76%;
    background:radial-gradient(circle,rgba(244,114,182,0.42) 0%,rgba(244,114,182,0.36) 40%,transparent 70%);
    animation:orbD 13.5s ease-in-out infinite}
  .orbs i:nth-child(5){width:130px;height:130px;left:48%;top:38%;
    background:radial-gradient(circle,rgba(52,211,153,0.44) 0%,rgba(52,211,153,0.38) 40%,transparent 70%);
    animation:orbE 7.5s ease-in-out infinite}
  @keyframes orbA{
    0%,100%{transform:translate3d(0,0,0)      scale(1)}
    33%    {transform:translate3d(90px,-70px,0) scale(1.25)}
    66%    {transform:translate3d(-60px,50px,0) scale(.85)}
  }
  @keyframes orbB{
    0%,100%{transform:translate3d(0,0,0)        scale(1)}
    40%    {transform:translate3d(-120px,80px,0) scale(1.3)}
    70%    {transform:translate3d(60px,120px,0)  scale(.9)}
  }
  @keyframes orbC{
    0%,100%{transform:translate3d(0,0,0)        scale(.9)}
    35%    {transform:translate3d(-80px,-110px,0) scale(1.35)}
    75%    {transform:translate3d(110px,-40px,0)  scale(1)}
  }
  @keyframes orbD{
    0%,100%{transform:translate3d(0,0,0)       scale(1.05)}
    45%    {transform:translate3d(130px,-90px,0) scale(.8)}
    80%    {transform:translate3d(-40px,-140px,0) scale(1.2)}
  }
  /* orbE：最短循环 + 纯位移，和其他几个的"缩放型"形成节奏差 */
  @keyframes orbE{
    0%  {transform:translate3d(-30px,20px,0)   scale(.9)}
    50% {transform:translate3d(-110px,-60px,0) scale(1.3)}
    100%{transform:translate3d(-30px,20px,0)   scale(.9)}
  }
  /* ⑥ 顶部流动渐变条：色相循环，明确表示"系统在运行" */
  .flowbar{position:fixed;top:0;left:0;right:0;height:3px;z-index:50;
    pointer-events:none;
    background:linear-gradient(90deg,
      #38bdf8,#22d3ee,#5eead4,#7dd3fc,#38bdf8,#22d3ee,#7dd3fc,#38bdf8);
    background-size:300% 100%;
    animation:flowBar 5s linear infinite;
    box-shadow:0 0 10px rgba(59,130,246,.55);
  }
  @keyframes flowBar{
    from{background-position:0% 0}
    to  {background-position:300% 0}
  }
  /* 光标跟随光晕：鼠标互动（叠加在自动流动之上，不是替代） */
  .cursorGlow{position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:0;
    background:radial-gradient(360px 360px at var(--mx) var(--my),
               rgba(56,189,248,.22), transparent 70%);
    transition:opacity .5s var(--ease-out-quart);   /* 原先这里还有 will-change:background，已删（见上） */
  }
  body.pointerFine .cursorGlow{opacity:1}
  /* 细噪点：仅 3.5% 不透明度，只为消除渐变的塑料感 */
  body::after{
    content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.42;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)' opacity='.035'/%3E%3C/svg%3E");
  }
.card{background:linear-gradient(180deg,rgba(255,255,255,.40) 0%,rgba(252,253,255,.30) 100%);
      border-color:rgba(255,255,255,.66);
      box-shadow:var(--sh-2),inset 0 1px 0 rgba(255,255,255,.60);
      backdrop-filter:blur(12px) saturate(160%);
      -webkit-backdrop-filter:blur(12px) saturate(160%);
        transition:box-shadow .24s var(--ease-out-quart),border-color .24s var(--ease-out-quart)}
  .card:hover{box-shadow:var(--sh-3);border-color:#dae2ee}
  /* 主按钮：光泽扫过 + 按压下沉 + 松开弹性回弹 */
  button{background:linear-gradient(180deg,var(--brand-2) 0%,var(--brand) 100%);
         box-shadow:0 1px 2px rgba(37,99,235,.26),0 6px 14px -6px rgba(37,99,235,.42);
         transition:transform .16s var(--ease-out-quart),
                    box-shadow .16s var(--ease-out-quart),
                    filter .16s var(--ease-out-quart);
         position:relative;overflow:hidden;isolation:isolate;
         transform:translateZ(0)}
  button::after{                      /* 光泽扫过 */
    content:"";position:absolute;inset:0;z-index:-1;pointer-events:none;
    background:linear-gradient(105deg,transparent 30%,rgba(255,255,255,.4) 50%,transparent 70%);
    transform:translate3d(-115%,0,0);transition:transform .62s var(--ease-out-expo)}
  button:hover:not(:disabled)::after{transform:translate3d(115%,0,0)}
  button:hover:not(:disabled){transform:translateY(-2px);
         box-shadow:0 3px 6px rgba(37,99,235,.3),0 12px 24px -8px rgba(37,99,235,.55)}
  button:active:not(:disabled){transform:translateY(1px) scale(.97);filter:brightness(.95);
         box-shadow:0 2px 5px rgba(37,99,235,.32);
         transition:transform .06s linear,filter .06s linear,box-shadow .06s linear}
  button:not(:active):not(:disabled){
         transition:transform .34s var(--ease-spring),
                    box-shadow .3s var(--ease-out-quart),
                    filter .2s var(--ease-out-quart)}
  /* 点击波纹 */
  button .ripple{position:absolute;border-radius:50%;pointer-events:none;
         background:radial-gradient(circle,rgba(255,255,255,.55) 0%,rgba(255,255,255,0) 70%);
         transform:translate(-50%,-50%) scale(0);opacity:.9;
         animation:rippleOut .52s var(--ease-out-quart) forwards;z-index:-1}
  @keyframes rippleOut{
    0%  {transform:translate(-50%,-50%) scale(0);opacity:.9}
    100%{transform:translate(-50%,-50%) scale(1);opacity:0}
  }
  /* 主按钮磁吸（仅非 ghost / 非 fab，位移上限 4px） */
  button.magnetic:hover:not(:disabled){
         transform:translate(var(--pull-x,0px),calc(-2px + var(--pull-y,0px)))}
  button.magnetic:active:not(:disabled){
         transform:translate(0,1px) scale(.97);
         filter:brightness(.95);
         box-shadow:0 0 0 0 rgba(37,99,235,.3),0 2px 5px rgba(37,99,235,.32);
         transition:transform .06s linear,filter .06s linear,box-shadow .06s linear}
  /* 成功态：短暂变绿打勾 */
  button.done{background:linear-gradient(180deg,#34d399 0%,#10b981 100%);
         box-shadow:0 1px 2px rgba(16,185,129,.3),0 8px 18px -6px rgba(16,185,129,.5);
         animation:donePop .42s var(--ease-spring)}
  @keyframes donePop{
    0%{transform:scale(1)} 45%{transform:scale(1.045)} 100%{transform:scale(1)}
  }
  button.ghost{background:linear-gradient(180deg,#fff 0%,#fafbfd 100%);
         box-shadow:0 1px 2px rgba(16,24,40,.05)}
  button.ghost:hover:not(:disabled){border-color:#b9c6dc;background:#fff;
         box-shadow:0 2px 6px rgba(16,24,40,.08);transform:translateY(-2px)}
  button.ghost::after{background:linear-gradient(105deg,transparent 30%,rgba(37,99,235,.055) 50%,transparent 70%)}
  button.danger{background:linear-gradient(180deg,#ef4444 0%,var(--err) 100%);
         box-shadow:0 1px 2px rgba(220,38,38,.26),0 6px 14px -6px rgba(220,38,38,.42)}
  .pbar{box-shadow:0 1px 2px rgba(16,24,40,.06) inset}
  .pfill{background:linear-gradient(90deg,#34d399 0%,var(--ok) 100%);
         box-shadow:0 0 8px rgba(34,197,94,.32)}
  .plat{transition:background .18s var(--ease-out-quart),color .18s var(--ease-out-quart),
        border-color .18s var(--ease-out-quart),box-shadow .18s var(--ease-out-quart),
        transform .18s var(--ease-out-quart)}
  .plat.on{background:linear-gradient(180deg,var(--brand-2) 0%,var(--brand) 100%);
         box-shadow:0 1px 2px rgba(37,99,235,.25),0 6px 14px -6px rgba(37,99,235,.5)}
  .plat:not(.on):hover{transform:translateY(-1px);
         box-shadow:0 4px 12px -4px rgba(16,24,40,.16)}
  .fab{box-shadow:0 4px 12px rgba(37,99,235,.32),0 12px 28px -8px rgba(37,99,235,.48);
       transition:transform .2s var(--ease-out-expo),box-shadow .2s var(--ease-out-expo)}
  .fab:hover{transform:translateY(-2px) scale(1.045);
       box-shadow:0 6px 16px rgba(37,99,235,.36),0 18px 36px -10px rgba(37,99,235,.5)}
  .hint{background:linear-gradient(180deg,#f8fafd 0%,#f4f7fb 100%)}
  /* ---- 视觉增强·第二轮：精致化细节 ---- */
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:rgba(100,116,139,.28);border-radius:999px;
        border:2px solid transparent;background-clip:content-box}
  ::-webkit-scrollbar-thumb:hover{background:rgba(100,116,139,.45);background-clip:content-box}
  button,.card,.plat,.task{transition-timing-function:var(--ease-out-quart)}
  body{animation:fadeIn .34s var(--ease-out-quart)}
  @keyframes fadeIn{from{opacity:0}to{opacity:1}}
  ::selection{background:rgba(37,99,235,.16)}
  .head h1{position:relative;padding-left:12px}
  .head h1::before{content:"";position:absolute;left:0;top:50%;transform:translateY(-50%);
        width:3.5px;height:20px;border-radius:2px;
        background:linear-gradient(180deg,var(--brand-3),var(--brand))}
  /* 进度条流光：宽度变化本身也带过渡，进度推进时是"生长"而不是"跳变" */
  .pfill{position:relative;overflow:hidden;
        transition:width .7s var(--ease-out-expo)}
  .pfill::after{content:"";position:absolute;inset:0;
        background:linear-gradient(90deg,transparent,rgba(255,255,255,.42),transparent);
        animation:shine 2.4s var(--ease-out-quart) infinite}
  @keyframes shine{0%{transform:translateX(-100%)}100%{transform:translateX(200%)}}
  /* 运行中实例的进度条加一层呼吸光晕，一眼看出"在干活" */
  .card[data-on="1"] .pfill{animation:barBreath 2.6s var(--ease-in-out) infinite}
  @keyframes barBreath{
    0%,100%{box-shadow:0 0 6px rgba(34,197,94,.28)}
    50%    {box-shadow:0 0 14px rgba(34,197,94,.55)}
  }
  .tag{letter-spacing:.2px}
  .empty{background:linear-gradient(180deg,#fbfcfe 0%,#f6f8fb 100%)}
  /* ---- 骨架屏：首次加载时先占位，避免"空一阵再蹦出来" ---- */
  .skel{background:#fff;border:1px solid var(--line);border-radius:13px;
        padding:16px 16px 14px;box-shadow:var(--sh-2);overflow:hidden}
  .skel i{display:block;border-radius:6px;
        background:linear-gradient(90deg,#eef2f7 25%,#e2e8f1 50%,#eef2f7 75%);
        background-size:220% 100%;animation:shimmer 1.25s linear infinite}
  .skel i.l1{width:42%;height:13px;margin-bottom:9px}
  .skel i.l2{width:62%;height:11px;margin-bottom:14px}
  .skel i.l3{width:34%;height:24px;margin-bottom:10px}
  .skel i.l4{width:100%;height:8px;border-radius:99px;margin-bottom:12px}
  .skel i.l5{width:78%;height:11px}
  @keyframes shimmer{from{background-position:120% 0}to{background-position:-120% 0}}
  /* 卡片入场：逐个上浮，--i 由 JS 注入；总量封顶避免久等。
     仅首屏生效（.enter），5 秒一次的差量重绘不会重播动画。 */
  .grid.enter .card{animation:cardIn .42s var(--ease-out-expo) both;
        animation-delay:calc(var(--i,0) * 45ms)}
  @keyframes cardIn{from{opacity:0;transform:translate3d(0,9px,0)}to{opacity:1;transform:none}}
  /* 顶栏按钮组入场 */
  .btnrow,.platbar{animation:cardIn .4s var(--ease-out-expo) both}
  .platbar{animation-delay:20ms}
  .btnrow{animation-delay:60ms}
  @media (prefers-reduced-motion: reduce){
    body,.grid.enter .card,.btnrow,.platbar{animation:none}
    body::before,.gridflow,.breathe,.orbs i,.sweep,.flowbar{animation:none}
    .sweep,.orbs{opacity:.35}
    .flowbar{opacity:.5}
    .cursorGlow{display:none}
    .pfill::after{animation:none;display:none}
    .card[data-on="1"] .pfill{animation:none}
    .skel i{animation:none;background:#eef2f7}
    button::after{display:none}
    button .ripple{display:none}
    button,.card,.plat,.fab,.sw .sl,.sw .sl:before,.pfill{transition:none}
    button:hover:not(:disabled),button:active:not(:disabled),
    button.magnetic:hover:not(:disabled),button.magnetic:active:not(:disabled),
    .fab:hover,.plat:not(.on):hover{transform:none}
    button.done{animation:none}
  }
  /* ===== 手机端专项（2026-09-24，第二版重做）=====
     ⚠️ 第一版两个失误（用户反馈"动效完全没有 / 排在一起太拥挤"）：
       ① 把 .sweep/.orbs/.flowbar 直接 display:none → 设计语言被砍光。
          这几层全是 transform/opacity，GPU 合成零重排，手机跑得动。
          **改为层数不动，只把 filter:blur 半径减半**（唯一吃 GPU 的属性）。
       ② 只做等比放大，没拉开层级差 → 大控件配小文字，显得闷。
          **改为：标题更大、卡片留白更宽、行距更松。** */
  @media (max-width: 640px){
    body{padding:22px 15px calc(52px + env(safe-area-inset-bottom))}
    .wrap{max-width:100%}
    h1,.head h1{font-size:25px;font-weight:650;letter-spacing:.3px}
    .sub,.head .sub{font-size:13.5px;line-height:1.65;
                    color:#66707f;margin-bottom:18px}
    .grid{grid-template-columns:1fr;gap:16px}
    .card{padding:22px 19px 19px;border-radius:22px;
          box-shadow:0 1px 2px rgba(15,32,74,.04),
                     0 16px 40px -18px rgba(15,23,42,.20)}
    .cno{font-size:17px}
    .cacc{font-size:12.5px;margin-bottom:12px}
    .pv .big{font-size:28px}
    .pv{margin:12px 0 9px}
    .cur{white-space:normal;overflow:visible;line-height:1.65;margin-top:10px}
    .hint{font-size:13px;padding:12px 13px;line-height:1.7;border-radius:11px}
    .log{margin-top:14px;padding-top:13px;font-size:12px;line-height:1.7}
    button{min-height:50px;padding:14px 18px;font-size:15.5px;border-radius:13px}
    button.fab{min-height:0;padding:0}
    .btnrow{gap:11px;margin-bottom:20px}
    .btnrow button{flex:1 1 calc(50% - 6px)}
    .acts{gap:11px;margin-top:16px}
    .acts a{min-height:46px;display:inline-flex;align-items:center;
            padding:12px 15px;font-size:14px;border-radius:11px;flex:1 1 auto;
            justify-content:center}
    .bar{gap:11px;margin-bottom:18px}
    .fab{right:16px;bottom:calc(20px + env(safe-area-inset-bottom));
         width:58px;height:58px}
    .sw{padding:12px 14px;margin:-12px -14px}
    button:hover:not(:disabled),.fab:hover,.plat:not(.on):hover{transform:none}
    button:active:not(:disabled),.fab:active{transform:scale(.97);filter:brightness(.95);
         transition:transform .08s linear,filter .08s linear}
    /* 背景动效全保留，只降 filter:blur（唯一真正吃 GPU 的属性）
       ⚠️ 必须用 :nth-child 级别的特异性，基础定义就写在那儿 */
    body .orbs i:nth-child(n){width:170px;height:170px}
    body .orbs i:nth-child(2){width:210px;height:210px}
    .sweep{opacity:.85}
    .breathe{opacity:.7}
    .gridflow{opacity:.24}
    .cursorGlow{display:none}
    ::-webkit-scrollbar{width:0;height:0}

    /* 🔴 2026-09-28 修「手机端偶尔闪一下 / 区域变黑」。
       根因链（不是新问题，是老问题复发）：
         · 2026-09-24 记录过：背景光带 .sweep 扫过时"整个框全部闪"，
           当时代价是——**降 sweep 的 opacity**（关掉后卡片内帧差 -76%）。
         · 但紧接着"卡片整体半融入背景"把卡片降到 rgba(.40/.30) **并加了
           backdrop-filter:blur(12px)** → 等于把光带又请回卡片内部，
           而且让每张卡都变成一个全屏级合成层。
         · 手机端一屏 8 张实例卡 = 8 个毛玻璃层，外加 极光/扫光/网格/呼吸/5 个光球
           ≈ 19 个合成层。中低端 GPU 扛不住 → 图层被回收重建 = **闪一下 / 区域变黑**。

       对策：**不砍动效**（用户明确要流动感），只做两件最划算的：
         ① 卡片不再透背景 —— 去掉 backdrop-filter + 提高不透明度
            （光带再也穿不透卡片，同时省掉 8 个合成层）
         ② 去掉永久 will-change —— 图层常驻显存会导致系统回收重建，
            交给浏览器按需提升即可。 */
    .card, .box{
      backdrop-filter:none !important;
      -webkit-backdrop-filter:none !important;
      background:linear-gradient(180deg,rgba(255,255,255,.90) 0%,
                                         rgba(252,253,255,.84) 100%);
    }
    body::before, body .sweep, body .gridflow, body .breathe,
    body .orbs i{will-change:auto}
  
  /* ===== ⑥ 流动感加强（2026-09-24 第三轮 · 用户第二次反馈）=====
     用户原话：「手机端的流动感还是没有，手机端的屏幕本身比较小，
               又不用加载那么大的地方，应该小区域的流动加大一点，
               要不然压根看不出流动感，就像一个死背景」

     根因（我算过账）：
       桌面参数是按 1440px 宽屏调的，照搬到 390px 屏上，
       **每秒可见位移 ÷ 屏宽** 被稀释了近 4 倍 →
         · 光点 orbA：90px/11s = 8.2px/s   （肉眼几乎静止）
         · 背景网格：72px/6s   = 12px/s    （勉强）
         · 极光 body::before：blur(48px) 在 390px 屏上把色斑糊成整屏单色，
           层次被抹平 —— 这是"死背景感"最大的元凶
       验收标准：**每秒可见位移 ≥ 15px**（比桌面翻倍，因为屏小、参照物近）。

     对策：「小区域、大位移、快周期」三件套 ——
       位移 → 放到 2.2 倍（光点 90→200px 级）
       周期 → 压到 0.62 倍（sweep 9→5.5s、grid 6→3.6s、breathe 8→5s）
       模糊 → 极光 48→26px（恢复层次，同时更省 GPU）
     ⚠️ 仍然只动 transform / opacity / background-position，
        不碰 layout，手机 GPU 合成完全扛得住。 */

  /* ① 极光：去糊、加幅、加速（"死背景"第一元凶） */
  body::before{
    animation-duration:9s;
  }
  @keyframes auroraA_m{
    0%  {transform:translate3d(-34%,-24%,0) scale(1.06) rotate(-7deg)}
    25% {transform:translate3d(26%,-10%,0)  scale(1.34) rotate(6deg)}
    50% {transform:translate3d(-8%,28%,0)   scale(1.18) rotate(-3deg)}
    75% {transform:translate3d(32%,14%,0)   scale(1.40) rotate(8deg)}
    100%{transform:translate3d(-34%,-24%,0) scale(1.06) rotate(-7deg)}
  }

  /* ② 光带：9s → 5.5s，位移 ±38% → ±52%（明显"刷"过去）
     ⚠️ 2026-09-24 追加：手机端**不再把 opacity 抬到 1**（桌面 .85）。
        卡片在窄屏上几乎占满整屏，光带越亮 → 卡片内明暗跳变越猛。
        用户录屏「整个框全部闪」的主犯就是它（关掉后卡片内帧差 -76%）。
        现在改成 .55 —— 背景里仍能看见光带扫过，卡片下不再打脸。 */
  body .sweep{
    animation-duration:5.5s;
    opacity:.55;
  }
  @keyframes sweepMove_m{
    0%  {transform:translate3d(-52%,0,0) rotate(-10deg);opacity:0}
    8%  {opacity:.55}
    50% {opacity:.55}
    58% {opacity:0}
    100%{transform:translate3d(52%,0,0)  rotate(-10deg);opacity:0}
  }

  /* ③ 网格：6s → 3.6s（走一格 72px ⇒ 20px/s，跨过 15px/s 门槛） */
  body .gridflow{
    animation-duration:3.6s;
    background-size:64px 64px;inset:-22%;      /* 屏小，格距也收一点，格数不变才不显空 */
    opacity:.22;
  }
  @keyframes gridDrift_m{
    from{transform:translate3d(0,0,0)}
    to  {transform:translate3d(64px,64px,0)}
  }

  /* ④ 呼吸：8s → 5s，幅度也拉开（.90↔1.22） */
  body .breathe{
    animation-duration:5s;
    opacity:.8;
  }
  @keyframes breathe_m{
    0%  {transform:scale(.90);opacity:.46}
    50% {transform:scale(1.22);opacity:1}
    100%{transform:scale(.90);opacity:.46}
  }

  /* ⑤ 光点：位移 ×2.2、周期压到 0.62 倍
        —— 这是手机端"有东西在游"的主力 */
  body .orbs i:nth-child(1){animation-duration:6.8s}
  body .orbs i:nth-child(2){animation-duration:10.5s}
  body .orbs i:nth-child(3){animation-duration:5.9s}
  body .orbs i:nth-child(4){animation-duration:8.4s}
  body .orbs i:nth-child(5){animation-duration:4.6s}
  @keyframes orbA_m{
    0%,100%{transform:translate3d(0,0,0)            scale(1)}
    33%    {transform:translate3d(190px,-150px,0)   scale(1.4)}
    66%    {transform:translate3d(-130px,110px,0)   scale(.78)}
  }
  @keyframes orbB_m{
    0%,100%{transform:translate3d(0,0,0)            scale(1)}
    40%    {transform:translate3d(-230px,160px,0)   scale(1.45)}
    70%    {transform:translate3d(120px,230px,0)    scale(.82)}
  }
  @keyframes orbC_m{
    0%,100%{transform:translate3d(0,0,0)            scale(.88)}
    35%    {transform:translate3d(-170px,-230px,0)  scale(1.5)}
    75%    {transform:translate3d(210px,-90px,0)    scale(1)}
  }
  @keyframes orbD_m{
    0%,100%{transform:translate3d(0,0,0)            scale(1.06)}
    45%    {transform:translate3d(250px,-180px,0)   scale(.72)}
    80%    {transform:translate3d(-90px,-270px,0)   scale(1.35)}
  }
  @keyframes orbE_m{
    0%  {transform:translate3d(-60px,40px,0)        scale(.86)}
    50% {transform:translate3d(-210px,-120px,0)     scale(1.4)}
    100%{transform:translate3d(-60px,40px,0)        scale(.86)}
  }

  /* ⑦ 输入框内部流动：手机端略微提速（屏小，慢了看不出）。
     ⚠️ 聚焦态**不再额外提速** —— 那是「一直在闪」的来源（2026-09-24 修）。 */
  input{
    animation-duration:3.4s, 6.5s, 1.7s;
  }

  /* 🔴 手机端专用 keyframes 绑定
     @keyframes 是全局作用域，同名会无条件覆盖 → 手机端必须用独立命名，
     再在这里把元素的 animation-name 指过去，否则会连带改坏桌面端。 */
  body::before{animation-name:auroraA_m}
  body .sweep{animation-name:sweepMove_m}
  body .gridflow{animation-name:gridDrift_m}
  body .breathe{animation-name:breathe_m}
  body .orbs i:nth-child(1){animation-name:orbA_m}
  body .orbs i:nth-child(2){animation-name:orbB_m}
  body .orbs i:nth-child(3){animation-name:orbC_m}
  body .orbs i:nth-child(4){animation-name:orbD_m}
  body .orbs i:nth-child(5){animation-name:orbE_m}
}
  @media (max-width: 380px){
    body{padding:18px 12px calc(46px + env(safe-area-inset-bottom))}
    h1,.head h1{font-size:22px}
    .card{padding:19px 15px;border-radius:20px}
    .btnrow button{flex:1 1 100%}
  
  /* 更窄屏（≤380px）：再压一档模糊，位移不变（屏更小反而更要动得明显） */
}

/* ===== 输入框内部流动光（2026-09-24 第三轮）=====
   用户要求：「把整个输入栏也加入流动效果，不过颜色比起周围稍微淡一点，
             然后整体的流动是和后面的同步的」

   两个约束怎么满足：
   ① 「比周围淡」 —— 周围背景格线 alpha 是 .055，这里只用 .018~.030，
      约为它的 1/3；而且全部走浅色（白/淡蓝），不抢输入文字。
   ② 「和后面同步」 —— 周期取背景各层的公约节奏：
      背景 sweep=9s / gridflow=6s / breathe=8s / flowbar=5s
      → 输入框用 2.25s(9s÷4) / 4.5s(9s÷2) / 9s 三档，
        和 sweep 光带同源；方向也一致（都往右下走 + 横向）。

   ⚠️ input 是**替换元素**，::before/::after 完全不生效（已踩过）。
      所以只能用 background-image 多铺几层渐变 + 动 background-position，
      这个是纯绘制层，不触发重排，GPU 友好。 */

/* 常态：两层流动斜纹 + 一层横向掠光 */
input{
  background-color:#fcfdff;
  background-image:
    /* ① 横向掠光（最上层，最显眼）*/
    linear-gradient(100deg,
      transparent 32%, rgba(147,197,253,.22) 46%,
      rgba(255,255,255,.40) 50%, rgba(147,197,253,.22) 54%,
      transparent 68%),
    /* ② 斜向细纹（与背景网格同向，向右下爬）*/
    repeating-linear-gradient(115deg,
      rgba(96,165,250,.042) 0px, rgba(96,165,250,.042) 1px,
      transparent 1px, transparent 26px),
    /* ③ 底晕（最淡，给上面两层"承托"）*/
    radial-gradient(130% 200% at 10% 50%,
      rgba(191,219,254,.42), rgba(255,255,255,0) 64%);
  background-size:
    900px 100%,
    26px 26px,
    150% 100%;
  background-position:
    -900px 0,
    0 0,
    0 0;
  background-repeat:no-repeat,repeat,no-repeat;
  animation:inputFlow 4.5s linear infinite,
            inputWeave 9s linear infinite,
            inputPulse 2.25s ease-in-out infinite;
  background-color:#fcfdff;
}
@keyframes inputFlow{      /* 横向掠光：与背景 sweep 同源（9s÷2） */
  from{background-position:-900px 0,   0 0,      0 0}
  to  {background-position: 900px 0,   0 0,      0 0}
}
@keyframes inputWeave{     /* 斜纹爬行：与背景 gridflow 同向同为线性 */
  from{background-position:-900px 0,   0 0,      0 0}
  to  {background-position:-900px 0,  26px 26px, 0 0}
}
@keyframes inputPulse{     /* 底晕呼吸：与背景 breathe 同族（8s 的 1/4 弱化版） */
  0%,100%{background-position:-900px 0, 0 0,   0% 0}
  50%    {background-position:-900px 0, 0 0, 100% 0}
}
/* 聚焦：流动更亮一档（用户正在操作，值得多给一点注意力）*/
/* 聚焦态：**不再「更亮一档 + 提速」**（2026-09-24 修）
   那套 3.2s/6.5s/1.8s 提速 + 换亮的背景，是「手机端一直在闪」的真因；
   现在聚焦只做**静态**变化（底色略提），流动与常态同速同亮度。 */
input:focus{
  background-color:rgba(255,255,255,.90);
}
/* 禁用态：不动（没什么可等的） */
input:disabled{animation:none}


/* 2026-09-24 第二十轮 GPU 优化（CDP LayerTree 实测）：
   input/select 的 background 流动动画会让**整个父卡片层**每帧重绘
   （手机 DPR=3 时 520x810 = 14.5MB/帧）。加 will-change:transform 让它
   独立成合成层后，重绘范围缩到 input 自身（约 348x50），降低约 12 倍。 */
input,select,textarea{will-change:transform}
/* 2026-09-24 第二十轮：移动端去掉 background-attachment:fixed。
   它会让移动端浏览器在滚动/视口变化时重绘整个背景（桌面无此问题）；
   页面装饰层本来就是 position:fixed 的独立元素，body 底色不需要 fixed。 */
@media (max-width:640px){body{background-attachment:scroll}}

</style></head><body>
<div class="gridflow" aria-hidden="true"></div>
<div class="breathe" aria-hidden="true"></div>
<div class="orbs" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div>
<div class="sweep" aria-hidden="true"></div>
<div class="cursorGlow" aria-hidden="true"></div>
<div class="flowbar" aria-hidden="true"></div>
<div class="wrap">
  <div class="head">
    <h1>多账号刷课控制台</h1>
    <div class="sub">一个入口管理所有账号实例 · <span id="who"></span></div>
  </div>
  <div class="platbar">
    <a class="plat on" href="/">学习通</a>
    <a class="plat" href="/wb/">安全微伴</a>
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
  <div class="grid" id="grid">
    <div class="skel"><i class="l1"></i><i class="l2"></i><i class="l3"></i><i class="l4"></i><i class="l5"></i></div>
    <div class="skel"><i class="l1"></i><i class="l2"></i><i class="l3"></i><i class="l4"></i><i class="l5"></i></div>
    <div class="skel"><i class="l1"></i><i class="l2"></i><i class="l3"></i><i class="l4"></i><i class="l5"></i></div>
  </div>
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

/* ---- 2026-09-24：环境动效与指针反馈 ----
   只写 CSS 变量 / 加 class，不动既有逻辑与 DOM 结构。
   触屏（指针为 coarse）时不启用光标光晕与磁吸，省掉无谓开销。 */
(function(){
  if (!window.matchMedia('(hover:hover) and (pointer:fine)').matches) return;
  document.body.classList.add('pointerFine');
  let raf = 0, px = 0, py = 0;
  window.addEventListener('pointermove', e => {
    px = e.clientX; py = e.clientY;
    if (raf) return;                       // 每帧最多写一次
    raf = requestAnimationFrame(() => {
      raf = 0;
      document.documentElement.style.setProperty('--mx', px + 'px');
      document.documentElement.style.setProperty('--my', py + 'px');
    });
  }, {passive:true});
})();

/* 点击波纹：从落点扩散一圈光，即时反馈「收到了」 */
let ptrDown = false;
document.addEventListener('pointerdown', e => {
  const b = e.target && e.target.closest && e.target.closest('button');
  if (!b || b.disabled) return;
  /* ⚠️ 插入波纹节点会触发布局重算，把 :active 的生效推迟到 200ms 之后（实测）。
     所以这里只标记状态，DOM 操作延到下一帧。 */
  ptrDown = true;
  const cx = e.clientX, cy = e.clientY;
  requestAnimationFrame(() => {
    const r = b.getBoundingClientRect();
    const d = Math.max(r.width, r.height) * 1.8;
    const s = document.createElement('span');
    s.className = 'ripple';
    s.style.width = s.style.height = d + 'px';
    s.style.left = (cx - r.left) + 'px';
    s.style.top  = (cy - r.top)  + 'px';
    b.appendChild(s);
    setTimeout(() => s.remove(), 560);
    b.style.setProperty('--pull-x', '0px');
    b.style.setProperty('--pull-y', '0px');
  });
}, {passive:true});

/* 主按钮磁吸：悬停时被鼠标轻微吸引（上限 4px）。
   只作用于实心主按钮 —— ghost / fab / 切换类按钮保持原位，避免误点。 */
document.addEventListener('pointerup', () => { ptrDown = false; }, {passive:true});
document.addEventListener('pointercancel', () => { ptrDown = false; }, {passive:true});
document.addEventListener('pointermove', e => {
  if (!document.body.classList.contains('pointerFine')) return;
  /* 按住不放时不做磁吸，让 :active 的下沉成为唯一反馈 */
  if (ptrDown || e.buttons & 1) return;
  const b = e.target && e.target.closest && e.target.closest('button');
  if (!b || b.disabled || b.classList.contains('ghost') || b.classList.contains('fab')
      || b.classList.contains('danger') || b.classList.contains('plat')) return;
  b.classList.add('magnetic');
  const r = b.getBoundingClientRect();
  const dx = (e.clientX - (r.left + r.width / 2)) / (r.width / 2);
  const dy = (e.clientY - (r.top + r.height / 2)) / (r.height / 2);
  b.style.setProperty('--pull-x', (dx * 4).toFixed(2) + 'px');
  b.style.setProperty('--pull-y', (dy * 4).toFixed(2) + 'px');
}, {passive:true});

/* 按钮走完「忙碌→成功」后，短暂变绿打勾再复位（三点式反馈） */
function flashDone(btn, ms) {
  if (!btn) return;
  btn.classList.remove('magnetic');
  btn.classList.add('done');
  setTimeout(() => btn.classList.remove('done'), ms || 900);
}

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
  // 2026-09-29：优先用平台侧真实完成。p.done 数的是"平台回过 isPassed=true 的任务名"，
  // 口径是**任务点**，而 p.total 是**章节数** —— 一个章节可有多个任务点，
  // 所以 p.done 会超过 p.total（实测 158/139 = 114%）。platform 是章节维度，不会超。
  const pf = (p && p.platform) || {};
  const _real = pf.total > 0;
  const d = _real ? pf.done : (p.done || 0);
  const t = _real ? pf.total : (p.total || 0);
  if (!t) return 0;
  return Math.min(100, Math.round(d / t * 100));
}

function card(it) {
  const on = it.online;
  const p = it.progress || {};
  const pct = fmtPct(p);
  // 主数字用平台侧真实值，拿不到才退回程序自报（2026-09-29，与实例页同口径）
  const _pf = p.platform || {};
  const _real = _pf.total > 0;
  const pdone = _real ? _pf.done : (p.done || 0);
  const ptotal = _real ? _pf.total : (p.total || 0);
  const _gap = (p.done || 0) - (_pf.done || 0);
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
  // 🔴 2026-09-29 用户要求：**没在跑 / 已结束**的实例卡片要回归干净。
  // 原先这类卡片还挂着上次残留的进度数字、进度条、"程序累计完成 N 个任务点"
  // 和最后一条日志，看着跟还在跑一样（用户原话："3和5这种没有在跑的结束的，
  // 就不要一直像这样展示。结束就结束了，回归正常就行"）。
  // ⚠️ 不含"已暂停"：暂停说明任务还在、只是被按下暂停键，进度必须保留。
  // 文案用中性的"暂无进行中的任务"：实测有的空闲实例其实**从没跑过**
  // （acc4 张旭：选过课但日志里 0 条"任务完成"），写"已结束"会误导。
  // "刷到哪了"这类信息放到**实例页**上显示（`#lastTaskTag`）。
  const idle = on && !it.running && !it.paused;
  const body = idle
    ? '<div class="cur">' + (on ? '暂无进行中的任务' : '—') + '</div>'
    : '<div class="pv"><span class="big">' + pdone
      + '<i>/' + ptotal + '</i></span>'
      + '<span class="tag">' + pct + '%</span></div>'
      + '<div class="pbar"><div class="pfill" style="width:' + pct + '%"></div></div>'
      + (_real
          ? '<div class="hint">程序累计完成 ' + (p.done || 0) + ' 个任务点（任务点 ≠ 章节，不可直接相减）</div>'
          : '')
      + '<div class="cur">' + esc(cur || (on ? '暂无进行中的任务' : '—')) + '</div>'
      + '<div class="log">' + esc(it.last_log || '暂无日志') + '</div>';
  // data-on 是"服务端说的状态"，只用于差量签名（checked 属性会被用户改，
  // 不能拿它当签名依据，否则手动切换后永远判定为"没变化"）
  return '<div class="card' + (it.risk ? ' riskcard' : '') + '" data-on="'
    + (on ? '1' : '0') + '">'
    + '<div class="chead"><span class="cno">实例 ' + it.n + '</span>' + tag
    + '<span class="sp"></span><span class="cport">:' + it.port + '</span></div>'
    + '<div class="cacc">' + esc(acc) + '</div>'
    + body
    + hint
    + risk
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
    // 首次渲染走入场动画；之后差量重绘直接落位，避免每 5 秒重播一次
    const first = grid.dataset.sig === undefined;
    grid.dataset.sig = html;
    grid.innerHTML = html;
    grid.querySelectorAll('.card').forEach((el, i) => {
      el.style.setProperty('--i', Math.min(i, 8));
    });
    if (first) {
      grid.classList.add('enter');
      // 入场播完就摘掉标记：否则后续 innerHTML 替换出的新卡片会再次命中动画，
      // 在慢速重绘/切回标签页时表现为「卡片闪一下再出现」。
      setTimeout(() => grid.classList.remove('enter'), 900);
    }
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
  load().finally(() => { setBusy(b, false); flashDone(b, 700); });
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
      flashDone(b);
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
    if (r.ok) { toast(r.msg || '调度已开始', 'ok'); flashDone(b); }
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
    # 自动回灌：学习库 → 全局题库，每 10 分钟一轮（用户要求"自动+可接受延迟"）
    threading.Thread(target=_sync_worker, daemon=True).start()
    print(f"自动回灌已启动：每 {_SYNC['every'] // 60} 分钟把全部实例的学习库汇总进题库。")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
