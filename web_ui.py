"""超星学习通刷课 - 网页界面启动器。

启动本地服务并自动打开浏览器：填账号 -> 勾选课程 -> 看实时日志。
复用项目自带的 Chaoxing / JobProcessor，不修改原有任何文件。

刷新恢复：登录态与运行态保存在服务端，前端刷新后通过 /api/status 回到原页面；
日志在服务端保留最近 800 段，前端用游标增量拉取，刷新后历史不丢。
"""

import ctypes
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

# 字体表/cookies/日志都按相对路径解析，必须固定到脚本目录，
# 否则从桌面双击 vbs 启动时 cwd 在桌面，解码静默失效、日志写错位置
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# pythonw 无控制台启动时 stdout/stderr 为 None，print 会直接炸，接住写到空设备
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from flask import (Flask, Response, jsonify, request, session, redirect, g)

from api.answer import Tiku
from api.base import Account, Chaoxing
from api.config import data_dir
from api.exceptions import PauseInterrupt, RiskControlError
from api.logger import tqdm_stream as _unused_stream  # noqa: F401  确保 logger 先完成初始化
import api.logger as app_logger
from main import ChapterTask, JobProcessor, load_config_from_file

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
# 多实例支持（2026-09-14）：一份程序跑一个账号，靠端口区分。
# 原实现把端口/互斥体都写死成"全局唯一"，导致复制出来的第二份会被自己人挡掉。
# 现在用环境变量 CK_HOST / CK_PORT 覆盖，默认仍是 127.0.0.1:5000（原启动方式不变）。
# 三个账号 = 三份程序 + 三个端口（5000/5001/5002），各自的 cookies.txt、cache.json
# 都是相对路径、且从各自目录启动，因此天然互不干扰。
HOST = os.environ.get("CK_HOST") or "127.0.0.1"
try:
    PORT = int(os.environ.get("CK_PORT") or 5000)
except ValueError:
    PORT = 5000
HISTORY_MAX = 800
# 2026-09-14 重大修复：必须带 use=local
# 原 URL 没带 use 参数 → TikuAdapter 只查外部免费题库，本地库(local)
# 的查询分支要求 strings.Contains(use,"local")，否则整段被跳过
# （internal/controller/search.go）。后果：我们积累的本地题库从未参与
# 查询，听力题等只能靠随机/AI。
# 改成 use=local,... → 先查本地库，本地命中即返回；本地没有才落外部题库。
DEFAULT_ADAPTER_URL = ("http://127.0.0.1:8060/adapter-service/search"
                       "?use=local,icodef,buguake,wanneng,tikuhai")


def _ensure_use_local(url: str) -> str:
    """确保 TikuAdapter 的 url 带 use=local（本地题库才参与查询）。

    2026-09-14：老配置/老档案里的 adapter_url 普遍没带 use 参数，
    TikuAdapter 只在 strings.Contains(use,"local") 时才查本地库，
    漏了它 → 本地题库形同虚设（听力题全搜不到）。这里做写入侧兜底。
    只对 adapter-service 的 url 生效，其它地址原样返回。
    """
    u = (url or "").strip()
    if not u or "adapter-service/search" not in u or "use=" in u:
        return u
    return u + ("&" if "?" in u else "?") + "use=local,icodef,buguake,wanneng,tikuhai"
# 界面配置（账号档案 / 上次参数 / 断续刷记录）随实例数据目录走（2026-09-14）：
# 多账号各起一份实例时各自一份，别人的界面读不到你的账号密码。
# config.ini（题库/AI 配置）保持共用，不随实例分家。
UI_CONFIG_PATH = os.path.join(data_dir(), "ui_config.json")

# 登录表单里需要记住的字段（含账密与 AI key：本地单用户工具，用户明确要求记住）
REMEMBER_FIELDS = ("username", "password", "tiku_type", "adapter_url", "token",
                   "ai_fallback", "ai_endpoint", "ai_key", "ai_model",
                   "auto_submit", "cover_rate", "speed", "jobs")


def load_ui_config():
    """返回 {"profiles": [...], "last_params": {...}}。兼容旧版平铺格式。"""
    try:
        with open(UI_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {"profiles": [], "last_params": {}}
    if isinstance(cfg, dict) and "profiles" not in cfg:
        if cfg.get("username"):
            cfg = {"profiles": [cfg], "last_params": cfg}
        else:
            cfg = {"profiles": [], "last_params": {}}
    cfg.setdefault("profiles", [])
    cfg.setdefault("last_params", {})
    return cfg


def _dump(cfg):
    with open(UI_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)


# ---- 卡死自愈：记录"待恢复任务"，进程自重启后据此自动续刷 ----
STALE_LIMIT = 180         # 工作线程已死 + 无日志超过此秒数 -> 判定卡死
HARD_STALE_LIMIT = 900    # 线程存活但无日志超过此秒数 -> 判定僵死（死等/锁队列）。
                          # 必须大于视频播完等判定的最长静默（约 10 分钟），否则会误杀
RESTART_LIMIT = 3         # 连续自重启上限，网络彻底断开时避免无限重启
_boot_time = time.time()
_restarts = 0             # 进程内计数；真实累计值以 resume.restarts 为准（跨进程）


def _resume_count():
    """读跨进程累计的重启次数。内存变量每次重启都归零，靠文件才拦得住死循环。"""
    try:
        return int((load_ui_config().get("resume") or {}).get("restarts") or 0)
    except (TypeError, ValueError):
        return 0


def _bump_restart(n):
    """把重启计数写回文件，供下一个进程读取（否则永远数不到上限）。"""
    cfg = load_ui_config()
    res = cfg.get("resume") or {}
    res["restarts"] = n
    cfg["resume"] = res
    _dump(cfg)


def _save_resume(courses, config):
    cfg = load_ui_config()
    cfg["resume"] = {"course_ids": [str(c.get("courseId", "")) for c in courses],
                     "config": config or {}, "restarts": _resume_count(),
                     "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _dump(cfg)


def _clear_resume():
    cfg = load_ui_config()
    cfg.pop("resume", None)
    _dump(cfg)


def _save_last_pick(picked, config):
    """长期保留"上次选课"，供网关的错峰启动复用。

    与 resume 的区别：resume 只在任务异常中断时存在、正常跑完就清；
    这里跑完也保留，所以错峰启动随时能按上次的选择重新开跑。
    """
    try:
        cfg = load_ui_config()
        cfg["last_pick"] = {
            "course_ids": [str(c.get("courseId", "")) for c in picked],
            "speed": (config or {}).get("speed", 2),
            "jobs": (config or {}).get("jobs", 1),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _dump(cfg)
    except Exception as e:
        print(f"[选课记忆] 保存失败（不影响刷课）: {e}")


def _pythonw():
    """返回无窗口解释器路径。

    pythonw.exe 不带控制台，双击/重启都不会弹黑窗；python.exe 会。
    自重启若沿用 sys.executable，原本若是 python.exe 启动就永远弹窗，
    所以这里强制把 python.exe 换成同目录的 pythonw.exe。
    """
    exe = sys.executable or ""
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if exe.lower().endswith("python.exe") and os.path.exists(cand) else exe


def _watchdog():
    """任务线程死亡 **或长时间僵死**：自动重启服务并续刷。

    判定依据两层：

    1.「线程是否存活」——视频播完后项目会静默等待平台判定（30s 一次确认、
       最长约 10 分钟才放弃），这段静默是**正常工作**；若在此期间重启，新进程
       会重新拉章节列表并回到同一视频，形成"每 220 秒重启一次"的死循环。
       所以只看静默时长会误杀，必须看线程。

    2.「存活但僵死」——只看线程存活同样不够。队列/锁/网络读这类死等不会让线程
       退出，日志却彻底停住，网页显示"正在运行"，实际再也不会前进（实测：章节
       切到下一章时卡住，日志停 2 小时而 running 仍为 True）。因此补一条兜底：
       线程存活但日志静默超过 HARD_STALE_LIMIT（远大于视频等待的 10 分钟上限）
       -> 判定僵死，同样重启。
    """
    global _restarts
    while True:
        time.sleep(30)
        if not state["running"] or state["paused"]:
            continue
        if time.time() - _boot_time < 120:   # 启动初期的静默不判定
            continue

        th = state.get("work_thread")
        silent = time.time() - _last_activity

        alive = bool(th and th.is_alive())
        # 存活时：静默未超硬上限 = 正常（含视频等判定），不重启
        if alive and silent < HARD_STALE_LIMIT:
            continue
        # 已死（含线程对象缺失）时：也要静默到阈值，避免任务刚结束瞬间误判
        if not alive and silent < STALE_LIMIT:
            continue

        if alive:
            print(f"\n[看门狗] 任务线程存活但已僵死 {int(silent)} 秒（超过硬上限），"
                  f"判定为卡死：自动重启服务\n")
        else:
            print(f"\n[看门狗] 任务线程已死亡且已 {int(silent)} 秒无日志，判定卡死：自动重启服务\n")

        if th and th.is_alive():
            try:  # 僵死线程先给个体面退出的机会
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_long(th.ident), ctypes.py_object(PauseInterrupt))
            except Exception:
                pass
            time.sleep(5)

        _restarts += 1
        total = _resume_count() + 1
        if total > RESTART_LIMIT:
            print(f"[看门狗] 已跨进程自动重启 {total - 1} 次仍未通过，停止自动恢复，"
                  f"多为网络或平台限制，请手动检查")
            _clear_resume()
            state["running"] = False
            continue
        if state.get("last_courses"):
            _save_resume(state["last_courses"], state.get("last_config"))
        _bump_restart(total)

        # 关键：execv 不释放监听中的 socket，新进程会因端口被自己占住而 bind 失败。
        # 用独立子进程接棒启动，本进程彻底退出把端口交还，避免"重启后打不开"。
        print("[看门狗] 正在交接给新进程并退出...")
        try:
            _env = dict(os.environ)
            _env["CK_NO_BROWSER"] = "1"   # 交接重启不重复开浏览器标签
            subprocess.Popen(
                [_pythonw(), os.path.abspath(__file__)],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                env=_env,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                close_fds=True)
        except Exception as e:
            print(f"[看门狗] 拉起新进程失败：{e}")
            continue
        # 留一点时间让子进程完成互斥体接管前的握手，再硬退出
        time.sleep(3)
        os._exit(0)


def save_last_params(data):
    """任何一次登录成功都记住参数（AI/题库/提交设置），下次打开不用重填。

    必须"合并非空字段"而非整体覆盖：cookie 免密登录时表单里没有账密/AI 参数，
    整体覆盖会把已保存的配置清空，进而让自动恢复（_auto_resume 用 last_params
    登录）拿到空配置 -> build_tiku("none") -> DummyTiku(DISABLE=True)
    -> study_work 开头直接 return SUCCESS -> 视频照刷但题目全部不做。
    """
    cfg = load_ui_config()
    lp = dict(cfg.get("last_params") or {})
    for k in REMEMBER_FIELDS:
        v = data.get(k)
        if v not in (None, ""):
            lp[k] = v
    cfg["last_params"] = lp
    _dump(cfg)


def save_profile(data):
    """账密登录才归档为账号档案（按账号名识别），最新使用的排最前。"""
    cfg = load_ui_config()
    entry = {k: data.get(k) for k in REMEMBER_FIELDS}
    name = entry.get("username") or ""
    others = [p for p in cfg.get("profiles", []) if p.get("username") != name]
    cfg["profiles"] = [entry] + others
    _dump(cfg)

app = Flask(__name__)
app.json.ensure_ascii = False
# 会话密钥：**持久化保存**。原来是每次启动随机生成，导致进程一重启
# （看门狗拉起 / 手动重启 / 更新）已登录的远程用户立刻被弹回登录页。
_secret_cfg = load_ui_config()
if not _secret_cfg.get("secret_key"):
    _secret_cfg["secret_key"] = secrets.token_hex(32)
    _dump(_secret_cfg)
app.secret_key = _secret_cfg["secret_key"]

# ============ 访问认证（2026-09-14）============
# 场景：程序要被内网穿透（SakuraFrp）暴露到公网，必须区分"谁能看、谁能操作"。
#   · 本机访问（127.0.0.1）→ 自动管理员，**免登录**（本机体验与以前完全一致）
#   · 远程访问 → 先过登录页，三种身份：
#       - 本机已保存的「学习通账号 + 密码」→ 普通用户（就是他自己那个号）
#       - 管理口令 → 管理员（全部功能）
#       - 只读口令 → 访客（只能看进度/日志）
# ⚠️ 校验只在**本机档案里比对**，绝不向超星发登录请求：否则访客账号会被
#    平台标记"异地登录"，也等于把别人的密码卷进我们的服务里（责任风险）。
ROLE_ADMIN, ROLE_USER, ROLE_VIEWER = "admin", "user", "viewer"
_AUTH_KEY = "auth"
# 只读角色禁止访问的写接口
_WRITE_PATHS = ("/api/login", "/api/start", "/api/pause", "/api/resume",
                "/api/shutdown", "/api/restart", "/api/quit")


def _is_local_request() -> bool:
    """是否来自本机浏览器（决定是否免登录）。

    ⚠️ 只看 remote_addr 不够（2026-09-14 教训）：若把实例也用内网穿透直连，
    frpc 就在本机转发，公网流量的源地址同样是 127.0.0.1，会被误判成
    "本机"而跳过登录。因此追加 Host 校验，并留一个 CK_STRICT=1 彻底关闭免登录。
    """
    if os.environ.get("CK_STRICT") == "1":
        return False
    if (request.remote_addr or "").strip() not in ("127.0.0.1", "::1"):
        return False
    host = (request.host or "").split(":")[0].strip().lower()
    return host in ("127.0.0.1", "localhost", "[::1]", "::1")


def _auth_tokens() -> dict:
    """取管理/只读口令；首次运行时生成并写回配置。"""
    cfg = load_ui_config()
    a = dict(cfg.get(_AUTH_KEY) or {})
    changed = False
    if not a.get("admin_token"):
        a["admin_token"] = secrets.token_urlsafe(9)
        changed = True
    if not a.get("viewer_token"):
        a["viewer_token"] = secrets.token_urlsafe(6)
        changed = True
    if changed:
        cfg[_AUTH_KEY] = a
        _dump(cfg)
        print(f"[访问口令] 管理口令 : {a['admin_token']}")
        print(f"[访问口令] 只读口令 : {a['viewer_token']}")
        print("           （已写入 ui_config.json，可自行修改）")
    return a


def _match_profile(user: str, pwd: str) -> bool:
    """与本机已保存的学习通档案比对（不发任何外部请求）。"""
    for p in (load_ui_config().get("profiles") or []):
        if (str(p.get("username") or "") == user
                and str(p.get("password") or "") == pwd):
            return True
    return False


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
        return None
    if request.path.startswith("/api/"):
        return jsonify(ok=False, need_login=True, msg="未登录或登录已过期"), 401
    return redirect("/login")


@app.before_request
def _viewer_guard():
    if getattr(g, "role", None) == ROLE_VIEWER and \
            any(request.path.startswith(p) for p in _WRITE_PATHS):
        return jsonify(ok=False, msg="当前是只读口令，没有操作权限"), 403
    return None


@app.get("/api/whoami")
def api_whoami():
    role = getattr(g, "role", ROLE_ADMIN)
    return jsonify(role=role, who=getattr(g, "who", ""),
                   local=_is_local_request(),
                   can_edit=role in (ROLE_ADMIN, ROLE_USER))


@app.post("/auth")
def do_auth():
    data = request.get_json(silent=True) or request.form or {}
    user = str(data.get("user") or "").strip()
    pwd = str(data.get("pwd") or "").strip()
    if not (user and pwd):
        return jsonify(ok=False, msg="请输入账号和密码")
    tk = _auth_tokens()
    if pwd == tk.get("admin_token"):
        session["role"], session["who"] = ROLE_ADMIN, "管理口令"
        return jsonify(ok=True, role=ROLE_ADMIN)
    if pwd == tk.get("viewer_token"):
        session["role"], session["who"] = ROLE_VIEWER, "只读口令"
        return jsonify(ok=True, role=ROLE_VIEWER)
    if _match_profile(user, pwd):
        session["role"], session["who"] = ROLE_USER, user
        return jsonify(ok=True, role=ROLE_USER)
    return jsonify(ok=False, msg="账号或密码不正确（需与本机已保存的学习通账号一致）")


@app.post("/auth/logout")
def do_auth_logout():
    session.clear()
    return jsonify(ok=True)


def _base_path() -> str:
    """反代前缀。网关转发时通过 X-Base-Path 头告知；本机直连则为空串。

    实例不知道自己被挂在哪个路径下（由网关决定），所以必须由网关告知，
    否则反代后前端所有 /api/... 请求都会打到网关根路径上而 404。
    """
    return (request.headers.get("X-Base-Path") or "").rstrip("/")


@app.get("/login")
def login_page():
    return Response(LOGIN_PAGE.replace("__BASE__", _base_path()),
                    mimetype="text/html; charset=utf-8")


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · 刷课进度</title>
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
  button:disabled{opacity:.6;cursor:default}
  .msg{margin-top:12px;font-size:13px;color:#dc2626;min-height:18px}
  .tip{margin-top:16px;padding-top:14px;border-top:1px dashed #e5e7eb;
       font-size:12px;color:#9ca3af;line-height:1.7}
</style></head><body>
<div class="box">
  <h1>刷课进度查看</h1>
  <p class="sub">用学习通账号登录，查看该账号的进度</p>
  <label>学习通账号</label>
  <input id="user" autocomplete="username" placeholder="手机号">
  <label>密码</label>
  <input id="pwd" type="password" autocomplete="current-password" placeholder="密码">
  <button id="btn">登录</button>
  <div class="msg" id="msg"></div>
  <div class="tip">账号密码只与本机已保存的档案比对，不会发送给学习通。<br>
    也可直接输入管理员口令或只读口令进入。</div>
</div>
<script>
const BASE = "__BASE__";
const $ = id => document.getElementById(id);
async function go() {
  const user = $('user').value.trim(), pwd = $('pwd').value.trim();
  if (!user || !pwd) { $('msg').textContent = '请填写账号和密码'; return; }
  $('btn').disabled = true; $('msg').textContent = '校验中...';
  try {
    const r = await fetch(BASE + '/auth', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({user, pwd})}).then(r => r.json());
    if (!r.ok) { $('msg').textContent = r.msg || '登录失败'; $('btn').disabled = false; return; }
    location.href = BASE + '/';
  } catch (e) { $('msg').textContent = '请求失败: ' + e; $('btn').disabled = false; }
}
$('btn').onclick = go;
$('pwd').onkeydown = e => { if (e.key === 'Enter') go(); };
$('user').onkeydown = e => { if (e.key === 'Enter') $('pwd').focus(); };
</script></body></html>"""


state = {"chaoxing": None, "courses": [], "running": False, "logged_in": False,
         "paused": False, "last_courses": None, "last_config": None,
         "work_thread": None, "resuming": False, "job_processor": None,
         # 账号核对信息（2026-09-14）：界面醒目显示"当前登的是谁"，避免
         # cookies 残留导致静默登错号——扫出来的课程全错，界面上却看不出异常
         "account_name": "", "account_user": ""}
log_history: list[str] = []
_old_stdout = _old_stderr = _old_tqdm_stream = None
_last_activity = time.time()   # 最近一条日志的时间，用于检测任务静默卡死


# PauseInterrupt 统一定义在 api/exceptions.py 并由上方导入。
# 不能在这里再定义一份：暂停时需要把它同时注入到 _work 线程和
# JobProcessor 的 worker 线程，而 worker 用的是 main.py 导入的那个类，
# 两份定义会让 isinstance 判定失败、worker 认不出暂停信号。

# ---- 日志模式（与 api/base.py、api/answer.py 的实际输出逐条核对过）----
RE_TOTAL = re.compile(r"共 (\d+) 个章节任务点")
RE_START = re.compile(r"开始任务: (.+), 总时长: (\d+)s, 已进行: (\d+)s")
RE_DONE = re.compile(r"(?:任务完成|任务瞬间完成): (.+)")
RE_EMPTY_DONE = re.compile(r"空页面任务完成 -> (.+)")
RE_CHAPTER_FINISHED = re.compile(r"章节：(.+) 已完成所有任务点")
RE_HIT = re.compile(r"获取答案：")  # 失败行是“获取答案失败：”，不会误命中
RE_MISS = re.compile(r"获取答案失败：")
RE_RANDOM = re.compile(r"随机选择 -> ")
RE_COVER = re.compile(r"章节检测题库覆盖率： (\d+)%")
RE_SUBMITTED = re.compile(r"提交答题成功")
RE_SAVED = re.compile(r"保存答题成功")
# 成绩可能是 "0.0 分"（含空格）或 "100分"，用 \S+\s* 兼容两种写法
RE_PASS = re.compile(r"章节检测全部正确（成绩 (\S+?)\s*分），通过！")
RE_REDO = re.compile(r"章节检测有 (\d+)/(\d+) 题回答错误")
# unit_scale 会把秒数格式化成 1.70k 这类带后缀的形式，必须兼容
RE_PBAR = re.compile(r"^(.*?):\s*\d+%\|.*\|\s*([\d.]+[kMG]?)\s*/\s*([\d.]+[kMG]?)\s*$")


def _fnum(s):
    s = s.strip()
    if s and s[-1] in "kMG":
        return int(float(s[:-1]) * {"k": 1e3, "M": 1e6, "G": 1e9}[s[-1]])
    return int(float(s))


class ProgressTracker:
    """从日志流增量解析刷课进度，供网页端展示结构化进度卡。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.total = 0
        self.done: set[str] = set()
        self.hits = 0
        self.misses = 0        # 题库未命中（多数已由 AI 补答，≠答错）
        self.random_n = 0      # 真正随机蒙的题数（题库未命中 + AI 也没答出来）
        self.submitted = 0
        self.saved = 0
        self.passed = 0
        self.redo = 0
        self.q_correct = 0
        self.q_total = 0
        self.last_qlen = 0
        self._last_qkey = ""      # 题面指纹，用于同题多行日志去重
        self.last_cover: int | None = None
        self.current: dict | None = None
        # 每个课程已完成的任务点数（2026-09-14）。任务点名在同一次运行里
        # 可能跨课程重名（"1.1 概述"这种），所以不按名字分、只累计总数，
        # 由 raw_done 供 ProgressTracker.snapshot 分给"当前正在刷的那门课"。
        self._point_done = 0

    def feed(self, chunk):
        for line in chunk.splitlines():  # splitlines 也按 \r 切，tqdm 行无需特殊处理
            line = line.strip()
            if line:
                self._parse(line)

    def _count_point_done(self, point_name):
        """任务点完成 +1，并当场记到对应课程头上。

        必须即时记账（不能靠后台轮询取差值）—— 轮询间隔比短任务还长，
        收尾时差值还没被取走，界面上那门课就永远是 0（实测踩过）。
        归属用任务点名反查（_work 建 task 时登记），不靠"最后 begin 的课"。
        """
        self._point_done += 1
        cid = run_tracker.cid_of_point(point_name)
        if cid and cid in run_tracker.rows:
            run_tracker.rows[cid]["done"] = run_tracker.rows[cid].get("done", 0) + 1

    def _parse(self, line):
        if m := RE_TOTAL.search(line):
            self.total = int(m.group(1))
        elif m := RE_START.search(line):
            # 任务点归属哪门课：多课程时用户必须能一眼看出"现在在刷哪门"。
            # 按任务点名反查（而不是记"最后 begin 的课"—— 并发下必错）
            nm = m.group(1)
            self.current = {"name": nm, "total": int(m.group(2)),
                            "pos": int(m.group(3)),
                            "course": run_tracker.course_of_point(nm)}
        elif m := RE_DONE.search(line):
            nm = m.group(1).strip()
            self.done.add(nm)
            self._count_point_done(nm)
        elif m := RE_EMPTY_DONE.search(line):
            nm = m.group(1).strip()
            self.done.add(nm)
            self._count_point_done(nm)
        elif m := RE_CHAPTER_FINISHED.search(line):
            nm = m.group(1).strip()
            self.done.add(nm)
            self._count_point_done(nm)
        elif RE_MISS.search(line):
            # 必须放在 RE_HIT 之前判断：失败行也含“获取答案”字样
            self.misses += 1
            self.q_total += 1
            self._last_qkey = ""
        elif m := RE_HIT.search(line):
            # 同一题组会打多行（题库命中 + 回退命中 + 多题库汇总），
            # 用题面指纹去重，避免一次答题被算成三次
            key = line[m.end():m.end() + 120].strip()
            if key and key != self._last_qkey:
                self._last_qkey = key
                self.hits += 1
                self.q_correct += 1
                self.q_total += 1
        elif RE_RANDOM.search(line):
            # 只有这一行才是真·随机蒙题（题库未命中且 AI 也没给出答案）。
            # 2026-09-14 修：早先这里也往 self.misses 里加，导致界面上
            # "随机作答 19" 其实全是题库未命中、AI 已补答的题（实测误报）。
            self.random_n += 1
        elif m := RE_COVER.search(line):
            self.last_cover = int(m.group(1))
        elif RE_SUBMITTED.search(line):
            self.submitted += 1
        elif RE_SAVED.search(line):
            self.saved += 1
        elif m := RE_REDO.search(line):
            self.redo += 1
        elif m := RE_PASS.search(line):
            self.passed += 1
        elif m := RE_PBAR.match(line):
            # tqdm 播放进度行：desc 与“开始任务”的任务名一致时才更新。
            # total 用“开始任务”行的精确秒数，tqdm 的 1.70k 缩写有精度损失
            if self.current and self.current["name"] == m.group(1):
                self.current["pos"] = _fnum(m.group(2))

    def snapshot(self):
        acc = round(self.q_correct / self.q_total * 100) if self.q_total else None        # 平台实际判定统计（2026-09-11）：此前界面上的 "accuracy" 其实是
        # "拿到答案的题占比"，被当成正确率显示会严重误导（用户实测 3.x 听力
        # 界面看着正常、实际大量判错）。这里补一组来自学习库的真实判据。
        ver = wrong = 0
        try:
            import os as _os
            _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                               "learned_answers.json")
            with open(_p, encoding="utf-8") as _f:
                _d = json.load(_f)
            ver = sum(1 for v in _d.values() if v.get("verified"))
            wrong = sum(1 for v in _d.values() if v.get("wrong"))
        except Exception:
            pass
        return {"total": self.total, "done": len(self.done), "hits": self.hits,
                "misses": self.misses, "random_n": self.random_n,
                "submitted": self.submitted,
                "saved": self.saved, "passed": self.passed, "redo": self.redo,
                "accuracy": acc, "last_cover": self.last_cover,
                "learned_verified": ver, "learned_wrong": wrong,
                "paused": state["paused"],
                "resuming": state["resuming"],
                "stale_secs": int(time.time() - _last_activity),
                "current": self.current, "running": state["running"],
                # 课程运行看板（2026-09-14）
                "courses": run_tracker.snapshot()}


tracker = ProgressTracker()


class CourseRunTracker:
    """多课程运行看板（2026-09-14）。

    用户诉求原话："勾了多个课程，但运行界面看不到在刷哪个课程、剩下的课程有没有在刷。"

    原来的界面只有一行「当前任务点」（curBox），而任务点是跨课程混在
    一个队列里并发跑的（4 个 worker），所以那一行既看不出是**哪门课**，
    也看不出**其它课在不在跑**。这里给出一份按课程分组的清单：

        [{title, id, video, work, tasks, done, phase, status}, ...]

    phase:  scan(读章节) / video(刷课) / homework(作业) / done(完成)
    status: running(正在刷) / pending(排队中) / done(已完成) / skipped(已跳过)

    「谁在刷」不靠猜：以 unit 为单位由 _work 显式 begin/finish，而不是
    从日志文本反推 —— 多 worker 并发时任务点名字会交错，反推必错。
    """

    PHASE_ORDER = {"scan": 0, "video": 1, "homework": 2, "done": 3}

    def __init__(self):
        self.reset()

    def reset(self):
        self._lock = threading.Lock()
        self.order: list[str] = []     # courseId，保持用户勾选顺序
        self.rows: dict[str, dict] = {}
        self._active: set[tuple[str, str]] = set()
        self._point_map: dict[str, str] = {}   # 任务点名 → courseId
        self._course_of_point = ""

    # ---- 每个 unit（课程 × 阶段）的开始 / 结束 ----
    def begin(self, cid, phase, title=""):
        with self._lock:
            row = self.rows.get(cid)
            if row is None:
                row = {"id": str(cid), "title": title, "video": False, "work": False,
                       "tasks": 0, "done": 0, "phase": "scan",
                       "status": "pending", "active": ""}
                self.rows[str(cid)] = row
                self.order.append(str(cid))
            if title and not row["title"]:
                row["title"] = title
            if phase == "video":
                row["video"] = True
            elif phase == "homework":
                row["work"] = True
            self._active.add((str(cid), phase))
            # 阶段推进：不让后面的阶段被前面的回退覆盖（作业阶段可能紧接刷课）
            if self.PHASE_ORDER.get(phase, 0) >= self.PHASE_ORDER.get(row["phase"], 0):
                row["phase"] = phase
            row["status"] = "running"
            row["active"] = phase

    def finish(self, cid, phase, status="done"):
        """结束某个 unit（课程 × 阶段）。

        注意：只有在**本课程已经没有任何 unit 在跑**时才改 status。
        begin() 阶段就是这么登记"多门课同时在跑"的 —— 一旦某门课结束就
        顺手改成 done，界面上就会出现"正在刷的课显示已完成"（实测踩过）。
        """
        key = (str(cid), phase)
        with self._lock:
            row = self.rows.get(str(cid))
            if row is None:
                return
            self._active.discard(key)
            # 本课程是否还有别的阶段在跑（典型的：刷课跑完 → 接着进作业阶段）
            still = [a for a in self._active if a[0] == str(cid)]
            if still:
                row["active"] = still[0][1]
                row["status"] = "running"
            else:
                row["active"] = ""
                row["status"] = status

    def set_tasks(self, cid, tasks):
        with self._lock:
            row = self.rows.get(str(cid))
            if row is not None:
                row["tasks"] = int(tasks)

    def set_done(self, cid, done):
        with self._lock:
            row = self.rows.get(str(cid))
            if row is not None:
                row["done"] = int(done)

    def mark_skip(self, cid, reason):
        """课程没勾选刷课等情况：明确标成"已跳过"，而不是永远停在"排队中"。
        这正是用户抱怨的点 —— 界面上看不出剩下的课到底动没动。"""
        with self._lock:
            row = self.rows.get(str(cid))
            if row is None:
                row = {"id": str(cid), "title": "", "video": False, "work": False,
                       "tasks": 0, "done": 0, "phase": "done",
                       "status": "pending", "active": ""}
                self.rows[str(cid)] = row
                self.order.append(str(cid))
            row["status"] = "skipped"
            row["reason"] = reason
            row["active"] = ""

    def note(self, cid, reason):
        with self._lock:
            row = self.rows.get(str(cid))
            if row is not None:
                row["reason"] = reason

    # ---- 「当前任务点」归属哪门课 ----
    # 不能靠"最后 begin 的那门课"来定归属：任务点是 4 个 worker 并发处理的，
    # 而且读章节阶段会把所有课都 begin 一遍，最后停在末尾那门课上（实测踩过：
    # 全部任务点都被记到最后一门课头上）。
    # 改用**任务点名 → 课程**的映射反查，同一轮运行里任务点名唯一。
    def set_point_map(self, mapping):
        with self._lock:
            self._point_map = dict(mapping or {})

    def course_of_point(self, point_name=None):
        if point_name:
            cid = getattr(self, "_point_map", {}).get(point_name)
            if cid and cid in self.rows:
                return self.rows[cid].get("title") or ""
        cid = getattr(self, "_course_of_point", "")
        if cid and cid in self.rows:
            return self.rows[cid].get("title") or ""
        return ""

    def cid_of_point(self, point_name):
        """任务点 → courseId。任务点完成的记账靠它，不能靠全局游标。"""
        return getattr(self, "_point_map", {}).get(point_name or "")

    def snapshot(self):
        with self._lock:
            rows = []
            for cid in self.order:
                r = dict(self.rows[cid])
                # active 用布尔量给前端驱动"转圈"动画，别把 phase 字符串
                # 混进这个字段（前端按它判断是否高亮）
                r["active"] = bool(r.get("active")) and r.get("status") == "running"
                rows.append(r)
            # 「正在刷」的排前面，其次排队中，最后已完成/已跳过
            rank = {"running": 0, "pending": 1, "done": 2, "skipped": 3}
            rows.sort(key=lambda r: rank.get(r.get("status"), 9))
            return {"courses": rows,
                    "done_n": sum(1 for r in rows if r.get("status") == "done"),
                    "all_n": len(rows),
                    "index": list(self.order)}


run_tracker = CourseRunTracker()


# 终端控制序列：ANSI 颜色 / 光标移动 / OSC 标题设置。
# loguru 默认带颜色输出、tqdm 会发光标上移，这些字节直接进日志历史时，
# 会在网页日志区显示成 "[A"、"[32m" 之类的乱码（2026-09-14 实测发现）。
_RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]"              # CSI 序列
                      r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"      # OSC 序列
                      r"|\x1b[@-Z\\-_]")                        # 其它两字节转义


def strip_ansi(text: str) -> str:
    """去掉终端控制序列，只留下可读文本。"""
    return _RE_ANSI.sub("", text or "")


class QueueRedirector:
    """把 stdout/stderr 留存进历史供刷新后回看，同时喂给进度解析器。"""

    def __init__(self, history):
        self.history = history

    def write(self, s):
        if not s:
            return
        tracker.feed(s)          # 进度解析用原始串（不能被清洗影响）
        if "\r" in s:  # tqdm 进度条行只进结构化进度，不占用日志历史
            return
        clean = strip_ansi(s)
        if not clean.strip():
            return               # 清洗后只剩控制字符，整行丢弃
        self.history.append(clean)
        global _last_activity
        _last_activity = time.time()   # 卡死自检依据：正常运行时日志持续在滚动
        if len(self.history) > HISTORY_MAX:
            del self.history[: len(self.history) - HISTORY_MAX]

    def flush(self):
        pass


def start_capture():
    """项目在导入时就把 stderr 引用固定到了 api.logger.tqdm_stream，
    只替换 sys.stderr 抓不到 loguru 的日志，必须连这个变量一起换掉。"""
    global _old_stdout, _old_stderr, _old_tqdm_stream
    _old_stdout, _old_stderr = sys.stdout, sys.stderr
    _old_tqdm_stream = app_logger.tqdm_stream
    r = QueueRedirector(log_history)
    sys.stdout = sys.stderr = r
    app_logger.tqdm_stream = r


def stop_capture():
    global _old_stdout, _old_stderr, _old_tqdm_stream
    if _old_stdout is not None:
        sys.stdout, sys.stderr = _old_stdout, _old_stderr
        _old_stdout = _old_stderr = None
    if _old_tqdm_stream is not None:
        app_logger.tqdm_stream = _old_tqdm_stream
        _old_tqdm_stream = None


def build_tiku(tiku_type, data):
    conf = {}
    try:
        _, tiku_conf, _ = load_config_from_file(CONFIG_PATH)
        conf = dict(tiku_conf)
    except Exception:
        pass

    provider = {"adapter": "TikuAdapter", "yanxi": "TikuYanxi",
                "ai": "AI"}.get(tiku_type, "")
    if tiku_type == "adapter" and data.get("ai_fallback"):
        provider = "TikuAdapter,AI"  # 免费题库搜不到时回落到 AI
    conf["provider"] = provider  # 留空 → DummyTiku，跳过答题任务

    if "TikuAdapter" in provider:
        conf["url"] = _ensure_use_local(
            (data.get("adapter_url") or "").strip() or DEFAULT_ADAPTER_URL)
    if "TikuYanxi" in provider:
        conf["tokens"] = (data.get("token") or "").strip()
    if "AI" in provider:
        conf["endpoint"] = (data.get("ai_endpoint") or "").strip()
        conf["key"] = (data.get("ai_key") or "").strip()
        conf["model"] = (data.get("ai_model") or "").strip()

    conf["submit"] = "true" if data.get("auto_submit") else "false"
    try:
        conf["cover_rate"] = str(min(1.0, max(0.0, float(data.get("cover_rate") or 0.9))))
    except (TypeError, ValueError):
        conf["cover_rate"] = "0.9"

    tiku = Tiku.get_tiku_from_config(conf)
    tiku.init_tiku()
    return tiku


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>超星刷课</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; padding:32px; background:#f4f6f8; color:#1f2328;
         font:14px/1.6 "Microsoft YaHei UI","PingFang SC",sans-serif; }
  .wrap { max-width:880px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:#6b7280; font-size:13px; margin-bottom:20px; }
  .card { background:#fff; border:1px solid #e5e7eb; border-radius:10px;
          padding:22px; margin-bottom:16px; }
  label { display:block; font-size:13px; color:#374151; margin:14px 0 6px; }
  input[type=text], input[type=password], input[type=number], select {
    width:100%; padding:9px 11px; border:1px solid #d1d5db; border-radius:6px;
    font-size:14px; outline:none; background:#fff; }
  input:focus { border-color:#2563eb; }
  .row { display:flex; gap:16px; }
  .row > div { flex:1; }
  .chk { display:flex; align-items:center; gap:8px; margin:14px 0 4px;
         font-size:13px; color:#374151; }
  button { background:#2563eb; color:#fff; border:0; border-radius:6px;
           padding:10px 20px; font-size:14px; cursor:pointer; }
  button:hover { background:#1d4ed8; }
  button:disabled { background:#9ca3af; cursor:not-allowed; }
  button.ghost { background:#fff; color:#374151; border:1px solid #d1d5db; }
  .msg { margin-top:14px; font-size:13px; color:#b91c1c; min-height:20px; }
  .course { display:flex; align-items:flex-start; gap:10px; padding:11px 13px;
            border:1px solid #e5e7eb; border-radius:7px; margin-bottom:8px;
            cursor:pointer; }
  .course:hover { background:#f9fafb; }
  .course.on { border-color:#2563eb; background:#eff6ff; }
  .course input { margin-top:4px; }
  .course .t { font-weight:600; }
  .course .m { color:#6b7280; font-size:12px; }
  .course .modes { margin-top:7px; display:flex; gap:16px; }
  .course .mode { display:inline-flex; align-items:center; gap:5px;
                  font-size:13px; color:#374151; cursor:pointer; }
  .course .mode input { margin:0; cursor:pointer; }
  .bar { display:flex; align-items:center; gap:10px; margin-bottom:14px; }
  .bar .sp { flex:1; }
  .ov { background:#f8fafc; border:1px solid #e5e7eb; border-radius:10px;
        padding:18px 20px; margin-bottom:14px; }
  .ov .top { display:flex; align-items:baseline; gap:10px; }
  .ov .big { font-size:30px; font-weight:700; letter-spacing:-.5px; }
  .ov .pct { font-size:15px; color:#16a34a; font-weight:600; }
  .ov .cap { color:#6b7280; font-size:12.5px; }
  .pbar { height:10px; background:#e5e7eb; border-radius:5px; overflow:hidden; margin:10px 0 6px; }
  .pbar.thin { height:7px; margin:8px 0 5px; }
  .pfill { height:100%; width:0%; background:#16a34a; border-radius:5px; transition:width .5s; }
  .pfill.blue { background:#2563eb; }
  .curbox { border:1px solid #e5e7eb; border-radius:8px; padding:13px 16px; margin-bottom:14px; }
  .curbox .curname { font-weight:600; font-size:13.5px; word-break:break-all; }
  .curbox .curmeta { color:#6b7280; font-size:12px; }
  /* 课程运行看板（2026-09-14）：多选课程时明确显示每门课在不在刷 */
  .cboard { border:1px solid #e5e7eb; border-radius:10px; margin-bottom:14px;
            overflow:hidden; }
  .cboard .chead { display:flex; align-items:center; gap:8px; padding:10px 14px;
                   background:#f8fafc; border-bottom:1px solid #e5e7eb;
                   font-size:13px; font-weight:600; }
  .cboard .chead .sp { flex:1; }
  .cboard .chead .csum { font-weight:400; font-size:12px; color:#6b7280; }
  .cboard .clist { max-height:250px; overflow:auto; }
  .crow { display:flex; align-items:center; gap:10px; padding:9px 14px;
          border-bottom:1px solid #f1f5f9; font-size:13px; }
  .crow:last-child { border-bottom:none; }
  .crow .dot { width:8px; height:8px; border-radius:50%; flex:none;
               background:#d1d5db; }
  .crow .ct { flex:1; min-width:0; word-break:break-all; }
  .crow .ct .cn { font-weight:500; }
  .crow .ct .cr { color:#9ca3af; font-size:11px; margin-top:1px;
                  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .crow .mini-bar { width:90px; height:6px; background:#e5e7eb; border-radius:3px;
                    overflow:hidden; flex:none; }
  .crow .mini-bar i { display:block; height:100%; width:0%; background:#16a34a;
                      transition:width .5s; }
  .crow .cst { flex:none; font-size:11.5px; padding:2px 8px; border-radius:10px;
               background:#f1f5f9; color:#64748b; white-space:nowrap; }
  .crow.running { background:#eff6ff; }
  .crow.running .dot { background:#2563eb; animation:pulse 1.2s infinite; }
  .crow.running .cst { background:#dbeafe; color:#1d4ed8; }
  .crow.done .dot { background:#16a34a; }
  .crow.done .cst { background:#d1fae5; color:#065f46; }
  .crow.skipped { opacity:.6; }
  .crow.skipped .cst { background:#f1f5f9; color:#94a3b8; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
  .stats { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:14px; }
  .scard { flex:1; min-width:96px; background:#f8fafc; border:1px solid #e5e7eb;
           border-radius:8px; padding:12px 8px; text-align:center; }
  .scard .n { font-size:22px; font-weight:700; }
  .scard .n.ok { color:#16a34a; }
  .scard .n.warn { color:#d97706; }
  .scard .l { font-size:12px; color:#6b7280; margin-top:2px; }
  .scard .s { font-size:11px; color:#9ca3af; margin-top:1px; }
  #log { height:260px; overflow:auto; background:#f8fafc; color:#334155;
         border:1px solid #e5e7eb; border-radius:8px; padding:12px;
         font:12px/1.65 Consolas,monospace; white-space:pre-wrap; word-break:break-all; }
  .hide { display:none; }
  .tag { display:inline-block; padding:2px 9px; border-radius:11px;
         font-size:12px; background:#fee2e2; color:#b91c1c; }
  .tag.ok { background:#d1fae5; color:#065f46; }
  .tag.paused { background:#fef3c7; color:#92400e; }
  .warn { background:#fffbeb; border:1px solid #fcd34d; color:#92400e;
          padding:10px 13px; border-radius:6px; font-size:12.5px; margin-bottom:14px; }
  .topbar { display:flex; align-items:center; margin-bottom:4px; }
  .topbar h1 { flex:1; }
  .mini { padding:5px 12px; font-size:12px; background:#fff; color:#6b7280;
          border:1px solid #d1d5db; }
  .mini:hover { color:#b91c1c; border-color:#fca5a5; background:#fef2f2; }
  .info { background:#eff6ff; border:1px solid #bfdbfe; color:#1e40af;
          padding:10px 13px; border-radius:6px; font-size:12.5px; margin-bottom:14px; }
  /* 按钮忙碌态：转圈 + 禁用，避免连点重复提交 */
  button[data-busy]::after { content:""; display:inline-block; width:9px; height:9px;
          margin-left:6px; border:2px solid rgba(255,255,255,.45);
          border-top-color:#fff; border-radius:50%;
          animation:spin .7s linear infinite; vertical-align:0; }
  button.ghost[data-busy]::after, button.mini[data-busy]::after {
          border-color:#cbd5e1; border-top-color:#374151; }
  @keyframes spin { to { transform:rotate(360deg) } }
  @media (prefers-reduced-motion: reduce) { button[data-busy]::after { animation:none } }
  button:disabled { opacity:.6; cursor:default; }
  button:focus-visible { outline:2px solid #2563eb; outline-offset:2px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <h1>超星学习通刷课</h1>
    <button class="mini" id="btnQuit">退出服务</button>
  </div>
  <div class="sub">本地运行 · 刷新页面不会丢失状态 · 账号密码只保存在你自己的电脑上</div>
  <div class="card hide" id="roleBar"></div>

  <div class="card" id="step1">
    <label>已保存的账号配置</label>
    <select id="profileSel">
      <option value="-1">-- 手动填写 --</option>
    </select>
    <label>手机号</label>
    <input type="text" id="user" autocomplete="off">
    <label>密码</label>
    <input type="password" id="pwd">
    <div class="row">
      <div><label>播放倍速（1 - 2，平台最高支持 2 倍）</label>
        <input type="number" id="speed" value="2" min="1" max="2" step="0.5"></div>
      <div><label>并发章节数（强烈建议 1）</label>
        <input type="number" id="jobs" value="1" min="1" max="8" step="1"></div>
    </div>
    <div class="warn">并发保持 1。上次你用高并发跑，1.3 章节疑似因此卡住，且平台有「进度回溯」机制。</div>

    <label>题库（用于章节检测自动答题；不启用则跳过答题任务）</label>
    <select id="tikuType">
      <option value="none">不使用（跳过答题任务）</option>
      <option value="adapter" selected>自建 TikuAdapter（免费，推荐）</option>
      <option value="yanxi">言溪题库（付费）</option>
      <option value="ai">纯 AI 大模型（自己的 API Key）</option>
    </select>
    <div id="adapterBox">
      <label>Adapter 地址</label>
      <input type="text" id="adapterUrl" value="http://127.0.0.1:8060/adapter-service/search?use=local,icodef,buguake,wanneng,tikuhai">
      <div class="hint">必须带 <code>?use=local</code>，否则本地题库不参与查询（听力题会全部搜不到）</div>
      <div class="chk"><input type="checkbox" id="aiFallback">
        <span>AI 兜底：免费题库搜不到的题自动改用 AI 作答（上次 25 题里 7 题没搜到，建议开启）</span></div>
    </div>
    <div id="yanxiBox" class="hide">
      <label>言溪 Token（官网个人中心 → 复制用户凭证）</label>
      <input type="text" id="token">
    </div>
    <div id="aiBox" class="hide">
      <div class="info">已按 Agnes 免费 API 预填端点和模型（实测可用），粘贴你的 sk- key 即可；也可换成硅基流动等任何 OpenAI 格式 API。</div>
      <label>API Endpoint（OpenAI 格式，含 /v1）</label>
      <input type="text" id="aiEndpoint" value="https://apihub.agnes-ai.cn/v1"
             placeholder="https://apihub.agnes-ai.cn/v1 或 https://api.siliconflow.cn/v1">
      <label>API Key</label>
      <input type="password" id="aiKey">
      <label>模型名</label>
      <input type="text" id="aiModel" value="agnes-3.0-flash"
             placeholder="agnes-3.0-flash（支持读图） / deepseek-ai/DeepSeek-V3">
      <div class="chk"><button class="ghost" id="btnTestAi" style="padding:5px 12px;font-size:12px">测试 AI 连接</button>
        <span id="aiTestResult" style="font-size:12px;color:#6b7280"></span></div>
    </div>

    <div class="chk"><input type="checkbox" id="autoSubmit" checked>
      <span>自动提交答案（达到覆盖率门槛就直接提交，不再人工逐章核对）</span></div>
    <div class="row">
      <div><label>提交覆盖率门槛（0 - 1，搜到题占比达到才提交）</label>
        <input type="number" id="coverRate" value="0.9" min="0" max="1" step="0.05"></div>
      <div></div>
    </div>
    <div class="info">覆盖率不达标时该章只保存不提交。开启 AI 兜底后命中率会明显提高，一般能到 100%。</div>

    <div style="margin-top:18px"><button id="btnLogin">登录并获取课程</button></div>
    <div class="msg" id="msg1"></div>
  </div>

  <div class="card hide" id="step2">
    <div style="margin-bottom:10px">
      <span id="curAccount" class="tag">账号未核对</span>
      <button class="ghost mini" id="btnLogout">切换账号</button>
    </div>
    <div class="bar">
      <strong>勾选要做的课程与内容</strong>
      <span class="sp"></span>
      <span id="picked" class="tag">已选 0</span>
      <button class="ghost" id="btnAll">全选</button>
      <button class="ghost" id="btnNone">全不选</button>
      <button class="ghost" id="btnAllWork">全选作业</button>
      <button class="ghost" id="btnNoWork">全不选作业</button>
      <button id="btnStart">开始</button>
    </div>
    <div style="display:flex;gap:8px;align-items:center;margin:10px 0">
      <input type="text" id="courseFilter"
             placeholder="筛选课程（名称或课程ID；只影响显示，不影响已勾选）"
             style="flex:1;padding:8px 10px;border:1px solid #d8dee4;border-radius:8px;font-size:13px">
      <span id="filterCount" class="tag">共 0 门</span>
    </div>
    <div id="list"></div>
    <div class="msg" id="msg2"></div>
  </div>

  <div class="card hide" id="step3">
    <div class="bar">
      <strong>运行进度</strong><span class="sp"></span>
      <span id="stat" class="tag">运行中</span>
      <button id="btnPause" class="hide">暂停刷课</button>
      <button class="ghost" id="btnBack">返回选课</button>
    </div>

    <div class="ov">
      <div class="top">
        <span class="big"><span id="pDone">0</span><span style="color:#9ca3af">/</span><span id="pTotal">0</span></span>
        <span class="pct" id="pPct">0%</span>
        <span class="cap">已完成任务点</span>
      </div>
      <div class="pbar"><div class="pfill" id="pFill"></div></div>
    </div>

    <div class="curbox hide" id="curBox">
      <div class="curname" id="curName"></div>
      <div class="pbar thin"><div class="pfill blue" id="curFill"></div></div>
      <div class="curmeta" id="curMeta"></div>
    </div>

    <div class="cboard hide" id="cBoard">
      <div class="chead">
        <span>课程运行状态</span>
        <span class="sp"></span>
        <span class="csum" id="cSum"></span>
      </div>
      <div class="clist" id="cList"></div>
    </div>

    <div class="stats">
      <div class="scard" title="拿到答案的题占比（含 AI 与搜题服务给出的未验证答案）。注意：这不是平台判定的正确率，只表示没走随机作答">
        <div class="n ok" id="nScore">-</div><div class="l">答案获取率</div><div class="s" id="sPass"></div>
      </div>
      <div class="scard" title="平台实际判定为正确的题组数 / 有明确判定的题组数（来自章节检测成绩解析，判对才进题库）">
        <div class="n ok" id="nReal">-</div><div class="l">平台判对</div><div class="s" id="sReal"></div>
      </div>
      <div class="scard"><div class="n ok" id="nHit">0</div><div class="l">题库命中</div></div>
      <div class="scard" title="题库里没收录、改由 AI 补答的题数。这不是答错，只是没走题库；真正随机蒙题的题数会单独标注">
        <div class="n warn" id="nMiss">0</div>
        <div class="l" id="nMissLabel">题库未命中</div></div>
      <div class="scard"><div class="n ok" id="nSub">0</div><div class="l">已提交</div></div>
      <div class="scard"><div class="n" id="nSave">0</div><div class="l">仅保存</div></div>
      <div class="scard"><div class="n" id="nCover">-</div><div class="l">最近章节覆盖率</div></div>
    </div>

    <div class="bar" style="margin-bottom:8px">
      <button class="ghost" id="btnLog">展开运行日志</button>
    </div>
    <div class="msg" id="msg3"></div>
    <div id="log" class="hide"></div>
  </div>
</div>

<script>
const BASE = "__BASE__";
const $ = id => document.getElementById(id);
let courses = [], timer = null, got = -1, pulling = false;

function showStep(n) {
  for (let i = 1; i <= 3; i++) $('step' + i).classList.toggle('hide', i !== n);
}

// ---- 题库 UI 联动 ----
function updateTikuUI() {
  const v = $('tikuType').value;
  $('adapterBox').classList.toggle('hide', v !== 'adapter');
  $('yanxiBox').classList.toggle('hide', v !== 'yanxi');
  const needAi = v === 'ai' || (v === 'adapter' && $('aiFallback').checked);
  $('aiBox').classList.toggle('hide', !needAi);
}
$('tikuType').onchange = updateTikuUI;
$('aiFallback').onchange = updateTikuUI;

// ---- AI 连接测试 ----
$('btnTestAi').onclick = async () => {
  const el = $('aiTestResult'), btn = $('btnTestAi');
  // 测试要真发一次请求，慢的时候十几秒；不给转圈用户会以为按钮没反应而狂点
  const old = btn.textContent;
  btn.disabled = true; btn.dataset.busy = '1';
  el.textContent = '测试中...';
  el.style.color = '#6b7280';
  try {
    const r = await fetch(BASE + '/api/test-ai', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ai_endpoint: $('aiEndpoint').value,
        ai_key: $('aiKey').value, ai_model: $('aiModel').value})}).then(r => r.json());
    if (r.ok) {
      el.textContent = '✓ 连通（' + r.delay + 's）模型回复：' + r.sample;
      el.style.color = '#16a34a';
    } else {
      el.textContent = '✗ ' + r.msg;
      el.style.color = '#b91c1c';
    }
  } catch (e) {
    el.textContent = '✗ 请求失败: ' + e;
    el.style.color = '#b91c1c';
  } finally {
    btn.disabled = false; delete btn.dataset.busy; btn.textContent = old;
  }
};

// ---- 已保存档案：下拉选择后自动填充 ----
let profiles = [], lastParams = {};

// 兼容修复（2026-09-14）：老档案里存的 adapter_url 往往没带 ?use=local，
// 那会让本地题库完全不参与查询（听力题全部搜不到）。这里统一补上，
// 用户不必手动改；已是新格式的保持原样。
function fixAdapterUrl(u) {
  u = (u || '').trim();
  if (!u) return ADAPTER_URL_DEFAULT;
  if (u.indexOf('adapter-service') === -1) return u;
  if (u.indexOf('use=') !== -1) return u;
  return u + (u.indexOf('?') === -1 ? '?' : '&') + 'use=local,icodef,buguake,wanneng,tikuhai';
}
const ADAPTER_URL_DEFAULT = 'http://127.0.0.1:8060/adapter-service/search?use=local,icodef,buguake,wanneng,tikuhai';

function fillProfile(i) {
  const p = profiles[i];
  if (!p) return;
  $('user').value = p.username || '';
  $('pwd').value = p.password || '';
  $('tikuType').value = p.tiku_type || 'none';
  $('adapterUrl').value = fixAdapterUrl(p.adapter_url);
  $('aiEndpoint').value = p.ai_endpoint || '';
  $('aiKey').value = p.ai_key || '';
  $('aiModel').value = p.ai_model || '';
  $('coverRate').value = p.cover_rate || 0.9;
  $('aiFallback').checked = !!p.ai_fallback;
  $('autoSubmit').checked = p.auto_submit !== false;
  updateTikuUI();
}

function fillParams(p) {
  if (!p) return;
  if (p.tiku_type) $('tikuType').value = p.tiku_type;
  if (p.adapter_url) $('adapterUrl').value = fixAdapterUrl(p.adapter_url);
  if (p.ai_endpoint) $('aiEndpoint').value = p.ai_endpoint;
  if (p.ai_key) $('aiKey').value = p.ai_key;
  if (p.ai_model) $('aiModel').value = p.ai_model;
  if (p.cover_rate) $('coverRate').value = p.cover_rate;
  if (p.speed) $('speed').value = p.speed;
  if (p.jobs) $('jobs').value = p.jobs;
  $('aiFallback').checked = !!p.ai_fallback;
  $('autoSubmit').checked = p.auto_submit !== false;
  updateTikuUI();
}

async function loadProfiles() {
  try {
    const r = await fetch(BASE + '/api/saved-config').then(r => r.json());
    profiles = r.profiles || [];
    lastParams = r.last_params || {};
  } catch (e) { profiles = []; lastParams = {}; }
  const sel = $('profileSel');
  sel.length = 1;   // 重建选项，只保留「-- 手动填写 --」，避免重复追加
  profiles.forEach((p, i) => {
    const opt = document.createElement('option');
    opt.value = i;
    const ai = p.ai_fallback || p.tiku_type === 'ai' ? ' · AI兜底' : '';
    opt.textContent = (p.username || '未命名') + ai;
    sel.appendChild(opt);
  });
  if (profiles.length) sel.value = '0';   // 默认选最近使用的
}

$('profileSel').onchange = () => fillProfile(+$('profileSel').value);

// ---- 角色检查（2026-09-14）：远程只读/用户身份提示 ----
async function checkRole() {
  let r = null;
  try { r = await fetch(BASE + '/api/whoami').then(x => x.json()); } catch (e) { return null; }
  const bar = $('roleBar');
  if (!bar || !r) return r;
  if (r.role === 'viewer') {
    bar.className = 'card';
    bar.style.cssText = 'background:#fff7ed;border-color:#fed7aa;color:#b45309';
    bar.textContent = '当前是只读访问：可以看进度和日志，不能改配置或启动任务。';
  } else if (!r.local) {
    bar.className = 'card';
    bar.style.cssText = 'background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8';
    bar.textContent = '已远程登录：' + (r.who || '') +
      '（如需退出请到登录页重新登录）';
  }
  return r;
}

// ---- 刷新恢复 + 自动预填/自动登录 ----
async function boot() {
  await checkRole();
  await loadProfiles();
  try {
    const st = await fetch(BASE + '/api/status').then(r => r.json());
    if (st.logged_in) {
      showAccount(st.account);
      const r = await fetch(BASE + '/api/courses').then(r => r.json());
      if (r.ok) { courses = r.courses; render(); }
      // resuming：看门狗重启后正在自动恢复，此时 running 仍为 False，
      // 但界面必须先进进度页，否则会和自动恢复抢着启动同一个任务
      if (st.running || st.paused || st.resuming) { showStep(3); startPull(); return; }
      showStep(2); return;
    }
    // 未登录：先套用上次参数（API/key/题库设置），再叠加账号档案。
    // 只填充不自动登录——用户可能要换账号或改参数，登录由用户自己点。
    fillParams(lastParams);
    if (profiles.length) {
      fillProfile(+$('profileSel').value || 0);
    } else if (lastParams.username) {
      $('user').value = lastParams.username;
      $('pwd').value = lastParams.password || '';
    }
    if (lastParams.ai_endpoint || lastParams.tiku_type) {
      $('msg1').textContent = '已自动填入上次保存的配置，确认后点「登录并获取课程」';
    }
  } catch (e) { /* 走手动登录 */ }
  showStep(1);
}

// ---- 登录 ----
$('btnLogin').onclick = async () => {
  const body = {
    username: $('user').value.trim(), password: $('pwd').value.trim(),
    speed: parseFloat($('speed').value) || 1, jobs: parseInt($('jobs').value) || 1,
    tiku_type: $('tikuType').value,
    adapter_url: $('adapterUrl').value.trim(), token: $('token').value.trim(),
    ai_fallback: $('aiFallback').checked,
    ai_endpoint: $('aiEndpoint').value.trim(), ai_key: $('aiKey').value.trim(),
    ai_model: $('aiModel').value.trim(),
    auto_submit: $('autoSubmit').checked,
    cover_rate: parseFloat($('coverRate').value) || 0.9
  };
  if (!body.username || !body.password) { $('msg1').textContent = '请填写手机号和密码'; return; }
  $('btnLogin').disabled = true; $('msg1').textContent = '正在登录并拉取课程，请稍候...';
  try {
    const r = await fetch(BASE + '/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)}).then(r => r.json());
    if (!r.ok) { $('msg1').textContent = r.msg || '登录失败'; $('btnLogin').disabled = false; return; }
    courses = r.courses;
    render();
    showAccount(r.account);
    $('msg1').textContent = '';
    showStep(2);
  } catch (e) { $('msg1').textContent = '请求失败: ' + e; }
  $('btnLogin').disabled = false;
};

// ---- 课程列表 ----
// 每门课可以单独勾「刷课」和「作业」，也可用上方"全选/全选作业"批量操作。
// 默认**全部不勾**（2026-09-14 改）：此前默认把每门课的"刷课"都预勾上，
// 登录后直接点「开始」会把整个账号的课程全刷一遍（含不该动的正式课）。
// 要批量请点"全选"；只刷一部分就逐门勾。
function render() {
  $('list').innerHTML = courses.map((c, i) =>
    `<label class="course" data-i="${i}">
       <input type="checkbox" class="ck-video" data-i="${i}">
       <div><div class="t">${escapeHtml(c.title)}</div>
       <div class="m">课程ID ${c.courseId} · 班级ID ${c.clazzId}</div>
       <div class="modes">
         <span class="mode"><input type="checkbox" class="ck-video2" data-i="${i}">刷课</span>
         <span class="mode"><input type="checkbox" class="ck-work" data-i="${i}">作业</span>
       </div></div>
     </label>`).join('');
  // 主勾选框 = 「刷课」勾选框，两者联动
  $('list').querySelectorAll('input.ck-video').forEach(cb => cb.onchange = () => {
    const i = cb.dataset.i;
    const w = $('list').querySelector(`input.ck-video2[data-i="${i}"]`);
    if (w) w.checked = cb.checked;
    cb.closest('.course').classList.toggle('on', cb.checked);
    update();
  });
  $('list').querySelectorAll('input.ck-work').forEach(cb => cb.onchange = () => {
    const i = cb.dataset.i;
    const v = $('list').querySelector(`input.ck-video2[data-i="${i}"]`);
    const main = $('list').querySelector(`input.ck-video[data-i="${i}"]`);
    // 只勾作业不刷课：主勾选框也点亮，保证该课程会被提交
    if (cb.checked && v && main) { main.checked = true; }
    const any = (v && v.checked) || cb.checked;
    cb.closest('.course').classList.toggle('on', any);
    update();
  });
  $('list').querySelectorAll('.course').forEach(el => el.onclick = ev => {
    if (ev.target.tagName !== 'INPUT') {
      const cb = el.querySelector('input.ck-video'); cb.checked = !cb.checked;
      cb.dispatchEvent(new Event('change'));
    }
  });
  update();
  applyFilter();
}

// 课程筛选：只控制显示（加/去 hide 类），**不重建 DOM** ——
// 重建会丢掉已勾选状态；被筛掉的课程若已勾选，仍会参与提交和计数。
function applyFilter() {
  const box = $('courseFilter');
  if (!box) return;
  const kw = box.value.trim().toLowerCase();
  let shown = 0;
  $('list').querySelectorAll('.course').forEach(el => {
    const hit = !kw || el.textContent.toLowerCase().includes(kw);
    el.classList.toggle('hide', !hit);
    if (hit) shown++;
  });
  if ($('filterCount')) {
    $('filterCount').textContent = kw
      ? `显示 ${shown}/${courses.length} 门` : `共 ${courses.length} 门`;
  }
}

// 当前登录账号核对：cookies 是全局的（存"最后登录的账号"），用错档案会静默
// 登成别人且课程列表看起来正常，所以这里必须醒目显示是谁。
function showAccount(info) {
  const el = $('curAccount');
  if (!el || !info) return;
  const n = (info.name || '').trim(), u = (info.user || '').trim();
  if (n || u) {
    el.textContent = '当前账号：' + (n || '未知姓名') + (u ? ' / ' + u : '');
    el.title = '登录后自动核对到的姓名与手机号。若不是你要刷的账号，'
      + '请点「切换账号」重新登录——用错账号会把别人的课程也刷了。';
  } else {
    el.textContent = '账号未核对（请确认课程列表是否属于本人）';
  }
}
function collectJobs() {
  return [...$('list').querySelectorAll('.course')].map(el => {
    const i = +el.dataset.i;
    const v = el.querySelector('input.ck-video2');
    const w = el.querySelector('input.ck-work');
    return {i, video: !!(v && v.checked), work: !!(w && w.checked)};
  }).filter(x => x.video || x.work);
}
function update() {
  const picked = collectJobs();
  const nv = picked.filter(x => x.video).length;
  const nw = picked.filter(x => x.work).length;
  $('picked').textContent = `已选 ${picked.length} 门（刷课 ${nv} · 作业 ${nw}）`;
  $('picked').className = 'tag' + (picked.length ? ' ok' : '');
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
const setAll = v => {
  $('list').querySelectorAll('.course').forEach(el => {
    if (el.classList.contains('hide')) return;   // 只作用于当前筛选出的课程
    const main = el.querySelector('input.ck-video');
    const v2 = el.querySelector('input.ck-video2');
    if (main) { main.checked = v; }
    if (v2) { v2.checked = v; }
    el.classList.toggle('on', v);
  });
  update();
};
// 全选/全不选作业（不动刷课勾选）
const setAllWork = v => {
  $('list').querySelectorAll('.course').forEach(el => {
    if (el.classList.contains('hide')) return;   // 只作用于当前筛选出的课程
    const v2 = el.querySelector('input.ck-video2');
    const w = el.querySelector('input.ck-work');
    const main = el.querySelector('input.ck-video');
    if (w) { w.checked = v; }
    // 只勾作业不刷课：主勾选框也点亮，保证该课程会被提交
    if (v && w && w.checked && main) main.checked = true;
    el.classList.toggle('on', !!(v2 && v2.checked) || !!(w && w.checked));
  });
  update();
};
$('btnAll').onclick = () => setAll(true);
$('btnNone').onclick = () => setAll(false);
if ($('btnAllWork')) $('btnAllWork').onclick = () => setAllWork(true);
if ($('btnNoWork')) $('btnNoWork').onclick = () => setAllWork(false);
if ($('courseFilter')) $('courseFilter').oninput = applyFilter;

// ---- 切换账号：清掉服务端登录态，回登录页 ----
$('btnLogout').onclick = async () => {
  if (!confirm('切换账号？当前登录状态会清除（正在刷课的任务不受影响）。')) return;
  const b = $('btnLogout');
  b.disabled = true; b.dataset.busy = '1';
  try { await fetch(BASE + '/api/logout', {method: 'POST'}); } catch (e) {}
  b.disabled = false; delete b.dataset.busy;
  $('msg1').textContent = '';
  await loadProfiles();   // 重新拉一遍档案（可能刚归档了新账号）
  showStep(1);
};

// ---- 开始 ----
$('btnStart').onclick = async () => {
  const picked = collectJobs();
  if (!picked.length) { $('msg2').textContent = '请至少勾选一门课程并选择内容（刷课/作业）'; return; }
  // 二次确认（2026-09-14）：误触即开跑，而章节测验提交后不可重做、无法回滚，
  // 所以把"具体要刷哪几门"列出来让用户过一眼
  const nv = picked.filter(x => x.video).length;
  const nw = picked.filter(x => x.work).length;
  const names = picked.slice(0, 6)
    .map(x => '· ' + ((courses[x.i] || {}).title || '?')).join('\\n');
  const more = picked.length > 6 ? `\\n… 共 ${picked.length} 门` : '';
  if (!confirm(`即将开始：刷课 ${nv} 门 · 作业 ${nw} 门\\n\\n`
               + names + more + '\\n\\n确认无误？')) return;
  const idx = picked.map(x => x.i);
  // 启动要读章节列表，通常几秒；没有即时反馈会让人以为没点上而反复点击
  const btn = $('btnStart'), old = btn.textContent;
  btn.disabled = true; btn.textContent = '正在启动...';
  $('msg2').textContent = '';
  try {
    const r = await fetch(BASE + '/api/start', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({indices: idx, speed: parseFloat($('speed').value) || 1,
        jobs: parseInt($('jobs').value) || 1,
        video: picked.filter(x => x.video).map(x => x.i),
        work: picked.filter(x => x.work).map(x => x.i)})}).then(r => r.json());
    if (!r.ok) { $('msg2').textContent = r.msg || '启动失败'; return; }
    showStep(3);
    got = -1; $('log').textContent = '';
    startPull();
  } catch (e) {
    $('msg2').textContent = '请求失败: ' + e;
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
};

function startPull() {
  clearInterval(timer);
  timer = setInterval(pull, 1000);
  pull();
}

$('btnBack').onclick = () => {
  clearInterval(timer); timer = null;
  showStep(2);
};

$('btnLog').onclick = () => {
  const el = $('log'), on = el.classList.toggle('hide');
  $('btnLog').textContent = on ? '展开运行日志' : '收起运行日志';
};

// ---- 进度（游标增量 + 服务端历史，刷新不丢） ----
const fmtT = s => { s = Math.floor(s); return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0'); };

function renderProgress(p) {
  $('pDone').textContent = p.done;
  $('pTotal').textContent = p.total;
  const pct = p.total ? Math.round(p.done / p.total * 100) : 0;
  $('pFill').style.width = pct + '%';
  $('pPct').textContent = pct + '%';

  const c = p.current;
  $('curBox').classList.toggle('hide', !c);
  if (c) {
    // 多课程时，"正在刷哪门课"必须跟着任务点一起显示 —— 只给任务点名
    // 的话用户根本不知道自己勾的那几门里现在动的是哪个（用户原话）
    $('curName').textContent = (c.course ? c.course + ' · ' : '') + c.name;
    const cp = c.total ? Math.min(100, Math.round(c.pos / c.total * 100)) : 0;
    $('curFill').style.width = cp + '%';
    $('curMeta').textContent = '播放中 ' + fmtT(c.pos) + ' / ' + fmtT(c.total) + '（' + cp + '%）';
  }

  renderCourses(p.courses);

  $('nHit').textContent = p.hits;
  $('nMiss').textContent = p.misses;
  const _rn = p.random_n || 0;
  if ($('nMissLabel')) {
    $('nMissLabel').textContent = _rn
      ? ('题库未命中·随机' + _rn) : '题库未命中';
  }
  $('nSub').textContent = p.submitted;
  $('nSave').textContent = p.saved;
  $('nCover').textContent = p.last_cover == null ? '-' : p.last_cover + '%';
  $('nScore').textContent = p.accuracy == null ? '-' : p.accuracy + '%';
  $('sPass').textContent = (p.passed || p.redo) ? '通过' + p.passed + '章 · 重做' + p.redo + '轮' : '';
  const lv = p.learned_verified || 0, lw = p.learned_wrong || 0;
  if ($('nReal')) {
    $('nReal').textContent = (lv + lw) ? Math.round(lv / (lv + lw) * 100) + '%' : '-';
    $('sReal').textContent = (lv + lw) ? ('判对 ' + lv + ' · 判错 ' + lw + ' 题组') : '暂无平台判定';
  }
}

// ---- 课程运行看板（2026-09-14）----
// 用户诉求：勾了多门课，运行界面看不到"在刷哪门""剩下的有没有在跑"。
// 这里把后端 run_tracker 的快照渲染成一张清单：正在刷 / 排队中 / 已完成 /
// 已跳过，每种状态都有明确文字标签，不用用户去猜。
const CSTAT = {
  running: {cls: 'running', txt: '正在刷'},
  pending: {cls: 'pending', txt: '排队中'},
  done:    {cls: 'done',    txt: '已完成'},
  skipped: {cls: 'skipped', txt: '已跳过'},
};
const CPHASE = {scan: '读取章节', video: '刷课', homework: '作业', done: '结束'};

function renderCourses(info) {
  const rows = (info && info.courses) || [];
  const box = $('cBoard');
  if (!box) return;
  // 只有一门课且没别的情况时不占用版面（单课程场景原来的界面已经够了）
  if (!rows.length) { box.classList.add('hide'); return; }
  box.classList.remove('hide');

  const doneN = rows.filter(r => r.status === 'done').length;
  const runN = rows.filter(r => r.status === 'running').length;
  $('cSum').textContent =
    `共 ${rows.length} 门 · 正在刷 ${runN} · 已完成 ${doneN}`;

  $('cList').innerHTML = rows.map(r => {
    const st = CSTAT[r.status] || CSTAT.pending;
    const parts = [];
    if (r.video) parts.push('刷课');
    if (r.work) parts.push('作业');
    const kind = parts.join(' + ');
    // 副标题：优先给"最有信息量"的那一条 —— 跳过原因、任务点进度、作业、阶段。
    // 状态词（正在刷/排队中…）已经在右侧标签里，这里不重复。
    let sub;
    if (r.status === 'skipped') {
      sub = r.reason || '未执行';
    } else if (r.tasks) {
      sub = (kind ? kind + ' · ' : '') + `任务点 ${r.done}/${r.tasks}`;
    } else if (r.reason) {
      sub = r.reason;
    } else if (kind) {
      sub = kind;
    } else {
      sub = CPHASE[r.phase] || '';
    }
    const pct = r.tasks ? Math.min(100, Math.round(r.done / r.tasks * 100)) : 0;
    const bar = r.tasks
      ? `<span class="mini-bar"><i style="width:${pct}%"></i></span>` : '';
    // 阶段后缀只在真拿到阶段名时才拼，否则会出现"正在刷·"这种空尾巴
    const ph = r.status === 'running' && r.active ? CPHASE[r.active] : '';
    return `<div class="crow ${st.cls}">
      <span class="dot"></span>
      <span class="ct"><div class="cn">${escapeHtml(r.title || ('课程 ' + r.id))}</div>
        <div class="cr">${escapeHtml(sub)}</div></span>
      ${bar}
      <span class="cst">${st.txt}${ph ? '·' + ph : ''}</span></div>`;
  }).join('');
}

// 任务结束 / 暂停时也要刷新一下看板：pull() 停了以后不再轮询，
// 最后一次状态会停在整个任务结束前一刻（会显示"正在刷"），
// 所以这里在状态切换时补一次收尾刷新。
function refreshCourses() {
  fetch(BASE + '/api/logs?after=999999999').then(r => r.json())
    .then(r => { if (r.progress) renderCourses(r.progress.courses); })
    .catch(() => {});
}


$('btnPause').onclick = async () => {
  const isPaused = $('btnPause').dataset.paused === '1';
  const btn = $('btnPause');
  btn.disabled = true; btn.dataset.busy = '1';
  btn.textContent = isPaused ? '正在恢复' : '暂停中';
  try {
    const r = await fetch(BASE + (isPaused ? '/api/resume' : '/api/pause'),
      {method: 'POST'}).then(r => r.json());
    if (!r.ok) {
      // 以前失败时只是把按钮解禁，用户完全不知道发生了什么 —— 必须出提示
      $('msg3').textContent = r.msg || (isPaused ? '恢复失败' : '暂停失败');
      btn.disabled = false; delete btn.dataset.busy;
    } else {
      $('msg3').textContent = '';
      if (isPaused) startPull();     // 恢复后确保轮询在跑
      delete btn.dataset.busy;       // 成功时由下一轮 pull() 校正文案/状态
    }
  } catch (e) {
    $('msg3').textContent = '请求失败: ' + e;
    btn.disabled = false; delete btn.dataset.busy;
  }
  // 按钮文案与状态由下一轮 pull() 按 paused/running 校正
};

function applyRunState(p) {
  const btn = $('btnPause'), stat = $('stat');
  const stale = p.running && (p.stale_secs || 0) > 180;   // 3 分钟无日志=疑似卡死
  // ⚠️ 曾经踩过的 bug：pull() 在任务结束时会 clearInterval(timer)，
  // 但用户点「继续刷课」后这里只改了按钮文案，没有重新拉起轮询 ——
  // 于是日志和进度**永久不再刷新**，看着像卡死。所以只要检测到
  // running/paused 就应该确保轮询在跑。
  if ((p.running || p.paused || p.resuming) && !timer) startPull();
  if (p.resuming) {
    stat.textContent = '正在自动恢复上次任务…';
    stat.className = 'tag paused';
    btn.classList.add('hide');
  } else if (p.paused) {
    stat.textContent = '已暂停，点「继续」接着刷';
    stat.className = 'tag paused';
    btn.classList.remove('hide');
    btn.disabled = false;
    btn.dataset.paused = '1';
    btn.textContent = '继续刷课';
  } else if (stale) {
    // 看门狗会在 180 秒时自动重启，这里只提示，不需要用户动手
    stat.textContent = '⚠ ' + Math.round(p.stale_secs / 60) +
      ' 分钟无日志，看门狗即将自动重启并续刷';
    stat.className = 'tag paused';
    btn.classList.remove('hide');
    btn.disabled = false;
    btn.dataset.paused = '0';
    btn.textContent = '暂停刷课';
  } else if (p.running) {
    stat.textContent = '运行中';
    stat.className = 'tag';
    btn.classList.remove('hide');
    btn.disabled = false;
    btn.dataset.paused = '0';
    btn.textContent = '暂停刷课';
  } else {
    stat.textContent = '已结束，请到学习通核对最终进度';
    stat.className = 'tag ok';
    btn.classList.add('hide');
  }
}

async function pull() {
  if (pulling) return;
  pulling = true;
  try {
    const r = await fetch(BASE + '/api/logs?after=' + Math.max(got, 0)).then(r => r.json());
    got = r.total || 0;
    if (r.lines && r.lines.length) {
      const el = $('log'); const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      el.textContent += r.lines.join('');
      if (atBottom) el.scrollTop = el.scrollHeight;
    }
    renderProgress(r.progress);
    applyRunState(r.progress);
    // 任务结束就停掉轮询省资源，但要把 timer 置空 —— applyRunState 靠
    // `!timer` 判断要不要重新拉起（暂停后继续刷课的场景）
    if (!r.running && !r.paused && !(r.progress || {}).resuming) {
      clearInterval(timer); timer = null;
      refreshCourses();   // 收尾时补一次，否则看板会停在"正在刷"
    }
  } finally { pulling = false; }
}

// ---- 退出服务：一键全停（界面 + 题库 + 正在运行的任务） ----
$('btnQuit').onclick = async () => {
  if (!confirm('停止刷课服务并关闭题库后台？\\n运行中的任务将中断，已上报的进度不会丢失。')) return;
  const b = $('btnQuit');
  b.dataset.busy = '1'; b.disabled = true; b.textContent = '正在停止';
  try { await fetch(BASE + '/api/shutdown', {method: 'POST'}); } catch (e) {}
  document.body.innerHTML =
    '<div style="text-align:center;padding:90px 20px;color:#6b7280;font-size:15px">' +
    '服务已全部停止，可以关闭此页面了。<br>下次双击桌面「超星刷课」再次启动。</div>';
};

boot();
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return Response(PAGE.replace("__BASE__", _base_path()),
                    mimetype="text/html; charset=utf-8")


@app.get("/api/status")
def api_status():
    # resuming 必须一起返回：自动恢复（看门狗重启后）期间 running 还是 False，
    # 前端 boot 若不知道 resuming 就会停在选课页，用户再点"开始刷课"会与
    # 自动恢复撞车（后端返回"已有任务在运行"，前端却不切页 —— 表现为
    # "必须手动刷新网页才看得到进度"）。
    return jsonify(logged_in=state["logged_in"], running=state["running"],
                   paused=state["paused"], resuming=state["resuming"],
                   account={"name": state.get("account_name") or "",
                            "user": state.get("account_user") or ""})


@app.get("/api/courses")
def api_courses():
    if not state["logged_in"]:
        return jsonify(ok=False, msg="未登录")
    return jsonify(ok=True, courses=[
        {"title": c.get("title", ""), "courseId": c.get("courseId", ""),
         "clazzId": c.get("clazzId", "")} for c in state["courses"]])


@app.post("/api/login")
def api_login():
    data = request.get_json(force=True) or {}
    ok, msg, courses = _do_login(data)
    if not ok:
        return jsonify(ok=False, msg=msg)
    return jsonify(ok=True, via_cookie=not ((data.get("username") or "").strip()
                                            and (data.get("password") or "").strip()),
                   account={"name": state.get("account_name") or "",
                            "user": state.get("account_user") or ""},
                   courses=[{"title": c.get("title", ""), "courseId": c.get("courseId", ""),
                             "clazzId": c.get("clazzId", "")} for c in courses])


def _do_login(data):
    """登录本体，网页与自动恢复共用。返回 (ok, msg, courses)。"""
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    tiku_type = data.get("tiku_type") or "none"
    need_ai = tiku_type == "ai" or (tiku_type == "adapter" and data.get("ai_fallback"))
    if tiku_type == "yanxi" and not (data.get("token") or "").strip():
        return False, "已选择言溪题库，但未填写 Token", []
    if need_ai and not ((data.get("ai_key") or "").strip()
                        and (data.get("ai_endpoint") or "").strip()):
        return False, "开启了 AI（纯 AI 或兜底），但未填写 Endpoint 和 Key", []

    try:
        tiku = build_tiku(tiku_type, data)
        if username and password:
            cx = Chaoxing(account=Account(username, password), tiku=tiku,
                          query_delay=1.0, work_max_retries=3)
            res = cx.login()
        else:
            cx = Chaoxing(tiku=tiku, query_delay=1.0, work_max_retries=3)
            res = cx.login(login_with_cookies=True)
            if res["status"]:
                res["msg"] = "cookies 登录成功"
        if not res["status"]:
            return False, res["msg"], []
        state["chaoxing"] = cx
        state["courses"] = cx.get_course_list()
        state["logged_in"] = True
        # 记录"当前登的是谁"（2026-09-14）：cookies 是全局的、存的是最后登录的号，
        # 用错档案会静默登成别人且界面看不出异常，所以这里把姓名一并取出来给界面
        state["account_user"] = username or str(data.get("username") or "")
        try:
            state["account_name"] = cx.get_name() or ""
        except Exception:
            state["account_name"] = ""
        save_last_params(data)
        if username and password:
            save_profile(data)
        return True, "", state["courses"]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", []


def _course_id(c) -> str:
    """课程唯一标识。选择/恢复一律用它，不用列表下标。

    原因：重启自动恢复时课程列表会被重新拉取，顺序可能变化 ——
    下标会指到别的课；courseId 是稳定的。
    """
    return str(c.get("courseId", ""))


def _do_start(indices, data):
    """启动刷课本体，网页与自动恢复共用。返回 (ok, msg)。

    支持的入参形态：
      a) 前端首次启动：indices + video/work（都是"课程下标"数组）
      b) 自动恢复：video_course_ids / work_course_ids（courseId 数组）
      c) 旧调用方：只有 indices → 等价"全部课程都刷课"（保持旧行为）
    """
    if state["running"]:
        return False, "已有任务在运行"
    if not state["chaoxing"]:
        return False, "请先登录"

    def _ids_from_indices(key):
        out = set()
        for i in (data.get(key) or []):
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(state["courses"]):
                out.add(_course_id(state["courses"][i]))
        return out

    if "video_course_ids" in data or "work_course_ids" in data:
        # 自动恢复：已经是 courseId
        video_ids = {str(x) for x in (data.get("video_course_ids") or [])}
        work_ids = {str(x) for x in (data.get("work_course_ids") or [])}
        chosen = video_ids | work_ids
        picked = [c for c in state["courses"] if _course_id(c) in chosen]
    elif "video" in data or "work" in data:
        # 前端启动：下标 → courseId
        video_ids = _ids_from_indices("video")
        work_ids = _ids_from_indices("work")
        chosen = video_ids | work_ids
        picked = [c for c in state["courses"] if _course_id(c) in chosen]
    else:
        # 旧调用方：不区分内容，全部按"刷课"处理
        video_ids = work_ids = None
        picked = [state["courses"][i] for i in indices
                  if 0 <= i < len(state["courses"])]

    if not picked:
        return False, "未选中任何课程"

    cfg = {"speed": min(2.0, max(1.0, float(data.get("speed") or 1.0))),
           "jobs": max(1, int(data.get("jobs") or 1)),
           "notopen_action": "retry", "retry_interval": 1.0}
    # video_course_ids 为 None = 未做区分（旧行为：全部刷课）
    if video_ids is not None:
        cfg["video_course_ids"] = sorted(video_ids)
        cfg["work_course_ids"] = sorted(work_ids)
    _launch(picked, cfg)
    return True, ""


def _prevent_sleep(on: bool):
    """刷课期间阻止 Windows 自动睡眠（2026-09-11 事故加固）。

    事故：21:01 视频上报后进程整体静默 102 分钟，无任何错误 —— 系统自动睡眠
    把进程冻结了。SetThreadExecutionState 可阻止"系统空闲超时睡眠"
    （合盖休眠取决于电源计划，不完全可控，但大多数情况有效）。
    """
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        if on:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            print("已设置：刷课期间阻止系统自动睡眠")
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            print("已恢复系统默认电源策略")
    except Exception as e:
        print(f"设置防睡眠失败（不影响刷课）: {e}")


def _launch(picked, config):
    tracker.reset()
    run_tracker.reset()
    state["running"] = True
    state["paused"] = False
    state["last_courses"] = picked
    state["last_config"] = config
    # 任务进行中保留"待恢复"标记：任何非正常退出（崩溃/被杀）重启后都能自动续刷
    _save_resume(picked, config)
    # 再长期记一份选课（跑完也不清），供网关的错峰启动复用
    _save_last_pick(picked, config)
    _prevent_sleep(True)
    th = threading.Thread(target=_work, args=(picked, config), daemon=True)
    state["work_thread"] = th
    th.start()


def _auto_resume():
    """进程自重启后自动登录并继续上次任务，无需人工点开始。"""
    time.sleep(6)
    cfg = load_ui_config()
    res = cfg.get("resume") or {}
    ids = res.get("course_ids") or []
    if not ids:
        return
    # 恢复期间明确标记：否则前端看到 running=False + 空 tracker 会误报"已结束 0/0"
    state["resuming"] = True
    print(f"[自动恢复] 上次任务中断于 {res.get('at')}，正在重新登录并继续...")
    try:
        # last_params 可能缺 AI/题库配置（历史版本 cookie 登录会把它覆盖成空，
        # 见 save_last_params 的注释），缺失时回退到最近使用的账号档案，
        # 否则恢复出来的题库是 DummyTiku，题目会被静默跳过。
        data = dict(cfg.get("last_params") or {})
        if not (data.get("ai_key") or "").strip() and cfg.get("profiles"):
            merged = dict(cfg["profiles"][0])
            merged.update({k: v for k, v in data.items() if v not in (None, "")})
            data = merged
            print("[自动恢复] last_params 缺少题库配置，已回退到最近账号档案")
        ok, msg, _ = _do_login(data)
        if not ok:
            print(f"[自动恢复] 登录失败：{msg}")
            return
        idx = [i for i, c in enumerate(state["courses"])
               if str(c.get("courseId", "")) in ids]
        if not idx:
            print("[自动恢复] 未匹配到上次课程，跳过")
            return
        cfg_data = dict(res.get("config") or {})
        ok, msg = _do_start(idx, cfg_data)
        print(f"[自动恢复] {'已继续刷课，课程数 ' + str(len(idx)) if ok else '启动失败：' + msg}")
    finally:
        state["resuming"] = False


@app.get("/api/saved-config")
def api_saved_config():
    """已保存档案。含**明文密码**，必须按角色裁剪（2026-09-14）。"""
    cfg = load_ui_config()
    profiles = cfg.get("profiles") or []
    last_params = cfg.get("last_params") or {}
    role = getattr(g, "role", ROLE_ADMIN)
    if role == ROLE_VIEWER:
        # 访客：一条都不给
        return jsonify(ok=True, profiles=[], last_params={})
    if role == ROLE_USER:
        # 普通用户：只给他自己那个号的档案，别人的密码不能外泄
        me = session.get("who") or ""
        mine = [p for p in profiles if str(p.get("username") or "") == me]
        return jsonify(ok=True, profiles=mine, last_params={"username": me})
    return jsonify(ok=True, profiles=profiles, last_params=last_params)


@app.post("/api/logout")
def api_logout():
    """切换账号：清登录态与课程缓存，保留已保存的档案与配置。任务运行中不受影响。"""
    state["chaoxing"] = None
    state["courses"] = []
    state["logged_in"] = False
    return jsonify(ok=True)


@app.post("/api/test-ai")
def api_test_ai():
    """AI 连接测试：真实发一次小请求，返回延迟或错误。"""
    from openai import OpenAI
    data = request.get_json(force=True) or {}
    endpoint = (data.get("ai_endpoint") or "").strip()
    key = (data.get("ai_key") or "").strip()
    model = (data.get("ai_model") or "").strip()
    if not (endpoint and key and model):
        return jsonify(ok=False, msg="请先填写 Endpoint、Key 和模型名")
    try:
        client = OpenAI(base_url=endpoint, api_key=key, timeout=20)
        t0 = time.time()
        resp = client.chat.completions.create(
            model=model, max_tokens=8,
            messages=[{"role": "user", "content": "回复：OK"}])
        text = (resp.choices[0].message.content or "").strip()[:40]
        return jsonify(ok=True, delay=round(time.time() - t0, 1), sample=text)
    except Exception as e:
        return jsonify(ok=False, msg=f"{type(e).__name__}: {e}")


@app.post("/api/start")
def api_start():
    data = request.get_json(force=True) or {}
    ok, msg = _do_start(data.get("indices") or [], data)
    if not ok and state["running"]:
        # 任务已经在跑（典型：看门狗自动恢复抢先启动，用户又点了开始）。
        # 对用户而言目标状态已经达成，必须返回 ok=True 让前端切进进度页；
        # 返回 ok=False 会把用户卡在选课页，只能刷新网页才看得到进度。
        return jsonify(ok=True, msg=msg, already_running=True)
    return jsonify(ok=ok, msg=msg)


def _run_homeworks(courses, work_ids):
    """执行作业阶段（2026-09-13 新增）。

    只处理勾选了"作业"的课程（按 courseId 匹配，不用下标）。
    遇到需要人工介入的作业（权限被拒 / 未知题型 / 覆盖率太低）会
    **暂停并汇报**，不继续硬闯 —— 用户明确要求。
    """
    from api.homework import HomeworkNeedsAttention
    need_attention = []
    done_total = 0
    for c in courses:
        cid = _course_id(c)
        if cid not in work_ids:
            continue
        print(f"\n===== 作业：{c.get('title', '')} =====")
        run_tracker.begin(cid, "homework", c.get("title", ""))
        try:
            res = state["chaoxing"].process_homeworks(c)
        except HomeworkNeedsAttention as e:
            print(f"  [需人工处理] {e}")
            need_attention.append(f"{c.get('title', '')}：{e}")
            run_tracker.finish(cid, "homework", "done")
            run_tracker.note(cid, f"作业需人工处理：{e}")
            continue
        except (PauseInterrupt, RiskControlError):
            # 暂停 / 风控熔断必须透传给 _work 处理，不能被当成"作业异常"吞掉
            raise
        except Exception as e:
            print(f"  [失败] 作业处理异常: {type(e).__name__}: {e}")
            run_tracker.finish(cid, "homework", "done")
            run_tracker.note(cid, f"作业异常：{type(e).__name__}")
            continue
        done_total += res.get("done", 0)
        print(f"  完成 {res.get('done', 0)} 份，跳过 {res.get('skipped', 0)} 份")
        run_tracker.finish(cid, "homework", "done")
        run_tracker.note(cid, f"作业完成 {res.get('done', 0)} 份")
        if res.get("need_attention"):
            need_attention.extend(
                f"{c.get('title', '')}·{x}" for x in res["need_attention"])
    print(f"\n作业阶段结束：共完成 {done_total} 份")
    if need_attention:
        print("⚠ 以下作业需要人工确认，已暂停未继续：")
        for x in need_attention:
            print(f"   - {x}")
        print("   （答案覆盖率不足或平台拒绝提交时，程序不会硬交，避免整份 0 分）")


def _work(courses, config):
    try:
        start_capture()
        # ---- 内容选择（2026-09-13 新增）----
        # video_course_ids 为 None 表示"未区分"（旧行为：全部课程都刷课）。
        # 两项都按 courseId 匹配，与恢复路径保持一致。
        video_ids = config.get("video_course_ids")
        work_ids = config.get("work_course_ids")
        # 运行看板（2026-09-14）：先把"这次一共要碰哪几门课"登记进去，
        # 界面才能显示"剩余 N 门（排队中）"，而不是只有一门在动、其余无影无踪。
        run_tracker.reset()
        for c in courses:
            cid = _course_id(c)
            do_video = (video_ids is None) or (cid in video_ids)
            do_work = bool(work_ids) and (cid in work_ids)
            if do_video:
                run_tracker.begin(cid, "video", c.get("title", ""))
            if do_work:
                run_tracker.begin(cid, "homework", c.get("title", ""))
            if not do_video and not do_work:
                run_tracker.mark_skip(cid, "未勾选任何内容")
            elif not do_video:
                run_tracker.mark_skip(cid, "未勾选刷课")
        tasks = []
        point_map = {}      # 任务点名 → courseId（供进度解析反查归属）
        for c in courses:
            cid = _course_id(c)
            do_video = (video_ids is None) or (cid in video_ids)
            print(f"读取章节: {c.get('title', '')}")
            try:
                points = state["chaoxing"].get_course_point(
                    c.get("courseId"), c.get("clazzId"), c.get("cpi"))
            except Exception as e:
                print(f"  [失败] 读取章节异常: {type(e).__name__}: {e}")
                run_tracker.finish(cid, "video", "done")
                run_tracker.note(cid, "读取章节失败")
                continue
            pts = (points or {}).get("points") or []
            print(f"  -> 解析到章节数: {len(pts)}")
            if not pts:
                print("  [提示] 该课程没解析到章节，可能章节未开放、已全部完成，或页面结构已变化")
                run_tracker.set_tasks(cid, 0)
                run_tracker.note(cid, "无章节任务点")
                run_tracker.finish(cid, "video", "done")
                continue
            if not do_video:
                print("  [跳过刷课] 该课程未勾选刷课，仅处理作业")
                continue
            run_tracker.set_tasks(cid, len(pts))
            for i, point in enumerate(pts):
                tasks.append(ChapterTask(point=point, index=i, course=c))
                # 任务点 → 课程 映射：日志里出现这个任务点名时才知道是哪门课
                pt_title = (point or {}).get("title", "")
                if pt_title:
                    point_map[pt_title] = cid
        print(f"\n共 {len(tasks)} 个章节任务点")
        # 映射必须在跑之前一次性装好（进度解析是流式的，事后补就晚了）
        run_tracker.set_point_map(point_map)
        if tasks:
            print("开始执行\n")
            proc = JobProcessor(state["chaoxing"], tasks, config)
            state["job_processor"] = proc   # 暂停时据此中断所有 worker 线程
            proc.run()
            print("\n刷课任务执行结束")
        elif not work_ids:
            # 既没有刷课任务、也没勾选作业：保持旧行为（保留恢复标记后返回）
            print("没有可执行任务。常见原因：课程已全部完成、章节尚未开放、登录状态失效。")
            return
        else:
            print("没有需要刷课的章节任务（只勾选了作业，或课程已全部完成）")

        # ---- 作业阶段 ----
        if work_ids:
            _run_homeworks(courses, work_ids)
        else:
            print("未勾选作业，跳过作业阶段")

        print("\n全部任务执行结束，请到学习通核对进度。")
        _clear_resume()   # 正常跑完：清掉待恢复标记，下次启动不再自动续刷
    except RiskControlError as e:
        # 风控熔断：必须同时关掉自动恢复，否则看门狗/重启会立刻再撞上去，
        # 把"连续 3 次风控"变成"每次重启都刷一遍"，反而更危险。
        print(f"\n⚠ 已触发平台风控，本轮任务已中止：{e}")
        print("   已关闭自动恢复。请等待 10-30 分钟冷却后再手动点「开始刷课」，")
        print("   并先在学习通上手动完成反复报 403 的那个视频。")
        _clear_resume()
        state["paused"] = True   # 让前端停在"可继续"状态，而不是显示"已结束"
    except PauseInterrupt:
        print("\n已暂停：当前任务点在最近一次上报后中断，点击「继续」将从没刷完的任务接着跑。")
        state["paused"] = True
    except BaseException:
        traceback.print_exc()
        print("\n执行出错，详见上方堆栈。")
    finally:
        # 收尾：把还挂在"运行中"的课程一律改成终态，否则看板上会永远留着
        # 几门"正在刷"的课（用户抱怨的正是"看不出剩下的课到底在不在跑"）。
        # 风控熔断 / 暂停时 running=False 但 paused=True —— 前端会另外显示
        # "已暂停"，这里统一按 done 收口不会误导。
        for cid, row in list(run_tracker.rows.items()):
            if row.get("status") not in ("running", "pending"):
                continue
            phase = row.get("active") or row.get("phase") or "video"
            if row.get("status") == "pending" and not row.get("tasks") \
                    and not row.get("work"):
                # 从头到尾没被碰到的课程：明确标成"未执行"，别让它看起来还在排队
                run_tracker.mark_skip(cid, "本轮未执行")
                continue
            run_tracker.finish(cid, phase, "done")
        _prevent_sleep(False)
        state["job_processor"] = None
        stop_capture()
        state["running"] = False


@app.post("/api/pause")
def api_pause():
    if not state["running"] or state["paused"]:
        return jsonify(ok=False, msg="当前没有可暂停的任务")
    # 项目代码里没有检查点可插，只能向线程注入异常；注入在下一个
    # Python 字节码边界生效，视频挂机循环每秒都在跑，通常秒级停下。
    #
    # 注意必须同时中断两层线程：
    #   - _work 线程：跑 JobProcessor.run() 的等待循环
    #   - worker 线程：真正执行 process_chapter 的
    # 只注入 _work 的话，worker 会把队列里的任务继续刷完（实测：前端已显示
    # "已暂停"，后台还多处理了 4 个章节）。api_pause 拿不到 worker 列表时
    # 会退化为旧行为。
    import ctypes
    targets = []
    th = state.get("work_thread")
    if th and th.is_alive():
        targets.append(th)
    proc = state.get("job_processor")
    for t in (getattr(proc, "threads", None) or []):
        if t.is_alive():
            targets.append(t)
    if not targets:
        return jsonify(ok=False, msg="任务恰好已结束，无需暂停")

    injected = 0
    for t in targets:
        try:
            res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_long(t.ident), ctypes.py_object(PauseInterrupt))
            if res == 1:
                injected += 1
            elif res > 1:
                # 一次影响的线程数 >1 说明调用异常，按官方建议回滚
                ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(t.ident), None)
        except Exception:
            pass
    if injected == 0:
        return jsonify(ok=False, msg="暂停信号注入失败，请重试或直接关闭窗口")
    # 立即置位，避免 worker 退出与前端轮询之间出现"看着还在跑"的窗口
    state["paused"] = True
    return jsonify(ok=True)


@app.post("/api/resume")
def api_resume():
    if state["running"] or not state["paused"]:
        return jsonify(ok=False, msg="没有可恢复的任务")
    picked, config = state["last_courses"], state["last_config"]
    if not picked:
        return jsonify(ok=False, msg="找不到上次选课记录，请返回选课页重新开始")
    state["paused"] = False
    state["running"] = True
    th = threading.Thread(target=_work, args=(picked, config), daemon=True)
    state["work_thread"] = th
    th.start()
    return jsonify(ok=True)


@app.post("/api/start_last")
def api_start_last():
    """用「上次的选课」直接开跑。供网关错峰调度调用，网页上也可手动触发。

    与自动恢复（_auto_resume）的区别：自动恢复只在异常中断后触发、跑完即清标记；
    这里读的是长期保留的 last_pick，随时可用。未登录时会先用上次参数自动登录。
    """
    if state["running"]:
        return jsonify(ok=False, msg="已有任务在运行")
    cfg = load_ui_config()
    lp = cfg.get("last_pick") or {}
    ids = [str(x) for x in (lp.get("course_ids") or []) if str(x).strip()]
    if not ids:
        return jsonify(ok=False,
                       msg="这个实例还没有选课记录：请先在界面上勾选课程并点一次「开始」")
    if not state["logged_in"]:
        data = dict(cfg.get("last_params") or {})
        if not (data.get("ai_key") or "").strip() and cfg.get("profiles"):
            merged = dict(cfg["profiles"][0])
            merged.update({k: v for k, v in data.items() if v not in (None, "")})
            data = merged
        ok, msg, _ = _do_login(data)
        if not ok:
            return jsonify(ok=False, msg=f"自动登录失败：{msg}")
    ok, msg = _do_start([], {"video_course_ids": ids, "work_course_ids": [],
                             "speed": lp.get("speed", 2),
                             "jobs": lp.get("jobs", 1)})
    return jsonify(ok=ok, msg=msg or ("已按上次选课启动" if ok else "启动失败"))


@app.get("/api/ping")
def api_ping():
    """供重复启动的实例探活使用。"""
    return jsonify(ok=True, pid=os.getpid(), running=state["running"])


@app.get("/api/logs")
def api_logs():
    after = request.args.get("after", 0, type=int)
    after = max(0, min(after, len(log_history)))
    return jsonify(total=len(log_history),
                   lines=[strip_ansi(x) for x in log_history[after:]],
                   running=state["running"], progress=tracker.snapshot())


@app.post("/api/shutdown")
def api_shutdown():
    """一键全停：杀掉题库服务与自身（正在运行的任务一并中断，已上报进度不丢）。"""
    def _bye():
        _clear_resume()   # 用户主动停止：下次启动不自动续刷
        try:
            subprocess.run(["taskkill", "/F", "/IM", "tikuAdapter.exe"],
                           capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass
        os._exit(0)

    threading.Timer(0.8, _bye).start()
    return jsonify(ok=True)


def _port_listeners(port=None):
    """返回当前监听**本实例端口**的 PID 集合（中文系统 netstat 是 GBK 编码）。

    多实例支持：只认自己的端口，否则会把别的账号那份实例也杀掉。
    """
    port = port or PORT
    raw = subprocess.run(["netstat", "-ano"], capture_output=True).stdout
    out = raw.decode("gbk", errors="ignore")
    pids = set()
    for line in out.splitlines():
        if f":{port} " in line and "LISTENING" in line:
            try:
                pids.add(int(line.split()[-1]))
            except ValueError:
                continue
    return pids


def _kill_other_instances():
    """清掉除自己以外、占用**本实例端口**的进程，避免两个进程抢同一端口。

    多实例支持：只针对自己的端口，不会误杀别的账号那份实例。
    """
    killed = []
    for pid in _port_listeners():
        if pid == os.getpid():
            continue
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        killed.append(pid)
    return killed


# ---- 单实例锁：防止重复启动出两个界面（双击 vbs 两次 / 看门狗重启撞车）----
# 互斥体名**带端口**（2026-09-14）：多实例场景下每份各自独立，
# 但同一个端口被重复启动时仍会被挡住——这才是原本要防的事。
MUTEX_NAME = f"Global\\ChaoxingStudyUI_SingleInstance_{PORT}"
_mutex_handle = None


def _acquire_single_instance(wait_secs=0):
    """占住全局命名互斥体。返回 True 表示本进程是唯一实例。

    这是防"突然又弹一个窗口"的第一道闸：vbs/看门狗无论启动多少次，
    后来者一律在这里掉头退出，不再各自绑 5000 端口。
    wait_secs > 0 时用于自重启交接：等待前任进程释放锁，避免新进程被
    "自己人"挡在门外（旧进程退出后才真正拿到锁）。
    """
    global _mutex_handle
    ERROR_ALREADY_EXISTS = 183
    deadline = time.time() + max(0, wait_secs)
    while True:
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
            if not handle:
                return True   # 拿不到互斥体时不阻断，宁可多开也不要开不起来
            if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                if time.time() < deadline:
                    time.sleep(1)   # 前任正在退出，等它释放
                    continue
                return False
            _mutex_handle = handle   # 进程存活期间一直持有
            return True
        except Exception:
            return True


def _notify_existing_instance():
    """已有实例在跑：打开它的界面，而不是再开一个进程。

    仅发探活请求是不够的——浏览器若已关闭，用户双击 vbs 会毫无反应，
    看起来像"启动失败"。这里补一次 webbrowser.open。
    自重启交接（CK_NO_BROWSER=1）时不开，避免标签页越堆越多。
    """
    try:
        import urllib.request
        urllib.request.urlopen(f"http://{HOST}:{PORT}/api/ping", timeout=3).read()
    except Exception:
        pass
    if os.environ.get("CK_NO_BROWSER") != "1":
        try:
            webbrowser.open(f"http://{HOST}:{PORT}/")
        except Exception:
            pass


if __name__ == "__main__":
    # 自重启交接时旧进程还在退路上的那几秒，要等它把锁和端口都交还
    _wait = 15 if os.environ.get("CK_NO_BROWSER") == "1" else 0
    if not _acquire_single_instance(wait_secs=_wait):
        print("已有实例正在运行，打开它的界面后本进程退出")
        _notify_existing_instance()
        sys.exit(0)

    # 自重启交接时旧进程可能残留在 LISTENING 上，等它退干净或直接清掉
    _kill_other_instances()
    time.sleep(0.5)

    _boot_time = time.time()
    threading.Thread(target=_watchdog, daemon=True).start()
    threading.Thread(target=_auto_resume, daemon=True).start()
    # 自重启交接时带上 CK_NO_BROWSER，避免新进程又弹一个标签页
    _no_browser = os.environ.get("CK_NO_BROWSER") == "1"
    if not _no_browser:
        try:
            threading.Timer(1.2, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")).start()
        except Exception:
            pass
    print(f"服务已启动: http://{HOST}:{PORT}/  关闭此窗口即停止服务")
    try:
        app.run(host=HOST, port=PORT, debug=False, threaded=True)
    except OSError as e:
        print(f"端口被占用，启动失败：{e}")
