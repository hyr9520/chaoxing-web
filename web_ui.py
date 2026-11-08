"""超星学习通刷课 - 网页界面启动器。

启动本地服务并自动打开浏览器：填账号 -> 勾选课程 -> 看实时日志。
复用项目自带的 Chaoxing / JobProcessor，不修改原有任何文件。

刷新恢复：登录态与运行态保存在服务端，前端刷新后通过 /api/status 回到原页面；
日志在服务端保留最近 800 段，前端用游标增量拉取，刷新后历史不丢。
"""

import collections
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

logger = app_logger.logger      # ⚠️ web_ui 里统一用 logger（原来漏了这行 → NameError）
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
# 2026-09-29 起保留**实测真正有答案**的源：
#   · local   —— 本地库 tiku.db（我们自己回灌的「平台已验证」答案）
#   · buguake —— 不挂科（免费在线源，40 题样本命中 33）
#   · icodef  —— 一之哥哥题库（40 题样本命中 34，加进来后全链 33→35）
# 删掉的 wanneng / tikuhai：40 题样本 **0 命中**，都要付费 Token，等于空转。
#   空转的源不只是没用：8060 是并发查所有源再聚合，无 token 的源每次都要等它
#   返回，白白拖慢整体响应（今日 35 次「请求异常」多与此有关），也抬高超时概率。
# ⚠️ icodef 需要限流 Token（免费，公众号「一之哥哥」发送「token」申请）。
#    Token **不写在这里** —— 见 `_read_icodef_token()`，它从 config.ini 的
#    [tiku] go_authorization 读（该键本就是官方位置，且 config.ini 已进 .gitignore）。
# ⚠️ 改了这里要同步改前端两处字面量：id="adapterUrl" 的 value 与 ADAPTER_URL_DEFAULT。
TIKU_USE = "local,buguake,icodef"
DEFAULT_ADAPTER_URL = ("http://127.0.0.1:8060/adapter-service/search"
                       "?use=" + TIKU_USE)


def _read_icodef_token() -> str:
    """读 icodef（一之哥哥题库）的限流 Token。

    唯一真源是 config.ini 的 `[tiku] go_authorization` —— 这一项在原始项目里
    就是「公众号申请到的解除限流 Token」的官方位置，且 config.ini 已被
    .gitignore 排除，不会把 Token 带进公开仓库。
    """
    try:
        import configparser
        cp = configparser.ConfigParser()
        cp.read(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini"),
                encoding="utf-8")
        return (cp.get("tiku", "go_authorization", fallback="") or "").strip()
    except Exception:
        return ""


def _ensure_use_local(url: str) -> str:
    """归一 TikuAdapter 的 url：确保带 use=，并在用到 icodef 时补上 Token。

    2026-09-14：老配置/老档案里的 adapter_url 普遍没带 use 参数，
    TikuAdapter 只在 strings.Contains(use,"local") 时才查本地库，
    漏了它 → 本地题库形同虚设（听力题全搜不到）。这里做写入侧兜底。
    只对 adapter-service 的 url 生效，其它地址原样返回。

    2026-09-29：新增 icodef Token 兜底 —— url 里出现 icodef 但没带
    `icodefToken` 时，从 config.ini 补上。这样即使界面配置被重置、
    回落到不含 Token 的默认 url，也不会退化成"空转"。
    """
    u = (url or "").strip()
    if not u or "adapter-service/search" not in u:
        return u
    if "use=" not in u:
        u = u + ("&" if "?" in u else "?") + "use=" + TIKU_USE
    if "icodef" in u and "icodefToken=" not in u:
        _tk = _read_icodef_token()
        if _tk:
            u = u + "&icodefToken=" + _tk
    return u
# 界面配置（账号档案 / 上次参数 / 断续刷记录）随实例数据目录走（2026-09-14）：
# 多账号各起一份实例时各自一份，别人的界面读不到你的账号密码。
# config.ini（题库/AI 配置）保持共用，不随实例分家。
UI_CONFIG_PATH = os.path.join(data_dir(), "ui_config.json")

# 登录表单里需要记住的字段（含账密与 AI key：本地单用户工具，用户明确要求记住）
REMEMBER_FIELDS = ("username", "password", "tiku_type", "adapter_url", "token",
                   "ai_fallback", "ai_endpoint", "ai_key", "ai_keys", "ai_model",
                   "auto_submit", "exam_auto_submit", "cover_rate", "speed", "jobs",
                   "exam_dump_dir")

# 🔴🔴 2026-09-27 内置 AI Key 池（用户要求：不用在界面上单独配置，登录即自动带上）
#
# 背景：用户在登录页不想再看到 AI 配置行。但 AI 兜底又是作业高分的刚需，
# 所以改成"内置默认池 + 高级折叠区可覆盖"：
#   · 界面默认不展开，普通流程完全无感；
#   · 登录时若用户没填 `ai_key`/`ai_keys`，自动套用这里的默认池；
#   · 想换池子仍可展开「高级」覆盖（覆盖后以用户填的为准）。
#
# 槽位格式：`Key` 或 `Key|Endpoint|Model`（每行一个）。解析见 api/answer.py `_init_tiku`。
# ⚠️ 这里只放**用户本人的 Key**（4 个 agnes 号 + 3 个腾讯 TokenHub + 1 个硅基流动兜底）。
#    位置：与 api/answer.py 的槽位机制一一对应，改这里即可对全部实例生效。
#
# 🔴 2026-09-27 新增腾讯 TokenHub 层；2026-09-29 按实测重写本段说明
#    · 端点 https://tokenhub.tencentmaas.com/v1（腾讯云大模型服务平台，OpenAI 兼容）
#    · **一 Key 多模型现在是安全的**：`_key_dead` 冷却表早已从「Key 字符串」
#      升级为「槽位签名 key|endpoint|model」（answer.py 的 `_sig`），
#      同一把 Key 配多个模型**不会连带死亡**。2026-09-29 在 acc3 实机验证：
#      弃用 `deepseek-v4.1-flash` 后，同 Key 的 `deepseek-v4-pro-0813` 仍标"可用"。
#      ⚠️ 旧注释曾写"必须一 Key 一模型"，那是升级前缺陷的绕行方案，**已作废**。
#    · **3 把 Key 属于同一个主账号**（`/v1/models` 返回完全相同的 118 个模型）
#      → 额度不会因为多 Key 而翻倍，多 Key 只用于摊开并发 + 单 Key 被吊销时的冗余。
#    · ⚠️ 免费额度**不是"每模型 100 万"那么简单**：`hy4-preview` 在控制台只统计到
#      219K 就报 `free trial quota exhausted`，`deepseek-v4.1-flash` 用到 911K 同样
#      耗尽。**具体额度以控制台「模型广场」为准**，别照文档推断。
#    · 排序意图：腾讯（强模型，先消耗）→ agnes（永久免费兜底）
#      → 硅基流动（最慢最弱，最后兜底）。
DEFAULT_AI_ENDPOINT = "https://apihub.agnes-ai.cn/v1"
DEFAULT_AI_MODEL = "agnes-3.0-flash"
# ⚠️ 必须留空：`_init_tiku` 会把「主 Key」自动插到**首位**，
#    一旦这里填了 agnes Key，它就会排在腾讯前面，腾讯前置失效。
DEFAULT_AI_KEY = ""
TENCENT_ENDPOINT = "https://tokenhub.tencentmaas.com/v1"

# ── 腾讯 TokenHub：3 把 Key ──
# ⚠️ 实测 2026-09-29：三把 Key 调 `/v1/models` 返回**完全相同的 118 个模型**
#    → 属于**同一个主账号**。所以「多 Key」**不会让额度翻倍**
#    （免费额度按「账号 + 模型」计），它的价值只有两条：
#      ① 把并发请求摊到不同 Key 上，降低多实例同时答题撞限流的概率；
#      ② 某把 Key 被吊销时还有冗余。
#
# 🔐 2026-09-29 脱敏：Key 全部移出源码 —— 本仓库是 **public**，硬编码等于泄露。
#    读取优先级：环境变量 `CHAOXING_AI_KEYS` → 同目录 `ai_keys.local.txt`（JSON，已进 .gitignore）。
#    两者都没有时 Key 为空：程序照常启动，AI 池为空，自动走题库兜底。
#    格式模板见仓库根目录 `ai_keys.local.txt.example`。


def _load_local_secrets() -> dict:
    """读取本地密钥（不进版本库）。环境变量优先于文件，解析失败一律返回空。"""
    raw = (os.environ.get("CHAOXING_AI_KEYS") or "").strip()
    if not raw:
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_keys.local.txt")
        try:
            with open(_p, encoding="utf-8") as _fh:
                raw = _fh.read().strip()
        except OSError:
            return {}
    try:
        _d = json.loads(raw)
    except ValueError:
        return {}
    return _d if isinstance(_d, dict) else {}


_SECRETS = _load_local_secrets()
_TX_KEYS = [k for k in (_SECRETS.get("tencent") or []) if k]
_AGNES_KEYS = [k for k in (_SECRETS.get("agnes") or []) if k]
_SF = _SECRETS.get("siliconflow") or {}
_SF_ITEM = ("%s|%s|%s" % (_SF.get("key", ""), _SF.get("endpoint", ""), _SF.get("model", ""))
            if _SF.get("key") else "")

# 🔴 2026-09-29 重排：**只放实测能调的模型**，按「通用问答能力」由强到弱。
#    这 34 个名字全部经 `/v1/models`（平台共 118 个模型 / 38 个语言模型）
#    + 真实 chat 请求**逐个验证可用**。已删除：
#      · `deepseek-v4.1-flash`、`hy4-preview` —— 3 把 Key × 各 2 次实测
#        **全部** HTTP 402 free trial quota exhausted（额度没了，用户确认删）
#      · `deepseek-v4-flash` —— 该模型 ID 不存在（400004）
#    排序依据：**优先"我们实测过正确率的"**，其次公开定位；
#    视觉 / 翻译 / 角色 / 代码这类**专用模型排最后** —— 只有前面全耗尽才会轮到，
#    到那一档时建议把「AI 二次确认（投票）」打开（配 ai_vote = true）。
_TX_MODELS = [
    # —— 旗舰通用（优先消耗）——
    "hy3",                                # ★实测 91.4%（140 题），当前主力，最可信
    "deepseek-v4-pro-202606",             # DeepSeek 原厂直供 Pro
    "kimi-k3",                            # Kimi 最新旗舰
    "minimax-m3",                         # MiniMax 旗舰
    "glm-5.3",                            # 智谱最新
    "deepseek-v4-pro-0813",               # 实测 75.0%（仅 4 题，样本太小）
    # —— 主力通用 ——
    "mimo-v2.6-pro",
    "kimi-k2.8-preview",
    "glm-5.3-flash",
    "deepseek-v4-pro",
    "step-5-preview",
    "glm-5.2",
    "glm-5.3-flashx",
    "kimi-k2.6",
    "mimo-v2.5-pro",
    "mimo-v2.6-flash",
    "minimax-m2.7",
    "deepseek-v4-flash-202605",
    "deepseek-v4-flash-0731",
    "deepseek/deepseek-flash",
    "glm-5.1",
    "glm-5-turbo",
    "glm-5",
    # —— 专用 / 兜底 ——
    "glm-5v-turbo",                       # 视觉
    "hy-vision-2.0-instruct",             # 视觉
    "hunyuan-t1-vision-20250916",         # 视觉
    "deepseek/deepseek-v4-flash-vision-exp",
    "youtu-vita",                         # 多模态理解
    "hy-mt2-pro",                         # 翻译
    "hy-mt2-plus",                        # 翻译
    "hy-role",                            # 角色扮演
    "hunyuan-role-latest",                # 角色扮演
    "kimi-k2.7-code",                     # 代码
    "kimi-k2.7-code-highspeed",           # 代码
]

DEFAULT_AI_KEYS = "\n".join(
    # 轮询分配：相邻模型落在不同 Key 上，并发时能摊开（同账号，额度不翻倍）
    ([f"{_TX_KEYS[i % len(_TX_KEYS)]}|{TENCENT_ENDPOINT}|{m}"
      for i, m in enumerate(_TX_MODELS)] if _TX_KEYS else [])
    # ── 第 2 层：agnes 免费池（永久免费，腾讯额度耗尽后的兜底）──
    + list(_AGNES_KEYS)
    # ── 第 3 层：硅基流动（最后兜底）──
    + ([_SF_ITEM] if _SF_ITEM else []))


def _apply_default_ai(data: dict) -> dict:
    """🔴 2026-09-27：用户未填 AI 配置时，自动套用**内置默认池**。

    只在 `need_ai`（纯 AI 或 adapter+AI 兜底）且用户没提供 Key 时生效，
    用户显式填了就用用户的（保证"高级区可覆盖"）。
    """
    if not isinstance(data, dict):
        return data
    _has_single = bool((data.get("ai_key") or "").strip())
    _has_pool = bool((data.get("ai_keys") or "").strip())
    if _has_single or _has_pool:
        return data                      # 用户填了 → 原样尊重
    data = dict(data)
    data["ai_endpoint"] = (data.get("ai_endpoint") or "").strip() or DEFAULT_AI_ENDPOINT
    data["ai_model"] = (data.get("ai_model") or "").strip() or DEFAULT_AI_MODEL
    data["ai_key"] = DEFAULT_AI_KEY
    data["ai_keys"] = DEFAULT_AI_KEYS
    return data


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
            # 2026-09-26 修复：只有 last_params（没有 profiles/username）的配置
            # 不能整份丢掉。网关给新实例预置的默认参数（含 AI Key）就是这个形态，
            # 丢掉后本函数返回空 last_params，紧接着 _auth_tokens 的 _dump 会把
            # 空值落盘 → 预置的 Key 被永久抹掉。last_params 需原样保留。
            cfg = {"profiles": [], "last_params": dict(cfg.get("last_params") or {})}
    cfg.setdefault("profiles", [])
    cfg.setdefault("last_params", {})
    return cfg


_CFG_LOCK = threading.Lock()      # 配置落盘互斥（见 _dump）


def _dump(cfg):
    """原子写配置。

    🔴 2026-10-03（第三方审计 P1-8）：原实现直接 `open(path, "w")` 截断再写，
    而调用方横跨任务线程 / 请求线程 / watchdog 线程（`_save_last_pick`、
    `_save_resume`、`save_last_params`、`save_profile`、`_persist_live_params`…），
    全项目**没有配置锁** —— 并发写会留下半截 JSON，下次 `load_ui_config`
    解析失败就把 profiles（明文账号）/ last_pick / resume / AI Key 全丢掉。

    做法：**配置锁 + 唯一临时名 + fsync + `os.replace`**。
      · 临时名带 PID / 线程号 → 多线程不会写同一个 tmp（固定名会互踩）；
      · 锁 → 保证"写 tmp → replace"整体不交错；
      · `os.replace` 同盘替换在 Windows 上是原子的 → 读方只会看到完整 JSON。
    失败时**原文件保持不变**（比原来"截断后写一半"安全），并按原语义抛错。
    """
    tmp = "%s.%d.%d.tmp" % (UI_CONFIG_PATH, os.getpid(), threading.get_ident())
    with _CFG_LOCK:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, UI_CONFIG_PATH)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


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
    """落盘"待恢复"标记。

    🔴 2026-09-26：必须连带保存 `work_items`（逐份作业清单）与
    `exam_items`。否则进程被杀后自动续刷时只剩"哪几门课"，
    又退化成"整门课全做" —— 用户在明细里挑的那几份就白挑了。
    """
    cfg = load_ui_config()
    keep = ("speed", "jobs", "video_course_ids", "work_course_ids",
            "quiz_course_ids", "work_items", "exam_items")
    # 🔴 2026-10-03：改为**只过滤 None**（键不存在），不再过滤空列表/空字典。
    #    否则"用户没勾测验"（quiz_course_ids=[]）会被丢掉，落盘后变成
    #    "没有这个键"，与"参数丢了"无法区分，续跑/错峰启动时无法正确回放。
    thin = {k: (config or {}).get(k) for k in keep
            if (config or {}).get(k) is not None}
    cfg["resume"] = {"course_ids": [str(c.get("courseId", "")) for c in courses],
                     "config": thin, "restarts": _resume_count(),
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

    🔴 2026-09-26：同样要存 `work_items`，否则错峰启动会把
    "精确到份"的选择放大成"整门课全做"。
    """
    try:
        cfg = load_ui_config()
        keep = ("speed", "jobs", "video_course_ids", "work_course_ids",
                "quiz_course_ids", "work_items", "exam_items")
        # 🔴 2026-10-03：同 `_save_resume`，改为只过滤 None，保留空列表语义。
        thin = {k: (config or {}).get(k) for k in keep
                if (config or {}).get(k) is not None}
        cfg["last_pick"] = dict(thin)
        cfg["last_pick"].update({
            "course_ids": [str(c.get("courseId", "")) for c in picked],
            # 🔴 2026-10-03：默认值由 2 改 1。全项目策略是 speed=1（防平台回溯），
            #    此前这里缺 speed 时会**往磁盘写 2**，是"配置莫名其妙变 2 倍速"的污染源。
            "speed": (config or {}).get("speed") or 1.0,
            "jobs": (config or {}).get("jobs", 1),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        _dump(cfg)
    except Exception as e:
        print(f"[选课记忆] 保存失败（不影响刷课）: {e}")


def _persist_live_params(config):
    """把「运行中改的参数」落盘（2026-09-29）。

    三处都要写，否则会出现"界面上改了、重启后又变回去"的不一致：
      · last_params   —— 登录页回填
      · last_pick     —— 网关错峰启动 / `/api/start_last`
      · resume.config —— 进程被杀后的自动续刷
    """
    try:
        cfg = load_ui_config()
        keys = ("jobs", "speed")
        lp = cfg.setdefault("last_params", {})
        for k in keys:
            if (config or {}).get(k) is not None:
                lp[k] = config[k]
        for blk in ("last_pick", "resume"):
            b = cfg.get(blk)
            if not isinstance(b, dict):
                continue
            for k in keys:
                if (config or {}).get(k) is not None and k in b:
                    b[k] = config[k]
            inner = b.get("config")
            if isinstance(inner, dict):
                for k in keys:
                    if (config or {}).get(k) is not None and k in inner:
                        inner[k] = config[k]
        _dump(cfg)
    except Exception as e:
        print("[运行参数] 落盘失败（不影响当前任务）: %s" % e)


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
        # 🔴 2026-09-27：**考试任务不参与「作业卡死」判定**。
        #    理由：考试有自己的完成/中断机制（`run_exam` 内逐题暂存、
        #    交卷等待与硬超时），且整场耗时可达数十分钟。用作业那套
        #    "线程死了 + 静默"去判它，会把正常运行判成卡死并重启服务。
        #    （实测：一场 50 题考试答到 171 秒时被判定僵死，服务被重启两次。）
        if (state.get("task_name") or "") == "考试作答":
            continue

        th = state.get("work_thread")
        # 🔴 静默判据取**两者较新**：
        #    · `_last_activity` —— 被 `print` 刷新（`QueueRedirector` 捕获）
        #    · `chaoxing.log` 的 mtime —— 被 loguru 刷新（刷课日志的实际载体）
        #    只用前者会把正常运行误判成僵死（print 太稀疏，详见 `_task_log_mtime`）。
        silent = time.time() - max(_last_activity, _task_log_mtime())

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
    # 🔴 2026-10-03：speed / jobs **不写进档案**。
    #    登录表单里没有这两个控件（它们在"任务参数"区，由 `fillParams(last_params)`
    #    回填），所以 `data.get("speed")` 恒为 None —— 每次登录都会把档案里的
    #    speed/jobs 刷成 None，造成"改了这里却没生效 / 重启又回落"的误导。
    #    后端从未读取 profiles 的 speed/jobs；真正的落盘点是
    #    `last_params` / `last_pick` / `resume.config`（见 `_persist_live_params`）。
    entry.pop("speed", None)
    entry.pop("jobs", None)
    name = entry.get("username") or ""
    others = [p for p in cfg.get("profiles", []) if p.get("username") != name]
    cfg["profiles"] = [entry] + others
    _dump(cfg)

app = Flask(__name__)
app.json.ensure_ascii = False


# 🔴🔴 2026-09-26：全局防缓存（用户报「点了重做没反应」的真因之一）
# 现象：改完代码重启服务后，浏览器仍用**缓存的旧 HTML/JS**，
#      点击毫无反应，且**后端日志里一条请求记录都没有**（← 关键判据）。
# 原因：整个服务此前没有设置任何 Cache-Control，浏览器对 GET / 做了启发式缓存。
# 修法：单页面应用 + 频繁迭代 → 一律 no-store，并禁掉 ETag/Last-Modified 复用。
@app.after_request
def _no_cache(resp):
    # 静态资源很少，全部禁缓存最省事，也避免"改了没生效"的经典困惑
    resp.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0")
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

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
_WRITE_PATHS = ("/api/login", "/api/start", "/api/start_last", "/api/pause",
                "/api/resume", "/api/shutdown", "/api/restart", "/api/quit",
                # 🔴 2026-09-28 补漏：下面这些原先**不在**清单里，
                #    导致"只读身份"也能做作业、开考、发题 —— 全是真动作。
                "/api/homework/run",      # 批量做作业
                "/api/exam/run",          # 开考（消耗唯一一次机会）
                "/api/exam/extra",        # 增删额外课程登记
                "/api/exam/sheet",        # 考试答题
                # 2026-10-12：预检本身只读（不开考），但会向平台发请求 ⇒ 同样挡在只读身份之外
                "/api/exam/precheck",
                "/api/teacher/",          # 教师端：发作业 / 建题 / 发布
                # 🔴 2026-10-03 补漏（第三方审计 P1-9）：
                #    下面这些同样是"真动作/真花钱/真改配置"，此前不在清单里，
                #    只读身份（viewer / 账密登录的用户）可以直接调用。
                "/api/courses/refresh",   # 重新拉课程列表（发平台请求）
                "/api/clear_account",     # 删实例配置（账号/题库/AI Key 全没）
                "/api/test-ai",           # 发 AI 测试请求（烧 Key 额度）
                "/api/tasks/scan",        # 扫描任务点
                "/api/exam/scan",         # 扫描考试
                "/api/homework/scan",     # 扫描作业
                "/api/work/redo",         # 🔴 真提交重做，会改平台成绩
                "/api/work/readback",     # 回读平台已存答案
                "/api/live_params",       # 改并发/倍速（内部 pause→resume 重建任务）
                "/api/append_courses",    # 追加课程并重建任务
                "/api/diag/probe",        # 任意 URL 探测（SSRF 原语）
                "/api/diag/exec",         # 🔴 在实例进程内执行脚本
                )
# 说明：`/api/logout` **故意不列入** —— 登出是用户对自己会话的操作，
#      把只读身份挡在登出之外没有安全收益，只会让人退不出去。


def _is_local_request() -> bool:
    """是否来自本机浏览器（决定是否免登录）。

    ⚠️ 只看 remote_addr 不够（2026-09-14 教训）：若把实例也用内网穿透直连，
    frpc 就在本机转发，公网流量的源地址同样是 127.0.0.1，会被误判成
    "本机"而跳过登录。因此追加 Host 校验，并留一个 CK_STRICT=1 彻底关闭免登录。

    🔴 2026-10-08 补（何同学反馈"账号密码登录后仍显示全套操作按钮"）：
    经**网关**（gateway.py `/acc/<n>/`）转发的请求，以**网关判定的"原始是否本机"**
    为准。原因：网关 `_SKIP_HEADERS` 会剔除 `host`，`requests` 随即补成
    `Host: 127.0.0.1:{端口}` ⇒ 本函数误判为 True ⇒ 实例一律授予 admin
    （远程只读用户因此看到全部操作按钮）。
    """
    if request.headers.get("X-CK-Via-Gateway") == "1":
        return (request.headers.get("X-CK-Local") or "") == "1"
    if os.environ.get("CK_STRICT") == "1":
        return False
    if (request.remote_addr or "").strip() not in ("127.0.0.1", "::1"):
        return False
    host = (request.host or "").split(":")[0].strip().lower()
    return host in ("127.0.0.1", "localhost", "[::1]", "::1")


def _is_cross_site() -> bool:
    """浏览器发起的**跨站**请求（CSRF 特征）。

    🔴 2026-10-03 加固（第三方审计 P0-1）：本机请求原本直接授予 admin，
    而浏览器发出的 `text/plain` POST 属"简单请求"、**不触发 CORS 预检** ——
    于是用户用同一台机器打开任意第三方网页，其中的 JS 就能
    `fetch('http://127.0.0.1:5000/api/start_last',{method:'POST',…})`
    以管理员身份启动/暂停刷课、开考、提交重做、执行脚本。

    判据**只认浏览器特征头，缺头一律放行**：
      · 脚本/工具（`_audit_all.py`、`_restore_instances.py`、curl）不带这两个头；
      · 网关反代已在 `gateway._SKIP_HEADERS` 中把它们剔除（下游不该看到上游浏览器身份）。
    所以"缺头即拒"会误伤整套运维链路与网关访问，这里不采用。
    """
    if (request.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
        return True
    origin = (request.headers.get("Origin") or "").strip()
    if not origin:
        return False
    try:
        from urllib.parse import urlsplit
        origin_host = (urlsplit(origin).hostname or "").lower()
    except ValueError:
        return True                       # Origin 畸形 -> 按跨站处理
    return origin_host not in ("127.0.0.1", "localhost", "::1")


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
    # 🔴 2026-10-03：CSRF 闸门必须在最前面 —— 本机请求免登录，
    #    若不先挡掉"浏览器跨站请求"，第三方网页的 JS 就等价于管理员。
    if _is_cross_site():
        return jsonify(ok=False, msg="拒绝跨站请求（CSRF 防护）"), 403
    if request.path in ("/login", "/auth", "/favicon.ico"):
        return None
    # 🔴 2026-10-08：经网关（gateway.py `/acc/<n>/`）转发的请求 —— 用**网关已认证的角色**。
    #    实例读不到网关的 session（两个进程 secret_key 不同），必须由网关转告。
    #    不做这一步的话，网关剔除 Host 后 requests 补成 127.0.0.1 ⇒
    #    `_is_local_request()` 判 True ⇒ 远程只读用户被当成管理员（界面显示全套按钮）。
    #    ⚠️ 未识别的角色值（如微伴的 "wb"）一律降为只读 —— 保守且安全。
    if request.headers.get("X-CK-Via-Gateway") == "1":
        _r = (request.headers.get("X-CK-Role") or "").strip()
        g.role = _r if _r in (ROLE_ADMIN, ROLE_USER, ROLE_VIEWER) else ROLE_VIEWER
        g.who = "经网关"
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
    """🔴 2026-09-28 改（用户要求）：**非管理员一律禁止写操作**。

    用户原话："通过账号密码单独登录进去的，就只能看到自己的进度，不能操作，
    然后进去就是进度的界面，其他的更多权限都是只能由管理员来"。
    所以原来是"只拦 viewer"，现在 ROLE_USER 也一并拦掉 ——
    普通用户身份只能看进度/日志。本机访问仍是 admin，不受影响。
    """
    role = getattr(g, "role", None)
    # 🔴 2026-10-03：加 `method != "GET"` 限定。
    #    原因：`/api/live_params` 同时有 GET（读实时参数）和 POST（改参数），
    #    只按路径前缀拦会把只读用户"看参数"也禁掉。所有写操作都是 POST/DELETE，
    #    所以"非 GET + 命中写清单"才是准确的判据。
    if role and role != ROLE_ADMIN and request.method != "GET" and \
            any(request.path.startswith(p) for p in _WRITE_PATHS):
        return jsonify(ok=False,
                       msg="当前身份只有查看权限，操作请用管理员口令登录"), 403
    return None


@app.get("/api/whoami")
def api_whoami():
    role = getattr(g, "role", ROLE_ADMIN)
    return jsonify(role=role, who=getattr(g, "who", ""),
                   local=_is_local_request(),
                   # 🔴 2026-09-28：只有管理员能改 —— 普通用户（账密登录）也降为只读
                   can_edit=role == ROLE_ADMIN)


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
  :root{
    --brand:#2563eb;--brand-2:#3b82f6;--line:#d8dee4;--ink:#0f172a;--ink-3:#64748b;
    --ease-out-quart:cubic-bezier(.165,.84,.44,1);
    --ease-out-expo:cubic-bezier(.19,1,.22,1);
    --ease-spring:cubic-bezier(.34,1.56,.64,1);
  }
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       color:var(--ink);overflow-x:hidden;
       font:14px/1.6 "Microsoft YaHei UI","PingFang SC",sans-serif;
       background:
         radial-gradient(1100px 700px at 12% -6%,  rgba(191,219,254,.50), transparent 58%),
         radial-gradient(1000px 760px at 96% 8%,  rgba(199,210,254,.44), transparent 58%),
         radial-gradient(900px 620px at 82% 104%,  rgba(186,230,253,.42), transparent 60%),
         linear-gradient(168deg,#f4f8fe 0%,#e8f0fb 48%,#e3ecf9 100%);
       background-attachment:fixed}
  /* ① 极光：14s 一圈、±26% 位移 + 旋转，肉眼明确可见在"游" */
  body::before{
    content:"";position:fixed;inset:-40%;z-index:-2;pointer-events:none;
    background:
      radial-gradient(62% 52% at 24% 26%, rgba(34,211,238,.55) 0%, rgba(34,211,238,.55) 44%, rgba(34,211,238,0.18) 64%, transparent 72%),
      radial-gradient(58% 48% at 76% 60%, rgba(45,212,191,.47) 0%, rgba(45,212,191,.47) 44%, rgba(45,212,191,0.16) 64%, transparent 72%),
      radial-gradient(54% 46% at 46% 90%, rgba(56,189,248,.42) 0%, rgba(56,189,248,.42) 44%, rgba(56,189,248,0.14) 64%, transparent 73%),
      radial-gradient(46% 40% at 88% 22%, rgba(103,232,249,.38) 0%, rgba(103,232,249,.38) 44%, rgba(103,232,249,0.13) 64%, transparent 74%);
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
  /* ② 光带扫过：斜向高光带 9s 横穿全屏 */
  .sweep{position:fixed;top:-60%;left:-60%;width:220%;height:220%;
    z-index:-2;pointer-events:none;
    background:linear-gradient(102deg,transparent 28%,rgba(255,255,255,.10) 47%,
      rgba(191,219,254,.08) 50%,rgba(255,255,255,.10) 53%,transparent 72%);
    animation:sweepMove 9s cubic-bezier(.45,0,.55,1) infinite;
    will-change:transform}
  @keyframes sweepMove{
    0%  {transform:translate3d(-38%,0,0) rotate(-8deg);opacity:0}
    8%  {opacity:.85}
    50% {opacity:.85}
    58% {opacity:0}
    100%{transform:translate3d(38%,0,0)  rotate(-8deg);opacity:0}
  }
  /* ③ 网格：6s 走一格（56px），约 9px/秒 */
  .gridflow{position:fixed;inset:-10%;z-index:-3;pointer-events:none;opacity:.30;
    background-image:
      linear-gradient(to right, rgba(37,99,235,.055) 1px, transparent 1px),
      linear-gradient(to bottom, rgba(37,99,235,.055) 1px, transparent 1px);
    background-size:72px 72px;animation:gridDrift 6s linear infinite;will-change:transform}
  @keyframes gridDrift{
    from{transform:translate3d(0,0,0)}
    to  {transform:translate3d(72px,72px,0)}
  }
  /* ④ 呼吸光晕：8s 起伏 */
  .breathe{position:fixed;inset:0;z-index:-2;pointer-events:none;
    background:radial-gradient(60% 50% at 50% 45%,rgba(96,165,250,.34),transparent 70%);
    animation:breathe 8s ease-in-out infinite;will-change:transform,opacity}
  @keyframes breathe{
    0%  {transform:scale(.92);opacity:.42}
    50% {transform:scale(1.16);opacity:.92}
    100%{transform:scale(.92);opacity:.42}
  }
  /* ⑤ 漂浮光点 ×5 */
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
    0%,100%{transform:translate3d(0,0,0) scale(1)}
    33%{transform:translate3d(90px,-70px,0) scale(1.25)}
    66%{transform:translate3d(-60px,50px,0) scale(.85)}
  }
  @keyframes orbB{
    0%,100%{transform:translate3d(0,0,0) scale(1)}
    40%{transform:translate3d(-120px,80px,0) scale(1.3)}
    70%{transform:translate3d(60px,120px,0) scale(.9)}
  }
  @keyframes orbC{
    0%,100%{transform:translate3d(0,0,0) scale(.9)}
    35%{transform:translate3d(-80px,-110px,0) scale(1.35)}
    75%{transform:translate3d(110px,-40px,0) scale(1)}
  }
  @keyframes orbD{
    0%,100%{transform:translate3d(0,0,0) scale(1.05)}
    45%{transform:translate3d(130px,-90px,0) scale(.8)}
    80%{transform:translate3d(-40px,-140px,0) scale(1.2)}
  }
  /* orbE：最短循环 + 纯位移，和其他几个的"缩放型"形成节奏差 */
  @keyframes orbE{
    0%{transform:translate3d(-30px,20px,0) scale(.9)}
    50%{transform:translate3d(-110px,-60px,0) scale(1.3)}
    100%{transform:translate3d(-30px,20px,0) scale(.9)}
  }
  /* ⑥ 顶部流动渐变条 */
  .flowbar{position:fixed;top:0;left:0;right:0;height:3px;z-index:50;
    pointer-events:none;
    background:linear-gradient(90deg,#38bdf8,#22d3ee,#5eead4,#7dd3fc,#38bdf8,#22d3ee,#7dd3fc,#38bdf8);
    background-size:300% 100%;animation:flowBar 5s linear infinite;
    box-shadow:0 0 10px rgba(59,130,246,.55)}
  @keyframes flowBar{
    from{background-position:0% 0}
    to  {background-position:300% 0}
  }
  .cursorGlow{position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:0;
    background:radial-gradient(360px 360px at var(--mx) var(--my),rgba(59,130,246,.20),transparent 70%);
    transition:opacity .5s var(--ease-out-quart);will-change:background}
  body.pointerFine .cursorGlow{opacity:1}
  body::after{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.42;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)' opacity='.035'/%3E%3C/svg%3E")}
  .box{position:relative;width:min(92vw,380px);
       background:linear-gradient(180deg,rgba(255,255,255,.40) 0%,rgba(252,253,255,.30) 100%);
       border:1px solid rgba(255,255,255,.68);border-radius:14px;
       padding:26px 24px;
       box-shadow:0 10px 30px -10px rgba(15,23,42,.16),
                  inset 0 1px 0 rgba(255,255,255,.60);
       backdrop-filter:blur(12px) saturate(160%);
       -webkit-backdrop-filter:blur(12px) saturate(160%);
       animation:boxIn .46s var(--ease-out-expo) both}
  @keyframes boxIn{from{opacity:0;transform:translate3d(0,10px,0)}to{opacity:1;transform:none}}
  h1{margin:0 0 6px;font-size:19px;letter-spacing:.2px}
  p.sub{margin:0 0 18px;color:#6b7280;font-size:12.5px}
  label{display:block;margin:12px 0 6px;font-size:13px;color:#374151}
  input{width:100%;padding:10px 12px;border:1px solid #d8dee4;border-radius:9px;font-size:14px;
        background:#fcfdff;
        transition:border-color .18s var(--ease-out-quart),box-shadow .18s var(--ease-out-quart),
                   transform .18s var(--ease-out-quart)}
  input:focus{outline:0;border-color:var(--brand-2);transform:translateY(-1px);
        box-shadow:0 0 0 4px rgba(37,99,235,.14)}
  button{position:relative;overflow:hidden;width:100%;margin-top:18px;padding:11px;
         border:0;border-radius:9px;color:#fff;font-size:15px;cursor:pointer;
         background:linear-gradient(180deg,var(--brand-2) 0%,var(--brand) 100%);
         box-shadow:0 1px 2px rgba(37,99,235,.26),0 6px 14px -6px rgba(37,99,235,.42);
         transition:transform .18s var(--ease-out-quart),box-shadow .18s var(--ease-out-quart),
                    filter .18s var(--ease-out-quart)}
  button::after{content:"";position:absolute;top:0;left:-120%;width:60%;height:100%;
         background:linear-gradient(100deg,transparent,rgba(255,255,255,.42),transparent);
         transition:left .6s var(--ease-out-quart)}
  button:hover:not(:disabled){transform:translateY(-2px);filter:brightness(1.05);
         box-shadow:0 3px 6px rgba(37,99,235,.24),0 10px 22px -8px rgba(37,99,235,.5)}
  button:hover:not(:disabled)::after{left:130%}
  button:active:not(:disabled){transition:transform .06s linear,box-shadow .06s linear,filter .06s linear;
         transform:translateY(0) scale(.985)}
  button:disabled{opacity:.6;cursor:default}
  .msg{margin-top:12px;font-size:13px;color:#dc2626;min-height:18px}
  .tip{margin-top:16px;padding-top:14px;border-top:1px dashed #e5e7eb;
       font-size:12px;color:#9ca3af;line-height:1.7}
  .ripple{position:absolute;border-radius:50%;pointer-events:none;
    background:radial-gradient(circle,rgba(255,255,255,0.50) 0%,rgba(255,255,255,0.43) 40%,transparent 70%);
    transform:translate(-50%,-50%) scale(0);animation:rip .56s var(--ease-out-quart) forwards}
  @keyframes rip{to{transform:translate(-50%,-50%) scale(1);opacity:0}}
  @media (prefers-reduced-motion: reduce){
    body::before,.gridflow,.breathe,.orbs i,.sweep,.flowbar,.box{animation:none}
    .sweep,.orbs{opacity:.35}
    .flowbar{opacity:.5}
    .cursorGlow{display:none}
    button,.box,input{transition:none}
    button .ripple{display:none}
    button:hover:not(:disabled),button:active:not(:disabled){transform:none}
    label::after{animation:none}
  }

  /* ===== 输入框「浅特效」（2026-09-24）=====
     用户要求「浅一点」——克制，不要霓虹感：
       ① 常驻极淡内阴影，让输入框"有厚度"而不是贴纸
       ② hover 只加一点蓝晕，不位移（避免鼠标划过就乱跳）
       ③ focus 上浮 1px + 三层由近及远的光环 + 底边高光扫过
     ④ 底边高光用 label 伪元素实现 —— ⚠️ input 是替换元素，
        ::before/::after 对它**完全不生效**，这是踩过的坑。 */
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
  /* 聚焦时 label 底边划过一道渐变高光 */
  label{position:relative;display:inline-block}
  label:has(+ input:focus)::after{
    content:"";position:absolute;left:0;right:0;bottom:-6px;height:1.5px;
    background:linear-gradient(90deg,transparent,var(--brand-2),var(--brand),transparent);
    border-radius:2px;animation:ulSweep .5s var(--ease-out-expo) both}
  @keyframes ulSweep{
    0%  {transform:scaleX(0);opacity:0;transform-origin:left}
    60% {opacity:1}
    100%{transform:scaleX(1);opacity:.85;transform-origin:left}
  }
  input::placeholder{color:#a8b3c2;transition:opacity .2s var(--ease-out-quart)}
  input:focus::placeholder{opacity:.5}

  /* ===== 手机端专项（2026-09-24）=====
     三个必须遵守的移动端事实：
       ① iOS Safari：输入框 font-size < 16px 时聚焦会**自动放大整页** → 必须 ≥16px
       ② 触控目标：iOS HIG ≥44px、Material ≥48dp → 输入框 48px、按钮 50px
       ③ 触屏没有 hover：桌面 hover 在手机上会产生「点完还亮着」的粘滞态
          → 改用 :active 做即时反馈 */
  @media (max-width: 640px){
    /* ⚠️ 第一版只做"等比放大"→ 用户反馈"放大排在一起太拥挤，没有灵活感"。
       正解是拉开层级差：标题更大 / 说明更清楚 / 字段间距更大 / 卡片更宽松。 */
    html{-webkit-text-size-adjust:100%}
    input{min-height:50px;padding:14px 16px;font-size:16px;border-radius:14px;
          box-shadow:inset 0 1px 2px rgba(15,32,74,.05),0 1px 2px rgba(15,32,74,.04)}
    input:hover{border-color:#d8dee4;box-shadow:inset 0 1px 2px rgba(15,32,74,.05),
                0 1px 2px rgba(15,32,74,.04)}
    input:focus{transform:none;border-color:var(--brand);
          box-shadow:inset 0 1px 2px rgba(15,32,74,.03),
                     0 0 0 4px rgba(37,99,235,.15),
                     0 4px 14px -4px rgba(37,99,235,.26)}
    /* 字段节奏：label 与输入框之间、字段之间都要透气 */
    label{display:block;font-size:14px;font-weight:500;color:#3d4657;
          margin:20px 0 9px;letter-spacing:.2px}
    p.sub + label{margin-top:16px}
    button{min-height:52px;font-size:16.5px;border-radius:14px;
           letter-spacing:.3px;font-weight:500;margin-top:28px}
    button:hover:not(:disabled){transform:none;filter:none;
          box-shadow:0 1px 2px rgba(37,99,235,.26),0 6px 14px -6px rgba(37,99,235,.42)}
    button:active:not(:disabled){transform:scale(.972);filter:brightness(.95);
          transition:transform .08s linear,filter .08s linear}
    .box{width:min(92vw,400px);padding:34px 26px 30px;border-radius:24px;
         box-shadow:0 1px 2px rgba(15,32,74,.04),
                    0 18px 44px -18px rgba(37,99,235,.28),
                    0 40px 80px -40px rgba(15,23,42,.22)}
    /* 头部各块之间给足气口，贴太紧会显得"挤成一坨" */
    h1{font-size:24px;font-weight:650;letter-spacing:.3px;margin:0 0 20px}
    p.sub{font-size:13.5px;line-height:1.7;color:#66707f;margin:0 0 4px}
    /* ⚠️ 背景动效全保留，只降级"贵"的部分：filter:blur 减半，层数一层不动。
       第一版直接 display:none 掉 sweep/orbs/flowbar → 用户立刻反馈"完全没有动效"。
       ⚠️ 必须用 `body .orbs i:nth-child(n)`（基础定义写在这个特异性上）。 */
    body .orbs i:nth-child(n){width:170px;height:170px}
    body .orbs i:nth-child(2){width:210px;height:210px}
    .sweep{opacity:.85}
    .breathe{opacity:.7}
    .gridflow{opacity:.24}
    .cursorGlow{display:none}
    ::-webkit-scrollbar{width:0;height:0}
  
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

  /* ② 光带：9s → 5.5s，位移 ±38% → ±52%（明显"刷"过去） */
  body .sweep{
    animation-duration:5.5s;
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
    .box{padding:28px 20px 26px;border-radius:22px}
    h1{font-size:22px}
    label{margin:17px 0 8px}
    input{font-size:16px;padding:13px 14px;min-height:48px}
    button{font-size:15.5px;min-height:50px;margin-top:24px}
  
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

</style></head><body>
<div class="gridflow" aria-hidden="true"></div>
<div class="breathe" aria-hidden="true"></div>
<div class="orbs" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div>
<div class="sweep" aria-hidden="true"></div>
<div class="cursorGlow" aria-hidden="true"></div>
<div class="flowbar" aria-hidden="true"></div>
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
/* 光标光晕 + 点击波纹：与其余三个界面同一套语言 */
(function(){
  const fine = window.matchMedia('(hover:hover) and (pointer:fine)').matches;
  if (!fine) return;
  document.body.classList.add('pointerFine');
  let raf = 0, px = 0, py = 0;
  window.addEventListener('pointermove', e => {
    px = e.clientX; py = e.clientY;
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      const s = document.documentElement.style;
      s.setProperty('--mx', px + 'px');
      s.setProperty('--my', py + 'px');
    });
  }, {passive:true});
})();
(function(){
  document.addEventListener('pointerdown', e => {
    const b = e.target && e.target.closest && e.target.closest('button');
    if (!b || b.disabled) return;
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
    });
  }, {passive:true});
})();
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
         "account_name": "", "account_user": "",
         # 风控中止原因（2026-09-24）：非空时前端在运行看板顶部出红色横幅。
         # 挂在 state 上而不是 run_tracker 上 —— 它描述的是"这一轮整体被中止了"，
         # 不属于任何单门课（单门课的中止原因在 row["reason"] 里）。
         "risk_abort": "",
         # 统一任务扫描（2026-09-26）：登录后自动扫「作业+考试」，后台线程跑，
         # 前端轮询 /api/tasks/scan/status。放 state 里是为了刷新页面不丢进度。
         "task_scan": {"running": False, "done": False, "msg": "",
                       "i": 0, "n": 0, "result": None, "err": "",
                       "started": 0.0},
         # 考试作答结果（2026-09-26）：最近一次考试任务的结果，供前端展示。
         "exam_result": {"done": 0, "skipped": 0, "results": []},
         "task_scan_lock": threading.Lock()}
log_history: list[str] = []
_old_stdout = _old_stderr = _old_tqdm_stream = None
_last_activity = time.time()   # 最近一条日志的时间，用于检测任务静默卡死

# 🔴 2026-10-01 二次修（上一版判错了对象，造成 8 个实例被误杀）：
#    「哪些线程的日志算『任务还活着』」不能用**白名单**（只认 work_thread）——
#    实际刷课日志是 **TaskHandler 的 worker 线程**（线程名形如 `Thread-6 (wrapper)`）
#    写的，而 `work_thread`（web_ui 的 `_work` 主线程）自己几乎不打日志。
#    ⇒ 白名单写法让 `_last_activity` **永不刷新**，看门狗在 `HARD_STALE_LIMIT=900`
#      秒后把**完全正常**的任务判成僵死，注入 `PauseInterrupt` 杀掉 `_work`
#      （state 变 paused=True），而**信号没打到 worker** → worker 成僵尸继续刷。
#      实测：2026-10-01 20:15 重启的 8 个实例，20:30 前后全部中招。
#    改用**黑名单**：只排除已知的常驻非任务线程，其余（含 worker）都算任务活动。
#    被排除的三类必须排斥，否则又会掩盖真卡死：
#      · `platform-progress`  —— 每 30 秒的心跳，就是它让旧实现永不触发
#      · `process_request_thread` —— Flask 的请求线程；前端轮询会持续写访问日志
#      · 保活/写盘类（keep-awake / battery-guard / loguru-writer / tqdm_monitor / _watchdog）
# ⚠️ 必须用**子串包含**判断，不能用 `startswith` —— Python 线程名是
#    `Thread-3 (_watchdog)`、`Thread-1386 (process_request_thread)` 这种
#    `Thread-N (名字)` 格式，`startswith("_watchdog")` 匹配不到括号里的名字。
_NON_TASK_THREADS = (
    "platform-progress", "process_request_thread", "keep-awake",
    "battery-guard", "loguru-writer", "tqdm_monitor", "_watchdog",
    "MainThread",
)


def _task_log_mtime() -> float:
    """任务日志 `chaoxing.log` 的最近写入时刻（epoch 秒）。

    🔴🔴 看门狗判「任务是否还活着」**必须**用这个，不能用 `_last_activity`。

    原因（2026-10-01 实测两次才定位）：
      `QueueRedirector`（stdout 重定向）只捕获 **`print()`** 输出，而刷课过程中
      几乎全是 loguru 的 `logger.xxx` 调用 —— loguru **直接写文件**，不经过 stdout。
      ⇒ `_last_activity` 只在极稀疏的 print 时刷新；任务跑起来之后它就**停在
        启动那一刻**，而 `HARD_STALE_LIMIT=900` → 看门狗 15 分钟后把完全正常的
        任务判成"僵死"→ 注入 PauseInterrupt 杀掉 `_work`（state 变 paused=True），
        而信号没打到 worker → **worker 变成僵尸继续刷，实例彻底失控**。
      实测：2026-10-01 20:15 重启的 8 个实例，20:30 前后全部中招，
      `resume` 标记还被看门狗的"超重启上限"分支一并清掉。

    `chaoxing.log` 的 mtime 直接反映"日志还在不在写" ——
    既不依赖线程名，也不依赖 print，且任务真卡死时它同样会停住（判据可靠）。
    """
    try:
        from api.config import data_dir
        return os.path.getmtime(os.path.join(data_dir(), "chaoxing.log"))
    except Exception:                                              # noqa: BLE001
        return 0.0


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
RE_HIT = re.compile(r"获取答案(?:（[^）]*）)?：")
#   🔴 2026-10-08 修：原为 `获取答案：` —— 但**学习库命中**的日志是
#      `从学习库获取答案（已验证）：…`（"获取答案"后是括号而非冒号），
#      于是"学习库命中"这一最优质来源**从未被计入** hits，而它的
#      "未命中"行（先走题库失败）却已被计入 misses ⇒ 两个后果：
#        ① 界面「题库命中」少算、获取率偏低
#        ② 与 miss 的题面指纹对不上，**回撤逻辑失效**（残留一批假未命中）
#      新写法兼容 `获取答案：` 与 `获取答案（…）：`，且仍**不会**误匹配
#      `获取答案失败：`（"获取答案"后面是"失败"）—— RE_MISS 另在前拦一道。
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
# 每次登录都会打 `当前登录用户: 何光杨`（api/base.py:login）。
# 2026-10-08 起用于「回放日志时按账号切分」—— 换账号后不能把上个账号的
# 统计算进界面（何同学明确要求）。
RE_LOGIN_USER = re.compile(r"当前登录用户[:：]\s*(\S+)")


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
        # 🔴 2026-10-08：按**题面**去重的集合（命中 / 未命中各一份）。
        #    见 _parse 里 miss / hit 分支的注释（界面卡片写的是"题数"）。
        self._hit_keys: set = set()
        self._miss_keys: set = set()
        self._replaying = False
        # 清零后立刻回放「当前账号」已有日志，让界面数字 = 「累计」而非「本次进程」
        # 见 _replay_logs 的说明（何同学：已提交/命中不该只有个位数）。
        self._replay_logs()

    def _qkey(self, text: str) -> str:
        """从「获取答案…」之后的内容里取出**题面**，作为跨来源的唯一键。

        命中行是 `…：{题面} -> {答案}`、失败行是 `…失败：{题面}`，
        必须剥掉尾部的 ` -> 答案` 才能让同一题在两个分支对上（否则
        "先失败后回退命中"的回撤判定永远不成立）。

        🔴 2026-10-09 修（实测残留 79 条假未命中）：**必须先剥掉终端控制码**。
          实时日志走 stdout 的终端 sink，**带颜色码**（文件 sink 不带）——
            miss 行：`…失败：{题面}\\x1b[0m`  ⇒ key 变成 `{题面}\\x1b[0m`
            hit  行：`…：{题面} -> {答案}\\x1b[0m` ⇒ rsplit 后 ANSI 被切掉，key 干净
          ⇒ 两个 key 永远不相等 ⇒ **回撤 100% 失效**，界面「答案获取率」被压低
          （acc9 显示 79 个未命中，实际 0 个）。文件回放路径没有这个问题，
          所以"复算=0 而实例=79"——差异只在实时 feed 这条路。
        """
        t = strip_ansi(text)
        t = t.rsplit(" -> ", 1)[0] if " -> " in t else t
        return t.strip()[:120]

    def _replay_logs(self):
        """回放本实例日志中**属于当前账号**的部分（让界面数字变成「累计」）。

        为什么需要（2026-10-08 何同学）：
          卡片文案本身是累计语义（"已提交""题库命中"），但 reset() 每次
          `/api/start`、resume 都清零 ⇒ 重启后只剩个位数，与同排的
          「平台进度 62/107」「平台判对」（两者都是累计）并列时自相矛盾。

        为什么按账号切分（何同学明确要求「换账号后不要读以前的账号」）：
          实例目录是 per 端口、不 per 账号，日志里混着历史账号的记录。
          日志每行不带账号，但每次登录都会打 `当前登录用户: X` —— 据此
          知道每段日志归谁；只回放 `X == 当前账号` 的段落。
          ⚠️ 认不出当前账号（或日志里没有登录行）时**直接放弃回放**：
             宁可只统计本次运行，也绝不把别的账号的数字算进来。
        """
        me = (state.get("account_name") or "").strip()
        if not me:
            return
        import glob as _glob
        base = (os.environ.get("CK_DATA_DIR") or "").strip()
        pattern = (os.path.join(base, "chaoxing*.log") if base else
                   os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "chaoxing*.log"))
        paths = sorted(_glob.glob(pattern))
        if not paths:
            return
        self._replaying = True
        try:
            for p in paths:
                owner = None
                try:
                    f = open(p, encoding="utf-8", errors="replace")
                except OSError:
                    continue
                with f:
                    for line in f:
                        # 字符串包含比正则快得多（先粗筛，再精确取名字）
                        if "当前登录用户" in line:
                            mm = RE_LOGIN_USER.search(line)
                            if mm:
                                owner = mm.group(1)
                            continue
                        if owner != me:      # 不是当前账号（或还没登录）⇒ 跳过
                            continue
                        s = line.strip()
                        if s:
                            self._parse(s)
        except Exception as e:                                 # noqa: BLE001
            # 回放只是"让数字更好看"，绝不能因为一次解析失败影响刷课
            print(f"日志回放失败（本轮统计从零开始，不影响刷课）: {e}")
        finally:
            self._replaying = False

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
        if self._replaying:
            # 回放历史日志时**不能**写课程看板：此刻 run_tracker 还挂着上一轮的
            # rows（_launch 的顺序是 tracker.reset() → run_tracker.reset()），
            # 会把历史任务点记到本轮某门课头上，看板数字虚高。
            return
        cid = run_tracker.cid_of_point(point_name)
        if cid and cid in run_tracker.rows:
            run_tracker.rows[cid]["done"] = run_tracker.rows[cid].get("done", 0) + 1

    def _parse(self, line):
        if m := RE_TOTAL.search(line):
            self.total = int(m.group(1))
        elif m := RE_START.search(line):
            # 任务点归属哪门课：多课程时用户必须能一眼看出"现在在刷哪门"。
            # 按任务点名反查（而不是记"最后 begin 的课"—— 并发下必错）
            # 回放历史日志时不设 current：历史任务不是"当前正在刷"。
            if not self._replaying:
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
        elif (mm := RE_MISS.search(line)):
            # 必须放在 RE_HIT 之前判断：失败行也含“获取答案”字样。
            # 🔴 2026-10-08 修（何同学：「界面数据要符合它自己写的设定」）：
            #    卡片写的是「题库里没收录、改由 AI 补答的**题数**」，
            #    而原实现每见一行失败就 +1 —— 一道题常被多家题库分别判失败
            #    （TikuAdapter 失败 → 全能搜题 失败 → 回退命中），于是同一题
            #    被记成 2 次未命中 ⇒ 「答案获取率」被系统性压低
            #    （实测 acc11 显示 74.3%，而真实是 **100%** 拿到答案）。
            #    现按**题面**去重；若该题随后被别家题库答出，下面 hit 分支会撤回。
            _mk = self._qkey(line[mm.end():])
            if _mk and _mk not in self._hit_keys and _mk not in self._miss_keys:
                self._miss_keys.add(_mk)
                self.misses += 1
                self.q_total += 1
            self._last_qkey = ""
        elif m := RE_HIT.search(line):
            # 同一题组会打多行（题库命中 + 回退命中 + 多题库汇总），
            # 用题面指纹去重，避免一次答题被算成三次
            key = self._qkey(line[m.end():])
            if key and key not in self._hit_keys:
                self._hit_keys.add(key)
                # 该题先被判「未命中」、随后又被别家题库答出 ⇒ 撤回那条未命中。
                # 不撤回的话，成功的回退也会被算成失败（分母虚高、获取率偏低）。
                if key in self._miss_keys:
                    self._miss_keys.discard(key)
                    self.misses -= 1
                    self.q_total -= 1
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
            # 2026-09-15 修 bug：原实现直接读源码目录下的 learned_answers.json，
            # 完全无视 CK_DATA_DIR，导致多实例下 5 个实例的界面**都显示同一组
            # 假数字**（实测 acc1/acc2 均报 verified=140/wrong=20，而两者真实
            # 学习库分别是 69/2 和 66/8）。必须与 api/learned.py 用同一套
            # data_dir() 解析，否则界面上的"判对/判错"与账号实际战绩无关。
            from api.config import data_dir as _data_dir
            _p = os.path.join(_data_dir(), "learned_answers.json")
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
                # 与看门狗同口径：取 print 活动与 chaoxing.log 写入中较新的一个
                "stale_secs": int(time.time() - max(_last_activity,
                                                    _task_log_mtime())),
                "current": self.current, "running": state["running"],
                # 风控中止横幅（2026-09-24）
                "risk_abort": state.get("risk_abort", ""),
                "courses": run_tracker.snapshot(),
                # 平台侧真实进度（2026-09-29）：界面主数字以它为准
                "platform": dict(_PLAT),
                # 预计完成时间（2026-09-29）：每次 snapshot 都实时重算 ——
                # 前端每次轮询都拿到最新值，满足"半分钟更新一次、要实时"。
                "eta": _eta_of_platform()}


tracker = ProgressTracker()


# ---- 平台侧真实进度（2026-09-29 新增）----
# 背景：ProgressTracker.done 数的是"平台回过 isPassed=true 的任务名"，从不校验
# 平台是否**真的结算**。实测 acc2 界面 141 而平台只有 22（虚高 6.4 倍），用户被
# 假数字误导了一整晚。这里定时拉 get_course_point 的 `has_finished`（章节级完成
# 标记），给界面一个可信的分子/分母。
#
# 只在后台线程里拉，snapshot() 只读缓存 —— 否则每次 /api/status 轮询都会发
# 网络请求，把界面拖死。写入用锁保护，读侧拿 dict() 拷贝。
_PLAT = {"at": 0.0, "done": 0, "total": 0, "courses": [], "err": ""}
_PLAT_LOCK = threading.Lock()
# 平台进度的历史采样（2026-09-29，供 ETA 外推）：deque 里存 (时间戳, 已完成数)。
# 30 秒一条、maxlen 300 → 覆盖 2.5 小时，够算滑动速率又几乎不占内存。
_PLAT_HIST = collections.deque(maxlen=300)
PLAT_REFRESH_SECS = 30   # 平台侧数据刷新间隔。用户要求"半分钟更新一次"（2026-09-29）；
                         # 3 门课 = 6 请求/分钟仍很轻，ETA 也能更快跟上进度变化。


def _refresh_platform_progress():
    """拉平台侧每门课的章节完成情况。异常一律吞掉 —— 它只是展示用的旁路。"""
    cx = state.get("chaoxing")
    courses = state.get("last_courses") or []
    if not courses:
        # 2026-09-29：**跑完的实例重启后平台进度会显示 0/0**（实例五 王宇阳实测）。
        # 原因：正常跑完会 `_clear_resume()` 清掉 resume 标记，重启后内存里的
        # `last_courses` 无从恢复 → 采集线程没课程可拉 → 界面看着像"进度归零"。
        # 用**长期保留**的 `last_pick.course_ids`（跑完也不清）在已登录课程里
        # 反查回来，**只用于展示平台进度**，不触发任何续跑（续跑只看 resume）。
        try:
            _ids = set((load_ui_config().get("last_pick") or {})
                       .get("course_ids") or [])
            if _ids:
                courses = [c for c in (state.get("courses") or [])
                           if str(c.get("courseId") or "") in _ids]
        except Exception:
            courses = []
    if not cx or not courses:
        return
    rows, d_n, t_n = [], 0, 0
    for c in courses:
        cid = str(c.get("courseId") or "")
        title = c.get("title") or cid
        try:
            pts = ((cx.get_course_point(cid, c.get("clazzId"),
                                        c.get("cpi") or "0") or {}).get("points") or [])
            flat = []
            stack = list(pts)
            while stack:                      # 章节可能嵌套子节点，迭代展开避免递归
                it = stack.pop()
                flat.append(it)
                stack.extend(it.get("points") or [])
            fin = sum(1 for it in flat if it.get("has_finished"))
            # id 必须带上：课程看板的行是按 courseId 组织（run_tracker.rows），
            # 前端要靠它把平台数字对上卡片，光靠 title 匹配会被重名/截断搞错。
            rows.append({"id": cid, "title": title, "done": fin, "total": len(flat)})
            d_n += fin
            t_n += len(flat)
        except Exception as e:
            rows.append({"title": title, "done": 0, "total": 0, "err": type(e).__name__})
    with _PLAT_LOCK:
        _PLAT.update({"at": time.time(), "done": d_n, "total": t_n,
                      "courses": rows, "err": ""})
        # 采样进历史，供 ETA 算滑动速率。**只接受单调不减的点**：某一门课采集
        # 失败时 d_n 会偏小，混进去就成了"平台进度倒退"，速率会被算崩。
        _t = time.time()
        if not _PLAT_HIST or d_n >= _PLAT_HIST[-1][1]:
            _PLAT_HIST.append((_t, d_n))


def _platform_watch():
    while True:
        try:
            _refresh_platform_progress()
        except Exception:
            pass
        time.sleep(PLAT_REFRESH_SECS)


# ---- 预计完成时间 ETA（2026-09-29）----
# 用户要求："加一个预计完成时间，准确度高一点，误差 5 分钟以内"。
# ⚠️ 实测结论：**做不到恒定 ±5 分钟**。用 1408 次历史回溯验证（脚本 _stat_eta2.py）：
#     单任务点耗时 中位 8.5 分 / 标准差 6.9 分 / P90 20.7 分 —— 光平台判定就
#     从几十秒到 19 分钟不等，单次任务就抖 ±10 分钟；多个任务叠加误差按 √N 放大。
#   分桶中位误差：剩余 1~2 个 = 2.0 分 | 3~5 = 4.3 | 6~10 = 6.0 |
#                 11~20 = 12.0 | 21~40 = 15.6 | 41+ = 39.9
#   → 所以**必须给区间**（± 值），只报一个"精确时刻"就是骗人。
#
# 口径：只用**平台维度**（剩余章节数 ÷ 平台结算速率）。
#   为什么不混用程序维度：`rows["tasks"]` 是**章节数**、`rows["done"]` 是**任务点数**，
#   跨维度相减会得出 114% 那种荒唐结果（2026-09-29 踩过，别再来一次）。
_ETA_WINDOW = 3600          # 速率统计窗口：最近 60 分钟
_ETA_MIN_WIN = 300          # 至少要有 5 分钟窗口才算速率 —— 只有一两个采样点时
                            # 算出来的速率噪声极大（实测 90 秒窗口就出结果，不可信）


def _eta_of_platform():
    """算预计完成时间。拿不到可靠数据时返回 ok=False，由前端显示对应提示。"""
    now = time.time()
    with _PLAT_LOCK:
        total = int(_PLAT.get("total") or 0)
        done = int(_PLAT.get("done") or 0)
        hist = [x for x in _PLAT_HIST if x[0] >= now - _ETA_WINDOW]
    if total <= 0:
        return {"ok": False, "why": "no_data", "at": now}
    remain = max(0, total - done)
    # 已结束 / 已暂停时不做外推（速率会归零或失真），但把"还差几章没结算"带出去
    if not state.get("running"):
        return {"ok": False, "why": "done", "remain": remain, "at": now}
    if state.get("paused"):
        return {"ok": False, "why": "paused", "remain": remain, "at": now}
    if remain == 0:
        return {"ok": True, "remain": 0, "secs": 0, "half": 0.0, "rate": 0.0,
                "finish": now, "at": now, "win_min": 0, "win_done": 0}
    # 速率 = 窗口内的**最小二乘斜率**，而不是首尾差：平台进度是"阶梯式跳变"的，
    # 首尾两点恰好都踩在跳变沿上就会把速率算歪（实测同日 acc2/acc7 差 3 倍）。
    # 回归对端点噪声鲁棒得多 —— ETA 的精度上限就取决于这一项。
    if len(hist) >= 3 and hist[-1][0] > hist[0][0]:
        span = float(hist[-1][0] - hist[0][0])
        gain = hist[-1][1] - hist[0][1]
        _n = len(hist)
        _xs = [x[0] - hist[0][0] for x in hist]
        _ys = [float(x[1]) for x in hist]
        _mx = sum(_xs) / _n
        _my = sum(_ys) / _n
        _den = sum((x - _mx) ** 2 for x in _xs)
        rate = (sum((x - _mx) * (y - _my) for x, y in zip(_xs, _ys)) / _den
                if _den > 0 else 0.0)                       # 章节 / 秒
        if span >= _ETA_MIN_WIN and rate > 0:
            secs = remain / rate
            # 半宽：剩余完成时间近似 Gamma 过程时 σ ≈ T/√remain。
            # 与实测分桶吻合（剩余 11~20 个算出 ±27 分，实测 P75 误差 28 分），
            # 取 2σ ≈ 95% 区间 —— 宁可给宽一点，也别给一个假的精确值。
            half = 2.0 * secs / max(1.0, remain ** 0.5)
            return {"ok": True, "remain": remain, "secs": secs, "half": half,
                    "rate": rate * 60.0, "finish": now + secs, "at": now,
                    "win_done": gain, "win_min": span / 60.0}
        if span >= _ETA_MIN_WIN:
            # 窗口够长但平台一章都没新结算 —— 它在"只判定不结算"，此时任何外推
            # 都不可信。如实说"暂无法推算"比编一个时间好。
            return {"ok": False, "why": "stalled", "remain": remain, "at": now,
                    "win_min": span / 60.0}
    return {"ok": False, "why": "warming", "remain": remain, "at": now}


# 线程句柄存成模块全局：热补丁脚本（_hot_patch_sync.py）靠它判断"是否已启动"，
# 不存的话补丁会再起一个采集线程，变成双份轮询（无害但没必要，2026-09-29 踩到）。
_PLAT_WATCH = threading.Thread(target=_platform_watch, name="platform-progress",
                               daemon=True)
_PLAT_WATCH.start()


# ---- 启动自愈：新起的实例自动装载运行时守护（2026-09-29）----
# 教训：防睡眠 / 低电量守护一直是"事后用 /api/diag/exec 注入"的，结果**新开的
# 账号实例（5006 朱天正）没人给它注入** —— 关屏后就有被 Modern Standby 冻结
# 的风险，而且从界面上完全看不出来。用户原话："以后新开的实例也要保证和前面
# 的一样都修好了。"
# 改成进程启动时自己装载：以后不管谁新开实例、不管用哪种方式启动，行为都一致。
# 两个脚本自身都是幂等的（_KEEP_AWAKE / _BATT_GUARD_V3 守卫），重复装载安全。
def _auto_load_guards():
    import runpy
    _here = os.path.dirname(os.path.abspath(__file__))
    for _fn in ("_keep_awake.py", "_battery_guard_v3.py"):
        _p = os.path.join(_here, _fn)
        if not os.path.exists(_p):
            continue
        try:
            # 两个脚本内部靠 sys.modules["__main__"] 找实例模块 —— 实例进程
            # 就是以本文件为 __main__ 启动的，所以它们会把线程句柄落在本模块上，
            # 与本文件里其它全局状态一致。run_name 只影响被装载脚本自身的 __name__。
            runpy.run_path(_p, run_name="__wb_guard__")
        except Exception:
            pass


threading.Thread(target=_auto_load_guards, name="auto-guards", daemon=True).start()


class CourseRunTracker:
    """多课程运行看板（2026-09-14）。

    用户诉求原话："勾了多个课程，但运行界面看不到在刷哪个课程、剩下的课程有没有在刷。"

    原来的界面只有一行「当前任务点」（curBox），而任务点是跨课程混在
    一个队列里并发跑的（4 个 worker），所以那一行既看不出是**哪门课**，
    也看不出**其它课在不在跑**。这里给出一份按课程分组的清单：

        [{title, id, video, work, tasks, done, phase, status}, ...]

    phase:  scan(读章节) / video(刷课) / homework(作业) / done(完成)
    status: running(正在刷) / pending(排队中) / done(已完成)
            / skipped(已跳过) / aborted(中止·未刷完)

    ⚠ done 与 aborted 的语义差别（2026-09-24 用户报障后新增）：
      done    = 这门课的任务点**真的跑到了收尾**，可以认为刷完了
      aborted = 因为风控熔断 / 403 / 异常而**被迫中断**，进度大概率没推上去
    以前这两种情况一律记成 done，结果界面上 4 门课全是绿色"已完成"，
    但总进度是 0/321、一门都没刷 —— 用户原话："那这个就不要绿色的完成了，
    给它改一下……和正常完成的做区别一下。"

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

    def abort(self, cid, reason, kind="risk"):
        """中止（未刷完）：风控熔断 / 403 拦截 / 读取异常等被迫中断。

        与 mark_skip 的区别：skip 是"本来就没打算刷"（用户没勾），
        abort 是"打算刷、但被平台挡下来了" —— 这种课必须在界面上
        明确标出来，既不能是绿色的"已完成"，也不能混进灰色的"已跳过"，
        否则用户会以为课刷完了（2026-09-24 实测踩坑）。
        """
        with self._lock:
            row = self.rows.get(str(cid))
            if row is None:
                row = {"id": str(cid), "title": "", "video": False, "work": False,
                       "tasks": 0, "done": 0, "phase": "done",
                       "status": "pending", "active": ""}
                self.rows[str(cid)] = row
                self.order.append(str(cid))
            self._active = {a for a in self._active if a[0] != str(cid)}
            row["status"] = "aborted"
            row["abort_kind"] = kind        # risk(风控) / error(异常) / manual(需人工)
            row["reason"] = reason
            row["active"] = ""
            row["phase"] = "done"

    def abort_all_running(self, reason, kind="risk"):
        """把当前所有 running/pending 的课程一律标成中止。

        风控熔断时用它：熔断发生后不可能还有课在跑，但 _active 里可能
        残留着没来得及 finish 的 unit；不主动兜底的话，看板上会永远
        挂着"正在刷"转圈（用户抱怨过"看不出到底在不在跑"）。
        """
        with self._lock:
            targets = [cid for cid, r in self.rows.items()
                       if r.get("status") in ("running", "pending")]
        n = 0
        for cid in targets:
            self.abort(cid, reason, kind)
            n += 1
        return n

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
            # 「正在刷」的排前面，其次排队中，最后非运行态
            # aborted 排在最前（比 running 还前）—— 风控中止是用户最需要
            # 立刻看到的信息，不能埋在绿色"已完成"里（2026-09-24 用户报障）
            rank = {"aborted": 0, "running": 1, "pending": 2, "done": 3, "skipped": 4}
            rows.sort(key=lambda r: rank.get(r.get("status"), 9))
            return {"courses": rows,
                    "done_n": sum(1 for r in rows if r.get("status") == "done"),
                    "abort_n": sum(1 for r in rows if r.get("status") == "aborted"),
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
        # 🔴 2026-10-01 **二次修**：这里原写成「只认 state["work_thread"]」，
        #    **判错了对象** —— 详见 `_NON_TASK_THREADS` 上方的长注释。
        #    一句话：刷课日志是 TaskHandler 的 worker 线程写的，不是 work_thread；
        #    白名单会让 `_last_activity` 永不刷新，看门狗 900 秒后把正常运行判成
        #    僵死并杀掉任务（8 个实例实测中招）。改用黑名单排除常驻非任务线程。
        if not any(k in threading.current_thread().name
                   for k in _NON_TASK_THREADS):
            _last_activity = time.time()
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
        # 🔴 2026-09-29 两个外部免费源的**位置是实测定的，别随手调**：
        #
        # `TikuJszkk`（全能搜题）放 **AI 之前**：
        #   40 道实测题干命中 67.5%、答案对得上选项 50%、与本地库一致率 90%，
        #   且 2 道冲突**都是它对**（本样本 20/20 全对）→ 质量不低于 AI（91%），
        #   放前面能把它接住的题直接消化掉，**省下对应比例的 AI token**。
        #
        # `TikuHive`（OnlineCourseAPI）放 **最后**：
        #   覆盖面仅 17.5%、与本地库一致率仅 57%（答案质量不如 AI），
        #   放 AI 前面会拉低正确率；放最后则零风险 —— 只在 AI 也答不出
        #   （或 Key 池耗尽）时接管，把"随机蒙"变成"有据可查"。
        provider = "TikuAdapter,TikuJszkk,AI,TikuHive"
    elif tiku_type == "adapter":
        provider = "TikuAdapter,TikuJszkk,TikuHive"
    conf["provider"] = provider  # 留空 → DummyTiku，跳过答题任务

    if "TikuAdapter" in provider:
        conf["url"] = _ensure_use_local(
            (data.get("adapter_url") or "").strip() or DEFAULT_ADAPTER_URL)
    if "TikuYanxi" in provider:
        conf["tokens"] = (data.get("token") or "").strip()
    if "AI" in provider:
        conf["endpoint"] = (data.get("ai_endpoint") or "").strip()
        conf["key"] = (data.get("ai_key") or "").strip()
        # 🔴 2026-09-26 23:25 多 Key 池：`ai_keys` 每行一个槽位，支持
        #    `sk-xxx`（沿用主 Endpoint/模型）或
        #    `sk-xxx|https://…/v1|model`（换 Key 时连 Endpoint/模型一起换）。
        #    解析见 api/answer.py `_init_tiku`（原样透传，不在这里拆）。
        conf["keys"] = (data.get("ai_keys") or "").strip()
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
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>超星刷课</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; padding:32px; background:#f4f6f8; color:#1f2328;
         font:14px/1.6 "Microsoft YaHei UI","PingFang SC",sans-serif; }
  .wrap { max-width:880px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:#6b7280; font-size:13px; margin-bottom:20px; }
  /* 卡片整体半融入背景（2026-09-24）：与 .box 主定义同一套参数 */
  /* ⚠️ overflow-x:hidden 是**兜底**：任何子元素顶出卡片右边界时，
     只裁掉它自己，而不是把整个页面撑出横向滚动条。
     （真凶一直是 .bar 缺 flex-wrap，已在 .bar 处修好） */
  .card { background:linear-gradient(180deg,rgba(255,255,255,.40) 0%,rgba(252,253,255,.30) 100%);
          border:1px solid rgba(255,255,255,.52); border-radius:10px;
          backdrop-filter:blur(12px) saturate(160%);
          -webkit-backdrop-filter:blur(12px) saturate(160%);
          box-shadow:0 2px 12px rgba(0,0,0,.03),inset 0 1px 0 rgba(255,255,255,.60);
          overflow-x:hidden; box-sizing:border-box;
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
  /* 🔴 2026-09-24 修：「往右滑才能看全」的根因就在这一行。
     .bar 里塞了 8 个按钮，没有 flex-wrap 时 flex 会把每个子项**压到最小宽度**
     （实测：`勾选要做的课程与内容` 被挤成竖排单字、6 个 ghost 按钮各压到 42px
     宽的竖条、`开始` 直接顶到屏幕外 x=492）→ 整页横向滚动。

     ⚠️ 这里必须写 `nowrap`（而不是靠默认值）：
     桌面 878px 卡片里 6 个批量按钮 + 标题 + `开始` 本来排得下，
       但如果换成 `wrap`，某个字号/字重下就会偷偷折到第二行 —— 那就是桌面回归。
     手机端由下面的媒体查询显式改成 `wrap`。 */
  /* 🔴 2026-09-27：改回 flex-wrap:wrap。
     原因：新增「考试清单」后按钮变 7 个，nowrap 下总宽再次超出 ——
     用户实测「开始」被挤出可视区（下面这套"像素级塞进 878px"的方案，
     每加一个按钮就得重调一遍，不可持续）。换行后按钮再多也不会被裁掉。 */
  .bar { display:flex; flex-wrap:wrap; align-items:center; gap:10px;
         margin-bottom:14px; }
  .bar .sp { flex:1; }
  /* 🔴 2026-09-24 修：`.bar` 改成 nowrap 后暴露的真 bug（两连修）——
     ① 「全不选」被压到 69px，3 个汉字放不下 → **内部折行** → 按钮高 79px，
        比同类按钮的 60px 高出 19px，整行看着错位。
        → 修：文字 `white-space:nowrap`。
     ② 但如果同时把按钮设成 `flex:none`（完全不压缩），6 个按钮 + 标题 + 开始
        在桌面 878px 卡片里**总宽 984px** → 又顶出卡片（新溢出）。
        → 修：允许压缩但**压缩到不折行就停**（`flex:0 1 auto` + `min-width:auto`），
          并把桌面 padding 从 `10px 20px` 收到 `10px 12px`、gap 收到 8px，
          实测总宽 878 → 一个像素都不越界。

     ⚠️ 必须用**后代选择器** `.bar button`，不能用 `.bar > button`：
        `#bulkBar` 是 `display:contents`，它的 6 个按钮**不是** `.bar` 的直接
        子元素（DOM 上是 `.bar > .bulkbar > button`），`>` 一个都匹配不到。 */
  .bar { gap:8px; }
  /* 🔴 收缩优先级（2026-09-24 定稿，桌面空间不够时的让位顺序）：
       ① 按钮 `flex-shrink:0` —— **绝不收缩**，文字必须完整
          （踩过：`flex:0 1 auto` 时浏览器把按钮从 80px 压到 60px，
            「全选测验」被裁成「全选测㡺」—— 点错按钮代价最大，不能牺牲）
       ② 「已选 N」`flex-shrink:1` + `min-width:0` —— **唯一可牺牲项**，
          超长打省略号（它只是补充说明，截断不影响操作）
       ③ `.sp` 退化为 0 宽
     效果：某天卡片变窄，只会看到「已选 0 门（视频 0 · 测…」被省略，
           而不会看到「全选测验」变成「全选测㡺」。 */
  .bar button { flex:0 0 auto; white-space:nowrap;
                padding-left:12px; padding-right:12px;
                min-width:max-content; }
  /* 🔴 2026-09-24 桌面空间预算（878px 卡片，明细实测）：
       标题 140 + 「已选 N」216 + 「开始」52 + gap 6×8=48  →  固定占 456
       6 个批量按钮只剩 878 − 456 = 422px
       但它们在 `padding:10px 20px; font-size:14px` 下共需 518px → **超 96px**。
       修法：只把批量按钮收小一档（用户不会读那 6 个字号的细微差别）：
         font-size 14 → 12.5px、padding 20 → 11px
       6 个按钮降到 ~404px，正好放得下，且文字一个不裁。
       ⚠️ 只动 `.ghost`（批量按钮），不动「开始」（主操作必须保持存在感）。 */
  .bar > .tag { white-space:nowrap; }
  .bar .ghost { font-size:12.5px; padding-left:11px; padding-right:11px; }
  /* 「已选 N 门（视频 … · 测验 … · 作业 …）」是唯一可牺牲项：
     `min-width:0` 是关键 —— 没有它，flex 项的收缩下限是内容宽，压根缩不动。 */
  .bar > .tag { flex:0 1 auto; min-width:0; overflow:hidden;
                text-overflow:ellipsis; }
  /* `.sp` 不再无限抢位：先让按钮拿到它们需要的宽度，剩下的才归 `.sp`。 */
  .bar .sp { flex:1 1 auto; min-width:0; }
  /* 🔴 标题与主操作「开始」一律 `flex:none` —— 它们不该被压缩：
     ① 标题被压窄 → 「勾选要做的课程与内容」在桌面折成两行（实测回归）
     ② 「开始」被压窄 → 变成 40px 的窄条，主操作失去视觉重量（实测回归）
     要腾空间时，宁可让 6 个批量按钮挤一点，也不动这两个。 */
  .bar > strong { flex:none; white-space:nowrap; }
  .bar > .magnetic { flex:none; }
  /* 🔴 2026-09-26 新增：主界面「提交方式」条（#submitBar）。
     原先提交开关只在**登录页**，登录后就找不到入口 —— 用户明确反映
     "我让你加我自己选择提不提交这一项，为什么没有？"
     这里补齐常驻开关：自动提交 + 覆盖率门槛，随手可改。 */
  .submitbar { display:flex; flex-wrap:wrap; align-items:center; gap:10px;
               margin:-4px 0 12px; padding:9px 12px; border-radius:9px;
               background:rgba(37,99,235,.05);
               border:1px solid rgba(37,99,235,.16);
               font-size:13px; }
  .submitbar .sw { display:inline-flex; align-items:center; gap:6px;
                   cursor:pointer; font-weight:600; color:#1f2937; margin:0; }
  .submitbar .sw input { margin:0; cursor:pointer; }
  .submitbar .hint { color:#6b7280; font-size:12.5px; }
  .submitbar .hint.manual { color:#b45309; font-weight:600; }
  .submitbar .thr { display:inline-flex; align-items:center; gap:6px;
                    color:#374151; margin:0; font-size:12.5px; }
  .submitbar .thr input { width:76px; padding:4px 7px; font-size:13px;
                          border:1px solid #d8dee4; border-radius:7px; }
  .submitbar .sp { flex:1 1 auto; min-width:0; }
  /* 模式说明行：随开关切换内容，永远只显示"当前模式"那一条，
     避免出现"自动提交（推荐）"和"审核提交"并列自相矛盾（实测踩过）。 */
  .submitdesc { margin:8px 0 0; padding:8px 12px; border-radius:8px;
                background:rgba(15,23,42,.03);
                border-left:3px solid #2563eb;
                font-size:12.5px; line-height:1.75; color:#374151; }
  .submitdesc.manual { border-left-color:#d97706;
                       background:rgba(217,119,6,.06); }
  .submitdesc .dim { color:#6b7280; }
  #submitTip .warn { color:#b45309; }
  @media (max-width:640px){
    .submitbar { flex-wrap:wrap; gap:8px; }
    .submitbar .sp { display:none; }
    .submitbar .thr { margin-left:auto; }
  }
  /* 🔴 2026-09-24 修：中间断点（平板 / 窄窗口 641~900px）
     这一段既不匹配 max-width:640px 的手机规则，也放不下桌面「一行到底」——
     实测 768px 下 6 个按钮被压成**竖排单字**（「全」「全」「全」…）。
     对策：允许换行，但仍保持"按钮自身不折行"。
     ⚠️ 只影响 641~900px，桌面(>900)与手机(≤640)都不会命中。 */
  @media (min-width:641px) and (max-width:900px){
    .bar{flex-wrap:wrap;gap:8px}
    .bar > strong{flex:1 1 100%}
    .bar .sp{display:none}
    .bar > .tag{flex:none}
    /* 换行之后每一行都有富余空间 → 按钮不必再压缩，保持内容宽 */
    .bar button{flex:none}
  }
  /* .bulkbar 是纯粹的分组壳子：桌面端用 display:contents 让它"透明化"，
     6 个按钮依旧直接在 .bar 的 flex 流里排成一行 —— 桌面版零变化。
     （手机端改回 inline-flex 才能整块折叠/展开） */
  /* 🔴 2026-09-27：按钮增多后 `.bar` 改为换行显示，原来"透明化"的写法
     会让 6 个批量按钮和「作业清单/考试清单/开始」混作一团、两行疏密不匀。
     给批量按钮加一个浅底分组，视觉上把"批量操作"与"清单/开始"分开。 */
  .bulkbar { display:inline-flex; gap:8px; align-items:center;
             padding:3px 10px; border-radius:10px; background:#f1f5f9; }
  /* 手机端的折叠开关，桌面端永不显示 */
  .only-mobile { display:none; }

  /* ===== 作业清单弹窗（2026-09-26 新增）===== */
  .hwmodal { position:fixed; inset:0; z-index:9000; display:flex;
             align-items:center; justify-content:center; padding:24px;
             background:rgba(15,23,42,.42); }
  .hwmodal.hide { display:none; }
  .hwbox { width:min(760px,100%); max-height:min(82vh,760px); display:flex;
           flex-direction:column; border-radius:14px; overflow:hidden;
           background:linear-gradient(180deg,rgba(255,255,255,.98),rgba(252,253,255,.96));
           border:1px solid rgba(255,255,255,.7);
           box-shadow:0 24px 60px rgba(15,23,42,.28);
           backdrop-filter:blur(22px) saturate(160%);
           -webkit-backdrop-filter:blur(22px) saturate(160%); }
  .hwhead { display:flex; align-items:center; gap:10px; padding:14px 16px;
            border-bottom:1px solid #eef2f7; }
  .hwhead .sp { flex:1; }
  .hwbar { display:flex; gap:8px; align-items:center; padding:10px 16px;
           border-bottom:1px solid #eef2f7; flex-wrap:wrap; }
  .hwbar input { flex:1; min-width:180px; padding:8px 10px;
                 border:1px solid #d8dee4; border-radius:8px; font-size:13px; }
  .hwtip { padding:9px 16px; font-size:12.5px; color:#475569;
           background:#f8fafc; border-bottom:1px solid #eef2f7; line-height:1.6; }
  .hw-warn { color:#b45309; }
  .hwlist { flex:1; overflow:auto; padding:6px 10px 10px; }
  .hwc { margin:8px 2px 4px; font-size:13px; font-weight:600; color:#334155;
         padding:6px 6px 4px; display:flex; align-items:baseline; gap:8px; }
  .hwc .n { font-weight:400; color:#94a3b8; font-size:12px; }
  .hwi { display:flex; align-items:flex-start; gap:10px; padding:9px 10px;
         border-radius:9px; transition:background .16s ease; }
  .hwi:hover { background:#f6f9fc; }
  .hwi input[type=checkbox] { width:17px; height:17px; margin-top:1px;
                              accent-color:#16a34a; cursor:pointer; flex:none; }
  .hwi .nm { flex:1; font-size:13.5px; color:#1f2937; line-height:1.5;
             word-break:break-all; }
  .hwi .st { flex:none; font-size:12px; padding:2px 8px; border-radius:999px;
             background:#f1f5f9; color:#64748b; }
  .hwi .st.done { background:#ecfdf5; color:#047857; }
  .hwi .st.todo { background:#fff7ed; color:#b45309; }
  /* 2026-09-27：弹窗补显示分数（此前只在卡片明细里显示，用户报"做完了看不到分"）*/
  .hwi .hwsc { flex:none; font-size:12px; color:#0369a1; background:#eff6ff;
               padding:2px 9px; border-radius:999px; }
  .hwi .hwsc b { font-size:13.5px; }
  .hwi .hwsc.zero { color:#b91c1c; background:#fef2f2; }
  .hwi .redo { flex:none; font-size:12px; color:#b45309; display:flex;
               align-items:center; gap:5px; cursor:pointer; user-select:none; }
  .hwi .redo input { width:15px; height:15px; accent-color:#f59e0b; }
  .hwfoot { display:flex; align-items:center; gap:10px; padding:12px 16px;
            border-top:1px solid #eef2f7; background:#fcfdff; }
  .hwfoot .sp { flex:1; }
  /* 🔴 2026-09-28 手机端修「弹窗划不动」：
     原布局里只有 .hwlist 可滚（flex:1 + overflow:auto），而弹窗上半部分的
     固定内容（标题 + 筛选 + 说明条 + 登记表单）在窄屏会换行stacking，总高
     一旦超过 .hwbox 的 max-height，列表就被压成 0 高、固定内容自身溢出，
     而 .hwbox 是 overflow:hidden → 溢出部分被裁掉且**无处可滚**（用户手机实测）。
     窄屏改为「整个弹窗放在遮罩层里滚动」，手指滑哪都能滚。 */
  @media (max-width:700px) {
    .hwmodal { align-items:flex-start; overflow-y:auto;
               -webkit-overflow-scrolling:touch; padding:12px 8px 20px; }
    .hwbox { max-height:none; }
    .hwlist { overflow:visible; flex:0 0 auto; }
  }
  .hwempty { padding:26px 16px; text-align:center; color:#94a3b8; font-size:13px; }
  /* 本轮任务结果（2026-09-28）：把失败原因显式摆在界面上，别再只写日志 */
  .trhead { font-size:13.5px; font-weight:600; color:#334155; margin-bottom:4px; }
  .tri { display:flex; align-items:baseline; gap:8px; margin:8px 0 2px; }
  .tri .trn { flex:1; font-size:13px; color:#1f2937; word-break:break-all;
              line-height:1.5; }
  .trs { flex:none; font-size:12px; padding:2px 8px; border-radius:999px;
         background:#f1f5f9; color:#64748b; }
  .trs.ok { background:#ecfdf5; color:#047857; }
  .trs.bad { background:#fff7ed; color:#b45309; }
  /* 「仅暂存」：考试跑了但**没替你交卷**（默认行为）—— 必须与"成功"区分开，
     否则用户会以为已经交上去了（2026-10-12）。 */
  .trs.staged { background:#eef2ff; color:#4338ca; }
  /* 开考前「预检」结果面板（2026-10-12）：把这场考试的要求一条条摆出来 */
  #examPre { margin:2px 0 8px; padding:9px 11px; border-radius:9px;
             background:#f8fafc; border:1px solid #e2e8f0; font-size:12.5px; }
  #examPre .pch { font-weight:600; color:#334155; margin-bottom:5px; }
  #examPre .pcr { display:flex; gap:8px; line-height:1.8; }
  #examPre .pck { flex:none; width:112px; color:#64748b; }
  #examPre .pcv { flex:1; color:#1f2937; word-break:break-all; }
  #examPre .ok { color:#047857; font-weight:600; }
  #examPre .warn { color:#b45309; font-weight:600; }
  #examPre .pcnote { margin-top:7px; padding-top:7px; border-top:1px dashed #cbd5e1;
                     color:#475569; line-height:1.75; }
  .trm { font-size:12.5px; color:#92400e; background:#fffbeb;
         border-left:3px solid #fcd34d; padding:6px 9px; margin:3px 0 6px;
         border-radius:0 8px 8px 0; line-height:1.6; word-break:break-all; }
  .trerr { color:#b91c1c; font-weight:600; margin-bottom:8px; }
  /* 额外课程标签（超星班等：不在课程列表接口里、无章节数据，只支持作业/考试）*/
  .tagx { display:inline-block; font-size:11px; font-weight:500; padding:1px 7px;
          border-radius:999px; background:#eef2ff; color:#4338ca;
          border:1px solid #c7d2fe; vertical-align:middle; margin-left:2px; }
  /* 🔴 2026-09-28 只读身份：进去只有「运行进度 + 日志」，没有任何操作入口。
     选课/登录页整页隐藏，进度页里的按钮也一并去掉。 */
  body.ro-mode #step1, body.ro-mode #step2 { display:none !important; }
  body.ro-mode .bar button { display:none !important; }
  /* 「退出服务」在 .topbar 里、不在 .bar 里 —— 上面的选择器盖不到它。
     它调的是 /api/shutdown?all=1，会连共享题库一起杀掉（其它实例立刻失去
     题库来源），对普通用户是纯破坏性按钮 → 必须单独隐藏。 */
  body.ro-mode #btnQuit { display:none !important; }
  body.ro-mode #btnBack { display:none !important; }   /* 返回选课：管理员已选好课，无用 */
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
  /* 风控中止：橙色警示，绝不能和绿色"已完成"混为一谈
     （2026-09-24 用户报障：4 门课全被风控挡下、一门没刷，界面却全绿）*/
  .crow.aborted { background:#fff7ed; border-bottom-color:#fed7aa;
                  box-shadow:inset 3px 0 0 #f97316; }
  .crow.aborted .dot { background:#f97316; }
  .crow.aborted .cst { background:#ffedd5; color:#c2410c; font-weight:600; }
  .crow.aborted .ct .cn { color:#9a3412; }
  .crow.aborted .mini-bar i { background:#f97316; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
  /* 风控横幅：熔断后停在看板顶部，明确告知"没刷完 + 怎么补救" */
  .riskbar { display:flex; gap:10px; align-items:flex-start; margin:0 0 12px;
             padding:11px 13px; background:#fef2f2; border:1px solid #fecaca;
             border-left:3px solid #dc2626; border-radius:8px; }
  .riskbar .ri { flex:none; font-size:15px; line-height:1.35; }
  .riskbar .rb { min-width:0; }
  .riskbar .rt { font-size:13px; font-weight:600; color:#b91c1c; }
  .riskbar .rs { font-size:11.5px; color:#7f1d1d; margin-top:3px; line-height:1.6; }
  .riskbar.hide { display:none; }
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
  /* 🔴 2026-09-27：登录页「高级：自定义 Key 池」折叠触发条 */
  .adv-toggle{display:inline-block;cursor:pointer;user-select:none;
    font-size:12px;color:#4b5563;padding:3px 8px;border-radius:6px;
    border:1px solid #d1d5db;background:#f9fafb;transition:all .15s}
  .adv-toggle:hover{color:#111827;border-color:#9ca3af;background:#f3f4f6}
  .adv-toggle[data-open]{color:#1d4ed8;border-color:#bfdbfe;background:#eff6ff}
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
/* ===== 视觉增强（2026-09-24）：与网关同一套设计语言 =====
   约束（沿用既有铁律）：只加/覆盖**外观**属性，不动任何尺寸、布局、
   结构与 JS 行为。动效只作用于 transform / opacity / box-shadow，
   保证跑在合成层，60fps。
   缓动选型依据：入场用 ease-out（起步快=显得响应快），
   屏内移动用 ease-in-out，悬停/变色用 ease，按压用极短 linear。 */
/* ---- 会动的环境背景（2026-09-24 第五轮：改成"明显在流动"）----
   ⚠️ 上一版的教训：极光 22s 漂 ±14%、网格 40s 走 56px，
   换算下来背景**每秒只挪 1-3 像素** —— 等于静止。用户反馈"没啥动效"。
   这一版的原则：**不用碰它，肉眼就能看到在流**。
   手段（全部只动 transform / background-position / opacity，走合成层）：
     ① 极光：周期压到 14s、位移拉到 ±26%、加独立旋转层
     ② 光带扫过：一条斜向高光带 9s 横穿全屏（最显眼的"流动感"）
     ③ 网格：速度提到 6s 走一格，肉眼可见在爬
     ④ 呼吸光晕：中心亮度 8s 周期起伏
     ⑤ 漂浮光点：若干小光斑各自独立漂移（有"活着"的感觉）
     ⑥ 顶部流动渐变条：色相循环，明确表示"系统在运行" */
:root{
  --ink:#161b22; --ink-2:#3d4757; --ink-3:#6b7686; --ink-4:#9aa3b0;
  --line:#e3e8f0; --line-2:#eef2f8;
  --brand:#2563eb; --brand-2:#3b82f6; --brand-3:#60a5fa;
  --blue-ink:#1d4ed8;
  --sh-1:0 1px 2px rgba(16,24,40,.05);
  --sh-2:0 1px 2px rgba(16,24,40,.05), 0 10px 28px -12px rgba(16,24,40,.14);
  --sh-3:0 2px 6px rgba(16,24,40,.07), 0 20px 44px -16px rgba(16,24,40,.20);
  --ease-out-quart:cubic-bezier(.165,.84,.44,1);
  --ease-out-quint:cubic-bezier(.23,1,.32,1);
  --ease-out-expo:cubic-bezier(.19,1,.22,1);
  --ease-in-out:cubic-bezier(.77,0,.175,1);
  --ease-spring:cubic-bezier(.34,1.56,.64,1);   /* 松开回弹 */
  --mx:50vw; --my:50vh;                          /* 光标位置，由 JS 写入 */
}
/* ---- 底层：四层色斑静态打底（避免整体"发灰"）---- */
body{
  color:var(--ink);
  background:
    radial-gradient(1100px 700px at 8% -6%,   rgba(125,211,252,.42), transparent 60%),
    radial-gradient(900px 580px at 92% 2%,    rgba(186,230,253,.32), transparent 58%),
    radial-gradient(820px 640px at 78% 106%,  rgba(103,232,249,.30), transparent 60%),
    radial-gradient(700px 520px at 16% 98%,   rgba(125,211,252,.26), transparent 60%),
    linear-gradient(168deg,#f4f8fe 0%,#e8f0fb 48%,#e3ecf9 100%);
  background-attachment:fixed;
}
/* ① 极光：14s 一圈、±26% 位移 + 旋转，肉眼明确可见在"游" */
body::before{
  content:"";position:fixed;inset:-40%;z-index:-2;pointer-events:none;
  background:
    radial-gradient(62% 52% at 24% 26%, rgba(125,211,252,.55) 0%, rgba(125,211,252,.55) 44%, rgba(125,211,252,0.18) 64%, transparent 72%),
    radial-gradient(58% 48% at 76% 60%, rgba(103,232,249,.47) 0%, rgba(103,232,249,.47) 44%, rgba(103,232,249,0.16) 64%, transparent 72%),
    radial-gradient(54% 46% at 46% 90%, rgba(56,189,248,.42) 0%, rgba(56,189,248,.42) 44%, rgba(56,189,248,0.14) 64%, transparent 73%),
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
/* ② 光带扫过：斜向高光带 9s 横穿全屏 —— 最直观的"有东西在流动"。
   只动 transform，GPU 合成，零重排。 */
body .sweep{
  position:fixed;top:-60%;left:-60%;width:220%;height:220%;
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
/* ④ 呼吸光晕：中心亮度 8s 起伏，整个页面像在"呼吸" */
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
/* ⑤ 漂浮光点：各自独立漂移，营造"有东西在游"的生气 */
body .orbs{
  position:fixed;inset:0;z-index:-2;pointer-events:none;
}
body .orbs i{
  position:absolute;display:block;border-radius:50%;
  will-change:transform,opacity;
}
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
  0%  {transform:translate3d(-30px,20px,0)  scale(.9)}
  50% {transform:translate3d(-110px,-60px,0) scale(1.3)}
  100%{transform:translate3d(-30px,20px,0)  scale(.9)}
}
/* ⑥ 顶部流动渐变条：色相循环，明确表示"系统在运行"。
   放在最上方 3px，不占布局高度（position:fixed）。 */
body .flowbar{
  position:fixed;top:0;left:0;right:0;height:3px;z-index:50;pointer-events:none;
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
body .cursorGlow{
  position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:0;
  background:radial-gradient(360px 360px at var(--mx) var(--my),
             rgba(56,189,248,.22), transparent 70%);
  transition:opacity .5s var(--ease-out-quart);
  will-change:background;
}
body.pointerFine .cursorGlow{opacity:1}
/* 细噪点：仅 3.5%，消除渐变的塑料感 */
body::after{
  content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.42;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)' opacity='.035'/%3E%3C/svg%3E");
}
/* ---- 卡片：玻璃质感 + 含蓄阴影 ---- */
.card{
  background:linear-gradient(180deg,rgba(255,255,255,.40) 0%,rgba(252,253,255,.30) 100%);
  border-color:rgba(255,255,255,.66);
  box-shadow:var(--sh-2),inset 0 1px 0 rgba(255,255,255,.60);
  backdrop-filter:blur(12px) saturate(160%);
  -webkit-backdrop-filter:blur(12px) saturate(160%);
  animation:cardIn .46s var(--ease-out-expo) both;
}
@keyframes cardIn{from{opacity:0;transform:translate3d(0,10px,0)}to{opacity:1;transform:none}}
h1{letter-spacing:.2px;color:var(--ink)}
/* ---- 输入控件：聚焦时轻微上浮 + 光环 ---- */
input[type=text],input[type=password],input[type=number],select{
  background:#fcfdff;color:var(--ink);
  transition:border-color .18s var(--ease-out-quart),
             box-shadow .18s var(--ease-out-quart),
             background .18s var(--ease-out-quart),
             transform .18s var(--ease-out-quart);
}
input[type=text]:hover,input[type=password]:hover,input[type=number]:hover,select:hover{
  border-color:#c3cfe2}
input[type=text]:focus,input[type=password]:focus,input[type=number]:focus,select:focus{
  outline:none;border-color:var(--brand);background:#fff;
  box-shadow:0 0 0 3.5px rgba(37,99,235,.14);transform:translateY(-1px)}
/* ---- 按钮：渐变底 + 光泽扫过 + 物理按压 + 松开回弹 ----
   缓动决策（对齐 web-animation-design）：
     悬停位移 → ease-out-quart（起步快，显得干脆）
     按下     → .06s linear（必须短且匀速，否则"点了没反应"）
     松开     → ease-spring（轻微过冲后落定，手感"弹回来"） */
button{
  background:linear-gradient(180deg,var(--brand-2) 0%,var(--brand) 100%);
  box-shadow:0 1px 2px rgba(37,99,235,.28),0 8px 18px -6px rgba(37,99,235,.45);
  transition:transform .16s var(--ease-out-quart),
             box-shadow .16s var(--ease-out-quart),
             filter .16s var(--ease-out-quart);
  position:relative;overflow:hidden;isolation:isolate;
  transform:translateZ(0);
}
button::after{
  content:"";position:absolute;inset:0;z-index:-1;pointer-events:none;
  background:linear-gradient(105deg,transparent 30%,rgba(255,255,255,.42) 50%,transparent 70%);
  transform:translate3d(-115%,0,0);transition:transform .62s var(--ease-out-expo)}
button:hover:not(:disabled)::after{transform:translate3d(115%,0,0)}
button:hover:not(:disabled){transform:translateY(-2px);
  box-shadow:0 3px 6px rgba(37,99,235,.3),0 14px 26px -8px rgba(37,99,235,.58)}
/* 按下：明确下沉。⚠️ 必须用 transition 简写整体覆盖 ——
   只写 transition-duration 会沿用 --ease-spring（前 34% 几乎不动），
   实测要 250ms+ 才看得出下沉，手感是"点了没反应"。 */
button:active:not(:disabled){transform:translateY(1px) scale(.97);
  filter:brightness(.95);
  box-shadow:0 0 0 0 rgba(37,99,235,.3),0 2px 5px rgba(37,99,235,.32);
  transition:transform .06s linear,filter .06s linear,box-shadow .06s linear}
button:not(:active):not(:disabled){
  transition:transform .34s var(--ease-spring),
             box-shadow .3s var(--ease-out-quart),
             filter .2s var(--ease-out-quart)}
/* 波纹：由 JS 从**点击落点**注入 span.ripple */
button .ripple{position:absolute;border-radius:50%;pointer-events:none;
  background:radial-gradient(circle,rgba(255,255,255,.55) 0%,rgba(255,255,255,0) 70%);
  transform:translate(-50%,-50%) scale(0);opacity:.9;
  animation:rippleOut .52s var(--ease-out-quart) forwards;z-index:-1}
@keyframes rippleOut{
  0%  {transform:translate(-50%,-50%) scale(0);opacity:.9}
  100%{transform:translate(-50%,-50%) scale(1);opacity:0}
}
/* 主按钮磁吸：JS 写入 --pull-x/--pull-y（上限 4px） */
button.magnetic:hover:not(:disabled){
  transform:translate(calc(var(--pull-x,0px) + 0px),calc(-2px + var(--pull-y,0px)))}
button.magnetic:active:not(:disabled){transform:translate(0,1px) scale(.97);
  filter:brightness(.95);
  box-shadow:0 0 0 0 rgba(37,99,235,.3),0 2px 5px rgba(37,99,235,.32);
  transition:transform .06s linear,filter .06s linear,box-shadow .06s linear}
/* 幽灵/次要按钮：不要渐变光泽，避免抢主按钮的视觉重心 */
button.ghost{background:#fff;color:var(--ink-2);border:1px solid #d8dee4;
  box-shadow:none;
  transition:border-color .16s var(--ease-out-quart),color .16s var(--ease-out-quart),
             background .16s var(--ease-out-quart),transform .16s var(--ease-out-quart),
             box-shadow .16s var(--ease-out-quart)}
button.ghost::after{display:none}
button.ghost:hover:not(:disabled){background:#f9fbff;border-color:#b9c6dc;
  color:var(--blue-ink);
  box-shadow:0 2px 6px rgba(16,24,40,.07)}
button.ghost:active:not(:disabled){background:#f1f5fb;border-color:#9fb3d1;
  transform:translateY(1px) scale(.985);filter:none;
  box-shadow:0 1px 2px rgba(16,24,40,.06);
  transition:transform .06s linear,background .06s linear,border-color .06s linear}
button.mini{background:#fff;color:var(--ink-3);border:1px solid #d8dee4;box-shadow:none}
button.mini::after{display:none}
button.mini:hover:not(:disabled){color:#b91c1c;border-color:#fca5a5;background:#fef2f2}
/* ---- 课程卡片：悬停抬升 + 选中态左移强调 ---- */
.course{
  background:rgba(255,255,255,.72);
  transition:background .16s var(--ease-out-quart),
             border-color .16s var(--ease-out-quart),
             box-shadow .2s var(--ease-out-quart),
             transform .2s var(--ease-out-quart)}
.course:hover{background:#fbfcff;border-color:#cfdcee;
  transform:translateY(-1px);box-shadow:0 6px 16px -8px rgba(16,24,40,.16)}
.course.on{border-color:var(--brand);background:linear-gradient(180deg,#f2f7ff,#eaf2ff);
  box-shadow:0 1px 2px rgba(37,99,235,.055),0 8px 20px -10px rgba(37,99,235,.32)}
.course.on:hover{border-color:var(--blue-ink)}
/* 勾选框：出现时轻微放大，给"被勾上"一个触感 */
.course input[type=checkbox]{accent-color:var(--brand);
  transition:transform .18s var(--ease-spring)}
/* ---- 任务扫描进度条（2026-09-26）---- */
.taskbar{
  display:flex;align-items:center;gap:10px;margin:0 0 10px;padding:9px 13px;
  border-radius:11px;font-size:13px;line-height:1.5;
  background:rgba(37,99,235,.06);color:#1e40af;
  border:1px solid rgba(37,99,235,.16)}
.taskbar.ok{background:rgba(16,185,129,.08);color:#065f46;
  border-color:rgba(16,185,129,.22)}
.taskbar.err{background:rgba(239,68,68,.07);color:#991b1b;
  border-color:rgba(239,68,68,.22)}
.taskbar.run::before{
  content:'';width:13px;height:13px;flex:0 0 auto;border-radius:50%;
  border:2px solid rgba(37,99,235,.25);border-top-color:var(--brand);
  animation:tspin .7s linear infinite}
@keyframes tspin{to{transform:rotate(360deg)}}
.taskbar .hide{display:none}
/* ---- 课程卡片：包裹层 + 明细展开区 ---- */
.course-wrap{display:flex;flex-direction:column}
.course-wrap.on{border-radius:13px}
.cdetail{padding:0 0 6px 14px;margin:-4px 0 4px 0}
.cdetail.hide{display:none}
/* ---- 卡片内分组行：章节任务 / 任务（作业·考试）----
   对齐超星 App 结构：作业与考试在一边，视频与章节在另一边。 */
.course .cbody{flex:1;min-width:0}
.course .grp{display:flex;align-items:center;gap:12px;margin-top:6px;
  flex-wrap:wrap}
.course .glab{font-size:11.5px;font-weight:600;color:#94a3b8;
  letter-spacing:.02em;flex:0 0 auto;min-width:52px}
.course .gexp{flex:0 0 auto;width:24px;height:24px;line-height:22px;
  text-align:center;border-radius:7px;color:#94a3b8;font-size:12px;
  cursor:pointer;user-select:none;
  transition:background .14s var(--ease-out-quart),
             color .14s var(--ease-out-quart)}
.course .gexp:hover{background:rgba(37,99,235,.10);color:var(--brand)}
.course .gexp.has{color:var(--brand);background:rgba(37,99,235,.07)}
/* 明细区二级切换 tab（作业 ｜ 考试） */
.cd-tabs{display:inline-flex;gap:2px;padding:2px;margin:4px 0 7px;
  background:rgba(15,23,42,.05);border-radius:9px}
.cd-tab{display:inline-flex;align-items:center;gap:5px;
  padding:4px 13px;border:0;border-radius:7px;cursor:pointer;
  font-size:12.5px;font-weight:600;color:#64748b;
  background:transparent;
  transition:background .16s var(--ease-out-quart),
             color .16s var(--ease-out-quart)}
.cd-tab:hover{color:var(--brand)}
.cd-tab.on{background:#fff;color:var(--brand);
  box-shadow:0 1px 3px rgba(15,23,42,.10)}
.cd-tab .tb{font-size:10.5px;font-weight:600;padding:0 5px;
  border-radius:5px;background:rgba(37,99,235,.11);color:#1d4ed8}
.cd-pane.hide{display:none}
.cd-sec{margin:8px 0}
.cd-sec-t{display:flex;align-items:center;gap:8px;font-size:12.5px;
  font-weight:600;color:var(--ink-3);margin:0 0 5px}
.cd-sec-t .n{font-weight:400;color:#94a3b8}
.cd-sec-t .sp{flex:1}
.cd-item{display:flex;align-items:center;gap:8px;padding:5px 9px;
  border-radius:8px;font-size:13px;cursor:pointer;
  transition:background .14s var(--ease-out-quart)}
.cd-item:hover{background:rgba(37,99,235,.05)}
.cd-item .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.cd-item .st{flex:0 0 auto;font-size:11.5px;padding:1px 7px;border-radius:6px}
.cd-item .st.done{background:#eef2f7;color:#64748b}
.cd-item .st.todo{background:rgba(245,158,11,.14);color:#b45309}
.cd-empty{padding:9px 12px;font-size:12.5px;color:#94a3b8}
/* ---- 作业条目：可重做时的操作条 ---- */
.cd-item-wrap{border-radius:9px;
  transition:background .14s var(--ease-out-quart)}
.cd-item-wrap:hover{background:rgba(37,99,235,.04)}
.cd-item-wrap:hover .cd-item{background:transparent}
.rd-bar{display:flex;align-items:center;gap:9px;flex-wrap:wrap;
  padding:0 10px 6px 32px;margin-top:-2px}
.rd-sc{font-size:11.5px;color:#64748b}
.rd-sc b{color:#0f172a;font-size:12.5px}
.rd-n{font-size:11.5px;color:#64748b;
  background:rgba(15,23,42,.05);padding:1px 8px;border-radius:6px}
.rd-n b{color:var(--brand);font-size:12.5px}
.rd-n.out{background:rgba(148,163,184,.14);color:#94a3b8}
.rd-rule{font-size:11px;padding:1px 7px;border-radius:6px;
  background:rgba(16,185,129,.12);color:#047857;cursor:help;
  border:1px solid rgba(16,185,129,.20)}
/* 「本次成绩」= 重做有风险，必须醒目 */
.rd-rule.risky{background:rgba(245,158,11,.15);color:#b45309;
  border-color:rgba(245,158,11,.28)}
.rd-btn{margin-left:auto}
.rd-btn.off{opacity:.42;cursor:not-allowed}
/* 有明细的课程：展开按钮常驻高亮（否则用户不知道能展开） */
.course .gexp.has{color:var(--brand);background:rgba(37,99,235,.07)}
.course{padding-right:8px}
/* 考试维度勾选框：与其它维度同款 */
.course .mode .ck-exam{accent-color:#7c3aed}
.course input[type=checkbox]:active{transform:scale(1.18)}
/* ---- 进度条：条纹流动，明确"正在推进" ---- */
.pfill{background:linear-gradient(90deg,#34d399,#10b981);
  background-size:200% 100%;
  animation:barFlow 1.6s var(--ease-in-out) infinite}
.pfill.blue{background:linear-gradient(90deg,#60a5fa,#2563eb);
  background-size:200% 100%}
@keyframes barFlow{0%{background-position:0% 0}100%{background-position:200% 0}}
/* ---- 概览/统计卡：入场时轻微上浮 ---- */
.ov,.stats{animation:cardIn .5s var(--ease-out-expo) both}
.scard{background:rgba(255,255,255,.72);border-color:var(--line);
  transition:transform .2s var(--ease-out-quart),box-shadow .2s var(--ease-out-quart)}
.scard:hover{transform:translateY(-2px);box-shadow:var(--sh-2)}
/* ---- 滚动条：精致化 ---- */
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(100,116,139,.28);border-radius:999px;
  border:2px solid transparent;background-clip:content-box}
::-webkit-scrollbar-thumb:hover{background:rgba(100,116,139,.45);background-clip:content-box}
body{animation:fadeIn .34s var(--ease-out-quart)}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
::selection{background:rgba(37,99,235,.16)}
/* 按钮成功态：短暂变绿打勾（忙碌→成功→复位三点式） */
button.done{background:linear-gradient(180deg,#34d399 0%,#10b981 100%);
  box-shadow:0 1px 2px rgba(16,185,129,.3),0 8px 18px -6px rgba(16,185,129,.5);
  animation:donePop .42s var(--ease-spring)}
@keyframes donePop{0%{transform:scale(1)}45%{transform:scale(1.045)}100%{transform:scale(1)}}
button.done .lbl::before{content:"✓ "}
/* 无障碍：系统开了"减弱动态效果"时，只保留必要的淡入淡出 */
@media (prefers-reduced-motion: reduce){
  body{animation:none}
  body::before,.gridflow,.breathe,.orbs i,.sweep,.flowbar{animation:none}
  .sweep,.orbs{opacity:.35}
  .flowbar{opacity:.5}
  body.pointerFine .cursorGlow{opacity:0}
  .card,.ov,.stats{animation:none}
  button::after{display:none}
  button .ripple{display:none}
  button:hover:not(:disabled){transform:none}
  button:active:not(:disabled){transform:none;filter:none}
  button.magnetic:hover:not(:disabled),button.magnetic:active:not(:disabled){transform:none}
  button.done{animation:none}
  .pfill,.pfill.blue{animation:none}
  .course:hover,.scard:hover{transform:none}
  label::after,.crow .mini-bar i{animation:none}
}

/* ===== 输入框「浅特效」（2026-09-24）=====
   与网关登录页同一套语言，克制不夺目。
   ⚠️ input 是替换元素，::before/::after 不生效 → 底边高光挂在 label 上。 */
input[type=text],input[type=password],input[type=number],select{
  box-shadow:inset 0 1px 2px rgba(15,32,74,.045), 0 1px 2px rgba(15,32,74,.04)}
input[type=text]:hover,input[type=password]:hover,input[type=number]:hover,select:hover{
  border-color:#c3cfe2;
  box-shadow:inset 0 1px 2px rgba(15,32,74,.05),
             0 2px 6px -1px rgba(37,99,235,.10)}
input[type=text]:focus,input[type=password]:focus,input[type=number]:focus,select:focus{
  border-color:var(--brand);background:#fff;
  box-shadow:inset 0 1px 2px rgba(15,32,74,.03),
             0 0 0 3.5px rgba(37,99,235,.14),
             0 4px 12px -3px rgba(37,99,235,.26),
             0 12px 28px -12px rgba(37,99,235,.30);
  transform:translateY(-1px)}
label:has(+ input:focus)::after,
label:has(+ select:focus)::after,
label:has(+ div input:focus)::after{
  content:"";position:absolute;left:0;right:0;bottom:-6px;height:1.5px;
  background:linear-gradient(90deg,transparent,var(--brand-2),var(--brand),transparent);
  border-radius:2px;animation:ulSweep .5s var(--ease-out-expo) both;
  pointer-events:none}
@keyframes ulSweep{
  0%  {transform:scaleX(0);opacity:0;transform-origin:left}
  60% {opacity:1}
  100%{transform:scaleX(1);opacity:.85;transform-origin:left}
}
input:focus::placeholder{opacity:.5}
input::placeholder{transition:opacity .2s var(--ease-out-quart)}

/* ===== 手机端专项（2026-09-24，第二版重做）=====
   ⚠️ 第一版教训（用户反馈）：
     ① "动态效果和流动效果还是完全没有" —— 我把 .sweep/.orbs/.flowbar 直接
        display:none，等于把整套设计语言砍掉了。手机上这几层全是
        transform/opacity，走 GPU 合成、零重排，中端机完全跑得动。
        **正确做法：层数一层不动，只降级"贵"的部分（filter:blur 半径）。**
     ② "放大排在一起太拥挤了，反而没有那种灵活感" —— 我只做了等比放大
        （输入框 48px+16px，说明文字还是 12px），大控件配小文字 = 闷且笨。
        **正确做法：拉开层级差** —— 标题更大、字段间距更大、卡片留白更宽。 */
@media (max-width: 640px){
  html{-webkit-text-size-adjust:100%}
  /* --- 表单节奏：够大够好点，同时给足呼吸 --- */
  input[type=text],input[type=password],input[type=number],select{
    min-height:50px;padding:14px 16px;font-size:16px;border-radius:14px}
  input[type=text]:focus,input[type=password]:focus,input[type=number]:focus,select:focus{
    transform:none;border-color:var(--brand);
    box-shadow:inset 0 1px 2px rgba(15,32,74,.03),
               0 0 0 4px rgba(37,99,235,.15),
               0 4px 14px -4px rgba(37,99,235,.26)}
  label{display:block;font-size:14px;font-weight:500;color:#3d4657;
        margin:20px 0 9px;letter-spacing:.2px}
  button{min-height:52px;font-size:16.5px;border-radius:14px;
         letter-spacing:.3px;font-weight:500}
  button.mini{min-height:40px;font-size:13.5px;padding:9px 14px;border-radius:10px}
  button:hover:not(:disabled){transform:none;filter:none}
  button:active:not(:disabled){transform:scale(.972);filter:brightness(.95);
    transition:transform .08s linear,filter .08s linear}
  /* --- 卡片：更宽松的留白 + 更大圆角，才有"轻盈"感 --- */
  .card{padding:26px 21px;border-radius:22px;
        box-shadow:0 1px 2px rgba(15,32,74,.04),
                   0 16px 40px -18px rgba(15,23,42,.20)}
  h1{font-size:24px;font-weight:650;letter-spacing:.3px}
  .topbar{gap:12px;flex-wrap:wrap;margin-bottom:8px}
  /* --- 统计卡：至少两列，别挤成一条线 --- */
  .stats{gap:10px;margin-bottom:18px}
  .scard{flex:1 1 calc(50% - 5px);min-width:0;padding:14px 8px;border-radius:12px}
  .scard .n{font-size:22px}
  .scard .l{font-size:12.5px}
  /* --- 课程看板：行高放宽，别把章节挤成一坨 --- */
  .cboard{margin-bottom:18px;border-radius:14px}
  .cboard .chead{padding:13px 15px;gap:8px}
  .cboard .chead .csum{font-size:11.5px;white-space:nowrap}
  .crow{padding:12px 15px;gap:10px}
  .crow .mini-bar{width:64px;flex:none;height:7px}
  .crow .ct .cn{font-size:14px;line-height:1.5}
  .crow .ct .cr{font-size:11.5px;margin-top:3px}
  .riskbar{padding:13px 14px;gap:10px;margin-bottom:14px;border-radius:12px}
  .riskbar .rt{font-size:13.5px}
  .riskbar .rs{font-size:12px;line-height:1.7;margin-top:5px}
  #log{height:220px;font-size:12px;padding:13px;line-height:1.75;border-radius:12px}
  table{font-size:13px}
  .tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
  /* ===== ⑧ 顶部按钮组排版（2026-09-24 第四/五轮 · 用户：｢往右滑才能全部看到，
             还不如以前的｣ + ｢收起，点"批量勾选"展开｣）=====
     .bar 桌面是「一行到底」，手机 390px 上塞 8 个按钮必然挤爆（实测 x 顶到 492）。
     最终形态（用户选定）：
       第 1 行：标题 + 「已选 N」 + 「批量勾选 ▾」（开关）+ 「开始」
       第 2 行（默认收起）：6 个批量勾选按钮，2 列 × 3 行
     ⚠️ .bulkbar 桌面是 display:contents（零影响），手机改成 inline-flex 才能折叠。 */
  .bar{flex-wrap:wrap;gap:8px 6px;margin-bottom:12px;align-items:center}
  .bar > strong{flex:1 1 100%;font-size:15px;line-height:1.5}
  .bar .sp{display:none}                       /* 手机上不需要"撑开的空白" */
  .bar > .tag{flex:1 1 auto;font-size:12.5px;padding:5px 9px}
  .only-mobile{display:inline-flex;align-items:center;justify-content:center;
               flex:none;min-height:38px;padding:0 12px;font-size:13px;
               border-radius:10px}
  /* 折叠区：默认收起（display:none），展开时 2 列网格 */
  .bulkbar{display:none}
  .bulkbar.on{display:grid;grid-template-columns:1fr 1fr;gap:8px;
              flex:1 1 100%;order:10}
  .bulkbar > button.ghost{min-height:44px;padding:10px 4px;font-size:13.5px;width:100%}
  /* 「开始」整宽，主操作必须一眼看见、拇指够得到 */
  .bar > button:not(.ghost):not(.only-mobile){flex:1 1 100%;min-height:52px;
                                              font-size:16.5px;order:9}
  /* 筛选行：输入框吃满剩余宽，计数标签不参与压缩 */
  .bar + div{flex-wrap:wrap}
  #filterCount{flex:none}
  /* --- 背景动效：全保留，只降"贵"的部分 ---
     filter:blur 是唯一真正吃 GPU 的属性 → 半径减半；层数与周期一概不动。
     ⚠️ 用 `body .orbs i:nth-child(n)` 是因为基础定义写在这个特异性上。 */
  body .orbs i:nth-child(n){width:170px;height:170px}
  body .orbs i:nth-child(2){width:210px;height:210px}
  .sweep{opacity:.85}
  .breathe{opacity:.7}
  .gridflow{opacity:.24}
  .cursorGlow{display:none}
  ::-webkit-scrollbar{width:0;height:0}

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

  /* ② 光带：9s → 5.5s，位移 ±38% → ±52%（明显"刷"过去） */
  body .sweep{
    animation-duration:5.5s;
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
  .card{padding:22px 17px;border-radius:20px}
  h1{font-size:22px}
  label{margin:17px 0 8px}
  input[type=text],input[type=password],input[type=number],select{
    font-size:16px;padding:13px 14px;min-height:48px}
  button{font-size:15.5px;padding:13px 16px;min-height:50px}
  .scard{flex:1 1 100%}

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
    260% 100%,
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

</style>
</head>
<body>
<div class="gridflow" aria-hidden="true"></div>
<div class="breathe" aria-hidden="true"></div>
<div class="orbs" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div>
<div class="sweep" aria-hidden="true"></div>
<div class="cursorGlow" aria-hidden="true"></div>
<div class="flowbar" aria-hidden="true"></div>
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
        <input type="number" id="speed" value="1" min="1" max="2" step="0.5"></div>
      <div><label>并发章节数（建议 2）</label>
        <input type="number" id="jobs" value="1" min="1" max="8" step="1"></div>
    </div>
    <div class="warn">控总量建议：<b>实例数 × 并发章节数 ≤ 8</b>。本机 CPU / 网络 / 题库余量都很大
      （实测 20 并发查题库零错误），真正要留意的是<b>平台风控</b>：触发 403 会自动冷却 10 分钟，
      连续 3 次熔断中止。</div>

    <label>题库（用于章节检测自动答题；不启用则跳过答题任务）</label>
    <select id="tikuType">
      <option value="none">不使用（跳过答题任务）</option>
      <option value="adapter" selected>自建 TikuAdapter（免费，推荐）</option>
      <option value="yanxi">言溪题库（付费）</option>
      <option value="ai">纯 AI 大模型（自己的 API Key）</option>
    </select>
    <div id="adapterBox">
      <label>Adapter 地址</label>
      <input type="text" id="adapterUrl" value="http://127.0.0.1:8060/adapter-service/search?use=local,buguake,icodef">
      <div class="hint">必须带 <code>?use=</code>，否则本地题库不参与查询。当前：<code>local,buguake,icodef</code>（icodef 的 Token 自动从 config.ini 补）</div>
      <div class="chk"><input type="checkbox" id="aiFallback" checked>
        <span>AI 兜底：免费题库搜不到的题自动改用 AI 作答（默认开启，建议保持）</span></div>
    </div>
    <div id="yanxiBox" class="hide">
      <label>言溪 Token（官网个人中心 → 复制用户凭证）</label>
      <input type="text" id="token">
    </div>
    <div id="aiBox" class="hide">
      <div class="info">AI 已内置多 Key 池（4 个 Agnes 号 + 硅基流动免费兜底），
        <b>无需手动配置</b>：新建账号登录即自动带上，调用时自动轮换，
        哪个限流就歇 45 秒再回来，全程不用管。同一时刻只发一个请求（避免触发并发限流）。</div>
      <div class="chk" style="margin-top:8px">
        <span id="aiPoolLink" class="adv-toggle">▸ 高级：自定义 Key 池（一般不需要动）</span>
      </div>
      <div id="aiPoolAdv" class="hide" style="margin-top:6px">
        <label>API Endpoint（OpenAI 格式，含 /v1）</label>
        <input type="text" id="aiEndpoint" value="https://apihub.agnes-ai.cn/v1"
               placeholder="https://apihub.agnes-ai.cn/v1 或 https://api.siliconflow.cn/v1">
        <label>主 API Key</label>
        <input type="password" id="aiKey">
        <label>备用 Key 池 <span style="color:#059669;font-weight:600"> · 限流/失效自动切换，可跨平台</span></label>
        <textarea id="aiKeys" rows="4"
                  placeholder="每行一个。需要换平台时用 | 追加该行自己的 Endpoint 和模型：&#10;sk-bbb...&#10;sk-ccc...|https://api.siliconflow.cn/v1|Qwen/Qwen2.5-7B-Instruct&#10;sk-ddd...|https://open.bigmodel.cn/api/paas/v4|glm-4.5-flash"
                  style="width:100%;box-sizing:border-box;border:1px solid #d1d5db;border-radius:8px;padding:8px 10px;font-size:12px;font-family:ui-monospace,Menlo,monospace;resize:vertical"></textarea>
        <div class="info" style="margin-top:4px;font-size:11px;line-height:1.6">
          格式：<code>Key</code> = 沿用上面的 Endpoint/模型；
          <code>Key|Endpoint|模型</code> = 换 Key 时<b>连带切换端点和模型</b>（平台不同也不会串）
        </div>
        <label>模型名</label>
        <input type="text" id="aiModel" value="agnes-3.0-flash"
               placeholder="agnes-3.0-flash（支持读图） / deepseek-ai/DeepSeek-V3">
        <div class="chk"><button class="ghost" id="btnTestAi" style="padding:5px 12px;font-size:12px">测试 AI 连接</button>
          <span id="aiTestResult" style="font-size:12px;color:#6b7280"></span></div>
      </div>
    </div>

    <div class="chk"><input type="checkbox" id="autoSubmit" checked>
      <span>自动提交答案（达到覆盖率门槛就直接提交，不再人工逐章核对）</span></div>
    <div class="row">
      <div><label>提交覆盖率门槛（0 - 1，搜到题占比达到才提交）</label>
        <input type="number" id="coverRate" value="0.9" min="0" max="1" step="0.05"></div>
      <div></div>
    </div>
    <div class="info">覆盖率不达标时该章只保存不提交。开启 AI 兜底后命中率会明显提高，一般能到 100%。</div>

    <div style="margin-top:18px"><button id="btnLogin" class="magnetic">登录并获取课程</button></div>
    <div class="msg" id="msg1"></div>
  </div>

  <div class="card hide" id="step2">
    <div style="margin-bottom:10px">
      <span id="curAccount" class="tag">账号未核对</span>
      <button class="ghost mini" id="btnLogout">切换账号</button>
      <button class="ghost mini" id="btnRescan" title="重新扫描作业与考试">
        重新扫描
      </button>
      <button class="ghost mini" id="btnRefreshCourses"
              title="重新拉取课程列表：平台新发的课会出现在下面。不会重新登录，也不影响已勾选。">
        刷新课程
      </button>
    </div>
    <div class="tag ok" id="lastTaskTag" style="display:none;margin-bottom:8px"></div>
    <div class="taskbar hide" id="taskBar">
      <span id="taskBarText"></span>
      <button class="ghost mini" id="taskBarRetry" style="display:none">
        重试
      </button>
    </div>
    <div class="bar">
      <strong>勾选要做的课程与内容</strong>
      <span class="sp"></span>
      <span id="picked" class="tag">已选 0</span>
      <button class="ghost only-mobile" id="btnBulkToggle" aria-expanded="false">批量勾选 ▾</button>
      <span class="bulkbar" id="bulkBar">
        <button class="ghost" id="btnAll">全选</button>
        <button class="ghost" id="btnNone">全不选</button>
        <button class="ghost" id="btnAllQuiz">全选测验</button>
        <button class="ghost" id="btnNoQuiz">全不选测验</button>
        <button class="ghost" id="btnAllWork">全选作业</button>
        <button class="ghost" id="btnNoWork">全不选作业</button>
      </span>
      <button class="ghost" id="btnHwList">作业清单</button>
      <button class="ghost" id="btnExamList">考试清单</button>
      <button id="btnStart" class="magnetic">开始</button>
    </div>

    <!-- 🔴 2026-09-26 新增：把「提交方式」放到主界面。
         原先它只存在于登录页，登录后就再也找不到入口 —— 用户明确反映
         "没有提交的开关"。而作业链路又会在自动提交下把蒙的答案直接交出去，
         所以这个开关必须常驻、随手可改。 -->
    <div class="submitbar" id="submitBar">
      <label class="sw">
        <input type="checkbox" id="autoSubmitMain" checked>
        <span>自动提交作业</span>
      </label>
      <label class="sw">
        <input type="checkbox" id="autoSubmitExam">
        <span>自动提交考试</span>
      </label>
      <span class="hint" id="submitHint">达标即直接交卷，全自动</span>
      <span class="sp"></span>
      <label class="thr">覆盖率门槛
        <input type="number" id="coverRateMain" value="0.9"
               min="0" max="1" step="0.05">
      </label>
    </div>
    <div class="submitdesc" id="submitDesc"></div>
    <div class="info" id="submitTip" style="margin:6px 0 0;line-height:1.7">
      <span class="warn">⚠ 注意：作业若在「本次成绩」规则下，重做考低会覆盖原分；
      「最高成绩」规则则安全（取历史最高）。</span>
    </div>

    <div class="info" style="margin:6px 0 0">
      「全选测验 / 全选作业」只作用于<strong>你已勾选的课程</strong>——
      先勾好要刷哪几门，再用它批量补勾；没勾的课不会被带进来。<br>
      想<strong>按份挑选</strong>？点「作业清单」先扫描全部作业，再逐份勾选要做哪些。
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

  <!-- 作业清单弹窗（2026-09-26 新增）：先扫描、再逐份勾选 -->
  <div class="hwmodal hide" id="hwModal" role="dialog" aria-modal="true"
       aria-labelledby="hwTitle">
    <div class="hwbox">
      <div class="hwhead">
        <strong id="hwTitle">作业清单</strong>
        <span class="sp"></span>
        <span class="tag" id="hwCount">共 0 份</span>
        <button class="ghost" id="hwClose">关闭</button>
      </div>
      <div class="hwbar">
        <input type="text" id="hwFilter" placeholder="筛选作业（名称 / 课程）">
        <button class="ghost" id="hwAllTodo">全选未完成</button>
        <button class="ghost" id="hwAll">全选</button>
        <button class="ghost" id="hwNone">全不选</button>
      </div>
      <div class="hwtip">
        <strong>「重做」是什么：</strong>已完成的作业默认不勾；勾上「重做」会把上一次作答
        覆盖重来（平台按最高成绩保留）。<span class="hw-warn">每份作业重做次数有限。</span>
      </div>
      <div class="hwlist" id="hwList"></div>
      <div class="hwfoot">
        <span class="msg" id="hwMsg"></span>
        <span class="sp"></span>
        <button class="ghost" id="hwRun">开始做选中的作业</button>
      </div>
    </div>
  </div>

  <!-- 考试清单弹窗（2026-09-27 新增）：把全部考试扫出来看，只读，不提交 -->
  <div class="hwmodal hide" id="examModal" role="dialog" aria-modal="true">
    <div class="hwbox">
      <div class="hwhead">
        <strong>考试清单</strong>
        <span class="sp"></span>
        <span class="tag" id="examCount">共 0 场</span>
        <button class="ghost" id="examClose">关闭</button>
      </div>
      <div class="hwbar">
        <input type="text" id="examFilter" placeholder="筛选考试（名称 / 课程）">
      </div>
      <div class="hwtip">
        <strong>这是只读清单</strong>：只把考试扫出来给你看，
        <span class="hw-warn">不会自动做任何考试</span>（考试通常只有一次机会）。
      </div>
      <div class="hwtip" style="background:#fff7ed;border-color:#fed7aa">
        <strong>找不到「超星班」这类课程？</strong>
        它们不在课程列表接口里，把 <code>courseId</code> + <code>clazzId</code>
        填在下面登记即可（<code>cpi</code> 会自动解析），登记后会单独列在清单底部。
      </div>
      <div class="hwbar" style="flex-wrap:wrap;gap:6px">
        <input type="text" id="exCid" placeholder="courseId" style="width:120px">
        <input type="text" id="exClz" placeholder="clazzId" style="width:120px">
        <input type="text" id="exTitle" placeholder="名称（可选）" style="width:120px">
        <button class="ghost" id="exAdd">登记课程</button>
      </div>
      <div id="examExtraList" style="margin:2px 0 6px"></div>
      <!-- 开考前只读预检结果（2026-10-12）：点「预检」后显示在这里 -->
      <div id="examPre" class="hide"></div>
      <div class="hwlist" id="examList"></div>
      <div class="hwfoot">
        <span class="msg" id="examMsg"></span>
      </div>
    </div>
  </div>

  <!-- 运行中「添加课程」弹窗（2026-10-01 新增）：进度页直接追加课程，
       不必「返回选课」重选一遍（重选会重建整个任务、容易漏掉原来在刷的课）。 -->
  <div class="hwmodal hide" id="addModal" role="dialog" aria-modal="true">
    <div class="hwbox">
      <div class="hwhead">
        <strong>添加课程到当前任务</strong>
        <span class="sp"></span>
        <span class="tag" id="addCount">共 0 门</span>
        <button class="ghost" id="addClose">关闭</button>
      </div>
      <div class="hwbar">
        <input type="text" id="addFilter" placeholder="筛选课程（名称 / 课程ID）">
        <button class="ghost" id="addNone">全不选</button>
      </div>
      <div class="hwtip">
        已在刷的课程会标「已在刷」并默认勾上；<strong>新勾的课程按「视频 + 章节测验」加入</strong>。
        提交后当前任务会先暂停、再用合并后的清单重新开始
        —— 已上报的进度不受影响，恢复后从没刷完的地方接着跑。
      </div>
      <div class="hwlist" id="addList"></div>
      <div class="hwfoot">
        <span class="msg" id="addMsg"></span>
        <span class="sp"></span>
        <button class="ghost" id="addRun">加入并继续</button>
      </div>
    </div>
  </div>

  <div class="card hide" id="step3">
    <div class="bar">
      <strong>运行进度</strong><span class="sp"></span>
      <span id="stat" class="tag">运行中</span>
      <button id="btnPause" class="hide">暂停刷课</button>
      <button class="ghost" id="btnBack">返回选课</button>
      <button class="ghost" id="btnAddCourse"
              title="把别的课并进当前任务，不必返回选课页重选">＋ 添加课程</button>
    </div>

    <div class="ov">
      <div class="top">
        <span class="big"><span id="pDone">0</span><span style="color:#9ca3af">/</span><span id="pTotal">0</span></span>
        <span class="pct" id="pPct">0%</span>
        <span class="cap">平台已认进度</span>
      </div>
      <div class="pbar"><div class="pfill" id="pFill"></div></div>
      <div class="sub" id="pNote"></div>
      <div class="sub" id="pEta" style="color:#0d9488;font-weight:600"></div>
      <!-- 🔴 2026-10-13（何同学要求）：**考试独立进度条**，显示在刷课大进度条下方。
           起因：考试进行中界面只有刷课的 `56/56 100%`，看不出考试跑到第几题，
           无法判断"到底有没有在跑"。刷题与考试同时勾选时两条并存、互不干扰。 -->
      <div id="examProgBox" class="hide" style="margin-top:10px">
        <div style="display:flex;align-items:baseline;gap:8px">
          <b id="examProgNum">0 / 0</b>
          <span class="pct" id="examProgPct">0%</span>
          <span class="cap" id="examProgTitle">考试作答</span>
        </div>
        <div class="pbar"><div class="pfill" id="examProgFill"></div></div>
        <div class="sub" id="examProgNote"></div>
      </div>
    </div>

    <!-- 本轮任务结果（2026-09-28 新增）：把每场考试/作业的成败与原因显示出来。
         此前这些只写进日志文件，界面上「已结束」却不说为什么，
         用户看不到就以为"没有日志"。 -->
    <div class="ov hide" id="taskRes">
      <div class="trhead" id="taskResHead">本轮任务结果</div>
      <div id="taskResList"></div>
    </div>

    <div class="curbox hide" id="curBox">
      <div class="curname" id="curName"></div>
      <div class="pbar thin"><div class="pfill blue" id="curFill"></div></div>
      <div class="curmeta" id="curMeta"></div>
    </div>

    <div class="cboard hide" id="cBoard">
      <div class="riskbar hide" id="riskBar">
        <span class="ri">⚠</span>
        <span class="rb">
          <div class="rt" id="riskTitle">本轮被平台风控中止</div>
          <div class="rs" id="riskSub"></div>
        </span>
      </div>
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

    <!-- 运行中快捷调参（2026-09-29）：用户要求"不用退出去切换账号、重新登录、重新刷"。
         实测事实：jobs / speed 是**任务级**参数（TaskHandler 启动时读一次），
         运行中改必须重建任务；所以这里明说"并发/倍速改动才需要重建"，
         并走 pause→resume 的既有路径，进度不丢。 -->
    <div class="bar" style="margin-bottom:8px;flex-wrap:wrap;gap:8px">
      <button class="ghost" id="btnLive">运行参数</button>
      <span class="hint" id="liveHint"></span>
      <span class="sp"></span>
      <button class="ghost" id="btnLog">展开运行日志</button>
    </div>
    <div class="submitbar hide" id="liveBox" style="margin-bottom:8px">
      <label class="thr">并发章节数
        <input type="number" id="liveJobs" min="1" max="8" step="1">
      </label>
      <label class="thr">播放倍速
        <input type="number" id="liveSpeed" min="1" max="2" step="0.5">
      </label>
      <span class="sp"></span>
      <button class="ghost" id="btnLiveApply">应用</button>
    </div>
    <div class="msg" id="liveMsg" style="margin:0 0 8px"></div>
    <div class="msg" id="msg3"></div>
    <div id="log" class="hide"></div>
  </div>
</div>

<script>
const BASE = "__BASE__";
const $ = id => document.getElementById(id);
let courses = [], timer = null, got = -1, pulling = false;
// 「添加课程」弹窗用：全部课程 + 当前任务已在刷的 courseId
let addCourses = [], addCur = new Set();

/* ---- 2026-09-24：环境动效与指针反馈 ----
   只写 CSS 变量 / 加 class / 注入临时 span，不改任何既有逻辑与 DOM 结构。
   细指针（鼠标）才启用光标光晕与磁吸；触屏跳过，避免无谓开销。
   ⚠️ 按下时必须停掉磁吸，否则 --pull-x/--pull-y 会把按钮"拽住"，
   视觉上抵掉 :active 的下沉，手感变成"点了没反应"。 */
(function(){
  const fine = window.matchMedia('(hover:hover) and (pointer:fine)').matches;
  if (!fine) return;
  document.body.classList.add('pointerFine');

  let raf = 0, px = 0, py = 0;
  window.addEventListener('pointermove', e => {
    px = e.clientX; py = e.clientY;
    if (raf) return;                        // 每帧最多写一次
    raf = requestAnimationFrame(() => {
      raf = 0;
      const s = document.documentElement.style;
      s.setProperty('--mx', px + 'px');
      s.setProperty('--my', py + 'px');
    });
  }, {passive:true});

  // 磁吸：实心主按钮被光标轻微"吸"过去，上限 4px（太大就晕）
  let ptrDown = false;
  document.addEventListener('pointerdown', () => { ptrDown = true; }, {passive:true});
  document.addEventListener('pointerup', () => { ptrDown = false; }, {passive:true});
  document.addEventListener('pointercancel', () => { ptrDown = false; }, {passive:true});
  document.addEventListener('pointermove', e => {
    if (ptrDown || (e.buttons & 1)) return;  // 按下中不磁吸
    const b = e.target && e.target.closest && e.target.closest('button');
    if (!b || b.disabled) return;
    if (b.classList.contains('ghost') || b.classList.contains('mini')
        || b.classList.contains('eye')) return;   // 只有实心主按钮磁吸
    const r = b.getBoundingClientRect();
    const dx = Math.max(-4, Math.min(4, (e.clientX - (r.left + r.width / 2)) * 0.14));
    const dy = Math.max(-4, Math.min(4, (e.clientY - (r.top + r.height / 2)) * 0.14));
    b.style.setProperty('--pull-x', dx.toFixed(2) + 'px');
    b.style.setProperty('--pull-y', dy.toFixed(2) + 'px');
  }, {passive:true});
  document.addEventListener('pointerout', e => {
    const b = e.target && e.target.closest && e.target.closest('button');
    if (!b) return;
    b.style.setProperty('--pull-x', '0px');
    b.style.setProperty('--pull-y', '0px');
  }, {passive:true});
})();

/* 点击波纹：从**点击落点**扩散（不是从中心，那样会显得假）。
   ⚠️ 插入 DOM 会触发布局重算，若同步做会推迟 :active 的下沉，
   所以整段包进 requestAnimationFrame。 */
(function(){
  document.addEventListener('pointerdown', e => {
    const b = e.target && e.target.closest && e.target.closest('button');
    if (!b || b.disabled) return;
    if (b.classList.contains('eye')) return;       // 眼睛按钮太小，不加波纹
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
      // 波纹期间清掉磁吸偏移，保证按压下沉是"实"的
      b.style.setProperty('--pull-x', '0px');
      b.style.setProperty('--pull-y', '0px');
    });
  }, {passive:true});
})();

/* 成功反馈：按钮短暂变绿打勾，再复位（三点式：忙碌 → 成功 → 复位） */
function flashDone(btn, ms) {
  if (!btn) return;
  btn.classList.add('done');
  setTimeout(() => btn.classList.remove('done'), ms || 900);
}

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

// 🔴 2026-09-27：高级 Key 池折叠开关。
//    用户要求登录页不再单独展示 AI 配置（已内置池，登录即自动带上），
//    所以这里默认收起；想看/想覆盖的人点一下才展开。
$('aiPoolLink').onclick = () => {
  const adv = $('aiPoolAdv'), open = adv.classList.toggle('hide') === false;
  $('aiPoolLink').textContent = (open ? '▾ ' : '▸ ') + '高级：自定义 Key 池（一般不需要动）';
  if (open) $('aiPoolLink').dataset.open = '1'; else delete $('aiPoolLink').dataset.open;
};

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
        ai_key: $('aiKey').value, ai_keys: ($('aiKeys') ? $('aiKeys').value : ''),
        ai_model: $('aiModel').value})}).then(r => r.json());
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
  return u + (u.indexOf('?') === -1 ? '?' : '&') + 'use=local,buguake,icodef';
}
const ADAPTER_URL_DEFAULT = 'http://127.0.0.1:8060/adapter-service/search?use=local,buguake,icodef';

function fillProfile(i) {
  const p = profiles[i];
  if (!p) return;
  $('user').value = p.username || '';
  $('pwd').value = p.password || '';
  // 2026-09-29（用户要求）：切账号后 AI 兜底 / 题库**默认就是全开**，不用每次手点。
  // 原写法 `|| 'none'`（题库）与 `!!p.ai_fallback`（兜底）在**档案缺这两个字段时**
  // 会退化成"不使用题库 + 不勾 AI 兜底"—— 新加账号必踩（用户原话："每次都要重新点"）。
  $('tikuType').value = p.tiku_type || 'adapter';
  $('adapterUrl').value = fixAdapterUrl(p.adapter_url);
  $('aiEndpoint').value = p.ai_endpoint || '';
  $('aiKey').value = p.ai_key || '';
  if ($('aiKeys')) $('aiKeys').value = p.ai_keys || '';
  $('aiModel').value = p.ai_model || '';
  $('coverRate').value = p.cover_rate || 0.9;
  // 同上：字段缺失时**默认勾上**（只有明确存了 false 才取消）
  $('aiFallback').checked = (p.ai_fallback === undefined || p.ai_fallback === null)
    ? true : !!p.ai_fallback;
  // 同上：字段缺失时**默认勾上**（只有明确存了 false 才取消）
  $('autoSubmit').checked = p.auto_submit !== false;
  // 🔴 2026-10-12（何同学要求）：考试开关**独立**，且默认关 —— 只暂存不交卷。
  //    旧档案没有 exam_auto_submit 字段时一律落到"不交卷"（宁可不交，不要误交）。
  if ($('autoSubmitExam')) $('autoSubmitExam').checked = p.exam_auto_submit === true;
  updateTikuUI();
  syncSubmitBar();          // 🔴 2026-09-26：切档案也要同步主界面提交开关
}

function fillParams(p) {
  if (!p) return;
  if (p.tiku_type) $('tikuType').value = p.tiku_type;
  if (p.adapter_url) $('adapterUrl').value = fixAdapterUrl(p.adapter_url);
  if (p.ai_endpoint) $('aiEndpoint').value = p.ai_endpoint;
  if (p.ai_key) $('aiKey').value = p.ai_key;
  if (p.ai_keys && $('aiKeys')) $('aiKeys').value = p.ai_keys;
  if (p.ai_model) $('aiModel').value = p.ai_model;
  if (p.cover_rate) $('coverRate').value = p.cover_rate;
  if (p.speed) $('speed').value = p.speed;
  if (p.jobs) $('jobs').value = p.jobs;
  // 同上：字段缺失时**默认勾上**（只有明确存了 false 才取消）
  $('aiFallback').checked = (p.ai_fallback === undefined || p.ai_fallback === null)
    ? true : !!p.ai_fallback;
  // 同上：字段缺失时**默认勾上**（只有明确存了 false 才取消）
  $('autoSubmit').checked = p.auto_submit !== false;
  // 🔴 2026-10-12（何同学要求）：考试开关**独立**，且默认关 —— 只暂存不交卷。
  //    旧档案没有 exam_auto_submit 字段时一律落到"不交卷"（宁可不交，不要误交）。
  if ($('autoSubmitExam')) $('autoSubmitExam').checked = p.exam_auto_submit === true;
  updateTikuUI();
  syncSubmitBar();          // 🔴 2026-09-26：登录页参数 → 主界面提交开关
}

// ---- 🔴 2026-09-26 新增：主界面「提交方式」开关 ----
// 背景：提交开关原先**只在登录页**（#autoSubmit），登录后进主界面就再也
// 找不到入口 —— 用户明确反映「我让你加我自己选择提不提交这一项，为什么没有？」。
// 这里把它常驻到主界面，并且与登录页双向同步（改哪边都生效）。
function syncSubmitBar() {
  const cb = $('autoSubmitMain'), thr = $('coverRateMain');
  if (!cb) return;
  cb.checked = $('autoSubmit').checked;
  thr.value = $('coverRate').value;
  updateSubmitHint();
}

function updateSubmitHint() {
  const w = $('autoSubmitMain'), ex = $('autoSubmitExam');
  const hint = $('submitHint'), tip = $('submitTip');
  if (!w || !hint) return;
  const won = w.checked;                  // 作业开关（默认开）
  const eon = ex ? ex.checked : false;    // 考试开关（2026-10-12 起**默认关** = 只暂存）
  // 🔴 2026-09-26：文案必须**随状态互斥**，不能两种模式都写在一段里。
  // 🔴 2026-10-12：拆成「作业 / 考试」两条独立开关 —— 考试只有一次机会，
  //    默认不替用户交卷；作业维持原有的自动提交默认。
  hint.textContent = `作业 ${won ? '达标即交卷' : '只存草稿'}`
    + ` ｜ 考试 ${eon ? '达标即交卷' : '只暂存不交卷'}`;
  hint.className = 'hint' + ((won && eon) ? '' : ' manual');
  const thr = $('coverRateMain');
  if (thr) thr.title = '覆盖率 ≥ 该门槛才交卷；低于门槛会保存草稿等你处理'
    + '（只对已开启「自动提交」的那一方生效）';
  // 说明行：把"当前模式做什么"写清楚，只保留一条，避免自相矛盾
  const desc = $('submitDesc');
  if (desc) {
    desc.className = 'submitdesc' + ((won && eon) ? '' : ' manual');
    desc.innerHTML =
      `<b>作业：${won ? '自动提交' : '审核提交（只存草稿）'}</b> —— `
      + (won ? '答案覆盖率达标就直接交卷，全程无需你介入。'
             : '只暂存草稿，你自己到学习通核对后交卷。')
      + '<br><b>考试：' + (eon ? '自动提交' : '审核提交（只存草稿）') + '</b> —— '
      + (eon ? '答完达标即自动交卷。'
             : '<b>默认只暂存草稿、不替你交卷</b>。考试机会在进入考场时已消耗，'
               + '请到学习通核对后手动交卷。');
  }
}

if ($('autoSubmitMain')) {
  $('autoSubmitMain').onchange = () => {
    // 双向同步：主界面改了，登录页那份也一起改（下次登录/保存沿用）
    $('autoSubmit').checked = $('autoSubmitMain').checked;
    updateSubmitHint();
  };
  // 2026-10-12：考试开关独立 —— 不联动登录页（那边是作业链路），改完只刷新提示。
  if ($('autoSubmitExam')) $('autoSubmitExam').onchange = updateSubmitHint;
  $('coverRateMain').oninput = () => { $('coverRate').value = $('coverRateMain').value; };
  updateSubmitHint();
}

// ---- 运行中快捷调参（2026-09-29）----
// 用户诉求："加一个快捷调节这些东西的入口。要不然每次都需要退出去切换账号
// 才能调节，还要重新登录，重新刷。"
// 事实：jobs / speed 是任务级参数，**必须重建任务**才生效 —— 走 pause→resume，
// 和手动暂停/继续同一条路径，进度不丢（当前视频会重播一次）。
// cover_rate / auto_submit 是每题组实时读的，改完立即生效。
async function loadLiveParams() {
  try {
    const d = await fetch(BASE + '/api/live_params').then(r => r.json());
    if (!d.ok) return;
    if ($('liveJobs')) $('liveJobs').value = d.jobs;
    if ($('liveSpeed')) $('liveSpeed').value = d.speed;
    const h = $('liveHint');
    if (h) {
      h.textContent = d.worker_num
        ? ('当前实际 ' + d.worker_num + ' 个并发 worker' +
           (d.paused ? ' · 已暂停' : ''))
        : '';
    }
  } catch (e) { /* 展示用旁路，失败不打扰 */ }
}

if ($('btnLive')) {
  $('btnLive').onclick = () => {
    const b = $('liveBox');
    b.classList.toggle('hide');
    if (!b.classList.contains('hide')) loadLiveParams();
  };
}

if ($('btnLiveApply')) {
  $('btnLiveApply').onclick = async () => {
    const body = { apply_now: true };
    if ($('liveJobs')) body.jobs = parseInt($('liveJobs').value, 10) || 1;
    if ($('liveSpeed')) body.speed = parseFloat($('liveSpeed').value) || 1;
    $('liveMsg').textContent = '应用中…（会暂停并续跑，进度不丢）';
    try {
      const d = await fetch(BASE + '/api/live_params', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      }).then(r => r.json());
      if (!d.ok) {
        $('liveMsg').textContent = '失败：' + (d.msg || '未知错误');
        return;
      }
      const parts = [];
      if ((d.changed || []).length) parts.push('已保存 ' + d.changed.join(' · '));
      else parts.push('无变化');
      if (d.restarted) parts.push('已重建任务（当前视频会重播一次，进度不丢）');
      $('liveMsg').textContent = parts.join('；');
      setTimeout(loadLiveParams, 2500);
    } catch (e) {
      $('liveMsg').textContent = '请求失败：' + e.message;
    }
  };
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
  if (!r) return r;
  // 🔴 2026-09-28（用户要求）：**非管理员一律只读** —— 进去就是进度界面，
  //    不给任何操作入口（暂停/选课/开始都不出现）。
  //    原先只有 viewer 走这条，普通用户（账密登录）仍能操作 → 现在统一。
  const ro = !!(r.role && r.role !== 'admin');
  if (ro) {
    document.body.classList.add('ro-mode');
    // 🔴 2026-09-28（用户要求）：普通登录**直接删掉这两颗按钮**（不是仅靠 CSS 隐藏，
    //    免得以后选择器一改就漏出来）：
    //    · 「退出服务」调 /api/shutdown?all=1 —— 会把**共享题库**一起杀掉，
    //      其它实例随即失去题库来源（用户原话："会终止所有服务吧？我记得会的"）
    //    · 「返回选课」—— 刷的就是管理员已经选好的那几门课，没必要再选一次
    ['btnQuit', 'btnBack'].forEach(id => {
      const el = $(id);
      if (el && el.parentNode) el.parentNode.removeChild(el);
    });
  }
  if (bar) {
    if (ro) {
      bar.className = 'card';
      bar.style.cssText = 'background:#fff7ed;border-color:#fed7aa;color:#b45309';
      bar.textContent = '当前身份只有查看权限：只能看进度和日志，'
        + '操作请用管理员口令登录。';
    } else if (!r.local) {
      bar.className = 'card';
      bar.style.cssText = 'background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8';
      bar.textContent = '已远程登录：' + (r.who || '') +
        '（如需退出请到登录页重新登录）';
    }
  }
  r.readonly = ro;
  return r;
}

// ---- 刷新恢复 + 自动预填/自动登录 ----
async function boot() {
  const _role = await checkRole();
  // 🔴 2026-09-28：只读身份**直接落到「运行进度」**并开始拉取，
  //    不加载账号档案/选课界面（用户要求"进去就是进度的界面"）。
  if (_role && _role.readonly) {
    showStep(3);
    startPull();
    return;
  }
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
      showStep(2);
      // 落在选课页时补拉一次进度：跑完的实例不会启动轮询，
      // 但「上次任务已结束」那行提示要靠 progress 里的平台数据才算得出来
      // （只调 renderLastTaskTag，不调 renderProgress —— 见它的注释）。
      fetch(BASE + '/api/logs?after=999999999').then(r => r.json())
        .then(x => renderLastTaskTag(x.progress || {}))
        .catch(() => {});
      taskScan();                        // ★ 刷新后也自动扫描（复用后台进度）
      return;
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
      // 区分两种来源：有账号档案 = 上次的登录记录；无档案 = 网关给新实例
      // 预置的默认题库/AI 配置（用户只需补账密，不必碰题库设置）
      $('msg1').textContent = profiles.length
        ? '已自动填入上次保存的配置，确认后点「登录并获取课程」'
        : '题库与 AI 已预置好，填手机号和密码即可登录';
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
    ai_keys: ($('aiKeys') ? $('aiKeys').value.trim() : ''),
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
    flashDone($('btnLogin'));            // 成功反馈：短暂变绿打勾
    courses = r.courses;
    render();
    showAccount(r.account);
    $('msg1').textContent = '';
    showStep(2);
    taskScan();                          // ★ 登录后自动扫描作业 + 考试
  } catch (e) { $('msg1').textContent = '请求失败: ' + e; }
  $('btnLogin').disabled = false;
};

// 用户手动重扫：清掉逐条勾选状态，重新走一遍
$('btnRescan').onclick = () => {
  Object.keys(taskPick).forEach(k => delete taskPick[k]);
  taskScan();
};

// 刷新课程列表（2026-09-30 用户要求）：平台刚发的课原先刷不出来，
// 只能「切换账号」重登才看得到。这里给一个专职按钮。
// ⚠️ 与「重新扫描」区分：那个扫的是作业和考试，不动课程列表。
$('btnRefreshCourses').onclick = async () => {
  const b = $('btnRefreshCourses');
  if (b.disabled) return;
  const old = b.textContent;
  b.disabled = true; b.dataset.busy = '1'; b.textContent = '刷新中…';
  try {
    const r = await fetch(BASE + '/api/courses/refresh', { method: 'POST' })
      .then(x => x.json());
    if (!r.ok) { alert(r.msg || '刷新失败'); return; }
    const oldIds = courses.map(c => String(c.courseId));
    // 保留「课程仍在」的勾选（taskKey 首段就是 courseId，键稳定）
    const keep = {};
    Object.keys(taskPick).forEach(k => {
      if (oldIds.includes(k.split('|')[0])) keep[k] = taskPick[k];
    });
    courses = r.courses || [];
    Object.keys(taskPick).forEach(k => delete taskPick[k]);
    Object.assign(taskPick, keep);
    render();
    flashDone(b, 900);
    const added = courses.filter(c => !oldIds.includes(String(c.courseId)));
    if (added.length) {
      alert('课程已刷新：新增 ' + added.length + ' 门\\n\\n'
            + added.map(c => '· ' + c.title).join('\\n'));
    }
  } catch (e) {
    alert('刷新失败：' + (e && e.message || e));
  } finally {
    b.disabled = false; delete b.dataset.busy; b.textContent = old;
  }
};

// ---- 课程列表 ----
// 每门课可以单独勾「视频 / 测验 / 作业」三个维度，也可用上方按钮批量操作。
// 默认**全部不勾**（2026-09-14 改）：此前默认把每门课的"刷课"都预勾上，
// 登录后直接点「开始」会把整个账号的课程全刷一遍（含不该动的正式课）。
//
// 2026-09-15 拆维度：原「刷课」= 视频 + 章节测验（两者同属章节任务点，绑在一起跑）。
// 用户要求能单做测验而不刷视频，故拆成两个独立勾选框。
//
// 🔴 2026-09-26 按用户实机截图重构**分组结构**：
// 超星 App 的课程内部是这样组织的（用户截图确认）——
//   「任务」tab : 讨论 / 【作业 ｜ 考试】（点进去是二级切换）/ …
//   「章节」tab : 视频 + 章节测验（独立的一套，进度 0/55）
// 即：**作业和考试在一边，视频和章节在另一边**。
// 所以卡片里现在放两个"组"，每组自带展开按钮 + 二级切换 tab：
//   ┌ ① 章节任务   视频 ｜ 章节测验   [▾]
//   └ ② 作业 / 考试 作业 ｜ 考试       [▾]
// 组内只勾一个维度 = 只做那一类；一个都不勾 = 这门课不提交。
function render() {
  $('list').innerHTML = courses.map((c, i) => {
    // 🔴 2026-09-28：额外课程（超星班等）不在课程列表接口里，**没有章节数据**，
    //    所以不给它"章节任务"组（勾了也刷不了），只保留「作业 / 考试」。
    const extra = !!c.extra;
    return `<div class="course-wrap" data-i="${i}">
     <label class="course" data-i="${i}">
       <input type="checkbox" class="ck-video" data-i="${i}">
       <div class="cbody">
         <div class="t">${escapeHtml(c.title)}${extra
           ? ' <span class="tagx" title="不在课程列表接口里，只能做作业/考试">额外课程</span>'
           : ''}</div>
         <div class="m">课程ID ${c.courseId} · 班级ID ${c.clazzId}${extra
           ? ' · 无章节数据，仅支持作业 / 考试' : ''}</div>
         ${extra ? '' : `<div class="grp">
           <span class="glab">章节任务</span>
           <span class="mode"><input type="checkbox" class="ck-video2" data-i="${i}">视频</span>
           <span class="mode"><input type="checkbox" class="ck-quiz" data-i="${i}">章节测验</span>
           <span class="gexp" data-i="${i}" data-g="chapter" title="展开章节明细">▾</span>
         </div>`}
         <div class="grp">
           <span class="glab">任务</span>
           <span class="mode"><input type="checkbox" class="ck-work" data-i="${i}">作业</span>
           <span class="mode"><input type="checkbox" class="ck-exam" data-i="${i}">考试</span>
           <span class="gexp" data-i="${i}" data-g="task" title="展开作业/考试明细">▾</span>
         </div>
       </div>
     </label>
     <div class="cdetail hide" data-i="${i}" data-g="task"></div>
    </div>`;
  }).join('');
  // ---- 四个维度勾选框的统一联动 ----
  // 任一维度勾上 → 主勾选框（ck-video）也点亮，保证该课程会被提交；
  // 主勾选框取消 → 同步 ck-video2（表示"不刷视频"）。
  // 🔴 2026-09-26：这里改调 taskSyncCourse —— 它同时负责"卡片 → 明细"回写，
  // 否则用户勾了卡片上的「作业」，明细里那几份不会被选中，提交清单就是空的。
  const syncCourse = i => {
    const q = sel => $('list').querySelector(`input.${sel}[data-i="${i}"]`);
    const main = q('ck-video');
    const v2 = q('ck-video2');
    if (!main) return;
    if (v2) v2.checked = main.checked;
    // 明细还没渲染（扫描未完成）时 taskSyncCourse 会空跑，但 update() 仍要刷新
    // 卡片上的框被点了 → 方向是 card（把选择铺开到明细）
    if (taskByCourse(courseIdOf(i))) taskSyncCourse(i, 'card');
    // 🔴 2026-09-28：考试清单的同步**不受 taskData 有无影响**，必须独立调用。
    //    原来它挂在 taskSyncCourse 里，而上面那行在"还没扫到数据"时不执行
    //    → 勾了卡片「考试」但考试清单没反应（盲区）。
    exSyncFromCard(i);
    const any = main.checked || (q('ck-quiz') || {}).checked
             || (q('ck-work') || {}).checked || (q('ck-exam') || {}).checked;
    const wrap = $('list').querySelector(`.course-wrap[data-i="${i}"]`);
    const lbl = $('list').querySelector(`.course[data-i="${i}"]`);
    if (lbl) lbl.classList.toggle('on', !!any);
    if (wrap) wrap.classList.toggle('on', !!any);
    update();
  };
  ['ck-video', 'ck-quiz', 'ck-work', 'ck-exam'].forEach(cls => {
    $('list').querySelectorAll('input.' + cls).forEach(cb => {
      cb.onchange = () => syncCourse(cb.dataset.i);
    });
  });
  $('list').querySelectorAll('input.ck-video2').forEach(cb => {
    cb.onchange = () => {
      const i = cb.dataset.i;
      const main = $('list').querySelector(`input.ck-video[data-i="${i}"]`);
      if (main) main.checked = cb.checked;
      syncCourse(i);
    };
  });
  // 点卡片空白处 = 切换该课「刷视频」
  $('list').querySelectorAll('.course').forEach(el => el.onclick = ev => {
    if (ev.target.tagName === 'INPUT') return;
    if (ev.target.closest('.gexp')) return;
    const cb = el.querySelector('input.ck-video'); cb.checked = !cb.checked;
    cb.dispatchEvent(new Event('change'));
  });
  // 展开/收起明细（每门课两个组各自独立展开）
  $('list').querySelectorAll('.gexp').forEach(btn => {
    btn.onclick = ev => {
      ev.preventDefault(); ev.stopPropagation();
      const i = btn.dataset.i, g = btn.dataset.g;
      const box = $('list').querySelector(`.cdetail[data-i="${i}"][data-g="task"]`);
      if (g !== 'task' || !box) return;   // 章节明细走原章节列表，不在这里展开
      box.classList.toggle('hide');
      btn.textContent = box.classList.contains('hide') ? '▾' : '▴';
    };
  });
  taskRenderAll();      // 若扫描结果已在，直接填充明细
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
// 课程级汇总：这门课要不要刷视频 / 测验 / 作业。
//
// 🔴 2026-09-26 定稿：**只读卡片级复选框**，不看明细 taskPick。
// 原因：taskPick 是三态的，"未表态"对未完成作业默认返回 true —— 拿它做
// "这门课要不要做"的判据，会把所有有未完成作业的课全算进来（审计实测）。
// 明细 → 卡片的联动由 `taskSyncCourse` 负责：用户在明细里勾了任意一份，
// 卡片级的「作业」和主勾选框都会被点亮，所以这里读卡片级**已经覆盖**了
// "只勾明细不碰卡片"的场景，数据流是单向的、没有二义性。
function collectJobs() {
  return [...$('list').querySelectorAll('.course')].map(el => {
    const i = +el.dataset.i;
    const v = el.querySelector('input.ck-video2');
    const q = el.querySelector('input.ck-quiz');
    const w = el.querySelector('input.ck-work');
    const e = el.querySelector('input.ck-exam');
    return {i, video: !!(v && v.checked), quiz: !!(q && q.checked),
            work: !!(w && w.checked), exam: !!(e && e.checked)};
  }).filter(x => x.video || x.quiz || x.work || x.exam);
}

// 收集精确到「份」的作业 / 考试清单：{courseId: [条目,...]}
//
// 🔴 判定顺序（2026-09-26 定稿，这里是唯一真源）：
//   ① 这门课**整体要不要管**？
//        卡片级勾了  → 管（用户在卡片上选了这门课）
//        卡片级没勾  → **整门跳过**（这是"不选这门课"的唯一表达）
//      ⚠ 不能拿 taskPickHas 当"管不管"的判据 —— 三态默认对"未完成的"
//      返回 true，于是所有有未完成作业的课都会被算进来。审计抓到的就是这个。
//   ② 管了之后**做哪几份**？只看该课的明细勾选（taskPickGet）。
function collectTaskItems(type) {
  const out = {};
  courses.forEach((c, i) => {
    const cid = String(c.courseId || '');
    const box = $('list').querySelector(`input.ck-${type}[data-i="${i}"]`);
    if (!box || !box.checked) return;      // ① 卡片级没勾 → 整门不管
    const d = taskByCourse(cid);
    if (!d) return;
    const arr = (type === 'work' ? d.works : d.exams) || [];
    const picked = [];
    arr.forEach((it, k) => {
      if (!taskPickGet(cid, type, k, it)) return;   // ② 逐份看明细勾选
      picked.push({
        name: it.name, status: it.status, taskrefid: it.taskrefid,
        enc_task: it.enc_task || '', href: it.href || '', cpi: it.cpi || '',
        userid: it.userid || '', clazzid: it.clazzid || '',
        redo: !!it.redo
      });
    });
    if (picked.length) out[cid] = picked;
  });
  return out;
}

// ============ 选择状态的唯一入口（2026-09-26 重构）============
// 🔴 背景：此前有**三套互不同步**的勾选状态 —— 卡片级复选框(DOM)、
// 明细级 taskPick(内存)、作业清单弹窗(.hw-ck)。用户实测两个后果：
//   ① 明细里只勾 2 份 → 点开始却把整门课的作业全做（卡片级只传课程下标）
//   ② 只勾明细不勾卡片 → 点开始被判定"没选任何课"，压根不启动
// 现在收敛成一条规则：**`taskPick` 是唯一真源，卡片级复选框只是它的视图**。
//   · taskPick 有该课的任意作业/考试条目 → 自动点亮卡片的「作业/考试」
//   · 卡片的「作业」被点亮/取消 → 同步该课全部明细条目
//   · 点「开始」时按 taskPick 生成精确到份的 work_items 交给后端
//
// taskPick 语义（三态）：
//   键不存在 = 尚未表态 → 按「默认做未完成的」解释（见 taskPickGet）
//   true     = 明确要做这一份
//   false    = 明确不做这一份
// 用三态是因为扫描完成后会自动预勾"未完成"的那批，用户若把某份取消，
// 必须能记住"这份是用户主动排除的"，否则每次重渲染又会被预勾回来。
function taskKey(cid, type, i) { return `${cid}|${type}|${i}`; }

// 考试终态判据：完成类 + 「已过期 / 已取消 / 已关闭」+ relationId=0（机会已用完）
// 2026-09-26 实测：两个真实账号的考试全是「已过期」或 relationId=0，
// 若不算终态会显示成"N 场未完成"，用户以为漏刷，实际平台已关入口。
// 🔴 提升为全局：taskPickGet（明细勾选默认值）与 taskPoll（横幅统计）共用。
function examDone(x) {
  x = x || {};
  const st = x.status || '';
  if (/已完成|已批阅|已交卷|已互评|已过期|已取消|已关闭/.test(st)) return true;
  const rid = String(x.relation_id || x.test_user_relation_id || '');
  return rid === '' || rid === '0';
}

// 读某一条的**有效**勾选状态（含"未表态"的默认解释）。
//
// 🔴 关键：默认值只在"这门课**完全没人表过态**"时生效。
// 否则会出现这种错：用户在明细里只勾了 R-1，其余 5 份没碰 → 它们"未表态"
// → 按 todo 默认 true → 提交时整门课 6 份全做，用户的收窄完全失效
// （审计 B/D/G 实测就是这个）。
// 所以先问一句：这门课该类型下，用户表过态吗？
//   · 表过 → 没表态的一律 false（以用户的实际选择为准）
//   · 没表过 → 才用默认（未完成 true / 已完成 false）
function taskPickGet(cid, type, i, item) {
  const k = taskKey(cid, type, i);
  if (k in taskPick) return !!taskPick[k];
  if (!item) return false;
  if (taskPickTouched(cid, type)) return false;
  return type === 'work' ? !!item.todo : !examDone(item);
}

// 这门课该类型下用户是否表过态（有无任何显式 true/false）
function taskPickTouched(cid, type) {
  const pre = `${cid}|${type}|`;
  for (const k in taskPick) {
    if (k.startsWith(pre)) return true;
  }
  return false;
}

// 写某一条的勾选状态
function taskPickSet(cid, type, i, on) {
  taskPick[taskKey(cid, type, i)] = !!on;
}

function update() {
  const picked = collectJobs();
  const nv = picked.filter(x => x.video).length;
  const nq = picked.filter(x => x.quiz).length;
  const nw = picked.filter(x => x.work).length;
  $('picked').textContent =
    `已选 ${picked.length} 门（视频 ${nv} · 测验 ${nq} · 作业 ${nw}）`;
  $('picked').className = 'tag' + (picked.length ? ' ok' : '');
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
// ============ 批量勾选的作用范围（2026-09-24 修） ============
// 用户反馈：先勾了前 3 门课，再点「全选作业」，结果把没勾的课也全勾上了。
// 期望语义：**批量按钮只作用于「用户已经选中的课程」**，是给已选课程
// 批量补勾某个维度的快捷方式，而不是把整个账号的课都拉进来。
//
// 判定"已选中"：该课程已被整体勾选（主勾选框 ck-video 为 checked）
//   或 已勾了任一维度（视频/测验/作业 有任意一个 checked）。
// 这样三种入口都能被认到：
//   ① 点课程卡片/主勾选框 → 主勾选框 checked
//   ② 单独勾「测验」或「作业」 → 对应维度 checked（且主框也会被联动点亮）
//   ③ 上一轮批量操作的结果
function pickedCourses() {
  return [...$('list').querySelectorAll('.course')].filter(el => {
    if (el.classList.contains('hide')) return false;   // 同时尊重筛选
    const main = el.querySelector('input.ck-video');
    const v2 = el.querySelector('input.ck-video2');
    const q = el.querySelector('input.ck-quiz');
    const w = el.querySelector('input.ck-work');
    return (main && main.checked) || (v2 && v2.checked)
        || (q && q.checked) || (w && w.checked);
  });
}
// 「全选」= 选中**当前筛选出的**全部课程（保持原有语义，仍是"从头全选"）
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
// 全选/全不选作业（不动视频、测验勾选）
// ⚠️ 只作用于**已被选中的课程**：没勾的课不会被这个按钮拉进来。
// 🔴 2026-09-26：改完后必须调 taskSyncCourse，让**明细列同步** ——
// 否则卡片勾上了、明细还是空的，提交的清单里一份作业都没有。
const setAllWork = v => {
  pickedCourses().forEach(el => {
    const v2 = el.querySelector('input.ck-video2');
    const q = el.querySelector('input.ck-quiz');
    const w = el.querySelector('input.ck-work');
    const main = el.querySelector('input.ck-video');
    if (w) { w.checked = v; }
    // 只勾作业不刷课：主勾选框也点亮，保证该课程会被提交
    if (v && w && w.checked && main) main.checked = true;
    el.classList.toggle('on',
      !!(v2 && v2.checked) || !!(q && q.checked) || !!(w && w.checked));
    // 卡片级批量按钮改了「作业」框 → 方向 card
    taskSyncCourse(el.dataset.i, 'card');
  });
  update();
};
// 全选/全不选测验（不动视频、作业勾选）
// ⚠️ 同上：只作用于**已被选中的课程**。
const setAllQuiz = v => {
  pickedCourses().forEach(el => {
    const v2 = el.querySelector('input.ck-video2');
    const q = el.querySelector('input.ck-quiz');
    const w = el.querySelector('input.ck-work');
    const main = el.querySelector('input.ck-video');
    if (q) { q.checked = v; }
    // 只勾测验不刷视频：主勾选框也点亮，保证该课程会被提交
    if (v && q && q.checked && main) main.checked = true;
    el.classList.toggle('on',
      !!(v2 && v2.checked) || !!(q && q.checked) || !!(w && w.checked));
  });
  update();
};
$('btnAll').onclick = () => setAll(true);
$('btnNone').onclick = () => setAll(false);
if ($('btnAllWork')) $('btnAllWork').onclick = () => setAllWork(true);
if ($('btnNoWork')) $('btnNoWork').onclick = () => setAllWork(false);
if ($('btnAllQuiz')) $('btnAllQuiz').onclick = () => setAllQuiz(true);
if ($('btnNoQuiz')) $('btnNoQuiz').onclick = () => setAllQuiz(false);
if ($('courseFilter')) $('courseFilter').oninput = applyFilter;

/* 手机端「批量勾选」折叠开关（2026-09-24）
   —— 桌面端 .bulkbar 是 display:contents，这个开关本身 display:none，
      所以点了也不会影响桌面布局；手机上才真正收起/展开。 */
(function(){
  const t = $('btnBulkToggle'), b = $('bulkBar');
  if (!t || !b) return;
  t.onclick = () => {
    const on = b.classList.toggle('on');
    t.setAttribute('aria-expanded', on ? 'true' : 'false');
    t.textContent = on ? '批量勾选 ▴' : '批量勾选 ▾';
  };
})();

// ---- 切换账号：清掉服务端登录态，回登录页 ----
// 🔴 2026-10-07（何同学定案）：原实现只打 `/api/logout`，而 logout **故意保留**
//    磁盘上的 `last_pick` / `resume` —— 于是**同一实例换登另一个账号时，
//    新号会继承上一个号的选课勾选与续刷标记**，界面看起来"还用着之前的进度"。
//    现在改为**两步询问**：先确认要换账号，再让用户选"是否连进度一起清掉"。
//      · 选「清进度」→ 调 `/api/clear_account`（把 profiles/last_params/
//        last_pick/resume 一并抹掉，回到全新未配置状态）
//      · 选「保进度」→ 仍走 `/api/logout`（只清登录态，保留勾选与续刷）
//    注意：任务运行中 clear_account 会被后端拒绝（返回 ok:false），
//    此时降级为 logout 并提示用户先停止任务。
$('btnLogout').onclick = async () => {
  if (!confirm('切换账号？当前登录状态会清除（正在刷课的任务不受影响）。')) return;
  const b = $('btnLogout');
  b.disabled = true; b.dataset.busy = '1';
  // ⚠️ 换行一律用数组 join —— 本文件是 Python 源码，字符串里的 \n 会被
  //    Python 先解析成真实换行，写进 JS 就成了跨行字符串（语法错误）。
  //    用 join 拼接最稳，不依赖转义层级。
  const clearProg = confirm([
    '是否同时清空该实例的「选课勾选 / 续刷进度」？',
    '',
    '· 点「确定」= 一起清掉（相当于换一个全新账号使用）',
    '· 点「取消」= 保留进度（只切换登录状态，下次登录仍看到原来的勾选）',
    '',
    '⚠️ 同一实例换登不同账号时，建议「确定」——否则新号会继承上一个号的进度。',
  ].join(String.fromCharCode(10)));
  let ok = true, m = '';
  try {
    const path = clearProg ? '/api/clear_account' : '/api/logout';
    const r = await fetch(BASE + path, {method: 'POST'});
    const j = await r.json().catch(() => ({}));
    if (j && j.ok === false) {
      // 典型：任务运行中，clear_account 被拒 → 降级 logout，保住"能切号"
      m = j.msg || '清空失败';
      ok = false;
      await fetch(BASE + '/api/logout', {method: 'POST'}).catch(() => {});
    }
  } catch (e) { /* 网络异常：仍回登录页 */ }
  b.disabled = false; delete b.dataset.busy;
  $('msg1').textContent = ok ? '' : (m + '（已切换登录状态，进度未清）');
  await loadProfiles();   // 重新拉一遍档案（可能刚归档了新账号）
  showStep(1);
};

// ==================== 作业清单弹窗（2026-09-26 新增） ====================
// 用户要求：不盲目开始做，先扫描全部课程的作业，进页面自己勾选做哪些。
// 🔴 2026-09-26 二次修正：弹窗**不再自己维护一套勾选状态**。
// 之前它有独立的 `.hw-ck`（扫完就重置），和卡片/明细互不相通 ——
// 用户在卡片明细里勾了 2 份、在弹窗里看到的却是完全另一套结果，
// 提交时以弹窗为准（`hwCollect()`），于是"只勾 2 份却做了 16 份"。
// 现在：勾选一律写进 `taskPick`（以 taskrefid 为桥对齐课程），
// 弹窗只负责"按份浏览 + 批量勾选"，和卡片明细共享同一份状态。
let hwData = null;      // 扫描结果缓存

const hwEsc = s => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;');

// 弹窗条目 → taskPick 的定位：按 courseid 找课程下标、按 taskrefid 找明细序号。
// 找不到（比如该课在卡片列表里被筛掉了）时返回 null，此时弹窗只能"只读浏览"。
function hwLocate(courseid, taskrefid) {
  const i = courses.findIndex(x => String(x.courseId) === String(courseid));
  if (i < 0) return null;
  const d = taskByCourse(courseid);
  if (!d) return null;
  const k = (d.works || []).findIndex(w =>
    String(w.taskrefid) === String(taskrefid));
  if (k < 0) return null;
  return {i, k, cid: String(courseid)};
}

function hwRender() {
  const box = $('hwList');
  if (!hwData) { box.innerHTML = '<div class="hwempty">正在扫描…</div>'; return; }
  const kw = ($('hwFilter').value || '').trim().toLowerCase();
  let html = '', shown = 0, nOn = 0;
  (hwData.courses || []).forEach((c, ci) => {
    const hits = (c.works || []).filter(w =>
      !kw || (w.name || '').toLowerCase().includes(kw)
          || (c.title || '').toLowerCase().includes(kw));
    if (!hits.length) return;
    const todoN = hits.filter(w => w.todo).length;
    html += `<div class="hwc">${hwEsc(c.title)}
      <span class="n">共 ${hits.length} 份 · 未完成 ${todoN} 份</span></div>`;
    hits.forEach(w => {
      const gi = (c.works || []).indexOf(w);
      // 🔴 勾选态**从 taskPick 读**，与卡片明细同一份状态
      const loc = hwLocate(c.courseid, w.taskrefid);
      const on = loc
        ? taskPickGet(loc.cid, 'work', loc.k,
                      (taskByCourse(loc.cid).works || [])[loc.k])
        : !!w.todo;
      if (on) nOn++;
      const stCls = w.todo ? 'todo' : 'done';
      shown++;
      // 🔴 2026-09-27：已批阅的作业把分数显示出来（0 分标红）
      const sc = (w.score === '' || w.score === null || w.score === undefined)
        ? '' : String(w.score);
      const scBar = sc
        ? `<span class="hwsc${Number(sc) === 0 ? ' zero' : ''}"
             title="平台得分${w.score_rule ? '（取分规则：'
               + hwEsc(w.score_rule) + '）' : ''}">得分 <b>${hwEsc(sc)}</b>`
          + `${w.redotimes !== null && w.redotimes !== undefined
              ? ` · 可重做 ${hwEsc(String(w.redotimes))} 次` : ''}</span>`
        : '';
      html += `<label class="hwi">
        <input type="checkbox" class="hw-ck" data-c="${ci}" data-w="${gi}"
               ${on ? 'checked' : ''}
               data-cid="${hwEsc(String(w.courseid || ''))}"
               data-tid="${hwEsc(String(w.taskrefid || ''))}">
        <span class="nm">${hwEsc(w.name)}</span>
        ${scBar}
        <span class="st ${stCls}">${hwEsc(w.status)}</span>
        ${w.todo ? '' : `<span class="redo"><input type="checkbox"
             class="hw-redo" data-c="${ci}" data-w="${gi}">重做</span>`}
      </label>`;
    });
  });
  box.innerHTML = html || '<div class="hwempty">没有匹配的作业</div>';
  $('hwCount').textContent = `已选 ${nOn} 份 · 共 ${hwData.total} 份`
    + `（未完成 ${hwData.todo} 份）` + (kw ? ` · 显示 ${shown}` : '');
}

async function hwScan() {
  $('hwModal').classList.remove('hide');
  $('hwMsg').textContent = '正在扫描全部课程的作业…（只读，不会提交任何东西）';
  hwData = null; hwRender();
  try {
    const r = await fetch(BASE + '/api/homework/scan',
                          {method: 'POST'}).then(r => r.json());
    if (!r.ok) { $('hwMsg').textContent = r.msg || '扫描失败'; return; }
    hwData = r;
    $('hwMsg').textContent = '';
    hwRender();
  } catch (e) {
    $('hwMsg').textContent = '请求失败: ' + e;
  }
}

// 勾选状态：主勾选框控制"要做这份"；已完成的还要额外勾"重做"
// 🔴 2026-09-26：改动**写回 taskPick**（以 taskrefid 定位），并同步卡片明细，
// 这样弹窗、卡片、明细永远是同一份状态，不会出现"勾 2 份却做 16 份"。
$('hwList').addEventListener('change', ev => {
  const t = ev.target;
  if (t.classList.contains('hw-redo')) {
    const ck = $('hwList').querySelector(
      `.hw-ck[data-c="${t.dataset.c}"][data-w="${t.dataset.w}"]`);
    if (ck) ck.checked = t.checked;    // 勾重做 = 也要做
    if (ck) hwApply(ck, true);
    hwApplyRedo(ck || t, t.checked);
  } else if (t.classList.contains('hw-ck')) {
    if (!t.checked) {
      const rd = $('hwList').querySelector(
        `.hw-redo[data-c="${t.dataset.c}"][data-w="${t.dataset.w}"]`);
      if (rd) { rd.checked = false; hwApplyRedo(t, false); }  // 不做了就取消重做
    }
    hwApply(t, t.checked);
  }
  hwRefreshCount();
});

// 把弹窗里的勾选写回 taskPick + 同步卡片明细 + 同步该课的「作业」卡片级
function hwApply(el, on) {
  if (!el) return;
  const loc = hwLocate(el.dataset.cid, el.dataset.tid);
  if (!loc) return;
  taskPickSet(loc.cid, 'work', loc.k, on);
  // 弹窗改的是"明细里的某一份" → 方向 detail（汇总到卡片）
  taskSyncCourse(loc.i, 'detail');
}

// 「重做」标记写在明细条目本身上（collectTaskItems 会读 it.redo）
function hwApplyRedo(el, on) {
  if (!el) return;
  const loc = hwLocate(el.dataset.cid, el.dataset.tid);
  if (!loc) return;
  const d = taskByCourse(loc.cid);
  const item = d && (d.works || [])[loc.k];
  if (item) item.redo = !!on;
}

// 只刷新计数（避免整表重渲染导致滚动位置丢失）
function hwRefreshCount() {
  if (!hwData) return;
  const nOn = $('hwList').querySelectorAll('.hw-ck:checked').length;
  $('hwCount').textContent = `已选 ${nOn} 份 · 共 ${hwData.total} 份`
    + `（未完成 ${hwData.todo} 份）`;
}

$('hwFilter').oninput = () => hwRender();
$('hwClose').onclick = () => $('hwModal').classList.add('hide');
$('hwModal').onclick = ev => { if (ev.target === $('hwModal')) $('hwModal').classList.add('hide'); };

function hwToggle(setFn) {
  $('hwList').querySelectorAll('.hw-ck').forEach(ck => {
    const w = hwData.courses[+ck.dataset.c].works[+ck.dataset.w];
    const on = setFn(w);
    if (on === null) return;
    ck.checked = on;
    const rd = $('hwList').querySelector(
      `.hw-redo[data-c="${ck.dataset.c}"][data-w="${ck.dataset.w}"]`);
    if (!on && rd) { rd.checked = false; hwApplyRedo(ck, false); }
    hwApply(ck, on);                  // 🔴 写回 taskPick（与卡片同一套状态）
  });
  hwRefreshCount();
}
$('hwAllTodo').onclick = () => hwToggle(w => w.todo ? true : null);
$('hwAll').onclick = () => hwToggle(() => true);
$('hwNone').onclick = () => hwToggle(() => false);

// 🔴 弹窗提交也走 collectTaskItems（= taskPick），不再自己拼一份清单。
// 这样"弹窗里选的"和"卡片明细里选的"永远一致。
function hwCollect() {
  const all = collectTaskItems('work');
  return Object.values(all).flat();
}

$('hwRun').onclick = async () => {
  const items = hwCollect();
  if (!items.length) { $('hwMsg').textContent = '请至少勾选一份作业'; return; }
  const redoN = items.filter(x => x.redo).length;
  const names = items.slice(0, 6).map(x => '· ' + x.name).join('\\n');
  const more = items.length > 6 ? `\\n… 共 ${items.length} 份` : '';
  if (!confirm(`即将开始做 ${items.length} 份作业`
      + (redoN ? `（其中 ${redoN} 份为【重做】，会覆盖上次作答）` : '')
      + `\\n\\n` + names + more + '\\n\\n确认无误？')) return;
  const b = $('hwRun'); b.disabled = true; b.textContent = '正在启动…';
  $('hwMsg').textContent = '';
  try {
    // 🔴 走 /api/start + work_items：与主界面的「开始」是同一条链路，
    // 都是"精确到份"。旧的 /api/homework/run 只做作业、且不带复习标记，
    // 两条链路并存会让"做哪几份"的语义分裂，已统一。
    const workItems = collectTaskItems('work');
    const idx = [...new Set(Object.keys(workItems)
      .map(cid => courses.findIndex(c => String(c.courseId) === cid))
      .filter(i => i >= 0))];
    const r = await fetch(BASE + '/api/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({indices: idx,
        speed: parseFloat($('speed').value) || 1,
        jobs: parseInt($('jobs').value) || 1,
        video: [], quiz: [], work: idx,
        work_items: workItems, exam_items: {}})
    }).then(r => r.json());
    if (!r.ok) { $('hwMsg').textContent = r.msg || '启动失败'; return; }
    $('hwModal').classList.add('hide');
    showStep(3);
    got = -1; $('log').textContent = '';
    startPull();
  } catch (e) {
    $('hwMsg').textContent = '请求失败: ' + e;
  } finally {
    b.disabled = false; b.textContent = '开始做选中的作业';
  }
};

$('btnHwList').onclick = hwScan;

// ==================== 考试清单弹窗（2026-09-27 新增） ====================
// 需求：像「作业清单」一样，把**全部考试**扫出来看。
// 超星班等不在课程列表接口里的课程，通过「登记额外课程」纳入，
// 并在清单**底部单独列出**。本弹窗只读 —— 考试通常只有一次机会，不在这里批量开考。
let examData = null;

async function examExtraLoad() {
  try {
    const j = await fetch(BASE + '/api/exam/extra').then(r => r.json());
    const items = (j && j.items) || [];
    $('examExtraList').innerHTML = items.length
      ? items.map(it => {
          const k = hwEsc(String(it.courseid)) + '|' + hwEsc(String(it.clazzid));
          return `<span class="tag" style="margin:2px 6px 2px 0;display:inline-block">
            ${hwEsc(it.title || it.courseid)}（${hwEsc(String(it.courseid))}）
            <a href="javascript:void(0)" data-del="${k}"
               style="color:#b91c1c;margin-left:6px;text-decoration:none">✕</a>
          </span>`;
        }).join('')
      : '<span class="hint">暂未登记额外课程</span>';
  } catch (e) { /* 忽略 */ }
}

async function examExtraAdd() {
  const cid = ($('exCid').value || '').trim();
  const clz = ($('exClz').value || '').trim();
  const ttl = ($('exTitle').value || '').trim();
  if (!cid || !clz) { $('examMsg').textContent = 'courseId 与 clazzId 都必须填'; return; }
  try {
    const j = await fetch(BASE + '/api/exam/extra', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({courseid: cid, clazzid: clz, title: ttl})
    }).then(r => r.json());
    $('examMsg').textContent = j.msg || '';
    if (j.ok) {
      $('exCid').value = ''; $('exClz').value = ''; $('exTitle').value = '';
      await examExtraLoad();
      examScan();                 // 登记后立刻重扫
    }
  } catch (e) { $('examMsg').textContent = '登记失败：' + e; }
}

async function examExtraDel(key) {
  const p = String(key).split('|');
  try {
    await fetch(BASE + '/api/exam/extra', {
      method: 'DELETE', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({courseid: p[0] || '', clazzid: p[1] || ''})
    });
    $('examMsg').textContent = '已删除';
    await examExtraLoad();
    examScan();
  } catch (e) { /* 忽略 */ }
}

function examRender() {
  const box = $('examList');
  if (!examData) { box.innerHTML = '<div class="hwempty">正在扫描…</div>'; return; }
  const kw = ($('examFilter').value || '').trim().toLowerCase();
  const all = examData.courses || [];
  const normal = all.filter(c => !c.extra);
  const extra = all.filter(c => c.extra);
  const isDone = s => /已完成|已批阅|已交卷/.test(String(s || ''));
  let html = '';
  const paint = c => {
    const es = c.exams || [];
    const ci = all.indexOf(c);        // 用于与课程卡片互通（勾选定位）
    const hit = !kw
      || String(c.title || '').toLowerCase().includes(kw)
      || es.some(e => String(e.title || '').toLowerCase().includes(kw));
    if (!hit) return;
    const todoN = es.filter(e => !isDone(e.status)).length;
    html += `<div class="hwc">${hwEsc(c.title)}
      <span class="n">共 ${es.length} 场 · 可作答 ${todoN} 场</span></div>`;
    if (!es.length) {
      html += `<div class="hwempty" style="padding:6px 10px">${hwEsc(c.note || '暂无考试')}</div>`;
      return;
    }
    es.forEach((e, gi) => {
      const st = String(e.status || '');
      const t0 = e.expire_time || e.exam_time || '';
      const dn = isDone(st);
      // key = courseid|clazzid|exam_id|cpi|标题（cpi 取自列表页隐藏域；
      // 额外课程靠它才能进考场）
      const cpi = (e.hidden && e.hidden.cpi) || '';
      const key = [c.courseid || '', c.clazzid || '', e.exam_id || '', cpi,
                   String(e.title || '').replace(/[|]/g, ' ')].join('|');
      // 🔴 2026-09-28：加勾选框，与课程卡片「考试」维度双向互通。
      //    默认不勾 —— 考试只有一次机会，必须用户显式选择（且开考前还会弹框确认）。
      html += `<div class="hwi">
        <input type="checkbox" class="ex-ck" data-c="${ci}" data-k="${gi}">
        <label class="nm" style="cursor:pointer">${hwEsc(e.title)}</label>
        ${t0 ? `<span class="hwsc">${hwEsc(t0)}</span>` : ''}
        <span class="st ${dn ? 'done' : 'todo'}">${hwEsc(st)}</span>
        ${(!dn && e.exam_id) ? `<button class="ghost" data-ck="${hwEsc(key)}"
             style="margin-left:8px" title="只读：看清这场考试有什么要求（不开考、不消耗作答机会）">预检</button>
           <button class="ghost" data-run="${hwEsc(key)}"
             style="margin-left:6px">单独开考</button>` : ''}
      </div>`;
    });
  };
  normal.forEach(paint);
  if (extra.length) {
    html += `<div class="hwc" style="margin-top:12px;color:#b45309">
      额外课程（不在课程列表里，手动登记）<span class="n">${extra.length} 门</span></div>`;
    extra.forEach(paint);
  }
  box.innerHTML = html || '<div class="hwempty">没有匹配的考试</div>';
  $('examCount').textContent =
    `共 ${examData.total || 0} 场（可作答 ${examData.todo || 0}）`;
}

async function examScan() {
  $('examModal').classList.remove('hide');
  examPrecheckClear();               // 重扫就清掉上一次的预检结果
  $('examMsg').textContent = '正在扫描全部课程的考试…（只读，不会提交任何东西）';
  examData = null; examRender();
  try {
    const r = await fetch(BASE + '/api/exam/scan', {method: 'POST'});
    const j = await r.json();
    if (!j.ok) { $('examMsg').textContent = j.msg || '启动扫描失败'; return; }
    for (let k = 0; k < 900; k++) {
      await new Promise(z => setTimeout(z, 500));
      const s = await fetch(BASE + '/api/exam/scan/status').then(x => x.json());
      $('examMsg').textContent = s.msg || '扫描中…';
      if (s.done) { examData = s; examRender(); return; }
    }
    $('examMsg').textContent = '扫描超时，可关闭后重试';
  } catch (e) { $('examMsg').textContent = '扫描出错：' + e; }
}

// 单场开考（走 /api/exam/run，后端已有）。考试是一次性机会，必须 confirm。
async function examRun(key) {
  const p = String(key).split('|');
  const cid = p[0] || '', clz = p[1] || '', eid = p[2] || '', cpi = p[3] || '';
  const title = p.slice(4).join('|') || '这场考试';
  if (!cid || !eid) { $('examMsg').textContent = '这条考试信息不完整，无法开考'; return; }
  const _ae = $('autoSubmitExam') ? $('autoSubmitExam').checked : false;
  if (!confirm('确认开考《' + title + '》？\\n\\n⚠ 考试通常只有一次机会，'
      + '作答机会消耗后无法恢复。\\n\\n提交方式：'
      + (_ae ? '自动交卷（答完即交）'
             : '审核提交 —— 只暂存草稿，不替你交卷，你到学习通核对后手动交')
      + '\\n（可在主界面「自动提交考试」勾选来切换）')) return;
  $('examMsg').textContent = '正在启动考试任务…';
  try {
    const j = await fetch(BASE + '/api/exam/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        items: [{courseid: cid, clazzid: clz, cpi: cpi, exam_id: eid, title: title}],
        auto_submit: _ae
      })
    }).then(r => r.json());
    if (j.ok) {
      // 🔴 2026-09-27 修：原来只写了一句提示、不切页 —— 用户反馈
      // "开考之后不会跳转到做题的那个界面"。与作业路径（hwRun）对齐：
      // 关弹窗 → 切「运行进度」→ 清空日志 → 开始拉取。
      $('examMsg').textContent = '✅ 已开始考试任务';
      setTimeout(() => {
        $('examModal').classList.add('hide');
        showStep(3);
        got = -1; $('log').textContent = '';
        startPull();
      }, 500);
    } else {
      $('examMsg').textContent = '启动失败：' + (j.msg || '');
    }
  } catch (e) { $('examMsg').textContent = '启动失败：' + e; }
}

// 开考前只读预检（2026-10-12）：看清这场考试的要求。**不开考、不消耗作答机会**。
async function examPrecheck(key) {
  const p = String(key).split('|');
  const cid = p[0] || '', clz = p[1] || '', eid = p[2] || '', cpi = p[3] || '';
  const title = p.slice(4).join('|') || '这场考试';
  if (!cid || !eid) { $('examMsg').textContent = '这条考试信息不完整，无法预检'; return; }
  $('examMsg').textContent = `正在预检《${title}》…（只读，不会开考）`;
  try {
    const j = await fetch(BASE + '/api/exam/precheck', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({courseid: cid, clazzid: clz, cpi: cpi,
                            exam_id: eid, title: title})
    }).then(r => r.json());
    if (!j.ok) { $('examMsg').textContent = '预检失败：' + (j.msg || ''); return; }
    showExamPrecheck(title, j);
    $('examMsg').textContent = '预检完成（未开考，作答机会未消耗）';
  } catch (e) { $('examMsg').textContent = '预检失败：' + e; }
}

function showExamPrecheck(title, j) {
  const box = $('examPre');
  if (!box) return;
  // true=需要 / false=不需要 / null=未知（未开考时封面页不渲染这三项，
  // 此时写"否"是假信息 —— 2026-10-12 修正）
  const yn = v => v === true ? '<span class="warn">是</span>'
    : (v === false ? '否'
       : '<span style="color:#94a3b8">未知（开考后才可见）</span>');
  const row = (k, v) => `<div class="pcr"><span class="pck">${k}</span>` +
    `<span class="pcv">${v}</span></div>`;
  let h = `<div class="pch">预检结果：《${hwEsc(title)}》` +
    `<span style="font-weight:400;color:#94a3b8"> · 只读，未开考</span></div>`;
  h += row('能否进入', j.can_enter
    ? '<span class="ok">现在可以进入</span>'
    : `<span class="warn">暂时不行</span> —— ${hwEsc(j.reason || '')}`);
  if (j.state) h += row('平台状态', hwEsc(j.state));
  if (j.student) h += row('考生', hwEsc(j.student));
  if (j.start_time) h += row('开始时间', hwEsc(j.start_time));
  if (j.end_time) h += row('截止时间', hwEsc(j.end_time));
  h += row('人脸识别', yn(j.face_need) +
    (j.face_need ? ' <span style="color:#b45309">（本程序不处理，会跳过）</span>' : ''));
  h += row('人机验证', yn(j.captcha_need) +
    (j.captcha_need ? ' <span style="color:#b45309">（程序会尝试自动过滑块）</span>' : ''));
  h += row('考试码', yn(j.exam_code_need) +
    (j.exam_code_need ? ' <span style="color:#b45309">（开考时须填老师给的码）</span>' : ''));
  if ((j.limits || []).length) {
    h += '<div class="pcnote"><b>提示</b><br>' +
      j.limits.map(x => '· ' + hwEsc(x)).join('<br>') + '</div>';
  }
  if ((j.unknown || []).length) {
    h += '<div class="pcnote"><b>封面页探不到、需进考场才知道</b>：' +
      j.unknown.map(hwEsc).join('、') +
      '<br><span style="color:#64748b">（「离开作答页面」计数是浏览器端行为检测，' +
      '本程序走协议不执行 JS，不会产生离开记录；平台只在' +
      '<b>考试截止时间</b>到点时自动交卷。）</span></div>';
  }
  box.innerHTML = h;
  box.classList.remove('hide');
}

function examPrecheckClear() {
  const box = $('examPre');
  if (box) { box.classList.add('hide'); box.innerHTML = ''; }
}

$('examList').onclick = ev => {
  const c = ev.target.closest('[data-ck]');
  if (c) { examPrecheck(c.getAttribute('data-ck')); return; }
  const b = ev.target.closest('[data-run]');
  if (b) examRun(b.getAttribute('data-run'));
};
// 🔴 2026-09-28：考试清单勾选 → 写回 taskPick + 点亮该课卡片的「考试」维度。
//    反过来（卡片 → 清单）由 taskSyncCourse('card') 负责，两边共用同一份 taskPick。
$('examList').addEventListener('change', ev => {
  const t = ev.target;
  if (!t.classList.contains('ex-ck')) return;
  const c = ((examData || {}).courses || [])[+t.dataset.c];
  if (!c) return;
  const cid = String(c.courseid || '');
  taskPickSet(cid, 'exam', +t.dataset.k, t.checked);
  exSyncCard(cid);
});

/* 只看状态的"已完成"判定。
   ⚠️ 不能直接用 `examDone()`：那个函数在没有 `relation_id` 时**一律判已完成**
      （它是给卡片内联明细用的，那份数据来自 taskScan，带 relation_id）。
      考试清单弹窗的数据来自 examScan，**不带 relation_id** → 用 examDone 会把
      所有考试都当成"已完成"，勾了卡片也同步不过去（真机踩到）。*/
function examStatusDone(it) {
  return /已完成|已批阅|已交卷|已互评|已过期|已取消|已关闭/
    .test(String((it || {}).status || ''));
}

/* 卡片「考试」框 → 考试清单弹窗的对应项。
   🔴 独立成函数、且在 syncCourse 里**无条件调用**：
      原先它挂在 taskSyncCourse('card') 里，而 syncCourse 在 `taskByCourse(cid)`
      为假时会提前 return（作业/考试还没扫完就属于这种）→ 勾了卡片考试却同步不过去。*/
function exSyncFromCard(idx) {
  const c = courses[+idx];
  if (!c) return;
  const ckE = $('list').querySelector(`input.ck-exam[data-i="${idx}"]`);
  if (!ckE) return;
  const cid = String(c.courseId || '');
  $('examList').querySelectorAll('.ex-ck').forEach(x => {
    const ec = ((examData || {}).courses || [])[+x.dataset.c];
    if (!ec || String(ec.courseid) !== cid) return;
    const gi = +x.dataset.k;
    const on = ckE.checked && !examStatusDone((ec.exams || [])[gi] || {});
    x.checked = on;
    taskPickSet(cid, 'exam', gi, on);
  });
}

/* 按"该课考试清单里是否还有勾选项"决定卡片「考试」框的开关，并走统一联动
   （联动会把选择铺到卡片内联明细、刷新统计与高亮）*/
function exSyncCard(cid) {
  const ci = courses.findIndex(x => String(x.courseId) === cid);
  if (ci < 0) return;              // 该课不在选课列表（额外课程未纳入时会发生）
  const ck = $('list').querySelector(`input.ck-exam[data-i="${ci}"]`);
  const main = $('list').querySelector(`input.ck-video[data-i="${ci}"]`);
  if (!ck) return;
  const anyOn = [...$('examList').querySelectorAll('.ex-ck')].some(x => {
    const ec = ((examData || {}).courses || [])[+x.dataset.c];
    return x.checked && ec && String(ec.courseid) === cid;
  });
  ck.checked = anyOn;
  if (anyOn && main) main.checked = true;
  // ⚠️ 这里**绝不能**触发 main 的 change 事件：
  //    那会反向调用 exSyncFromCard，而批量取消时兄弟项还是勾着的
  //    → 刚被取消的这一项又被勾回来（真机必现的循环依赖）。
  //    只更新卡片高亮与统计即可；勾选状态本身已经写进 taskPick，
  //    内联明细下次重渲染时会按 taskPick 对齐。
  const any = anyOn || (main && main.checked);
  const lbl = $('list').querySelector(`.course[data-i="${ci}"]`);
  const wrap = $('list').querySelector(`.course-wrap[data-i="${ci}"]`);
  if (lbl) lbl.classList.toggle('on', !!any);
  if (wrap) wrap.classList.toggle('on', !!any);
  update();
}
$('btnExamList').onclick = () => { examExtraLoad(); examScan(); };
$('examClose').onclick = () => $('examModal').classList.add('hide');
$('examModal').onclick = ev => {
  if (ev.target === $('examModal')) $('examModal').classList.add('hide');
};
$('examFilter').oninput = () => examRender();
$('exAdd').onclick = examExtraAdd;
$('examExtraList').onclick = ev => {
  const a = ev.target.closest('[data-del]');
  if (a) examExtraDel(a.getAttribute('data-del'));
};

// ==================== 统一任务扫描（作业 + 考试）====================
// 2026-09-26：用户要求"登录后进选课界面就把所有课程的作业和考试都扫出来"，
// 且"考试由老师发布后才出现，要保证发布后能识别"。
// 扫描走后台线程（作业 ~0.5s + 考试 ~0.3s/门），前端轮询进度，
// 完成后把明细直接渲染进每张课程卡片。
let taskData = null;        // {courses:[{courseid,title,works:[],exams:[]}], ...}
let taskTimer = null;
// 用户逐条勾选的状态：key = `${courseid}|${type}|${idx}` → true/false（三态见上）
const taskPick = {};
// 明细区「作业 ｜ 考试」二级 tab 的记忆：courseid → 'work'|'exam'
// （不记的话每次重渲染都跳回第一个 tab，用户切到考试会被弹回去）
const taskTab = {};

// 把扫描结果按 courseid 建索引，方便按课程取
function taskByCourse(cid) {
  if (!taskData) return null;
  return (taskData.courses || []).find(c => String(c.courseid) === String(cid)) || null;
}

// 渲染单门课程的明细
function taskRenderCourse(idx) {
  const box = $('list').querySelector(`.cdetail[data-i="${idx}"]`);
  if (!box) return;
  const c = courses[idx];
  if (!c) return;
  const btn = $('list').querySelector(`.gexp[data-i="${idx}"][data-g="task"]`);
  const d = taskByCourse(c.courseId);
  if (btn) btn.classList.toggle('has', !!(d && ((d.works || []).length
    || (d.exams || []).length)));
  if (!d) {
    box.innerHTML = taskData
      ? '<div class="cd-empty">该课程没有作业 / 考试</div>'
      : '<div class="cd-empty">正在扫描…</div>';
    return;
  }
  let h = '';
  const sec = (title, arr, type) => {
    if (!arr || !arr.length) return '';
    let s = `<div class="cd-sec"><div class="cd-sec-t">${title}
      <span class="n">共 ${arr.length} 项</span>
      <span class="sp"></span>
      <button class="ghost mini cd-all" data-i="${idx}" data-t="${type}"
        data-on="1">全选</button>
      <button class="ghost mini cd-all" data-i="${idx}" data-t="${type}"
        data-on="0">全不选</button></div>`;
    arr.forEach((it, k) => {
      const checked = taskPickGet(c.courseId, type, k, it) ? 'checked' : '';
      const st = it.status || (type === 'work' ? '未完成' : '未考试');
      // 「已过期」= 平台已关闭入口，做不了 → 归入灰态，不显示成待办橙色
      const done = /已完成|已批阅|待批阅|已交卷|已互评|已过期|已取消|已关闭/
        .test(st);
      const cls = done ? 'done' : 'todo';

      // ---- 重做信息（作业用 redotimes；考试用 can_retest）----
      // 作业：次数来自详情页 redoTimes = 总上限 + 额外 - 已用
      // 考试：列表页只能拿到「有没有重考按钮」，次数在交卷页（允许 N 次/已 M 次）
      // 取分规则 tip："最高成绩"=重做考低了不亏 ／ "本次成绩"=重做覆盖（考低就亏）
      let redoBar = '';
      if (type === 'work' && done && !/已过期|已取消|已关闭/.test(st)) {
        const left = (it.redotimes === null || it.redotimes === undefined)
          ? null : Number(it.redotimes);
        const rule = it.score_rule || '';
        const risky = rule === '本次成绩';
        const noLeft = left !== null && left <= 0;
        const sc = (it.score === '' || it.score === null ||
                    it.score === undefined) ? '' : String(it.score);
        let badge;
        if (left === null) {
          badge = '<span class="rd-n">重做次数查询中…</span>';
        } else if (noLeft) {
          badge = '<span class="rd-n out">已无重做次数</span>';
        } else {
          badge = `<span class="rd-n">还可重做 <b>${left}</b> 次</span>`;
        }
        redoBar = `<div class="rd-bar">
          ${sc ? `<span class="rd-sc">得分 <b>${escapeHtml(sc)}</b></span>` : ''}
          ${badge}
          ${rule ? `<span class="rd-rule${risky ? ' risky' : ''}"
            title="${risky
              ? '重做会覆盖上一次成绩，考低了会拉低总分'
              : '取历史最高分，重做考低了不会拉低总分'}"
            >${risky ? '⚠ 取本次成绩' : '✓ 取最高成绩'}</span>` : ''}
          <button class="ghost mini rd-btn${noLeft || left === null ? ' off' : ''}"
            data-i="${idx}" data-t="${type}" data-k="${k}"
            ${noLeft || left === null ? 'disabled' : ''}
            >重做</button>
        </div>`;
      }
      // 🔴 2026-11-07：**考试重做**（何同学要求）。
      //   考试只有在平台开「重考」时才显示按钮；次数在列表页拿不到，
      //   有 retest_allowed/retest_used 就显示，没有就只显示"可重考"。
      if (type === 'exam' && it.can_retest) {
        const sc = (it.score === '' || it.score === null ||
                    it.score === undefined) ? '' : String(it.score);
        let badge = '<span class="rd-n">可重考</span>';
        if (it.retest_allowed !== undefined && it.retest_allowed !== null) {
          const used = Number(it.retest_used || 0);
          const left = Math.max(0, Number(it.retest_allowed) - used);
          badge = left > 0
            ? `<span class="rd-n">还可重考 <b>${left}</b> 次</span>`
            : '<span class="rd-n out">已无重考次数</span>';
        }
        redoBar = `<div class="rd-bar">
          ${sc ? `<span class="rd-sc">得分 <b>${escapeHtml(sc)}</b></span>` : ''}
          ${badge}
          <span class="rd-rule risky" title="重考会覆盖上一次成绩">⚠ 覆盖上次成绩</span>
          <button class="ghost mini ex-retest" data-i="${idx}" data-k="${k}"
            >重考</button>
          <button class="ghost mini ex-getans" data-i="${idx}" data-k="${k}"
            title="从「试卷详情」提取正确答案并写入题库（仅平台允许查看时有内容）"
            >提取答案</button>
        </div>`;
      }

      s += `<div class="cd-item-wrap">
        <label class="cd-item">
          <input type="checkbox" class="cd-ck" data-i="${idx}" data-t="${type}"
                 data-k="${k}" ${checked}>
          <span class="nm">${escapeHtml(it.name || it.title || '(无标题)')}</span>
          <span class="st ${cls}">${escapeHtml(st)}</span>
        </label>
        ${redoBar}
      </div>`;
    });
    return s + '</div>';
  };
  // 二级切换 tab：作业 ｜ 考试（对齐超星 App「作业/考试」页的顶部切换，
  // 用户截图确认）。默认停在「作业」，没有作业时自动落到「考试」。
  const nW = (d.works || []).length, nE = (d.exams || []).length;
  const defTab = nW ? 'work' : (nE ? 'exam' : 'work');
  const tab = taskTab[c2id(c)] || defTab;
  taskTab[c2id(c)] = tab;
  let head = '<div class="cd-tabs">'
    + `<button class="cd-tab${tab === 'work' ? ' on' : ''}" data-t="work">作业`
    + (nW ? `<span class="tb">${nW}</span>` : '') + '</button>'
    + `<button class="cd-tab${tab === 'exam' ? ' on' : ''}" data-t="exam">考试`
    + (nE ? `<span class="tb">${nE}</span>` : '') + '</button>'
    + '</div>';
  let body = '<div class="cd-pane" data-t="work">'
    + (sec('作业', d.works, 'work')
       || '<div class="cd-empty">暂无作业</div>') + '</div>';
  body += '<div class="cd-pane" data-t="exam">'
    + (sec('考试', d.exams, 'exam')
       || '<div class="cd-empty">暂无考试</div>') + '</div>';
  box.innerHTML = head + body;
  box.dataset.tab = tab;
  taskApplyTab(box, tab);

  box.querySelectorAll('.cd-tab').forEach(b => b.onclick = ev => {
    ev.preventDefault(); ev.stopPropagation();
    taskTab[c2id(c)] = b.dataset.t;
    box.dataset.tab = b.dataset.t;
    box.querySelectorAll('.cd-tab').forEach(x =>
      x.classList.toggle('on', x.dataset.t === b.dataset.t));
    taskApplyTab(box, b.dataset.t);
  });

  box.querySelectorAll('.cd-ck').forEach(ck => ck.onchange = () => {
    taskPickSet(c.courseId, ck.dataset.t, ck.dataset.k, ck.checked);
    // 明细里的框被点 → 方向 detail（汇总到卡片）
    taskSyncCourse(ck.dataset.i, 'detail');
  });
  // ---- 「重做」按钮：立刻调平台重做接口（重置本份作业的作答记录）----
  box.querySelectorAll('.rd-btn').forEach(b => b.onclick = async ev => {
    ev.preventDefault(); ev.stopPropagation();
    const i = b.dataset.i, t = b.dataset.t, k = +b.dataset.k;
    const dd = taskByCourse(courseIdOf(i));
    const it = dd && dd[t === 'exam' ? 'exams' : 'works']
      ? (t === 'exam' ? dd.exams : dd.works)[k] : null;
    if (!it) return;
    const left = Number(it.redotimes);
    const risky = (it.score_rule || '') === '本次成绩';
    let tip = `确认重做《${it.name || it.title}》？\n\n`
      + `· 还可以重做 ${left} 次\n`
      + `· 重做后**上一次的作答记录会被覆盖**\n`;
    if (risky) {
      tip += `· ⚠ 本份作业取「本次成绩」——重做考低了会拉低最终成绩！\n`;
    } else {
      tip += `· 本份作业取「最高成绩」——重做考低了不会拉低最终成绩\n`;
    }
    tip += `\n确认要继续吗？`;
    if (!window.confirm(tip)) return;

    const old = b.textContent;
    b.disabled = true; b.textContent = '重置中…';
    try {
      const r = await fetch(BASE + '/api/work/redo', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({courseid: courseIdOf(i), taskrefid: it.taskrefid,
                              name: it.name,
                              // 🔴 必须带上这三个：详情页走 href（含 enc_task 签名）
                              // 才能解析出 workId/redoTimes；缺 href 只能手工拼参数，
                              // 而手工拼参数会被平台打回 3991B 壳页 → 「未解析到 workId」
                              href: it.href || '', enc_task: it.enc_task || '',
                              cpi: it.cpi || '', userid: it.userid || '',
                              clazzid: it.clazzid || ''})}).then(x => x.json());
      if (!r.ok) { alert('重做失败：' + (r.msg || '未知错误')); return; }
      it.redotimes = Math.max(0, left - 1);
      it.redo_used = (Number(it.redo_used) || 0) + 1;
      // 重做成功 → 自动勾上「重做」标记，等用户点开始去刷
      taskPick[taskKey(courseIdOf(i), t, k)] = true;
      taskRenderCourse(i);
      alert(`已重置《${it.name || it.title}》。\n`
            + `现在可以点「开始刷课」去做这份作业了。`);
    } catch (e) {
      alert('重做请求失败：' + e);
    } finally {
      b.disabled = false; b.textContent = old;
    }
  });
  // 🔴 2026-11-07：考试「重考」「提取答案」（何同学要求）
  box.querySelectorAll('.ex-retest').forEach(b => b.onclick = async ev => {
    ev.preventDefault(); ev.stopPropagation();
    const i = b.dataset.i, k = +b.dataset.k;
    const dd = taskByCourse(courseIdOf(i));
    const it = dd && dd.exams ? dd.exams[k] : null;
    if (!it) return;
    if (!window.confirm(
      `确认重考《${it.title || it.name}》？\n\n`
      + `· 重考会**覆盖上一次的作答与分数**\n`
      + `· 建议先点「提取答案」，把正确答案存进题库再重考\n`
      + `· 设置好后，点「开始」勾上这场考试即可作答\n\n继续吗？`)) return;
    const old = b.textContent;
    b.disabled = true; b.textContent = '设置中…';
    try {
      const r = await fetch(BASE + '/api/exam/retest', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          courseid: courseIdOf(i), exam_id: it.exam_id,
          paper_id: it.test_paper_id || it.paper_id || it.exam_id,
          relation_id: it.test_user_relation_id || it.relation_id})}
      ).then(x => x.json());
      alert(r.ok ? ('重考已就绪：' + (r.msg || '')) : ('失败：' + (r.msg || '')));
      if (r.ok) refreshCourses();
    } catch (e) { alert('请求失败：' + e); }
    finally { b.disabled = false; b.textContent = old; }
  });

  box.querySelectorAll('.ex-getans').forEach(b => b.onclick = async ev => {
    ev.preventDefault(); ev.stopPropagation();
    const i = b.dataset.i, k = +b.dataset.k;
    const dd = taskByCourse(courseIdOf(i));
    const it = dd && dd.exams ? dd.exams[k] : null;
    if (!it) return;
    if (!window.confirm(
      `从《${it.title || it.name}》提取正确答案？\n\n`
      + `· 只有平台**允许查看答案**时才有内容，不允许会明确失败（不猜）\n`
      + `· 提取到的答案会写进题库，重考时直接命中\n\n继续吗？`)) return;
    const old = b.textContent;
    b.disabled = true; b.textContent = '提取中…';
    try {
      const r = await fetch(BASE + '/api/exam/answers', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          courseid: courseIdOf(i), exam_id: it.exam_id,
          paper_id: it.test_paper_id || it.paper_id || it.exam_id,
          relation_id: it.test_user_relation_id || it.relation_id})}
      ).then(x => x.json());
      alert(r.ok ? `${r.msg}\n已写入题库 ${r.imported} 题`
                 : ('提取失败：' + (r.msg || '')));
    } catch (e) { alert('请求失败：' + e); }
    finally { b.disabled = false; b.textContent = old; }
  });

  box.querySelectorAll('.cd-all').forEach(b => b.onclick = ev => {
    ev.preventDefault(); ev.stopPropagation();
    const on = b.dataset.on === '1';
    const t = b.dataset.t;
    const dr = taskByCourse(c.courseId);
    const arr = dr ? ((t === 'work' ? dr.works : dr.exams) || []) : [];
    box.querySelectorAll(`.cd-ck[data-t="${t}"]`).forEach(ck => {
      // 「全选」只勾"未完成 / 可作答"的：把已完成的一起勾上会触发重做，
      // 而重做有次数上限且在「本次成绩」规则下考低会亏 —— 不能由"全选"
      // 隐式触发。已完成的那几份由用户点每行的「重做」按钮单独处理。
      let v = on;
      if (on) {
        const it = arr[+ck.dataset.k] || {};
        v = t === 'work' ? !!it.todo : !examDone(it);
      }
      ck.checked = v;
      taskPickSet(c.courseId, t, ck.dataset.k, v);
    });
    // 明细「全选/全不选」也是明细侧的改动 → 方向 detail
    taskSyncCourse(b.dataset.i, 'detail');
  });
}

// 只显示当前二级 tab 对应的那一栏
function taskApplyTab(box, tab) {
  box.querySelectorAll('.cd-pane').forEach(p =>
    p.classList.toggle('hide', p.dataset.t !== tab));
}

// 课程 courseId → 稳定字符串键（明细 tab 记忆用）
function c2id(c) { return String((c || {}).courseId || ''); }

// 下标 → courseId（明细按钮只带下标，回传接口要 courseId）
function courseIdOf(i) {
  const c = courses[+i];
  return c ? String(c.courseId || '') : '';
}

// 明细级 ⇄ 卡片级 双向同步（2026-09-26 重构，第二版）。
//
// 🔴 第一版的 bug（审计 ⑤ 实测抓到）：函数里先跑"明细 → 卡片"，用户取消
// 卡片勾选后调这里，① 发现明细还有勾就**又把卡片点亮了**，于是 ② 的
// "取消"分支永远进不去 —— 用户取消作业勾选后，明细里那几份纹丝不动，
// 看起来像"取消不了"。
//
// 第二版：把方向显式交给调用方。只有明确知道"这次是谁变的"，才能正确同步。
//   from='detail' —— 明细里某个框被点 → 只做"明细 → 卡片"（汇总）
//   from='card'   —— 卡片上某个框被点 → 只做"卡片 → 明细"（铺开/清空）
// 不传 = 两件事都不做，只刷新高亮（用于扫描完成后的重渲染）。
function taskSyncCourse(idx, from) {
  const c = courses[+idx];
  if (!c) return;
  const cid = String(c.courseId || '');
  const box = $('list').querySelector(`.cdetail[data-i="${idx}"]`);
  const q = sel => $('list').querySelector(`input.${sel}[data-i="${idx}"]`);
  const main = q('ck-video'), v2 = q('ck-video2');

  // ---- 方向一：明细变了 → 汇总到卡片 ----
  if (from === 'detail') {
    const anyW = box && box.querySelector('.cd-ck[data-t="work"]:checked');
    const anyE = box && box.querySelector('.cd-ck[data-t="exam"]:checked');
    const set = (sel, on) => {
      const el = q(sel);
      if (el && el.checked !== !!on) el.checked = !!on;
    };
    // 明细有勾 → 点亮对应维度；整列都取消 → 熄灭对应维度
    if (box) {
      const ckW = [...(box.querySelectorAll('.cd-ck[data-t="work"]') || [])];
      const ckE = [...(box.querySelectorAll('.cd-ck[data-t="exam"]') || [])];
      if (ckW.length) set('ck-work', ckW.some(x => x.checked));
      if (ckE.length) set('ck-exam', ckE.some(x => x.checked));
    }
    if (anyW || anyE) set('ck-video', true);
    else if (box && !(ckW.some(x => x.checked)) && !(ckE.some(x => x.checked))) {
      // 明细全空 且 测验也没勾 → 主勾选框也可以放开
      const qz = q('ck-quiz');
      if (!(qz && qz.checked)) set('ck-video', false);
    }
  }

  // ---- 方向二：卡片变了 → 铺开/清空明细 ----
  // 补勾范围 = **未完成的那些**。已完成的不补：勾上会隐式触发重做
  // （有次数上限，且「本次成绩」规则下考低会亏），不能由"勾个作业框"
  // 这种轻量操作触发。想做已完成的，走每行的「重做」按钮。
  if (from === 'card' && box) {
    [['work', 'ck-work'], ['exam', 'ck-exam']].forEach(([type, sel]) => {
      const card = q(sel);
      if (!card) return;
      const cks = [...box.querySelectorAll(`.cd-ck[data-t="${type}"]`)];
      if (!cks.length) return;
      const d = taskByCourse(cid);
      const arr = d ? ((type === 'work' ? d.works : d.exams) || []) : [];
      if (card.checked) {
        cks.forEach(x => {
          const it = arr[+x.dataset.k] || {};
          const on = type === 'work' ? !!it.todo : !examDone(it);
          x.checked = on;
          taskPickSet(cid, type, x.dataset.k, on);
        });
      } else {
        cks.forEach(x => {
          x.checked = false;
          taskPickSet(cid, type, x.dataset.k, false);
        });
      }
    });
  }

  // 🔴 2026-09-28：卡片 → **考试清单弹窗**也要同步。
  //    考试清单的勾选不在 `.cdetail` 里（那是卡片内联明细），所以上面那段
  //    铺不到它 —— 用户会看到"卡片勾了考试，打开考试清单却是空的"。
  //    注意：这一步**不能依赖 taskData**（见 syncCourse 里的调用点），
  //    否则作业/考试还没扫完时勾卡片就同步不过去。
  if (from === 'card') exSyncFromCard(idx);

  if (v2 && main) v2.checked = main.checked;
  const any = main && (main.checked
    || ($('list').querySelector(`input.ck-quiz[data-i="${idx}"]`) || {}).checked
    || ($('list').querySelector(`input.ck-work[data-i="${idx}"]`) || {}).checked
    || ($('list').querySelector(`input.ck-exam[data-i="${idx}"]`) || {}).checked);
  const lbl = $('list').querySelector(`.course[data-i="${idx}"]`);
  const wrap = $('list').querySelector(`.course-wrap[data-i="${idx}"]`);
  if (lbl) lbl.classList.toggle('on', !!any);
  if (wrap) wrap.classList.toggle('on', !!any);
  update();
}

function taskRenderAll() {
  if (!courses.length) return;
  courses.forEach((_, i) => taskRenderCourse(i));
}

// 扫描进度条
function taskSetBanner(text, cls) {
  const b = $('taskBar');
  if (!b) return;
  b.classList.remove('hide');
  b.className = 'taskbar ' + (cls || '');
  $('taskBarText').textContent = text || '';
  const rt = $('taskBarRetry');
  if (rt) rt.style.display = (cls === 'err') ? '' : 'none';
}
$('taskBarRetry').onclick = () => taskScan();

async function taskScan() {
  taskSetBanner('正在扫描全部课程的作业与考试…', 'run');
  taskData = null;
  taskRenderAll();
  try {
    const r = await fetch(BASE + '/api/tasks/scan', {method: 'POST'})
      .then(r => r.json());
    if (!r.ok) { taskSetBanner(r.msg || '扫描启动失败', 'err'); return; }
    if (taskTimer) clearInterval(taskTimer);
    taskTimer = setInterval(taskPoll, 900);
    taskPoll();
  } catch (e) {
    taskSetBanner('扫描请求失败：' + e, 'err');
  }
}

async function taskPoll() {
  try {
    const r = await fetch(BASE + '/api/tasks/scan/status').then(r => r.json());
    if (r.running) { taskSetBanner(r.msg || '扫描中…', 'run'); return; }
    if (taskTimer) { clearInterval(taskTimer); taskTimer = null; }
    if (r.err) { taskSetBanner('扫描失败：' + r.err, 'err'); return; }
    taskData = r;    let wT = 0, wD = 0, eT = 0, eD = 0;
    (r.courses || []).forEach(c => {
      (c.works || []).forEach(w => { wT++; if (!w.todo) wD++; });
      (c.exams || []).forEach(x => { eT++; if (examDone(x)) eD++; });
    });
    // 🔴 2026-09-26：**不再自动写 taskPick**。
    // taskPick 改为纯粹记录"用户的显式表态"（三态）：
    //   没记录 = 没表态 → 界面显示 & 提交时按默认（未完成的做、已完成的不做）
    //   有记录 = 用户点了 → 一律以记录为准
    // 之前扫描完就把所有"未完成"写进 taskPick，导致这些条目一律变成
    // "已表态 true"，用户再想"只留其中一份"时无法收窄（其余仍是显式 true）。
    // 现在由 taskPickGet / taskPickTouched 在读取时现算默认值。
    (r.courses || []).forEach(c => {
      // 清理已不存在的条目的陈旧表态（重扫后序号可能变）
      const nW = (c.works || []).length, nE = (c.exams || []).length;
      Object.keys(taskPick).forEach(k => {
        const [cid, ty, idx] = k.split('|');
        if (cid !== String(c.courseid)) return;
        const lim = ty === 'work' ? nW : nE;
        if (+idx >= lim) delete taskPick[k];
      });
    });
    // 提示语：考试全都没得做时要说清原因，否则用户以为程序漏扫了。
    // 2026-09-26 实测两个账号：考试全是「已过期」或作答机会已用完（relationId=0）。
    let tip = `扫描完成 · 作业 ${wT} 份（未完成 ${wT - wD}）`
      + ` · 考试 ${eT} 场（可作答 ${eT - eD}）`;
    if (eT && eT === eD) {
      tip += ' · 考试均已完成或已过期，平台未开放新的作答机会';
    }
    if (r.exam_failed) tip += ` · ${r.exam_failed} 门课程考试读取失败`;
    taskSetBanner(tip, 'ok');
    taskRenderAll();
  } catch (e) {
    if (taskTimer) { clearInterval(taskTimer); taskTimer = null; }
    taskSetBanner('轮询失败：' + e, 'err');
  }
}


// ---- 开始 ----
$('btnStart').onclick = async () => {
  const picked = collectJobs();
  if (!picked.length) { $('msg2').textContent = '请至少勾选一门课程并选择内容（视频/测验/作业）'; return; }
  const nv = picked.filter(x => x.video).length;
  const nq = picked.filter(x => x.quiz).length;
  const nw = picked.filter(x => x.work).length;
  // 🔴 2026-09-26：确认框必须列出**逐份**的作业/考试清单，而不是只报课程数。
  // 之前只写"作业 N 门"，用户在明细里挑了哪几份完全不可见，无法核对。
  const workItems = collectTaskItems('work');
  const examItems = collectTaskItems('exam');
  const nwItem = Object.values(workItems).reduce((a, b) => a + b.length, 0);
  const neItem = Object.values(examItems).reduce((a, b) => a + b.length, 0);
  const nwRedo = Object.values(workItems)
    .reduce((a, b) => a + b.filter(x => x.redo).length, 0);
  const names = picked.slice(0, 6)
    .map(x => '· ' + ((courses[x.i] || {}).title || '?')).join('\\n');
  const more = picked.length > 6 ? `\\n… 共 ${picked.length} 门` : '';
  let detail = '';
  if (nwItem) {
    detail += `\\n\\n作业 ${nwItem} 份`
      + (nwRedo ? `（其中 ${nwRedo} 份为重做，会覆盖上次作答）` : '') + '：';
    Object.entries(workItems).slice(0, 4).forEach(([cid, arr]) => {
      const c = courses.find(x => String(x.courseId) === cid) || {};
      detail += `\\n  【${c.title || cid}】`;
      arr.slice(0, 5).forEach(it => { detail += `\\n    · ${it.name}`; });
      if (arr.length > 5) detail += `\\n    … 共 ${arr.length} 份`;
    });
    if (Object.keys(workItems).length > 4) {
      detail += `\\n  … 共 ${Object.keys(workItems).length} 门课`;
    }
  }
  // 🔴 2026-09-28（按用户要求修正）：考试确认**只在这一次**。
  //    用户原话"刷课选择完直接弹一次确认" —— 这里点「确定」即视为同意开考，
  //    刷课/作业跑完后会**自动接着考**，不再二次询问。
  //    所以考试的风险（只有一次机会、默认自动交卷）必须在**这一段**说清楚。
  if (neItem) detail += `\\n\\n考试 ${neItem} 场 —— ⚠ 点「确定」即视为同意开考：`
    + `刷课/作业跑完后会自动接着开考（不再二次询问）。`
    + `\\n考试通常只有一次机会，作答机会消耗后无法恢复。`;
  // 🔴 2026-09-26：确认框必须显式告知**提交方式**。
  // 自动提交下蒙的答案会被直接交出去（判低分），这一步是最后的知情机会。
  // 作业提交方式（默认开）—— 随 /api/start 一起提交给 build_tiku 的 SUBMIT。
  const _auto = $('autoSubmitMain') ? $('autoSubmitMain').checked : true;
  // 考试提交方式（2026-10-12 起**默认关** = 只暂存不交卷，何同学要求）。
  const _autoExam = $('autoSubmitExam') ? $('autoSubmitExam').checked : false;
  const _thr = parseFloat(($('coverRateMain') || {}).value) || 0.9;
  detail += `\\n\\n提交方式（作业）：`
    + (_auto ? `自动提交（覆盖率 ≥ ${Math.round(_thr * 100)}% 即交卷）`
             : '审核提交（只暂存草稿）');
  if (neItem) detail += `\\n提交方式（考试）：`
    + (_autoExam ? '自动交卷'
                 : '审核提交 —— 只暂存草稿，不替你交卷（自己到学习通核对后交）');
  if (!confirm(`即将开始：视频 ${nv} 门 · 测验 ${nq} 门 · 作业 ${nw} 门\\n\\n`
               + names + more + detail + '\\n\\n确认无误？')) return;
  const idx = picked.map(x => x.i);
  pendingExam = (neItem && Object.keys(examItems).length) ? examItems : null;
  pendingExamAsked = false;
  // 启动要读章节列表，通常几秒；没有即时反馈会让人以为没点上而反复点击
  const btn = $('btnStart'), old = btn.textContent;
  btn.disabled = true; btn.textContent = '正在启动...';
  $('msg2').textContent = '';
  try {
    const r = await fetch(BASE + '/api/start', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({indices: idx, speed: parseFloat($('speed').value) || 1,
        jobs: parseInt($('jobs').value) || 1,
        // 🔴 2026-09-26 新增：主界面提交开关必须随每次「开始」一起提交，
        // 否则 build_tiku 的 SUBMIT 只能沿用**登录时**缓存的旧值，
        // 用户在主界面改开关完全不起作用（"改了没用"）。
        auto_submit: _auto,
        // 🔴 2026-10-12：考试提交开关随启动一起提交 —— 后端把它写进 last_params，
        //    刷新/重启后由 fillParams 恢复；考试侧链路不读 `auto_submit`。
        exam_auto_submit: _autoExam,
        cover_rate: _thr,
        video: picked.filter(x => x.video).map(x => x.i),
        quiz: picked.filter(x => x.quiz).map(x => x.i),
        work: picked.filter(x => x.work).map(x => x.i),
        // 🔴 2026-09-28：同时传 courseId 数组（后端优先用它）。下标会因
        //    "列表长度变化"错位（例如扫描时自动登记了新的额外课程），
        //    courseId 不会 —— 选课列表现在含额外课程，必须防这一手。
        video_course_ids: picked.filter(x => x.video)
          .map(x => String((courses[x.i] || {}).courseId || '')).filter(Boolean),
        quiz_course_ids: picked.filter(x => x.quiz)
          .map(x => String((courses[x.i] || {}).courseId || '')).filter(Boolean),
        work_course_ids: picked.filter(x => x.work)
          .map(x => String((courses[x.i] || {}).courseId || '')).filter(Boolean),
        exam_course_ids: picked.filter(x => x.exam)
          .map(x => String((courses[x.i] || {}).courseId || '')).filter(Boolean),
        // 🔴 精确到份的清单：后端看到它就"只做这几份"，不再整门课全做
        work_items: workItems,
        exam_items: examItems})}).then(r => r.json());
    if (!r.ok) { $('msg2').textContent = r.msg || '启动失败'; return; }
    // 🔴 2026-10-13：后端已直接开考（只勾考试）⇒ 清掉待接续清单，
    //    否则 `maybeRunExam` 会在考试跑完后**再开一次**（浪费作答机会）。
    if (r.exam_launched) { pendingExam = null; pendingExamAsked = true; }
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

// ===== 运行中「添加课程」（2026-10-01）=====
// 用户原话：「这个界面只有一个返回选课，如果我想添加课程接在上面的，
//   只能返回选课重新选…直接在这里加个添加的。」
// 流程：拉「全部课程 + 当前任务清单」→ 勾选 → POST /api/append_courses
//      后端先暂停当前任务，再用「原清单 ∪ 新课」重建（已上报进度不回滚）。
function addRender() {
  const q = ($('addFilter').value || '').trim().toLowerCase();
  const rows = addCourses.filter(c =>
    !q || String(c.title || '').toLowerCase().includes(q)
    || String(c.courseId).includes(q));
  $('addCount').textContent = '共 ' + rows.length + ' 门';
  if (!rows.length) {
    $('addList').innerHTML = '<div class="hwtip">没有匹配的课程</div>';
    return;
  }
  $('addList').innerHTML = rows.map(c => {
    const id = String(c.courseId);
    const on = addCur.has(id);
    return '<label class="hwi">'
      + '<input type="checkbox" class="ck-add" data-id="' + id + '"'
      + (on ? ' checked' : '') + '>'
      + '<span class="nm">' + escapeHtml(c.title || '')
      + ' <span class="n">' + escapeHtml(id) + '</span></span>'
      + (on ? '<span class="st done">已在刷</span>'
            : '<span class="st todo">可添加</span>')
      + '</label>';
  }).join('');
}

$('btnAddCourse').onclick = async () => {
  $('addModal').classList.remove('hide');
  $('addMsg').textContent = '';
  $('addFilter').value = '';
  $('addList').innerHTML = '<div class="hwtip">正在读取课程列表…</div>';
  try {
    const [rc, rp] = await Promise.all([
      fetch(BASE + '/api/courses').then(r => r.json()),
      fetch(BASE + '/api/current_pick').then(r => r.json()),
    ]);
    if (!rc.ok) {
      $('addList').innerHTML =
        '<div class="hwtip">' + escapeHtml(rc.msg || '读取课程失败') + '</div>';
      return;
    }
    addCourses = rc.courses || [];
    addCur = new Set(((rp || {}).course_ids || []).map(String));
    addRender();
  } catch (e) {
    $('addList').innerHTML = '<div class="hwtip">读取失败：'
      + escapeHtml(String(e)) + '</div>';
  }
};
$('addClose').onclick = () => $('addModal').classList.add('hide');
$('addModal').onclick = ev => {
  if (ev.target === $('addModal')) $('addModal').classList.add('hide');
};
$('addFilter').oninput = addRender;
$('addNone').onclick = () => {
  $('addList').querySelectorAll('input.ck-add').forEach(x => { x.checked = false; });
};

$('addRun').onclick = async () => {
  const ids = Array.from($('addList').querySelectorAll('input.ck-add'))
    .filter(x => x.checked).map(x => x.dataset.id);
  const fresh = ids.filter(id => !addCur.has(id));
  if (!fresh.length) { $('addMsg').textContent = '没有新勾的课程'; return; }
  const b = $('addRun');
  if (b.disabled) return;
  b.disabled = true;
  $('addMsg').textContent = '正在加入…任务会暂停几秒再继续';
  try {
    const r = await fetch(BASE + '/api/append_courses', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ course_ids: fresh }) }).then(x => x.json());
    if (!r.ok) { $('addMsg').textContent = r.msg || '加入失败'; return; }
    $('addModal').classList.add('hide');
    showStep(3);
    got = -1; $('log').textContent = '';
    startPull();
  } catch (e) {
    $('addMsg').textContent = '请求失败: ' + e;
  } finally {
    b.disabled = false;
  }
};

$('btnLog').onclick = () => {
  const el = $('log'), on = el.classList.toggle('hide');
  $('btnLog').textContent = on ? '展开运行日志' : '收起运行日志';
};

// ---- 进度（游标增量 + 服务端历史，刷新不丢） ----
const fmtT = s => { s = Math.floor(s); return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0'); };

// 选课页的「上次任务已结束」提示（2026-09-29 用户要求）。
// 背景：跑完的实例重启后前端落在**选课页**（step2），而 #stat="已结束" 只在
// 运行页可见 —— 用户点进来只看到一堆课程卡片，不知道上一轮刷完了没
// （用户原话："只要它正常查询的页面显示已结束也就行了"）。
// ⚠️ 单独做成函数是有原因的：**跑完的实例不会进轮询**（boot() 只在
// running/paused/resuming 时才 startPull），所以 boot() 里要能单独调它一次；
// 直接复用 renderProgress 会把"课程运行状态"看板也拉出来，选课页上显乱。
// 判据 `pf.done > 0`：只认**确实刷出过东西**的 —— 光"选过课"不算
// （acc4 张旭选过课但一条没刷，显示 0/M 反而误导）。
function renderLastTaskTag(p) {
  const lt = $('lastTaskTag');
  if (!lt) return;
  p = p || {};
  const pf = p.platform || {};
  const over = !p.running && !p.paused && !p.resuming && pf.done > 0;
  if (over) {
    lt.textContent = '上次任务已结束 · 平台已认 ' + pf.done + '/' + pf.total
      + '（点「开始」可继续补漏）';
    lt.style.display = '';
  } else {
    lt.style.display = 'none';
  }
}

function fmtDur(s) {
  // ETA 用的人话时长："1 小时 10 分"，而不是 70 分或 01:10:00
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  if (h > 0) return h + ' 小时 ' + m + ' 分';
  if (m > 0) return m + ' 分钟';
  return '不到 1 分钟';
}

function renderEta(p) {
  // 预计完成时间（2026-09-29）。⚠️ 实测**做不到恒定 ±5 分钟**：单任务点耗时
  // 中位 8.5 分、标准差 6.9 分（平台判定从几十秒到 19 分钟不等），多任务叠加后
  // 误差按 √N 放大 —— 1408 次回溯：剩余 1~2 个中位误差 2 分、11~20 个 12 分、
  // 41+ 个 40 分。所以这里**必须把区间一起显示**，只给一个时刻会误导。
  const el = $('pEta');
  if (!el) return;
  const e = p.eta || {};
  if (e.ok && e.remain === 0) {
    el.textContent = '✓ 平台进度已满，预计已完成';
    return;
  }
  if (!e.ok) {
    if (e.why === 'paused') el.textContent = '⏸ 已暂停，暂停期间不估算完成时间';
    else if (e.why === 'done') {
      el.textContent = (e.remain > 0)
        ? '⏹ 本次任务已结束；平台还有 ' + e.remain + ' 章没结算，进度不会再涨了'
        : '';
    }
    else if (e.why === 'no_data') el.textContent = '⏱ 预计完成时间：还没取到平台进度';
    else if (e.why === 'warming') {
      el.textContent = '⏱ 预计完成时间：正在收集速率数据…（约 5 分钟）'
        + (e.remain ? '，还差 ' + e.remain + ' 章' : '');
    } else if (e.why === 'stalled') {
      el.textContent = '⏱ 预计完成时间：平台最近 ' + Math.round(e.win_min || 0)
        + ' 分钟没结算新章节（在等判定），暂无法推算 · 还差 ' + e.remain + ' 章';
    } else el.textContent = '';
    return;
  }
  const f = new Date(e.finish * 1000);
  const hm = String(f.getHours()).padStart(2, '0') + ':'
           + String(f.getMinutes()).padStart(2, '0');
  const half = Math.max(1, Math.round(e.half / 60));
  el.textContent = '⏱ 预计 ' + hm + ' 完成 · 约剩 ' + fmtDur(e.secs)
                 + ' · 误差 ±' + half + ' 分钟'
                 + '（还差 ' + e.remain + ' 章，按最近'
                 + Math.round(e.win_min) + '分钟速度推算）';
}

function renderProgress(p) {
  // 2026-09-29：主数字以**平台侧真实完成**为准。p.done 数的是"平台回过 isPassed=true
  // 的任务名"，不校验平台是否结算，实测曾虚高 6.4 倍（界面 141 / 平台 22）。
  // 平台数据拉不到时才退回程序自报，并在下面明确标注，避免又被当成真实进度。
  const pf = p.platform || {};
  const useReal = pf.total > 0;
  // 程序自报口径（平台数据还没取到）下分母可能为 0 —— 没有「共 N 个章节任务点」
  // 那行时确实会这样，而此时 p.done 是**累计**完成数 ⇒ 会出现 "117 / 0"。
  // 分母缺失就拿完成数当分母（显示满格），不让界面出现无意义的比例。
  const dn = useReal ? pf.done : p.done;
  const tt = useReal ? pf.total : (p.total || p.done);
  $('pDone').textContent = dn;
  $('pTotal').textContent = tt;
  const pct = tt ? Math.min(100, Math.round(dn / tt * 100)) : 0;
  $('pFill').style.width = pct + '%';
  $('pPct').textContent = pct + '%';
  const note = $('pNote');
  if (note) {
    if (!useReal) {
      note.textContent = '⚠ 还没取到平台数据，此处是程序自报（会偏高）';
    } else {
      // 口径提示：p.done 是"任务点"维度（一个章节可有多个视频任务点），上方分母是
      // "章节"维度，两者不能相减 —— 早先写"其中 N 个平台未结算"是错的，已改。
      note.textContent = '程序累计完成 ' + p.done + ' 个任务点（任务点 ≠ 章节，不可直接相减）';
    }
  }

  renderEta(p);
  renderLastTaskTag(p);

  const c = p.current;
  $('curBox').classList.toggle('hide', !c);
  if (c) {
    // 多课程时，"正在刷哪门课"必须跟着任务点一起显示 —— 只给任务点名
    // 的话用户根本不知道自己勾的那几门里现在动的是哪个（用户原话）
    $('curName').textContent = (c.course ? c.course + ' · ' : '') + c.name;
    const cp = c.total ? Math.min(100, Math.round(c.pos / c.total * 100)) : 0;
    $('curFill').style.width = cp + '%';
    // 🔴 2026-09-29：播完后**平台要过几分钟才结算**（实测第一个视频等了 6 分钟、
    //    第 8 次上报才判通过）。原来一直写"播放中 11:13/11:13（100%）"，
    //    用户看到数字不动就以为程序卡死了（真机报障）。这里明确告知在等判定。
    $('curMeta').textContent = (cp >= 100)
      ? '已播完，等待平台判定… 平台通常要几分钟才结算，进度会稍后跳变'
      : '播放中 ' + fmtT(c.pos) + ' / ' + fmtT(c.total) + '（' + cp + '%）';
  }

  renderCourses(p.courses, p.risk_abort, (p.platform || {}).courses);

  $('nHit').textContent = p.hits;
  $('nMiss').textContent = p.misses;
  const _rn = p.random_n || 0;
  if ($('nMissLabel')) {
    // 卡片上那个数字是**未命中的题数**，"随机 N" 只是其中"连 AI 都没答出来、
    // 只能蒙"的一小部分。原写法「题库未命中·随机1」会被读成"143 个随机"，
    // 这里明确成括号补充（2026-10-08 何同学指出歧义）。
    $('nMissLabel').textContent = _rn
      ? ('题库未命中（其中随机 ' + _rn + '）') : '题库未命中';
  }
  $('nSub').textContent = p.submitted;
  $('nSave').textContent = p.saved;
  $('nCover').textContent = p.last_cover == null ? '-' : p.last_cover + '%';
  $('nScore').textContent = p.accuracy == null ? '-' : p.accuracy + '%';
  // "重做 N 轮"只在真的有重做时才显示 —— `RE_REDO` 依赖的日志格式
  // （`章节检测有 N/M 题回答错误`）在真实日志里从未出现，恒为 0，
  // 常驻一个 "重做 0 轮" 没有信息量还容易让人以为"该重做却没做"（2026-10-08）。
  $('sPass').textContent = (p.passed || p.redo)
    ? ('通过' + p.passed + '章' + (p.redo ? ' · 重做' + p.redo + '轮' : '')) : '';
  const lv = p.learned_verified || 0, lw = p.learned_wrong || 0;
  if ($('nReal')) {
    $('nReal').textContent = (lv + lw) ? Math.round(lv / (lv + lw) * 100) + '%' : '-';
    $('sReal').textContent = (lv + lw) ? ('判对 ' + lv + ' · 判错 ' + lw + ' 题组') : '暂无平台判定';
  }
}

// ---- 课程运行看板（2026-09-14）----
// 用户诉求：勾了多门课，运行界面看不到"在刷哪门""剩下的有没有在跑"。
// 这里把后端 run_tracker 的快照渲染成一张清单：正在刷 / 排队中 / 已完成 /
// 已跳过 / 中止，每种状态都有明确文字标签，不用用户去猜。
//
// ⚠ 「已完成」和「中止」必须是两种颜色两种文案（2026-09-24 用户报障）：
//   以前风控熔断后全被收口成 done，4 门课全绿写"已完成"，实际 0/321 一门没刷。
//   done=绿(真的刷完了) / aborted=橙(被风控或异常挡下，没刷完)。
const CSTAT = {
  running: {cls: 'running', txt: '正在刷'},
  pending: {cls: 'pending', txt: '排队中'},
  done:    {cls: 'done',    txt: '已完成'},
  skipped: {cls: 'skipped', txt: '已跳过'},
  aborted: {cls: 'aborted', txt: '未刷完'},
};
const CPHASE = {scan: '读取章节', video: '刷课', homework: '作业', done: '结束'};
// 中止原因分类 → 右侧标签文案（比笼统的"未刷完"更具体）
const ABORT_TAG = {risk: '风控中止', error: '异常中止', manual: '待人工'};

function renderCourses(info, riskAbort, platCourses) {
  const rows = (info && info.courses) || [];
  const box = $('cBoard');
  if (!box) return;

  // 平台侧每门课的章节完成数（2026-09-29 用户报障后接上）。
  // 顶部主数字已经改成平台真实值，卡片若还用程序自报的"任务点名计数"，
  // 同一页就会出现两套数字 —— 用户原话："上下显示的还是不一致"，
  // 截图里上面 137/139，下面三门加起来才 84/139。统一口径，全用平台值。
  const pmap = {};
  (platCourses || []).forEach((c) => {
    if (!c) return;
    if (c.id) pmap[String(c.id)] = c;
    if (c.title) pmap[String(c.title)] = c;
  });

  // 风控横幅：只要后端报了中止原因就出，且优先于其它提示
  const rb = $('riskBar');
  if (rb) {
    const msg = riskAbort || '';
    if (msg && rows.some(r => r.status === 'aborted')) {
      rb.classList.remove('hide');
      $('riskSub').innerHTML =
        escapeHtml(msg) + '。' +
        '<br><b>已中止的课程没有刷完，进度不会计入。</b>' +
        '请等 10~30 分钟冷却后，先在学习通上' +
        '<b>手动看完反复报 403 的那个视频</b>，再回来点「开始刷课」。';
    } else {
      rb.classList.add('hide');
    }
  }

  // 只有一门课且没别的情况时不占用版面（单课程场景原来的界面已经够了）
  if (!rows.length) { box.classList.add('hide'); return; }
  box.classList.remove('hide');

  const doneN = rows.filter(r => r.status === 'done').length;
  const runN = rows.filter(r => r.status === 'running').length;
  const abortN = rows.filter(r => r.status === 'aborted').length;
  // 汇总里必须单列"中止 N 门"：只说"已完成 0"用户不知道是没刷还是还没轮到
  $('cSum').textContent =
    `共 ${rows.length} 门 · 正在刷 ${runN} · 已完成 ${doneN}` +
    (abortN ? ` · ⚠ 未刷完 ${abortN}` : '');

  $('cList').innerHTML = rows.map(r => {
    const st = CSTAT[r.status] || CSTAT.pending;
    const parts = [];
    if (r.video) parts.push('刷课');
    if (r.work) parts.push('作业');
    const kind = parts.join(' + ');
    // 数字口径：优先平台（章节级，与顶部一致），拿不到才退回程序自报（任务点级）
    const pfc = pmap[String(r.id)] || pmap[String(r.title)] || null;
    const hasPlat = !!(pfc && pfc.total > 0);
    const dNum = hasPlat ? (pfc.done || 0) : (r.done || 0);
    const tNum = hasPlat ? pfc.total : (r.tasks || 0);
    const unit = hasPlat ? '章节' : '任务点';
    // 副标题：优先给"最有信息量"的那一条 —— 跳过原因、进度、作业、阶段。
    // 状态词（正在刷/排队中…）已经在右侧标签里，这里不重复。
    let sub;
    if (r.status === 'aborted') {
      // 中止的课最需要看到"哪门课 + 卡在哪 + 刷了多少"，
      // 光写原因会让人以为整门课被跳过了
      const prog = tNum ? ` · ${unit} ${dNum}/${tNum}` : '';
      sub = (r.reason || '被中止') + prog;
    } else if (r.status === 'skipped') {
      sub = r.reason || '未执行';
    } else if (tNum) {
      sub = (kind ? kind + ' · ' : '') + `${unit} ${dNum}/${tNum}`;
    } else if (r.reason) {
      sub = r.reason;
    } else if (kind) {
      sub = kind;
    } else {
      sub = CPHASE[r.phase] || '';
    }
    const pct = tNum ? Math.min(100, Math.round(dNum / tNum * 100)) : 0;
    const bar = tNum
      ? `<span class="mini-bar"><i style="width:${pct}%"></i></span>` : '';
    // 阶段后缀只在真拿到阶段名时才拼，否则会出现"正在刷·"这种空尾巴
    const ph = r.status === 'running' && r.active ? CPHASE[r.active] : '';
    // 中止的课右侧标签细化成"风控中止 / 异常中止 / 待人工"
    const tag = r.status === 'aborted'
      ? (ABORT_TAG[r.abort_kind] || st.txt) : st.txt;
    return `<div class="crow ${st.cls}">
      <span class="dot"></span>
      <span class="ct"><div class="cn">${escapeHtml(r.title || ('课程 ' + r.id))}</div>
        <div class="cr">${escapeHtml(sub)}</div></span>
      ${bar}
      <span class="cst">${tag}${ph ? '·' + ph : ''}</span></div>`;
  }).join('');
}

// 任务结束 / 暂停时也要刷新一下看板：pull() 停了以后不再轮询，
// 最后一次状态会停在整个任务结束前一刻（会显示"正在刷"），
// 所以这里在状态切换时补一次收尾刷新。
function refreshCourses() {
  fetch(BASE + '/api/logs?after=999999999').then(r => r.json())
    .then(r => { if (r.progress) renderCourses(r.progress.courses, r.progress.risk_abort,
                                                (r.progress.platform || {}).courses); })
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

function renderTaskResult(p) {
  // 把本轮任务（考试/作业）的逐条结果摆到界面上（2026-09-28）。
  // 原来这些只进日志文件，界面只会说"已结束"→ 用户以为程序什么都没做。
  const box = $('taskRes'), list = $('taskResList'), head = $('taskResHead');
  if (!box) return;
  const er = p.exam_result || {};
  const items = er.results || [];
  if (!items.length && !er.error) { box.classList.add('hide'); return; }
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  head.textContent = (p.task_name || '任务') +
    '结果（成功 ' + (er.done || 0) + ' · 跳过 ' + (er.skipped || 0) + '）';
  let html = er.error ? '<div class="trerr">任务异常：' + esc(er.error) + '</div>' : '';
  html += items.map(r => {
    const ok = r.ok !== false;
    // 🔴 2026-10-12：考试「只暂存未交卷」必须单独标出来 —— 它 ok=true（跑完了），
    //    submitted=false（我们没替你交）。都显示"成功"会让用户误以为已交卷。
    const staged = ok && r.submitted === false;
    const cls = !ok ? 'bad' : (staged ? 'staged' : 'ok');
    const label = !ok ? '未完成' : (staged ? '仅暂存·待交卷' : '成功');
    return '<div class="tri"><span class="trn">' + esc(r.title) + '</span>' +
           '<span class="trs ' + cls + '">' + label + '</span></div>' +
           (r.msg ? '<div class="trm">' + esc(r.msg) + '</div>' : '');
  }).join('');
  list.innerHTML = html;
  box.classList.remove('hide');
}

// ============ 考试接续执行（2026-09-28，按用户要求修正）============
// 用户原话："不是等他们刷完才弹确认，而是刷课选择完直接弹一次确认"。
// ⇒ 确认**只在点「开始」时弹一次**（btnStart 的那个 confirm，里面列出考试场次）。
// ⇒ 任务跑完后**不再弹第二次询问**，直接把勾选的考试接着跑掉。
// 考试的风险提示（只有一次机会 / 默认自动交卷）都在启动确认框里 —— 用户点头才算同意。
let pendingExam = null;        // {courseId: [条目,...]}，待接续执行的考试
let pendingExamAsked = false;  // 防重入：只接续一次

function examItemsToRun(byCourse) {
  const out = [];
  Object.entries(byCourse || {}).forEach(([cid, arr]) => {
    (arr || []).forEach(it => out.push({
      courseid: cid, clazzid: it.clazzid || '', cpi: it.cpi || '',
      exam_id: it.taskrefid || '', taskrefid: it.taskrefid || '',
      title: it.name || '考试', enc_task: it.enc_task || '',
      href: it.href || '', userid: it.userid || '',
    }));
  });
  return out;
}

/* 刷课/作业跑完后：直接把勾选的考试接着跑掉（**不再弹确认框**）。*/
function maybeRunExam(p) {
  if (!pendingExam || pendingExamAsked) return;
  if (p.running || p.paused || p.resuming) return;   // 还在跑，等它结束
  pendingExamAsked = true;
  const flat = examItemsToRun(pendingExam);
  pendingExam = null;
  if (!flat.length) return;
  $('msg3').textContent = `作业任务已结束，接着开考（${flat.length} 场）…`;
  launchPendingExam(flat);
}

async function launchPendingExam(flat) {
  $('msg3').textContent = `正在启动 ${flat.length} 场考试…`;
  try {
    const r = await fetch(BASE + '/api/exam/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        items: flat,
        // 2026-10-12：考试走**专用开关** `autoSubmitExam`，缺省 = 不提交（只暂存）。
        auto_submit: $('autoSubmitExam') ? $('autoSubmitExam').checked : false,
      })
    }).then(r => r.json());
    if (!r.ok) { $('msg3').textContent = r.msg || '开考失败'; return; }
    got = -1; $('log').textContent = '';
    startPull();
  } catch (e) { $('msg3').textContent = '开考请求失败：' + e; }
}

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
  renderTaskResult(p);      // 每轮都刷新结果卡片（含原因），别只写日志
  maybeRunExam(p);          // 任务结束后：接过勾选的考试继续跑（启动时已确认过）
}

/* 🔴 2026-10-13：考试独立进度条。与刷课大进度条**互不干扰** ——
   刷题由 renderProgress 画，考试由本函数画，各写各的 DOM。
   rr = /api/exam/status 的 result。 */
function renderExamProgress(rr) {
  const box = $('examProgBox');
  if (!box) return;
  const d = Number((rr || {}).done || 0);
  const t = Number((rr || {}).total || 0);
  if (!d && !t) { box.classList.add('hide'); return; }   // 没考试在跑就收起来
  box.classList.remove('hide');
  $('examProgNum').textContent = d + ' / ' + (t || '?');
  const pct = t ? Math.min(100, Math.round(d / t * 100)) : 0;
  $('examProgFill').style.width = pct + '%';
  $('examProgPct').textContent = t ? pct + '%' : '…';
  $('examProgTitle').textContent =
    '考试作答' + ((rr || {}).title ? ' · ' + rr.title : '');
  $('examProgNote').textContent = t
    ? '考试默认只暂存草稿，不替你交卷（需自己去学习通核对后交）'
    : '正在读取总题数…';
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
    // 🔴 2026-10-13：当前任务是考试时，画**独立考试进度条**；
    //   切回刷课则收起考试条。**完全不碰刷课大进度条**（两条并存不冲突）。
    try {
      const _isExam = (r.progress || {}).task_name === '考试作答';
      if (_isExam) {
        const es = await fetch(BASE + '/api/exam/status').then(x => x.json());
        renderExamProgress(es.result || {});
      } else {
        renderExamProgress(null);
      }
    } catch (e) { /* 进度显示失败不该影响任务 */ }
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
  try { await fetch(BASE + '/api/shutdown?all=1', {method: 'POST'}); } catch (e) {}
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
    # 🔴 2026-09-28：把**本轮任务结果**一并回给前端。
    #    起因：考试《550》因"限时提交：考试10分钟内不允许提交考试"而交卷失败，
    #    日志里有完整原因，但界面只显示「已结束，请到学习通核对最终进度」——
    #    用户报"几分钟不到就结束了，而且没有任何日志"。结果早就存在
    #    `state["exam_result"]` 里，只是没收发出去。
    _er = state.get("exam_result") or {}
    _res = [{"title": r.get("title") or r.get("name") or "",
             "ok": r.get("ok"), "msg": r.get("msg") or ""}
            for r in (_er.get("results") or [])][:30]
    return jsonify(logged_in=state["logged_in"], running=state["running"],
                   paused=state["paused"], resuming=state["resuming"],
                   task_name=state.get("task_name") or "",
                   exam_result={"done": _er.get("done", 0),
                                "skipped": _er.get("skipped", 0),
                                "error": _er.get("error") or "",
                                "results": _res},
                   account={"name": state.get("account_name") or "",
                            "user": state.get("account_user") or ""})


def _view_courses():
    """选课/清单界面看到的课程全集 = 常规课程 + 额外课程（超星班等）。

    🔴 2026-09-28 新增。**绝不写回 `state["courses"]`** —— 那会让下标错位、
       勾选错课（选课界面早期按下标回传）。这里每次现算，保证后端解析时
       用的列表与前端渲染时拿到的**同序同长**。

    额外课程带 `extra: True`，前端据此隐藏"章节任务"组（它们不在课程列表
    接口里、没有章节数据），只保留「作业 / 考试」两个维度。
    """
    base = list(state.get("courses") or [])
    try:
        from api import extra_courses
        have = {str(c.get("courseId") or "") for c in base}
        for c in extra_courses.as_courses():
            if str(c.get("courseId") or "") not in have:
                base.append(c)
    except Exception as e:
        logger.warning(f"读取额外课程失败（不影响常规课程）：{e}")
    return base


def _view_course_dicts():
    """给前端的课程列表（含额外课程标记）。"""
    return [{"title": c.get("title", ""), "courseId": c.get("courseId", ""),
             "clazzId": c.get("clazzId", ""),
             "extra": bool(c.get("extra")),
             "hasChapter": not bool(c.get("extra"))}
            for c in _view_courses()]


@app.get("/api/courses")
def api_courses():
    if not state["logged_in"]:
        return jsonify(ok=False, msg="未登录")
    return jsonify(ok=True, courses=_view_course_dicts())


@app.post("/api/courses/refresh")
def api_courses_refresh():
    """重新拉取课程列表 —— 平台新发的课不必再「切换账号」重登才能看到。

    2026-09-30 用户要求（原话）：
      「进入这种刷完了、但是又有新的课程出现的课程，进去之后无法刷新课程。
        就比如他刚发的课程，我今天刚发了一个劳动教育，然后我进去之后
        怎么也刷不出来，只能切换账号重新登录才能刷出来。
        应该要在这个界面插上一个课程刷新的按钮，而不是上面的那个重新扫描，
        是扫描作业和考试的。」

    为什么原先刷不出：`state["courses"]` 只在**登录时**填一次
    （`/api/login` → `_do_login`），之后所有视图都读这份缓存，
    平台侧新增的课程不会自动出现。

    ⚠️ 与 `/api/tasks/scan`（重新扫描）区分：那个扫的是**作业和考试**，
    只在课程列表为空时才顺带拉一次课程；本接口专职刷课程。
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    try:
        courses = state["chaoxing"].get_course_list()
    except Exception as e:                                     # noqa: BLE001
        return jsonify(ok=False, msg=f"读取课程失败：{e}")
    if not courses:
        return jsonify(ok=False, msg="平台没返回课程（可能是登录已过期，试试重新登录）")
    state["courses"] = courses
    return jsonify(ok=True, courses=_view_course_dicts())


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
                   courses=_view_course_dicts())


def _do_login(data):
    """登录本体，网页与自动恢复共用。返回 (ok, msg, courses)。"""
    # 🔴 2026-09-27：内置 AI 池兜底（用户要求界面不再单配 AI）。
    #    必须在读 tiku_type / need_ai 之前套用，否则后面的 key 校验会误判。
    data = _apply_default_ai(data)
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    tiku_type = data.get("tiku_type") or "none"
    need_ai = tiku_type == "ai" or (tiku_type == "adapter" and data.get("ai_fallback"))
    if tiku_type == "yanxi" and not (data.get("token") or "").strip():
        return False, "已选择言溪题库，但未填写 Token", []
    if need_ai:
        # 🔴 2026-09-26 23:25 修缺陷：原来只校验 `ai_key`（单个），
        #    用户在**只填 Key 池 `ai_keys`、留空单个 Key** 时会被误判为
        #    "未填写 Key" → 整个登录被拒（真机踩坑）。现在两个位置任填其一即可。
        _has_single = bool((data.get("ai_key") or "").strip())
        _has_pool = bool((data.get("ai_keys") or "").strip())
        if not (_has_single or _has_pool):
            return False, "开启了 AI（纯 AI 或兜底），但未填写 Key（单个或 Key 池）", []
        if not (data.get("ai_endpoint") or "").strip():
            return False, "开启了 AI（纯 AI 或兜底），但未填写 Endpoint", []

    try:
        tiku = build_tiku(tiku_type, data)
        # 🔴 2026-11-07：DummyTiku = 一个题库都没配上 ⇒ 答题全走随机/AI，
        #   而且是**静默**的（日志只写「随机兜底」，看不出根因）。
        #   实测代价：免疫学作业1 一次重做 201 题里 165 题全错。
        #   这里明确告警，别再让"没配题库"看起来像"程序答错了"。
        if type(tiku).__name__ == "DummyTiku" or getattr(tiku, "DISABLE", False):
            logger.warning("题库未配置（tiku_type=%r）→ 答题将退化为随机/纯 AI",
                           tiku_type)
            state["tiku_warn"] = "未配置题库，答题会退化为随机或纯 AI"
        else:
            state["tiku_warn"] = ""
        # 🔴 2026-10-12：考试题目落盘目录（题干+完整选项+程序答案）。
        #   考试正确率此前无法核算（日志只有字母、没有选项全文），而要拿
        #   选项只能"再进一次考场"——反复 start 未交卷会被平台限制。
        #   ⇒ 趁本场在考场内落盘，是零额外风险的取数时机。
        #   空值 = 不落盘，行为与从前完全一致。
        _dump_dir = str(data.get("exam_dump_dir") or "").strip()
        _extra = {"query_delay": 1.0, "work_max_retries": 3}
        if _dump_dir:
            _extra["exam_dump_dir"] = _dump_dir
        if username and password:
            cx = Chaoxing(account=Account(username, password), tiku=tiku,
                          **_extra)
            res = cx.login()
        else:
            cx = Chaoxing(tiku=tiku, **_extra)
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


def _sanitize_work_items(items):
    """把前端传来的作业清单洗成后端能直接跑的形状。

    🔴 2026-09-26：用户在**明细里逐份勾选**后，提交的就不再是"哪几门课"，
    而是"哪几份作业"。这份清单必须精确到份 —— 尤其 `taskrefid` 是唯一的
    身份标识（课程里的作业顺序会变，顺序号不可靠）。

    这里做两件事：
      1. 丢掉没有 `taskrefid` 的条目（没有它就取不到详情页，跑了也是空转）
      2. 逐字段白名单化，避免前端顺手把整个对象塞进来
    """
    if not items:
        return {}
    out = {}
    for k, arr in (items or {}).items():
        cid = str(k or "").strip()
        if not cid or not isinstance(arr, list):
            continue
        clean = []
        for it in arr:
            if not isinstance(it, dict):
                continue
            tid = str(it.get("taskrefid") or "").strip()
            if not tid:
                continue
            clean.append({
                "name": it.get("name"),
                "status": it.get("status"),
                "taskrefid": tid,
                "enc_task": it.get("enc_task") or "",
                "href": it.get("href") or "",
                "cpi": it.get("cpi") or "",
                "userid": it.get("userid") or "",
                "clazzid": it.get("clazzid") or "",
                "redo": bool(it.get("redo")),
            })
        if clean:
            out[cid] = clean
    return out


def _ui_default_jobs() -> int:
    """启动参数 `jobs` 的兜底值：回读登录时保存的 `last_params.jobs`。

    🔴 2026-10-01 修 bug：`data` 里没带 jobs 时（自动恢复 / start_last /
    作业启动等路径都会走到），旧代码硬编码 `or 1` —— 于是界面上设好的
    "并发章节数 2" 在这些路径上**悄悄退化成 1**（实测 8 个实例里 6 个
    `worker_num=1`，而 ui_config 里写的是 2）。更糟的是这个 1 会被写回
    `resume.config` / `last_pick`，从此**永久固定**，怎么改配置都不生效。
    """
    try:
        v = (load_ui_config().get("last_params") or {}).get("jobs")
        return max(1, int(v))
    except Exception:
        return 1


def _do_start(indices, data):
    """启动刷课本体，网页与自动恢复共用。返回 (ok, msg)。

    支持的入参形态：
      a) 前端首次启动：indices + video/work（都是"课程下标"数组）
      b) 自动恢复：video_course_ids / work_course_ids（courseId 数组）
      c) 旧调用方：只有 indices → 等价"全部课程都刷课"（保持旧行为）
      d) 🔴 2026-09-26 新增：`work_items` = {courseId: [作业条目, ...]}
         —— **精确到"哪几份作业"**，不再整门课全做。给了它就以它为准：
         作业清单以字段本身携带的 courseId 为准，`work`（下标数组）只是
         用来"顺带勾上这些课的视频/测验"的补充。自动恢复会原样回放它。
    """
    if state["running"]:
        return False, "已有任务在运行"
    if not state["chaoxing"]:
        return False, "请先登录"

    def _ids_from_indices(key):
        # 🔴 2026-09-28：下标要按**界面看到的那份列表**解析（常规 + 额外课程），
        #    否则用户在界面上勾的额外课程会指到别的课上去。
        _vc = _view_courses()
        out = set()
        for i in (data.get(key) or []):
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(_vc):
                out.add(_course_id(_vc[i]))
        return out

    # 2026-09-15 ——— 内容维度拆成三个独立勾选框：视频 / 测验 / 作业 ———
    #
    # 背景：章节测验（workid）此前只能跟着"刷课"一起做（它和视频同属章节任务点，
    # 由 process_chapter 遍历处理），没法规避"我只想做测验、不想刷视频"。
    # 用户明确要求拆开，并要"默认全做"。
    #
    # 三个维度的落地方式：
    #   视频 video → 走章节遍历，only_kinds 含 "video"
    #   测验 quiz  → 走章节遍历，only_kinds 含 "quiz"
    #   作业 work  → 走事后独立的 _run_homeworks（不受 only_kinds 影响）
    # 若某门课只勾了测验，就只遍历章节、只答题、视频一律跳过。
    # 🔴 精确作业清单（可空）。非空 = 这门课的作业"只做清单里的这几份"。
    work_items = _sanitize_work_items(data.get("work_items"))
    # 考试清单同理（考试自动作答链路通了之后即可直接生效）
    exam_items = _sanitize_work_items(data.get("exam_items"))

    has_selection = any(k in data for k in ("video", "work", "quiz"))
    # 🔴 2026-09-28：三点改动
    #   ① 课程集合改用 `_view_courses()`（常规 + 额外课程），与界面同序
    #   ② `chosen` 并入 `exam_items.keys()` —— 此前只并 video/work/quiz，
    #      **只勾「考试」的课会被整门漏掉**（picked 里没有它 → 白勾）
    #   ③ exam 维度**不在这里自动执行**：考试只有一次机会，按用户要求
    #      "弹框确认"—— 作业跑完后前端再弹确认框，确认了才调 /api/exam/run
    _view = _view_courses()
    if "video_course_ids" in data or "work_course_ids" in data or "quiz_course_ids" in data:
        # 自动恢复：已经是 courseId
        video_ids = {str(x) for x in (data.get("video_course_ids") or [])}
        work_ids = {str(x) for x in (data.get("work_course_ids") or [])}
        quiz_ids = {str(x) for x in (data.get("quiz_course_ids") or [])}
        exam_ids = {str(x) for x in (data.get("exam_course_ids") or [])}
        # 清单里出现的课，即便 work_course_ids 漏了也必须算进来，否则
        # 清单存下来了却因为"不在勾选里"被整门跳过。
        work_ids |= set(work_items.keys())
        chosen = video_ids | work_ids | quiz_ids | exam_ids | set(exam_items.keys())
        picked = [c for c in _view if _course_id(c) in chosen]
    elif has_selection:
        # 前端启动：下标 → courseId
        video_ids = _ids_from_indices("video")
        work_ids = _ids_from_indices("work")
        quiz_ids = _ids_from_indices("quiz")
        # 同上：清单自身携带的 courseId 一定算选中
        work_ids |= set(work_items.keys())
        chosen = video_ids | work_ids | quiz_ids | set(exam_items.keys())
        picked = [c for c in _view if _course_id(c) in chosen]
    else:
        # 旧调用方：不区分内容，全部按"刷课"处理（含测验），保持兼容
        video_ids = work_ids = None
        quiz_ids = set()
        picked = [_view[i] for i in indices
                  if 0 <= i < len(_view)]

    if not picked:
        return False, "未选中任何课程"

    # 🔴🔴 2026-10-13 修（何同学实测反馈，"我压根无法自己启动考试"）：
    #   **只勾了考试** ⇒ 直接开考，不进刷课链路。
    #
    #   症状：勾「☐视频 ☐章节测验 ☐作业 ☑考试」后点开始，界面一直显示
    #         「视频节点」/「已完成」，考试迟迟不启动（实测 acc4 连续 3 分钟
    #         每 30s 读一次章节列表，考试相关日志一条都没有）。
    #   原因：考试按设计是「刷课/作业**跑完后**接着开考」，前端
    #         `maybeRunExam` 要等 `running=false` 才接续；而只勾考试时
    #         `walk_course_ids` 与 `only_kinds` 都为空 ⇒ 刷课链路空转数分钟
    #         ⇒ 考试被无限排在后面，用户看到的却是"在刷视频"。
    #   修法：没有任何刷课/作业内容时，直接 `_launch_exam_only`。
    #   （勾了刷课/作业时保持原逻辑：先刷后考，不变。）
    if exam_items and not (video_ids or quiz_ids or work_ids):
        _flat = []
        for _cid, _arr in exam_items.items():
            for _it in _arr or []:
                _flat.append({
                    "courseid": str(_cid),
                    "clazzid": _it.get("clazzid") or "",
                    "cpi": _it.get("cpi") or "",
                    "exam_id": str(_it.get("taskrefid") or ""),
                    "title": _it.get("name") or "考试",
                    "enc_task": _it.get("enc_task") or "",
                })
        _flat = [x for x in _flat if x["exam_id"]]
        if not _flat:
            return False, "没有有效的考试条目"
        try:
            _launch_exam_only(
                _flat, auto_submit=bool(data.get("exam_auto_submit")))
        except Exception as _e:                                  # noqa: BLE001
            return False, f"启动考试失败：{type(_e).__name__}: {_e}"
        # 标记给 api_start 回传前端：让它清掉 pendingExam，避免跑完后再开一次
        state["exam_launched_by_start"] = True
        return True, f"已直接开考 {len(_flat)} 场（本次未勾视频/测验/作业）"

    cfg = {"speed": min(2.0, max(1.0, float(data.get("speed") or 1.0))),
           # jobs 缺失时回读 ui_config（见 _ui_default_jobs 的说明），别再硬兜 1
           "jobs": max(1, int(data.get("jobs") or _ui_default_jobs())),
           "notopen_action": "retry", "retry_interval": 1.0}
    # 🔴 2026-09-26 新增：主界面「提交方式」必须能覆盖登录时缓存的配置。
    # 背景：`build_tiku` 的 SUBMIT / COVER_RATE 来自**登录那一刻**保存的
    # ui_config，登录后在主界面改开关完全不起作用（用户会认为"改了没用"）。
    # 这里把本次启动携带的值透传下去，由 _run_homeworks 在答题前现场套用。
    # 缺省（自动恢复/旧调用方）不带这两个键 → 沿用登录时的配置，行为不变。
    if "auto_submit" in data:
        cfg["auto_submit"] = bool(data.get("auto_submit"))
    if "cover_rate" in data:
        try:
            cfg["cover_rate"] = min(1.0, max(0.0, float(data.get("cover_rate"))))
        except (TypeError, ValueError):
            pass
    # video_course_ids 为 None = 未做区分（旧行为：全部刷课）
    if video_ids is not None:
        cfg["video_course_ids"] = sorted(video_ids)
        cfg["work_course_ids"] = sorted(work_ids)
        cfg["quiz_course_ids"] = sorted(quiz_ids)
        # 章节遍历型的白名单：某门课勾了视频或测验才需要遍历该课章节。
        # 只勾"作业"的课不在此列（它走 _run_homeworks，不需要遍历章节）。
        walk_ids = video_ids | quiz_ids
        cfg["walk_course_ids"] = sorted(walk_ids)
        # 全局内容开关：本次任务里"视频"和"测验"各自是否至少有一门被勾。
        # 用于 process_job 的 only_kinds —— 注意这是**全局**粒度，
        # 逐课粒度由 walk_course_ids 控制（不在集合里的课根本不遍历）。
        only = set()
        if video_ids:
            only.add("video")
        if quiz_ids:
            only.add("quiz")
        cfg["only_kinds"] = sorted(only)
    # 🔴 精确清单必须透传：`_run_homeworks` 看到它就"只做这几份"，
    # 没看到就退回"整门课全做"（旧的、会误伤的行为）。
    if work_items:
        cfg["work_items"] = work_items
    if exam_items:
        cfg["exam_items"] = exam_items
    # 🔴 2026-10-27（何同学要求）：把"勾了考试的课程"交底给 `_work`。
    #   课程考试要**刷完课才解锁** —— 点「开始」那一刻扫到的是未解锁状态，
    #   拿到的 enc_task 也会失效，所以那份 exam_items 开不了考。
    #   ⇒ 先记下"哪几门课要考"，刷课结束后按课重扫、拿新签名再开考。
    #   注意：用 exam_course_ids（前端按勾选给）而不是 exam_items.keys()，
    #   因为考试未解锁时 exam_items 可能就是空的。
    _exam_cids = {str(x) for x in (data.get("exam_course_ids") or [])}
    _exam_cids |= set(exam_items.keys())
    if _exam_cids:
        cfg["exam_course_ids"] = sorted(_exam_cids)
        cfg["exam_auto_submit"] = bool(data.get("exam_auto_submit", False))
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
    state["risk_abort"] = ""      # 新一轮开始：清掉上一轮的风控横幅
    # 清空"被 403 拦过的视频"屏蔽名单（2026-09-24）：
    # 用户手动点「开始刷课」说明已经过了冷却期，允许重新尝试。
    # 不这么做的话，一旦某视频被拦过一次，之后永远刷不动。
    try:
        if state.get("chaoxing"):
            state["chaoxing"]._clear_blocked()
    except Exception:
        pass
    # 2026-09-29：开新任务时**立刻清掉平台进度缓存**。否则界面会残留上一轮的
    # 进度（用户实测：实例 6 一点进去先显示上次刷完的 131 章，"卡半天"才变成
    # 新课程的真实值）—— 因为采集线程要等 30 秒才刷一轮，这期间读到的全是旧值。
    with _PLAT_LOCK:
        _PLAT.update({"at": 0.0, "done": 0, "total": 0, "courses": [], "err": ""})
        _PLAT_HIST.clear()
    state["last_courses"] = picked
    state["last_config"] = config
    # 任务进行中保留"待恢复"标记：任何非正常退出（崩溃/被杀）重启后都能自动续刷
    _save_resume(picked, config)
    # 再长期记一份选课（跑完也不清），供网关的错峰启动复用
    _save_last_pick(picked, config)
    # 防睡眠不在这里做：请求是线程级的，本函数跑在 Flask 请求线程上，
    # 返回后请求即被清除。真正的持有在 _work()（长期线程）里。
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
        # 🔴 恢复时也要把"哪几份作业"一起交底，不能只回放 course_ids。
        # `_do_start` 里 sanitize 会校验清单形状，脏数据自行丢弃。
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
    # 🔴 2026-10-01：原先漏了这两个 —— 登出后 `account_name` 残留，
    #    网关上该实例仍显示上一个账号的姓名（看着像"已登录"）。
    state["account_name"] = ""
    state["account_user"] = ""
    return jsonify(ok=True)


@app.post("/api/clear_account")
def api_clear_account():
    """🔴 2026-10-01 新增（何同学要求）：**彻底清空该实例的账号信息**。

    与 /api/logout 的区别：logout 只清内存里的登录态，磁盘上 ui_config.json 里的
    档案（profiles / last_params / last_pick / resume）都还在，下次打开页面会回填。
    本接口把磁盘上这些账号痕迹一并抹掉，让实例回到"全新未配置"状态。
    """
    if state["running"]:
        return jsonify(ok=False, msg="任务正在运行，请先停止再清空")
    # 内存
    state["chaoxing"] = None
    state["courses"] = []
    state["logged_in"] = False
    state["account_name"] = ""
    state["account_user"] = ""
    state["last_courses"] = []
    state["last_config"] = {}
    state["resume"] = None
    # 磁盘：只删账号相关键，保留题库/AI 等公共配置
    try:
        cfg = load_ui_config()
        for k in ("profiles", "last_params", "last_pick", "resume"):
            cfg.pop(k, None)
        _dump(cfg)
    except Exception as e:
        return jsonify(ok=False, msg=f"清理配置失败：{e}")
    return jsonify(ok=True, msg="已清空该实例的账号信息")


@app.post("/api/test-ai")
def api_test_ai():
    """AI 连接测试：真实发一次小请求，返回延迟或错误。

    🔴 2026-09-26 23:25 支持 Key 池：只填 `ai_keys` 时取**第一条**槽位来测
    （支持 `Key|Endpoint|Model` 三段格式，各字段可覆盖上面的输入框）。
    """
    from openai import OpenAI
    data = request.get_json(force=True) or {}
    endpoint = (data.get("ai_endpoint") or "").strip()
    key = (data.get("ai_key") or "").strip()
    model = (data.get("ai_model") or "").strip()
    # 池里第一条作为补充（单 Key 为空时）
    pool = (data.get("ai_keys") or "").strip()
    if not key and pool:
        first = ""
        for line in pool.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                first = line
                break
        if first:
            parts = [p.strip() for p in first.split("|")]
            key = parts[0]
            if len(parts) > 1 and parts[1]:
                endpoint = parts[1]
            if len(parts) > 2 and parts[2]:
                model = parts[2]
    if not (endpoint and key and model):
        return jsonify(ok=False, msg="请先填写 Endpoint、Key（单个或池里第一条）和模型名")
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


@app.post("/api/tasks/scan")
def api_tasks_scan():
    """启动「作业 + 考试」全量扫描（后台线程，立即返回）。

    2026-09-26 新增：用户要求"登录后进入选课界面就把所有课程的作业和
    考试都扫出来"，且"考试由老师发布后才出现，要保证发布后能识别"。

    扫描耗时：作业 ~0.5s（全量页 1 次请求）+ 考试 ~0.3s/门（必须逐门拿令牌）
    → 58 门课约 17 秒。故走后台线程，前端轮询 /api/tasks/scan/status。
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    cx = state["chaoxing"]
    courses = state["courses"] or []
    if not courses:
        try:
            courses = cx.get_course_list()
            state["courses"] = courses
        except Exception as e:
            return jsonify(ok=False, msg=f"读取课程失败：{e}")

    with state["task_scan_lock"]:
        if state["task_scan"]["running"]:
            return jsonify(ok=True, already=True)
        state["task_scan"].update({
            "running": True, "done": False, "msg": "正在扫描作业…",
            "i": 0, "n": len(courses), "result": None, "err": "",
            "started": time.time(),
        })

    def _work():
        ts = state["task_scan"]

        def _prog(i, n, title):
            ts["i"] = i
            ts["n"] = n
            ts["msg"] = f"正在扫描考试… {i}/{n}  {title[:24]}"

        try:
            ts["msg"] = "正在扫描作业…"
            res = cx.scan_all_tasks(courses, with_exam=True,
                                    exam_progress=_prog)
            ts["result"] = res
            ts["msg"] = (f"扫描完成：作业 {res['work_total']} 份"
                         f"（未完成 {res['work_todo']}）·"
                         f" 考试 {res['exam_total']} 场"
                         f"（未完成 {res['exam_todo']}）")
        except Exception as e:
            ts["err"] = f"{type(e).__name__}: {e}"
            ts["msg"] = f"扫描失败：{ts['err']}"
        finally:
            ts["running"] = False
            ts["done"] = True
            ts["i"] = ts["n"]

    threading.Thread(target=_work, daemon=True).start()
    return jsonify(ok=True, started=True, n=len(courses))


@app.get("/api/tasks/scan/status")
def api_tasks_scan_status():
    """轮询扫描进度。done=True 时 result 里是完整清单。"""
    ts = state["task_scan"]
    out = {"ok": True, "running": ts["running"], "done": ts["done"],
           "msg": ts["msg"], "i": ts["i"], "n": ts["n"], "err": ts["err"]}
    if ts["done"] and ts["result"]:
        out.update(ts["result"])
    return jsonify(**out)


# ==================== 考试清单（2026-09-27 新增）====================
# 背景：超星班等**新建班级的课程不在任何课程列表接口里**（/api/courses、
# visit/courselistdata、stu-work 全都没有），常规考试扫描看不到它们的考试。
# 对策：允许手动登记「额外课程」（只需 courseId + clazzId，cpi 扫描时自动解析），
# 扫描时一并纳入，界面上单独标注。

@app.post("/api/exam/scan")
def api_exam_scan():
    """扫描全部考试（常规课程 + 手动登记的额外课程）。**只读，绝不提交**。"""
    if not state.get("logged_in"):
        return jsonify(ok=False, msg="请先登录")
    es = state.setdefault("exam_scan", {})
    if es.get("running"):
        return jsonify(ok=False, msg="正在扫描中，请稍候")
    courses = list(state.get("courses") or [])
    es.update({"running": True, "done": False, "err": "",
               "msg": "开始扫描考试…", "i": 0, "n": max(1, len(courses)),
               "result": None})

    def _work():
        try:
            from api.base import SessionManager
            from api.exam import scan_exams_all as _scan_exams
            from api import extra_courses
            _extra = extra_courses.as_courses()
            allc = courses + _extra
            es["n"] = max(1, len(allc))

            def _prog(i, n, title):
                es["i"] = i
                es["n"] = n
                es["msg"] = f"正在扫描考试… {i}/{n}  {str(title)[:24]}"

            res = _scan_exams(SessionManager.get_session(), allc, progress=_prog)
            es["result"] = res
            es["msg"] = (f"扫描完成：考试 {res.get('total', 0)} 场"
                         f"（可作答 {res.get('todo', 0)}）· 课程 {len(allc)} 门"
                         + (f"（含额外课程 {len(_extra)} 门）" if _extra else ""))
        except Exception as e:
            es["err"] = f"{type(e).__name__}: {e}"
            es["msg"] = f"扫描失败：{es['err']}"
        finally:
            es["running"] = False
            es["done"] = True
            es["i"] = es["n"]

    threading.Thread(target=_work, daemon=True).start()
    return jsonify(ok=True, started=True, n=len(courses))


@app.get("/api/exam/scan/status")
def api_exam_scan_status():
    """轮询考试扫描进度；done=True 时带 courses（每项含 extra 标记）。"""
    es = state.get("exam_scan") or {}
    out = {"ok": True, "running": bool(es.get("running")),
           "done": bool(es.get("done")), "msg": es.get("msg") or "",
           "i": es.get("i", 0), "n": es.get("n", 0), "err": es.get("err") or ""}
    if es.get("done") and es.get("result"):
        out.update(es["result"])
    return jsonify(**out)


@app.get("/api/exam/extra")
def api_exam_extra_list():
    """列出已登记的「额外课程」。"""
    from api import extra_courses
    return jsonify(ok=True, items=extra_courses.load())


@app.post("/api/exam/extra")
def api_exam_extra_add():
    """登记一门额外课程（courseId + clazzId 必填）。"""
    from api import extra_courses
    d = request.get_json(silent=True) or {}
    return jsonify(**extra_courses.add(d.get("courseid"), d.get("clazzid"),
                                       d.get("title")))


@app.delete("/api/exam/extra")
def api_exam_extra_del():
    """删除一门已登记的额外课程。"""
    from api import extra_courses
    d = request.get_json(silent=True) or {}
    return jsonify(**extra_courses.remove(d.get("courseid"), d.get("clazzid")))


def _spawn_recycle(cx, scan_res) -> None:
    """后台回收「已批阅作业」的正确答案（失败静默，绝不影响主流程）。

    🔴 2026-09-27 新增。背景：作业的正确答案本来只在"提交那一瞬间"有机会沉淀，
    而平台批阅总是滞后 → 作业答案永远进不了题库。实测发现已批阅详情页会
    **直接给出正确答案**，且不依赖"我的答案"，所以**不必等批阅**：
    每次扫描作业时顺带探一次即可 —— 已批阅的回收，没批阅的下次再探。
    零轮询、零定时器，只在用户主动扫描时触发。
    """
    def _run(works):
        try:
            from api.recycle import try_recycle_all
            st = try_recycle_all(cx, works, log=logger.info)
            if st["done"]:
                logger.info(f"♻ 作业答案回收：{st['done']} 份，"
                            f"新增正确答案 {st['added']} 题，错题记录 {st['wrong']} 条")
        except Exception as e:
            logger.debug(f"作业答案回收跳过（不影响主流程）：{e}")

    try:
        works = [w for c in (scan_res.get("courses") or [])
                 for w in (c.get("works") or [])]
        if works:
            threading.Thread(target=_run, args=(works,), daemon=True).start()
    except Exception:
        pass


@app.post("/api/homework/scan")
def api_homework_scan():
    """扫描**所有课程**的作业清单（只读，绝不提交）。

    2026-09-26 新增：用户要求"先别盲目开始做，让我进入页面选择做哪些"。
    返回按课程分组、逐份列出，前端弹窗里逐份勾选。
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    cx = state["chaoxing"]
    # ⚠️ 用 list() 复制：下面要往里追加额外课程，绝不能污染 state["courses"]
    #    —— 选课界面按**下标**索引 `state["courses"][i]` 回传勾选，
    #    往原列表塞元素会让下标错位、勾选错课。
    courses = list(state["courses"] or [])
    if not courses:
        try:
            courses = cx.get_course_list()
            state["courses"] = courses
        except Exception as e:
            return jsonify(ok=False, msg=f"读取课程失败：{e}")
    # 🔴 2026-09-28：并入「额外课程」（超星班等）。
    #    此前只有**考试**扫描并入了（`scan_exams_all`），作业扫描只认
    #    state["courses"] → 同一门课考试扫得到、作业扫不到
    #    （用户报："既然能识别出来考试和作业，为什么不把课程直接加到列表"）。
    try:
        from api import extra_courses
        _have = {str(c.get("courseId")) for c in courses}
        courses += [c for c in extra_courses.as_courses()
                    if str(c.get("courseId")) not in _have]
    except Exception as e:
        logger.warning(f"并入额外课程失败（不影响常规课程扫描）：{e}")
    try:
        res = cx.scan_homeworks_all(courses, include_done=True)
    except Exception as e:
        return jsonify(ok=False, msg=f"作业扫描失败：{type(e).__name__}: {e}")
    # 🔴 2026-09-27：顺带回收已批阅作业的正确答案（后台线程，不阻塞本次返回）
    _spawn_recycle(cx, res)
    return jsonify(ok=True, **res)


@app.post("/api/work/redo")
def api_work_redo():
    """重置指定作业的作答记录（= 平台「重做」按钮的等价操作）。

    2026-09-26 新增。用户要求："作业和考试这些扫描出来可以重做的，加个重做按钮，
    并且提示还有几次重做次数。然后点重做按钮就可以重做。"

    ⚠️ 副作用（平台原文）：会覆盖上一次作答记录，最终成绩按该作业的取分规则
       （「最高成绩」不亏 / 「本次成绩」考低了会亏）结算。前端已弹 confirm 提示。

    入参：{"courseid": "…", "taskrefid": "…"}
    返回：{"ok": bool, "msg": str, "redotimes": int}
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    if state["running"]:
        return jsonify(ok=False, msg="当前有任务在跑，请先暂停再重做")
    data = request.get_json(force=True) or {}
    cid = str(data.get("courseid") or "")
    tid = str(data.get("taskrefid") or "")
    if not (cid and tid):
        return jsonify(ok=False, msg="缺少 courseid / taskrefid")

    courses = state["courses"] or []
    course = next((c for c in courses if str(c.get("courseId")) == cid), None)
    if not course:
        # 🔴 2026-09-27 修：班级型课程（HHHH）不在 get_course_list() 里，
        # 只在统一扫描缓存中（同 _launch_homework_only 的 orphan 兜底）。
        # 原先只查 state["courses"] → 重做 HHHH 的作业恒报「找不到该课程」。
        c2 = None
        for c3 in ((state.get("task_scan") or {}).get("result") or {}).get("courses") or []:
            if str(c3.get("courseid") or c3.get("courseId") or "") == cid:
                c2 = c3
                break
        if c2:
            course = {"courseId": cid, "courseid": cid,
                      "title": c2.get("title") or cid,
                      "clazzId": c2.get("clazzId") or c2.get("clazzid") or "",
                      "cpi": c2.get("cpi") or ""}
    if not course:
        return jsonify(ok=False, msg="找不到该课程，请刷新课程列表")

    # 🔴🔴 2026-09-26 修复（用户报「重做失败：作业详情页未解析到 workId」的根因）：
    # 这里原来只传 taskrefid/workid/enc_task，**丢了 href 和 cpi**，而
    #   · href 是 `fetch_work_detail` 的首选路径（带完整 enc_task+userId+clazzId+cpi
    #     的签名 URL，实测 12841B 可解析）；缺了它只能走手工拼参数兜底；
    #   · 兜底路径里 `cpi` 又是 `doHomeWork` 的必备参数，缺了会被平台打回
    #     **3991B 壳页** → parse_work_detail 解析不到 workId → 抛
    #     "作业详情页未解析到 workId"，与用户截图完全一致（已本地复现）。
    # 修法：① 从前端传入的 href 优先用；② 没有 href 时**从扫描结果里按
    #       taskrefid 反查**拿全字段；③ 仍缺 cpi 就用 course 里的兜底。
    href = (data.get("href") or "").strip()
    enc_task = data.get("enc_task") or ""
    cpi = data.get("cpi") or course.get("cpi") or ""
    userid = data.get("userid") or ""

    if not (href and enc_task and cpi):
        # 反查：统一任务扫描的结果（state["task_scan"]["result"]）里存着完整字段
        # 结构：{"courses": [{"courseid","title","works":[{...,"href","enc_task","cpi"}]}]}
        try:
            res = (state.get("task_scan") or {}).get("result") or {}
            for c2 in (res.get("courses") or []):
                for w in (c2.get("works") or []):
                    if str(w.get("taskrefid")) == tid:
                        href = href or (w.get("href") or "")
                        enc_task = enc_task or (w.get("enc_task") or "")
                        cpi = cpi or (w.get("cpi") or "")
                        userid = userid or (w.get("userid") or "")
        except Exception as e:
            print(f"[重做] 反查作业缓存失败（taskrefid={tid}）：{e}")

    # ④ 终极兜底：按平台 URL 规则自己拼 href（参数与真机一致）
    if not href:
        from urllib.parse import urlencode
        qs = urlencode({
            "taskrefId": tid, "msgId": "0", "courseId": cid,
            "userId": userid, "clazzId": course.get("clazzId") or "",
            "type": "work", "enc_task": enc_task, "cpi": cpi, "ut": "s",
        })
        href = ("https://mooc1.chaoxing.com/mooc-ans/android/"
                f"mtaskmsgspecial?{qs}")

    work = {"taskrefid": tid, "workid": tid, "courseid": cid,
            "clazzid": course.get("clazzId"),
            "userid": userid, "cpi": cpi,
            "enc_task": enc_task, "href": href}
    print(f"[重做] taskrefid={tid} href={'有' if href else '无'} "
          f"cpi={cpi or '(空)'} enc_task={'有' if enc_task else '无'}")
    try:
        from api.homework import fetch_work_detail, redo_work, HomeworkNeedsAttention
        from api.base import SessionManager
        sess = SessionManager.get_session()
        detail = fetch_work_detail(sess, course, work)
        res = redo_work(sess, course, detail)
    except HomeworkNeedsAttention as e:
        return jsonify(ok=False, msg=f"作业详情读取失败：{e}")
    except Exception as e:
        return jsonify(ok=False, msg=f"重做失败：{type(e).__name__}: {e}")

    ok = bool(res.get("status"))
    left = max(0, (detail.get("redotimes") or 0) - (1 if ok else 0))
    return jsonify(ok=ok, msg=res.get("msg") or ("重做成功" if ok else "重做失败"),
                   redotimes=left, score_rule=detail.get("score_rule", ""))


@app.post("/api/work/readback")
def api_work_readback():
    """🔴 2026-09-26 新增：**只读**诊断 —— 从平台回读某份作业"现在存了什么答案"。

    用户要求（原话）：
      「提交时需要测它的完成率这些，都是要从平台那边获得正确的反馈。
        比如已经把答案写上了才能点提交。」

    这个接口就是"从平台获得反馈"的验证入口：它**只读不写**，
    把平台答题页里回显的已存答案原样返回，用来看
    "程序以为写进去了"和"平台实际存了什么"到底对不对得上。

    ⚠️ 全程不发提交、不发暂存，不消耗作答次数，不产生任何成绩记录。

    入参：{"courseid": "…", "taskrefid": "…"}
    返回：{
      "ok": bool, "msg": str,
      "total": 题目数, "platform_answers": {qid: 值},
      "readable": bool,     # 平台有没有回显能力
      "raw_len": int,       # 回读页面字节数
      "questions": [{id,type,title,platform_value}]
    }
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    cid = str(data.get("courseid") or "")
    tid = str(data.get("taskrefid") or "")
    if not (cid and tid):
        return jsonify(ok=False, msg="缺少 courseid / taskrefid")

    course = next((c for c in (state["courses"] or [])
                   if str(c.get("courseId")) == cid), None)
    if not course:
        return jsonify(ok=False, msg="找不到该课程，请刷新课程列表")

    # 从扫描缓存里取这份作业的完整条目（含 href / cpi / enc_task）
    work = None
    try:
        res = (state.get("task_scan") or {}).get("result") or {}
        for c2 in (res.get("courses") or []):
            for w in (c2.get("works") or []):
                if str(w.get("taskrefid")) == tid:
                    work = dict(w)
                    break
            if work:
                break
    except Exception as e:
        print(f"[回读] 反查缓存失败：{e}")
    if not work:
        work = {"taskrefid": tid, "courseid": cid,
                "clazzid": course.get("clazzId")}

    try:
        from api.homework import fetch_work_detail, fetch_work_page
        from api.base import SessionManager, Chaoxing
        sess = SessionManager.get_session()
        detail = fetch_work_detail(sess, course, work)
        work["_detail"] = detail

        # 打开答题页（只读，不 redo、不提交）
        parsed = fetch_work_page(sess, course, detail,
                                 cpi=(work.get("cpi") or ""))
        qs = parsed.get("questions") or []

        # 用同一个入口取原始 HTML（回读校验走的就是这条）
        ch = state.get("chaoxing")
        raw = ""
        if ch is not None:
            raw = ch._fetch_raw_answer_page(sess, course, work, detail=detail)

        plat = Chaoxing._parse_platform_answers(raw) if raw else {}
        rows = []
        for q in qs:
            qid = str(q.get("id"))
            rows.append({
                "id": qid,
                "type": q.get("type"),
                "title": str(q.get("title") or "")[:80],
                "platform_value": plat.get(qid, ""),
                "has_value": bool(plat.get(qid)),
            })
        return jsonify(
            ok=True, msg="",
            work_name=work.get("name") or (detail or {}).get("name") or "",
            total=len(qs),
            readable=bool(plat),
            raw_len=len(raw or ""),
            platform_answers=plat,
            questions=rows,
        )
    except Exception as e:
        return jsonify(ok=False,
                       msg=f"回读失败：{type(e).__name__}: {e}")


@app.post("/api/homework/run")
def api_homework_run():
    """按用户勾选的作业清单逐份执行（只做清单里的，其他一律不碰）。"""
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    if state["running"]:
        return jsonify(ok=False, msg="当前有任务在跑，请先暂停或等它结束")
    data = request.get_json(force=True) or {}
    items = data.get("items") or []
    if not items:
        return jsonify(ok=False, msg="请至少勾选一份作业")
    try:
        _launch_homework_only(items)
    except Exception as e:
        return jsonify(ok=False, msg=f"启动失败：{type(e).__name__}: {e}")
    return jsonify(ok=True, count=len(items))


# ======================================================================
# 考试作答接口（2026-09-26 新增，手机端协议，链路见 api/exam.py）
# ======================================================================

@app.get("/api/diag/aikeys")
def api_diag_aikeys():
    """🔧 只读诊断：暴露当前 AI 题库的 Key 池状态（多 Key 轮换验证用）。

    返回：{"ok", "enabled", "n_keys", "keys"(掩码), "current", "dead"}
    ⚠️ Key 一律掩码，只留末 6 位。
    """
    def _mask(k):
        k = str(k or "")
        return ("…" + k[-6:]) if len(k) > 6 else ("…" + k if k else "")

    try:
        cx = state.get("chaoxing")
        tiku = getattr(cx, "tiku", None) if cx is not None else None
        if tiku is None:
            return jsonify(ok=True, enabled=False, n_keys=0,
                           msg="尚未登录/未加载题库")
        # 可能是 TikuFallback 链，递归找带 _keys 的子题库
        found = []
        def _walk(t, depth=0):
            if t is None or depth > 4:
                return
            if hasattr(t, "_keys"):
                found.append(t)
            for attr in ("providers", "_providers", "tikus", "_tikus"):
                kids = getattr(t, attr, None)
                if isinstance(kids, (list, tuple)):
                    for c in kids:
                        _walk(c, depth + 1)
        _walk(tiku)
        if not found:
            return jsonify(ok=True, enabled=False, n_keys=0,
                           name=getattr(tiku, "name", type(tiku).__name__),
                           msg="当前题库非 AI（或多题库链中无 AI）")
        t = found[0]
        keys = list(getattr(t, "_keys", []) or [])
        # 🔴 2026-09-27：`_key_dead` 已由 set 升级为**冷却表** {key: 解冻时间戳}。
        #    inf = 永久弃用；数字 = 临时限流，到点自动复活。
        _deadmap = getattr(t, "_key_dead", {}) or {}
        if isinstance(_deadmap, (set, list, tuple)):
            dead = list(_deadmap)
            _deadmap = {k: float('inf') for k in dead}
        else:
            dead = [k for k in _deadmap]
        import time as _t

        def _dead_state(k):
            """返回 (是否不可用, 剩余冷却秒, 是否永久)"""
            until = _deadmap.get(k)
            if until is None:
                return False, 0.0, False
            if until == float('inf'):
                return True, -1.0, True
            left = max(0.0, until - _t.time())
            if left <= 0:
                return False, 0.0, False      # 已解冻
            return True, left, False

        slots = list(getattr(t, "_slots", []) or [])
        out_slots = []
        for s in slots:
            _bad, _left, _perm = _dead_state(s.get("key") or "")
            out_slots.append({
                "key": _mask(s.get("key")),
                "endpoint": s.get("endpoint") or "(沿用主 Endpoint)",
                "model": s.get("model") or "(沿用主 Model)",
                "dead": _bad,
                "permanent": _perm,
                "cooldown_left": round(_left, 1) if _left > 0 else 0,
            })
        return jsonify(ok=True, enabled=len(keys) > 1,
                       name=getattr(t, "name", type(t).__name__),
                       n_keys=len(keys),
                       keys=[_mask(k) for k in keys],
                       slots=out_slots,
                       current=_mask(getattr(t, "key", "")),
                       current_endpoint=getattr(t, "endpoint", ""),
                       current_model=getattr(t, "model", ""),
                       dead=[_mask(k) for k in dead],
                       n_dead=len(dead),
                       model=getattr(t, "model", ""),
                       endpoint=getattr(t, "endpoint", ""))
    except Exception as e:
        return jsonify(ok=False, msg=f"{type(e).__name__}: {e}")


@app.post("/api/diag/probe")
def api_diag_probe():
    """🔧 只读探针：用**已登录的 session** 访问指定 URL（调试教师端接口用）。

    ⚠️ 只允许 GET/HEAD，只允许 chaoxing 域名，不回传 cookie。
    入参：{"url":"…","headers":{…},"allow_redirects":bool}
    返回：{"ok", "status", "url", "len", "text"(截断), "location"}
    """
    from urllib.parse import urlparse
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify(ok=False, msg="缺少 url")
    host = urlparse(url).hostname or ""
    if "chaoxing.com" not in host:
        return jsonify(ok=False, msg=f"只允许 chaoxing 域名，收到 {host}")
    method = (data.get("method") or "GET").upper()
    if method not in ("GET", "HEAD"):
        return jsonify(ok=False, msg="只允许 GET / HEAD")
    hdrs = data.get("headers") or {}
    if not isinstance(hdrs, dict):
        hdrs = {}
    try:
        from api.base import SessionManager
        sess = SessionManager.get_session()
        r = sess.request(method, url, headers=hdrs, timeout=25,
                         allow_redirects=bool(data.get("allow_redirects", True)))
        txt = r.text or ""
        try:
            max_len = int(data.get("max_len") or 20000)
        except Exception:
            max_len = 20000
        max_len = max(1000, min(max_len, 400000))
        kw = data.get("keywords") or []
        if isinstance(kw, str):
            kw = [kw]
        hits = {}
        for k in kw:
            if not k:
                continue
            pos = txt.find(k)
            hits[k] = pos
            if pos >= 0:
                hits[k + "@ctx"] = txt[max(0, pos - 300):pos + 500]
        return jsonify(ok=True, status=r.status_code, url=r.url,
                       location=r.headers.get("Location", ""),
                       ctype=r.headers.get("Content-Type", ""),
                       len=len(txt), hits=hits, text=txt[:max_len])
    except Exception as e:
        return jsonify(ok=False, msg=f"{type(e).__name__}: {e}")


@app.post("/api/teacher/work/create")
def api_teacher_work_create():
    """🔴 教师端：**新建作业草稿**（193 号在 HHHH 里发作业用）。

    入参：{"courseid","clazzid","cpi","title",
           "start_time","end_time","passing_standard",
           "redo_times","redo_highest_score","publish":bool}

    publish=False → 只建草稿（返回 workid，供后续配题）
    publish=True  → 一条龙（建草稿→保存→发布）；⚠️ 无题时会被平台拒绝
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    courseid = str(data.get("courseid") or "").strip()
    clazzid = str(data.get("clazzid") or "").strip()
    cpi = str(data.get("cpi") or "").strip()
    title = (data.get("title") or "").strip()
    if not (courseid and clazzid and cpi):
        return jsonify(ok=False, msg="缺少 courseid / clazzid / cpi")
    if not title:
        title = time.strftime("自动作业%Y%m%d%H%M%S", time.localtime())
    try:
        from api.base import SessionManager
        from api.teacher import WorkPublisher, _now_str, _add_days

        class _Wrap:
            session = SessionManager.get_session()

        pub = WorkPublisher(_Wrap())
        pub.open_editor(courseid, clazzid)
        workid = pub.create_work(courseid, clazzid, cpi, title)

        if not data.get("publish"):
            return jsonify(ok=True, workid=workid, title=title,
                           msg=f"草稿已建：{title}")

        st = (data.get("start_time") or "").strip() or _now_str()
        et = (data.get("end_time") or "").strip() or _add_days(st, 7)
        pub.save_work(courseid, clazzid, cpi, workid, title)
        d = pub.publish_work(
            courseid, cpi, workid, clazzid,
            start_time=st, end_time=et,
            passing_standard=str(data.get("passing_standard") or "60"),
            redo_times=str(data.get("redo_times") or "0"),
            redo_highest_score=str(data.get("redo_highest_score") or "0"),
        )
        return jsonify(ok=True, workid=workid, title=title,
                       start_time=st, end_time=et, msg=d.get("msg") or "已发布")
    except Exception as e:
        return jsonify(ok=False, msg=f"{type(e).__name__}: {e}")


@app.post("/api/teacher/work/publish")
def api_teacher_work_publish():
    """🔴 教师端：**发布已有作业草稿**（配题完成后调它）。

    入参：{"courseid","clazzid","cpi","workid","title",
           "start_time","end_time","passing_standard",...}
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    courseid = str(data.get("courseid") or "").strip()
    clazzid = str(data.get("clazzid") or "").strip()
    cpi = str(data.get("cpi") or "").strip()
    workid = str(data.get("workid") or "").strip()
    title = (data.get("title") or "").strip() or "作业"
    if not (courseid and clazzid and cpi and workid):
        return jsonify(ok=False, msg="缺少 courseid / clazzid / cpi / workid")
    try:
        from api.base import SessionManager
        from api.teacher import WorkPublisher, _now_str, _add_days

        class _Wrap:
            session = SessionManager.get_session()

        pub = WorkPublisher(_Wrap())
        # 发布前先看题目校验结果
        chk = {}
        try:
            chk = pub.check_question(courseid, clazzid, cpi, workid)
        except Exception as e:      # noqa: BLE001
            chk = {"err": str(e)}

        # 先保存（workStatus=0）
        pub.save_work(courseid, clazzid, cpi, workid, title)

        st = (data.get("start_time") or "").strip() or _now_str()
        et = (data.get("end_time") or "").strip() or _add_days(st, 7)
        d = pub.publish_work(
            courseid, cpi, workid, clazzid,
            start_time=st, end_time=et,
            passing_standard=str(data.get("passing_standard") or "60"),
            redo_times=str(data.get("redo_times") or "0"),
            redo_highest_score=str(data.get("redo_highest_score") or "0"),
        )
        return jsonify(ok=True, workid=workid, check=chk,
                       start_time=st, end_time=et, msg=d.get("msg") or "已发布")
    except Exception as e:
        return jsonify(ok=False, msg=f"{type(e).__name__}: {e}")


@app.post("/api/teacher/work/check")
def api_teacher_work_check():
    """🔴 教师端：发布前校验作业题目（只读）。"""
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    try:
        from api.base import SessionManager
        from api.teacher import WorkPublisher

        class _Wrap:
            session = SessionManager.get_session()

        pub = WorkPublisher(_Wrap())
        d = pub.check_question(
            str(data.get("courseid") or ""), str(data.get("clazzid") or ""),
            str(data.get("cpi") or ""), str(data.get("workid") or ""))
        return jsonify(ok=True, result=d)
    except Exception as e:
        return jsonify(ok=False, msg=f"{type(e).__name__}: {e}")


def _teacher_pub():
    """构造一个 WorkPublisher（复用已登录会话）。"""
    from api.base import SessionManager
    from api.teacher import WorkPublisher

    class _Wrap:
        session = SessionManager.get_session()

    return WorkPublisher(_Wrap())


@app.post("/api/teacher/work/add_questions")
def api_teacher_work_add_questions():
    """🔴 教师端：给已有草稿批量加题（纯 HTTP，无需 Playwright）。

    入参：
    {
      "courseid":"267569467", "clazzid":"155927249", "cpi":"493276646",
      "workid":"<草稿ID>",
      "questions":[
        {"type":0,"stem":"题干","options":["A项","B项"],"answers":["A"],"score":10},
        {"type":1,"stem":"…","options":[…],"answers":["A","B"],"score":10},
        {"type":2,"stem":"中国的首都是____","answers":["北京"],"score":10},
        {"type":3,"stem":"光年是时间单位。","answers":["错"],"score":10},
        {"type":4,"stem":"简述…","answers":["参考答案"],"score":10}
      ]
    }
    返回 {ok, result:{ok_n, fail_n, ok:[…], fail:[…]}}
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    courseid = str(data.get("courseid") or "")
    clazzid = str(data.get("clazzid") or "")
    workid = str(data.get("workid") or "")
    qs = data.get("questions") or []
    if not (courseid and clazzid and workid):
        return jsonify(ok=False, msg="缺少 courseid / clazzid / workid")
    if not qs:
        return jsonify(ok=False, msg="questions 为空")
    try:
        pub = _teacher_pub()
        ctx = pub.load_editor_context(courseid, clazzid, workid)
        # cpi 以页面为准，允许入参覆盖
        if data.get("cpi"):
            ctx["cpi"] = str(data["cpi"])
        r = pub.add_questions(ctx, qs)
        return jsonify(ok=True, result=r, workid=workid)
    except Exception as e:
        return jsonify(ok=False, msg=f"{type(e).__name__}: {e}")


@app.post("/api/teacher/work/quiz_run")
def api_teacher_work_quiz_run():
    """🔴 教师端一条龙：建草稿 → 加题 → 保存 → 发布（测试用造题神器）。

    入参：
    {
      "courseid":"267569467","clazzid":"155927249","cpi":"493276646",
      "title":"程序测试作业（自动生成）",
      "classid":"155927249",              # 发布对象（默认=clazzid）
      "start_time":"2026-09-26 22:00",    # 空则 now
      "end_time":"2026-10-03 22:00",      # 空则 now+7d
      "passing_standard":"60",
      "redo_times":"0","redo_highest_score":"0",
      "publish":true,                     # false = 只建草稿+加题，不发布
      "questions":[ … ]                   # 同 add_questions
    }
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    courseid = str(data.get("courseid") or "")
    clazzid = str(data.get("clazzid") or "")
    cpi = str(data.get("cpi") or "")
    title = str(data.get("title") or "程序测试作业")
    qs = data.get("questions") or []
    if not (courseid and clazzid and cpi):
        return jsonify(ok=False, msg="缺少 courseid / clazzid / cpi")
    if not qs:
        return jsonify(ok=False, msg="questions 为空")

    log = []

    def _log(s: str):
        log.append(s)
        logger.info(f"[quiz_run] {s}")

    try:
        pub = _teacher_pub()
        _log(f"① 建草稿：{title}")
        workid = pub.create_work(courseid, clazzid, cpi, title)
        _log(f"   workid = {workid}")

        _log(f"② 加载编辑器上下文（enc）")
        ctx = pub.load_editor_context(courseid, clazzid, workid, force=True)

        _log(f"③ 批量加题（{len(qs)} 道）")
        add = pub.add_questions(ctx, qs)
        _log(f"   成功 {add['ok_n']} / 失败 {add['fail_n']}")
        for x in add["fail"]:
            _log(f"   ✗ 第{x['idx']}题({x.get('type')}) {x['err'][:110]}")
        if add["ok_n"] == 0:
            return jsonify(ok=False, msg="一道题都没加上，终止发布",
                           workid=workid, log=log, add=add)

        _log("④ 保存（workStatus=0）")
        try:
            pub.save_work(courseid, clazzid, cpi, workid, title)
        except Exception as e:      # noqa: BLE001
            _log(f"   保存告警（不中断）：{e}")

        _log("⑤ 发布前校验")
        chk = pub.check_question(courseid, clazzid, cpi, workid)
        _log(f"   check = {chk}")
        if str(chk.get("status")) == "2":
            return jsonify(ok=False, msg="平台说没有题目，终止发布",
                           workid=workid, log=log, add=add, check=chk)

        if not data.get("publish", True):
            _log("（publish=false，跳过发布）")
            return jsonify(ok=True, workid=workid, log=log, add=add,
                           check=chk, published=False)

        _log("⑥ 正式发布")
        d = pub.publish_work(
            courseid, cpi, workid,
            classid=str(data.get("classid") or clazzid),
            start_time=str(data.get("start_time") or ""),
            end_time=str(data.get("end_time") or ""),
            passing_standard=str(data.get("passing_standard") or "60"),
            redo_times=str(data.get("redo_times") or "0"),
            redo_highest_score=str(data.get("redo_highest_score") or "0"),
        )
        _log(f"   发布回包 = {d}")
        return jsonify(ok=True, workid=workid, log=log, add=add,
                       check=chk, publish=d, published=True)
    except Exception as e:
        import traceback as _tb
        tb = _tb.format_exc()
        try:
            logger.warning(f"[quiz_run] 失败：{type(e).__name__}: {e}")
            logger.warning(tb.replace("{", "{{").replace("}", "}}"))
        except Exception:                        # noqa: BLE001
            pass
        log.append(f"!! {type(e).__name__}: {e}")
        log.append(tb[-1500:])
        # 用 json.dumps 兜底，避免 Flask 序列化再崩
        from flask import Response
        return Response(
            json.dumps({"ok": False, "msg": f"{type(e).__name__}: {e}",
                        "log": log}, ensure_ascii=False),
            status=200, mimetype="application/json; charset=utf-8")


@app.post("/api/teacher/work/list")
def api_teacher_work_list():
    """🔴 教师端：列出作业库（确认发布了哪些）。只读。"""
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    courseid = str(data.get("courseid") or "")
    clazzid = str(data.get("clazzid") or "")
    cpi = str(data.get("cpi") or "")
    if not courseid:
        return jsonify(ok=False, msg="缺少 courseid")
    try:
        from api.teacher import HOST
        s = __import__("api.base", fromlist=["SessionManager"]).SessionManager.get_session()
        url = (f"{HOST}/mooc2-ans/work/list"
               f"?courseid={courseid}&clazzid={clazzid}&cpi={cpi}&mooc=1")
        r = s.get(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
        })
        html = r.text or ""
        # 粗解析：作业名 + workid
        works = []
        for m in re.finditer(
                r'href="[^"]*work/library/setting[^"]*?workLibraryId=([0-9a-f]{32})[^"]*"'
                r'[^>]*>\s*([^<]{2,60})', html):
            works.append({"workid": m.group(1), "title": m.group(2).strip()})
        if not works:
            for m in re.finditer(r'workLibraryId=([0-9a-f]{32})', html):
                works.append({"workid": m.group(1), "title": ""})
        return jsonify(ok=True, len=len(html), works=works[:60])
    except Exception as e:
        return jsonify(ok=False, msg=f"{type(e).__name__}: {e}")


def _maybe_run_exams_after_work(courses, config):
    """刷课/作业跑完后：重扫本次课程的考试，把**新解锁**的「待做」考试接着做。

    为什么需要（2026-10-27 何同学实测）：**课程考试要「刷完课」才解锁**。
    点「开始」那一刻扫描到的是未解锁状态，`enc_task` 也随之失效 ——
    前端 pendingExam 里那份清单根本开不了考（表现为：刷完就结束，没有考试）。

    安全边界（考试机会不可逆，必须收窄）：
      · 只处理 `exam_course_ids` 里的课：用户没勾考试的课**绝不**擅自开考
      · 只挑 `status == "待做"`，未解锁/已完成/已过期一律跳过
      · `auto_submit` 沿用本次启动传入的值（默认 False = 只暂存不交卷）

    返回 True = 已把控制权交给考试线程，调用方的 finally **不要再改 running**。
    """
    want = {str(x) for x in (config.get("exam_course_ids") or [])}
    if not want:
        return False
    cx = state.get("chaoxing")
    if cx is None:
        return False
    targets = [c for c in (courses or []) if _course_id(c) in want]
    if not targets:
        return False
    try:
        print(f"\n[考试] 刷课结束，重扫 {len(targets)} 门课的考试"
              f"（课程考试要刷完才解锁）…")
        res = cx.scan_exams_all(targets)
    except Exception as e:                                       # noqa: BLE001
        print(f"[考试] 重扫失败（不影响刷课成果）：{type(e).__name__}: {e}")
        return False

    items = []
    for c in (res or {}).get("courses") or []:
        cid = str(c.get("courseid") or c.get("courseId") or "")
        if cid not in want:
            continue
        for e in (c.get("exams") or []):
            if str(e.get("status") or "") != "待做":
                continue
            items.append({
                "courseid": cid,
                "clazzid": str(c.get("clazzid") or c.get("clazzId") or ""),
                "cpi": str(c.get("cpi") or ""),
                "exam_id": str(e.get("exam_id") or ""),
                "title": e.get("title") or "考试",
                "enc_task": e.get("exam_enc") or e.get("enc_task") or "",
            })
    items = [x for x in items if x["exam_id"]]
    if not items:
        print("[考试] 重扫后仍没有「待做」的考试 —— 可能还未解锁，"
              "或该课考试已完成/已过期。")
        return False

    print(f"[考试] 发现 {len(items)} 场待做考试，接着开考："
          + "、".join((x["title"] or "")[:18] for x in items))
    try:
        _launch_exam_only(items,
                          auto_submit=bool(config.get("exam_auto_submit")))
    except Exception as e:                                       # noqa: BLE001
        print(f"[考试] 开考失败：{type(e).__name__}: {e}")
        return False
    return True


def _launch_exam_only(items, auto_submit=False):
    """启动一个"只做指定考试"的任务（走 Chaoxing.process_exams）。

    每个 item：{courseid, exam_id(或 taskrefid), enc_task, title}
    ⚠️ `auto_submit` 缺省 **False**（2026-10-12 何同学要求）：
       考试只有一次机会，默认只暂存草稿、不替用户交卷。
    """
    groups: dict = {}
    for it in items:
        cid = str(it.get("courseid") or "")
        if not cid:
            continue
        groups.setdefault(cid, []).append({
            "exam_id": str(it.get("exam_id") or it.get("taskrefid") or ""),
            "enc_task": it.get("enc_task") or "",
            "title": it.get("title") or it.get("name") or "考试",
        })
    if not groups:
        raise ValueError("没有有效的考试条目")

    courses = state["courses"] or []
    by_cid = {str(c.get("courseId")): c for c in courses}
    todo_courses = []
    for cid, exs in groups.items():
        c = by_cid.get(cid)
        if c is None:
            # 🔴 2026-09-27 修 bug：**班级型课程**（如 HHHH）不在 `state["courses"]`
            # 里（那只是"我学的课"列表），此前直接 raise → 这类课程的考试
            # 完全没法作答（真机报错："找不到课程 267569467，请刷新课程列表"，
            # 刷新也没用，因为它根本不在列表里）。
            # 与作业路径（`/api/homework/run` 只用 items 自带信息）对齐：
            # 用条目里的 courseid/clazzid/cpi 构造一个最小 course 对象。
            _it = next((x for x in items
                        if str(x.get("courseid")) == cid), {}) or {}
            _clz = str(_it.get("clazzid") or _it.get("clazzId") or "")
            _cpi = str(_it.get("cpi") or "")
            if not _clz:
                raise ValueError(
                    f"找不到课程 {cid}（不在课程列表），且条目未提供 clazzid")
            c = {
                "courseId": cid,
                "clazzId": _clz,
                "cpi": _cpi,
                "title": str(_it.get("title") or "考试"),
            }
            logger.warning(
                f"考试课程 {cid} 不在课程列表（班级型课程），已用条目信息构造："
                f"clazzId={_clz} cpi={_cpi or '(空)'}")
        todo_courses.append((c, exs))

    state["running"] = True
    # 🔴 2026-09-27 修 bug：原先这里直接取 state["stop_flag"] / state["pause_event"]
    # / state["cx"]，但这三个键在 state 初始化里**根本不存在**（作业路径靠
    # PauseInterrupt 异常暂停，没有 stop_flag 概念）→ 点「开考」必崩：
    #     KeyError: 'stop_flag'
    # 这段从 2026-09-26 写下后一直没被真正跑过，今天第一次跑才暴露。
    state.setdefault("stop_flag", threading.Event()).clear()
    state.setdefault("pause_event", threading.Event()).clear()
    # 🔴 2026-09-27：显式清掉上一轮的结果 —— 否则前端在任务还没写完
    #    exam_result 时会读到**上一轮的残留**（表现为"任务已结束但结果是空的"，
    #    真机排查时被这个误导了很久）。
    state["exam_result"] = {"done": 0, "skipped": 0, "results": []}
    state["task_name"] = "考试作答"
    state["task_start"] = time.time()

    def _worker():
        # 🔴 2026-10-27：考试可能连跑几小时（85 题 + 滑块），**必须自己持有防睡眠**。
        #   起因：这条链路是由 `_work` 结束后转交过来的，而 `_work` 的 finally
        #   会 `_prevent_sleep(False)`（那是**线程级**请求）⇒ 若不在这里重新持有，
        #   刷课一结束防睡眠就失效，考试跑到一半系统休眠 → 进程被冻结。
        _prevent_sleep(True)
        try:
            cx = state.get("chaoxing")
            if cx is None:
                raise RuntimeError("尚未登录（chaoxing 实例为空）")
            total_done = total_skip = 0
            all_res = []
            logger.info(f"[考试任务] 待处理课程 {len(todo_courses)} 门："
                        f"{[(c.get('title'), len(e)) for c, e in todo_courses]}")
            for c, exs in todo_courses:
                if state["stop_flag"].is_set():
                    logger.warning("[考试任务] stop_flag 已置位，提前结束")
                    break
                r = cx.process_exams(c, stop_flag=state["stop_flag"],
                                     exam_list=exs, auto_submit=auto_submit)
                logger.info(f"[考试任务] 《{c.get('title')}》返回："
                            f"done={r.get('done')} skipped={r.get('skipped')} "
                            f"results={len(r.get('results') or [])} 条")
                total_done += r.get("done", 0)
                total_skip += r.get("skipped", 0)
                all_res.extend(r.get("results") or [])
            state["exam_result"] = {
                "done": total_done, "skipped": total_skip,
                "results": all_res,
            }
            logger.info(f"[考试任务] 全部结束：done={total_done} "
                        f"skipped={total_skip} results={len(all_res)}")
        except Exception as e:
            logger.error(f"考试任务异常：{type(e).__name__}: {e}")
            state["exam_result"] = {"done": 0, "skipped": 0,
                                    "error": f"{type(e).__name__}: {e}"}
        finally:
            _prevent_sleep(False)
            state["running"] = False
            state["task_name"] = ""

    t = threading.Thread(target=_worker, daemon=True)
    # 🔴 2026-09-27 修 bug：**必须把考试线程注册到 `work_thread`**。
    # 看门狗（`_watchdog`）只认 `state["work_thread"]`：考试任务不设置它，
    # 于是 `alive=False` → 被当作"作业线程已死"→ 判定卡死 → **重启整个服务**，
    # 考试跑到一半就被打断（实测 20:54 / 21:07 两次中断都是它干的；
    # 重启次数超上限后更直接把 `running` 强设为 False，让前端以为任务提前结束，
    # 而实际考试线程还在正常答题、最终答完 50 题）。
    state["work_thread"] = t
    t.start()
    return t


@app.post("/api/exam/retest")
def api_exam_retest():
    """把指定考试设为「重考」（等价在页面上点那个「重考」按钮）。

    入参：{courseid, exam_id, relation_id, paper_id?, clazzid?}
    ⚠️ 每次重考都会**覆盖上一次作答**，前端必须确认。
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    cid = str(data.get("courseid") or "")
    course = next((c for c in (state["courses"] or [])
                   if _course_id(c) == cid), None)
    if course is None:
        return jsonify(ok=False, msg=f"找不到课程 {cid}（请刷新课程列表）")
    try:
        from api.exam import set_retest
        res = set_retest(state["chaoxing"].get_session(), course, data)
    except Exception as e:                                       # noqa: BLE001
        return jsonify(ok=False, msg=f"{type(e).__name__}: {e}")
    logger.info("[重考] 课程 %s 考试 %s → %s", cid,
                data.get("exam_id"), res.get("msg"))
    return jsonify(**res)


@app.post("/api/exam/answers")
def api_exam_answers():
    """提取「试卷详情」里的正确答案，写入学习库（供后续重考全对）。

    ⚠️ 安全边界：**只有平台允许查看答案时才有内容**。拿不到就明确失败，
       绝不做反推/猜测 —— 这也是何同学 2026-11-07 的要求。

    入参：{courseid, exam_id, relation_id, paper_id?, write: true}
    返回：{ok, question_count, with_answer, imported, msg}
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    cid = str(data.get("courseid") or "")
    course = next((c for c in (state["courses"] or [])
                   if _course_id(c) == cid), None)
    if course is None:
        return jsonify(ok=False, msg=f"找不到课程 {cid}")
    try:
        from api.exam import fetch_exam_answer_page, parse_answer_page
        res = fetch_exam_answer_page(state["chaoxing"].get_session(),
                                     course, data)
    except Exception as e:                                       # noqa: BLE001
        return jsonify(ok=False, msg=f"{type(e).__name__}: {e}")
    if not res.get("ok"):
        # 明确告诉用户"这门考试看不了答案"，而不是给个空结果
        return jsonify(ok=False, msg=res.get("msg") or "无法获取答案页",
                       question_count=0)

    items = parse_answer_page(res["html"])
    with_ans = [x for x in items if (x.get("right") or "").strip()]
    imported = 0
    if data.get("write", True) and with_ans:
        try:
            from api.learned import LearnedAnswers
            la = LearnedAnswers()
            for x in with_ans:
                # 写学习库：mark="dui" ⇒ 同时写 verified（后续 query 直接命中）
                opts_str = "\n".join(o.get("text") or ""
                                     for o in (x.get("options") or []))
                la.record(x["title"], x["right"], "dui", options=opts_str)
            imported = len(with_ans)
        except Exception as e:                                   # noqa: BLE001
            logger.warning("[答案提取] 写入学习库失败：%s", e)
    logger.info("[答案提取] %s 共 %d 题，有答案 %d，已入库 %d",
                data.get("exam_id"), len(items), len(with_ans), imported)
    return jsonify(ok=True, question_count=len(items),
                   with_answer=len(with_ans), imported=imported,
                   msg=f"提取 {len(items)} 题，其中 {len(with_ans)} 有答案")


@app.post("/api/exam/run")
def api_exam_run():
    """按用户勾选的考试清单逐场执行。

    🔴 **每场都是真金白银的一次作答机会**，前端必须逐场 confirm。

    ⚠️ `auto_submit` **缺省 False**（2026-10-12 何同学要求）：默认「只暂存草稿、
    不替用户交卷」，要自动交卷必须前端显式传 `auto_submit: true`
    （对应主界面「自动提交考试」开关）。
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    if state["running"]:
        return jsonify(ok=False, msg="当前有任务在跑，请先暂停或等它结束")
    data = request.get_json(force=True) or {}
    items = data.get("items") or []
    if not items:
        return jsonify(ok=False, msg="请至少勾选一场考试")
    # 🔴 2026-10-12：缺省 **False** = 只暂存不交卷（考试只有一次机会，默认不冒险）。
    auto_submit = bool(data.get("auto_submit", False))
    try:
        _launch_exam_only(items, auto_submit=auto_submit)
    except Exception as e:
        return jsonify(ok=False, msg=f"启动失败：{type(e).__name__}: {e}")
    return jsonify(ok=True, count=len(items), auto_submit=auto_submit)


@app.get("/api/exam/status")
def api_exam_status():
    """考试任务进度（前端轮询）。

    🔴 2026-10-13：考试**进行中**也必须能读到"已答 N 题"。
    原实现只有任务结束才填 `done`，进行中恒为 0 ⇒ 界面看不出考试在不在跑
    （何同学实测：只能看到刷课的 `56/56 100%`）。
    """
    r = dict(state.get("exam_result") or {})
    try:
        from api import exam as _em
        p = _em.get_exam_progress()
        _d = int(p.get("done") or 0)
        if _d > int(r.get("done") or 0):
            r["done"] = _d
        if _d:
            r["total"] = int(p.get("total") or 0)
            r["title"] = r.get("title") or p.get("title") or ""
    except Exception:                                            # noqa: BLE001
        pass
    return jsonify(running=bool(state.get("running")),
                   task_name=state.get("task_name") or "",
                   result=r)


@app.post("/api/exam/precheck")
def api_exam_precheck():
    """**只读**预检：开考前看清这场考试的要求。**不消耗作答机会**。

    入参：{courseid, exam_id, enc_task?, clazzid?, cpi?, title?, userid?}

    🔴 与 `/api/exam/sheet` 的区别（重要）：
       那个接口为了读答题卡会调 `start_exam` —— **等于真进考场**；
       本接口只 GET 封面页，**绝不开考**，可以放心反复调用。
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    cid = str(data.get("courseid") or data.get("courseId") or "")
    eid = str(data.get("exam_id") or data.get("taskrefid") or "")
    if not (cid and eid):
        return jsonify(ok=False, msg="缺少 courseid / exam_id")
    # 课程对象：优先取已登录列表，找不到就用条目自带的 clazzid/cpi 兜底
    # （班级型课程不在 state["courses"] 里，与 `_launch_exam_only` 同一套处理）
    courses = (state.get("courses") or [])
    course = next((c for c in courses if str(c.get("courseId")) == cid), None)
    if not course:
        clz = str(data.get("clazzid") or data.get("clazzId") or "")
        if not clz:
            return jsonify(ok=False, msg="找不到该课程，且条目未提供 clazzid")
        course = {"courseId": cid, "clazzId": clz,
                  "cpi": str(data.get("cpi") or ""),
                  "title": str(data.get("title") or "")}
    try:
        from api.exam import fetch_exam_list_phone, precheck_exam
        from api.base import SessionManager
        sess = SessionManager.get_session()
        uid = str(data.get("userid") or "")
        try:
            uid = uid or str(state["chaoxing"].get_uid() or "")
        except Exception:                                            # noqa: BLE001
            pass
        enc_task = str(data.get("enc_task") or "")
        if not enc_task:
            for it in fetch_exam_list_phone(sess, course, uid):
                if str(it.get("exam_id")) == eid:
                    enc_task = it.get("enc_task") or ""
                    break
        if not enc_task:
            return jsonify(ok=False,
                           msg="拿不到 enc_task（该考试当前不可进入）")
        info = precheck_exam(sess, course, eid, enc_task, uid)
        info["ok"] = True
        logger.info(f"[考试预检] 《{info.get('title') or data.get('title') or eid}》"
                    f"state={info.get('state')} can_enter={info.get('can_enter')} "
                    f"face={info.get('face_need')} captcha={info.get('captcha_need')} "
                    f"code={info.get('exam_code_need')}")
        return jsonify(info)
    except Exception as e:                                           # noqa: BLE001
        return jsonify(ok=False, msg=f"预检失败：{type(e).__name__}: {e}")


@app.post("/api/exam/sheet")
def api_exam_sheet():
    """读某场考试的答题卡状态。

    🔴🔴 **会真进考场**（内部调 `start_exam` 拿 enc），**可能消耗/占用作答机会**。
       2026-10-12 修正旧 docstring —— 它此前写"不消耗作答机会"，是**错的**，
       照它调用很可能白白用掉一次机会。
       ⇒ 现在的调用顺序是：想先看清要求请用 `/api/exam/precheck`（纯只读）；
         本接口只在**确认过要作答**之后才用。

    入参：{"courseid":"…","exam_id":"…","enc_task":"…"}
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    cid = str(data.get("courseid") or "")
    eid = str(data.get("exam_id") or data.get("taskrefid") or "")
    # 🔴 2026-10-12：防止误调 —— 必须显式 confirm，否则直接拒绝。
    if not data.get("confirm"):
        return jsonify(ok=False, msg="本接口会真进考场（可能消耗作答机会）；"
                                     "请改用 /api/exam/precheck 预检，"
                                     "或显式传 confirm=true 表示确认要作答。")
    if not (cid and eid):
        return jsonify(ok=False, msg="缺少 courseid / exam_id")
    courses = state["courses"] or []
    course = next((c for c in courses if str(c.get("courseId")) == cid), None)
    if not course:
        return jsonify(ok=False, msg="找不到该课程，请刷新课程列表")
    try:
        from api.exam import (fetch_exam_list_phone, fetch_exam_cover,
                              start_exam, fetch_answer_sheet)
        from api.base import SessionManager
        sess = SessionManager.get_session()
        uid = str(data.get("userid") or "")
        logger.warning(f"[考试] ⚠ 调用 /api/exam/sheet（会进考场）exam_id={eid}")
        # 补 enc_task
        enc_task = data.get("enc_task") or ""
        if not enc_task:
            for it in fetch_exam_list_phone(sess, course, uid):
                if str(it.get("exam_id")) == eid:
                    enc_task = it.get("enc_task") or ""
                    break
        if not enc_task:
            return jsonify(ok=False, msg="拿不到 enc_task（该考试不可进入）")
        exam = {"exam_id": eid, "enc_task": enc_task}
        cover = fetch_exam_cover(sess, course, eid, enc_task, uid)
        enc = start_exam(sess, course, exam, cover["exam_answer_id"], user_id=uid)
        sheet = fetch_answer_sheet(sess, course, exam, cover["exam_answer_id"],
                                   enc, user_id=uid)
        return jsonify(ok=True, title=cover.get("title"),
                       exam_answer_id=cover.get("exam_answer_id"),
                       face_need=cover.get("face_need"), sheet=sheet)
    except Exception as e:
        return jsonify(ok=False, msg=f"读取答题卡失败：{type(e).__name__}: {e}")


def _launch_homework_only(items):
    """启动一个"只做指定作业"的任务（不刷课、不碰章节）。"""
    # 先从统一扫描缓存里建索引：taskrefid → 作业条目 / courseid → 课程卡片。
    # 🔴 2026-09-26：班级型课程（HHHH）不在 get_course_list() 里，
    # 只能从 task_scan 结果里拿它的伪课程卡片与作业 href。
    scan = (state.get("task_scan") or {}).get("result") or {}
    work_idx: dict = {}
    course_idx: dict = {}
    for c2 in (scan.get("courses") or []):
        _cid = str(c2.get("courseid") or c2.get("courseId") or "")
        if _cid:
            course_idx[_cid] = c2
        for w in (c2.get("works") or []):
            tid = str(w.get("taskrefid") or "")
            if tid:
                work_idx[tid] = w

    groups: dict = {}
    for it in items:
        cid = str(it.get("courseid") or "")
        if not cid:
            continue
        # 用扫描缓存补全 href / enc_task / cpi / userid / clazzid（作答必需）
        src = work_idx.get(str(it.get("taskrefid") or "")) or {}
        merged = dict(it)
        for k in ("href", "enc_task", "cpi", "userid", "clazzid"):
            if not merged.get(k) and src.get(k):
                merged[k] = src[k]
        groups.setdefault(cid, []).append({
            "name": merged.get("name"),
            "status": merged.get("status"),
            "taskrefid": merged.get("taskrefid"),
            "enc_task": merged.get("enc_task"),
            "href": merged.get("href"),
            "cpi": merged.get("cpi"),
            "userid": merged.get("userid"),
            "clazzid": merged.get("clazzid"),
            "courseid": cid,
            "redo": bool(merged.get("redo")),
        })
    if not groups:
        raise ValueError("没有有效的作业条目")

    courses = [c for c in (state["courses"] or [])
               if str(c.get("courseId")) in groups]
    have = {str(c.get("courseId")) for c in courses}
    # 补：扫描缓存里有、但不在课程列表里的（班级型课程等）
    for cid in groups:
        if cid in have:
            continue
        c2 = course_idx.get(cid)
        if c2:
            courses.append({"courseId": cid,
                            "courseid": cid,
                            "title": c2.get("title") or cid,
                            "clazzId": c2.get("clazzId") or c2.get("clazzid") or "",
                            "cpi": c2.get("cpi") or ""})
    if not courses:
        raise ValueError("选中的课程不在课程列表里")

    cfg = {"speed": 1.0, "jobs": _ui_default_jobs(), "notopen_action": "retry",
           "retry_interval": 1.0,
           "video_course_ids": [], "quiz_course_ids": [],
           "walk_course_ids": [], "only_kinds": [],
           "work_course_ids": sorted(groups.keys()),
           "work_items": groups}
    _launch(courses, cfg)


@app.post("/api/start")
def api_start():
    data = request.get_json(force=True) or {}
    # 🔴 2026-10-12：考试提交开关的落盘点。
    #    它只存在于主界面（登录表单里没有），`/api/login` 的 save_last_params
    #    存不到它 ⇒ 刷新页面就回落成默认。这里随每次「开始」写进 last_params，
    #    与作业侧 `auto_submit` 的持久化语义一致。
    if "exam_auto_submit" in data:
        save_last_params({"exam_auto_submit": bool(data.get("exam_auto_submit"))})
    ok, msg = _do_start(data.get("indices") or [], data)
    # 🔴 2026-10-13：`_do_start` 可能已**直接开考**（只勾考试的情形）。
    #    标记回传前端，让它清掉 pendingExam —— 否则考试跑完后
    #    `maybeRunExam` 会再开一次（同一场考试被开考两次 = 浪费机会）。
    if state.pop("exam_launched_by_start", False):
        return jsonify(ok=True, msg=msg, exam_launched=True)
    if not ok and state["running"]:
        # 任务已经在跑（典型：看门狗自动恢复抢先启动，用户又点了开始）。
        # 对用户而言目标状态已经达成，必须返回 ok=True 让前端切进进度页；
        # 返回 ok=False 会把用户卡在选课页，只能刷新网页才看得到进度。
        return jsonify(ok=True, msg=msg, already_running=True)
    return jsonify(ok=ok, msg=msg)


def _enrich_work_items(items):
    """补全作业清单里缺的 `href` / `enc_task` / `cpi` / `userid` / `clazzid`。

    🔴 为什么必须做：详情页只有走**列表页给的完整 href**（含 enc_task 签名）
    才能解析出 workId。缺 href 时程序会按平台 URL 规则"自己拼参数"，而平台
    对缺签名的请求一律回 3.9KB 壳页 → `未解析到 workId` → 用户看到
    「作业详情读取失败」。实测：有 href = 12841B 正常；无 href = 3996B 壳页。

    数据源用统一任务扫描的缓存 `state["task_scan"]["result"]["courses"][]`，
    按 `taskrefid` 精确匹配（顺序号会变，taskrefid 不会）。
    """
    try:
        res = (state.get("task_scan") or {}).get("result") or {}
        index = {}
        for c2 in (res.get("courses") or []):
            for w in (c2.get("works") or []):
                tid = str(w.get("taskrefid") or "")
                if tid:
                    index[tid] = w
        if not index:
            return items
        fixed, miss = 0, []
        for it in items:
            tid = str(it.get("taskrefid") or "")
            src = index.get(tid)
            for k in ("href", "enc_task", "cpi", "userid", "clazzid"):
                if not it.get(k) and src and src.get(k):
                    it[k] = src[k]
                    if k == "href":
                        fixed += 1
            if not it.get("href"):
                miss.append(f"{it.get('name') or tid}({tid})")
        if fixed:
            print(f"  [清单自愈] 从扫描缓存补齐了 {fixed} 份作业的 href")
        if miss:
            # 不是致命错：详情页还有"按平台规则拼 href"的兜底，但成功率低，
            # 所以必须显式告警，别让失败显得莫名其妙。
            print(f"  ⚠ [清单自愈] {len(miss)} 份作业在扫描缓存里找不到 href，"
                  f"将退化到拼参数（易被平台打回壳页）：{', '.join(miss[:5])}")
    except Exception as e:
        print(f"  [清单自愈] 失败（不影响其余作业）：{e}")
    return items


def _apply_submit_cfg(config):
    """把本次启动携带的「提交方式」覆盖到当前题库对象（2026-09-26 新增）。

    背景：提交开关原先只存在于**登录页**，`build_tiku()` 在登录那一刻把
    `auto_submit` / `cover_rate` 写进 Tiku 的 `SUBMIT` / `COVER_RATE`，
    之后 User 在主界面改开关是**完全无效**的（因为没人再去读界面）。
    现在主界面也常驻了这个开关，每次「开始」都会把它带下来，这里现场套用。

    容错：
      · config 里没有这两个键（自动恢复、旧调用方）→ 什么都不做，保持原行为
      · tiku 对象不存在（未登录/已被重置）→ 静默跳过，不影响主流程
      · 属性名兼容多种历史写法（SUBMIT / submit、COVER_RATE / cover_rate）
    """
    tiku = getattr(state.get("chaoxing"), "tiku", None)
    if tiku is None:
        return
    if "auto_submit" in config:
        val = bool(config["auto_submit"])
        for name in ("SUBMIT", "submit"):
            if hasattr(tiku, name):
                setattr(tiku, name, val)
        print(f"  [提交方式] 本次任务使用：{'自动提交' if val else '审核提交（只存草稿）'}")
    if "cover_rate" in config:
        val = config["cover_rate"]
        for name in ("COVER_RATE", "cover_rate"):
            if hasattr(tiku, name):
                setattr(tiku, name, val)
        print(f"  [提交方式] 覆盖率门槛：{val * 100:.0f}%")


def _run_homeworks(courses, work_ids, work_items=None):
    """执行作业阶段（2026-09-13 新增）。

    只处理勾选了"作业"的课程（按 courseId 匹配，不用下标）。
    work_items（2026-09-26 新增）：{courseId: [作业条目...]} —— 精确到单份作业。
    给了就**只做清单里的**，不再"整门课全做"；每份可带 redo=True 表示重做。
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
        only = (work_items or {}).get(cid)
        # 🔴 自愈：清单里缺 href / enc_task 的条目，从统一扫描缓存按 taskrefid
        # 反查补齐。**没有 href 就取不到详情页**（平台会打回 3.9KB 壳页，
        # 解析不到 workId → 报"作业详情读取失败"），所以这一步不是优化而是必需。
        if only:
            only = _enrich_work_items(only)
        print(f"\n===== 作业：{c.get('title', '')} "
              f"{'（指定 %d 份）' % len(only) if only else ''}=====")
        run_tracker.begin(cid, "homework", c.get("title", ""))
        try:
            res = state["chaoxing"].process_homeworks(c, work_list=only)
        except HomeworkNeedsAttention as e:
            print(f"  [需人工处理] {e}")
            need_attention.append(f"{c.get('title', '')}：{e}")
            # 需人工确认 ≠ 完成：标成中止（manual），别让用户以为这份作业交掉了
            run_tracker.abort(cid, f"作业需人工处理：{e}", "manual")
            continue
        except (PauseInterrupt, RiskControlError):
            # 暂停 / 风控熔断必须透传给 _work 处理，不能被当成"作业异常"吞掉
            raise
        except Exception as e:
            print(f"  [失败] 作业处理异常: {type(e).__name__}: {e}")
            run_tracker.abort(cid, f"作业异常：{type(e).__name__}", "error")
            continue
        done_total += res.get("done", 0)
        print(f"  完成 {res.get('done', 0)} 份，跳过 {res.get('skipped', 0)} 份")
        skipped_n = int(res.get("skipped", 0) or 0)
        if skipped_n and not int(res.get("done", 0) or 0):
            # 一份都没交出去、全被跳过：实质等于没做，不能报"已完成"
            run_tracker.abort(cid, f"作业全部被跳过（{skipped_n} 份）", "error")
        else:
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


_AUDIT_MAX_ROUNDS = 2   # 收尾复核后最多补刷的轮次（防死循环的硬上限）


def _chapter_flat_points(points):
    """把 `get_course_point` 的返回展平成节点列表（含子节点，滤掉无标题项）。"""
    flat, stack = [], list((points or {}).get("points") or [])
    while stack:
        it = stack.pop(0)
        flat.append(it)
        stack.extend(it.get("points") or [])
    return [x for x in flat if (x.get("title") or "").strip()]


def _audit_and_resume(courses, config, walk_ids, state):
    """🔴 2026-10-03（何同学要求）：任务收尾**轻量复核** + 按需继续。

    为什么需要：`JobProcessor.run()` 返回只代表"任务队列跑完了"，
    **不等于平台侧真的刷完** —— 实测 acc2 安全人机有 15 个节点在平台仍是黄点
    （根因见 HANDOFF §16：平台侧完成记录丢失 + 程序跳过 isPassed=true 的卡）。

    轻量设计（对应"不要太过繁重、影响其他"）：
      · 每轮只发 **课程数** 次请求（`get_course_point`，与任务开始时同量级）
      · 只为**未完成的节点**重建任务，不重扫整门课
      · 最多补刷 `_AUDIT_MAX_ROUNDS` 轮；**一旦无进展立即停** ——
        平台侧计数矛盾导致的"怎么刷都不动"会被这条挡住，不会死循环
      · 不做浏览器渲染、不并发、不新开线程、不写额外状态
      · 整个过程包在调用方的 try 里，异常不影响主流程
    """
    chaoxing = state.get("chaoxing")
    if not chaoxing:
        return
    prev = None
    for rnd in range(1, _AUDIT_MAX_ROUNDS + 1):
        bad = []          # [(course, [未完成节点])]
        for c in courses:
            cid = _course_id(c)
            if walk_ids is not None and cid not in walk_ids:
                continue
            try:
                pts = _chapter_flat_points(chaoxing.get_course_point(
                    c.get("courseId"), c.get("clazzId"), c.get("cpi")))
            except Exception as e:                                 # noqa: BLE001
                print(f"  [复核] 《{c.get('title', '')}》章节读取失败（跳过）: "
                      f"{type(e).__name__}")
                continue
            rest = [p for p in pts if not p.get("has_finished")]
            if rest:
                bad.append((c, rest))

        total = sum(len(r) for _, r in bad)
        if total == 0:
            print("[复核] ✅ 平台侧已无未完成节点")
            return
        print(f"[复核] 平台仍有 {total} 个未完成节点，涉及 {len(bad)} 门课")
        for c, r in bad:
            print(f"        《{c.get('title', '')}》 {len(r)} 个")
        if prev is not None and total >= prev:
            print(f"[复核] 与上一轮（{prev} 个）相比**无进展** → 停止补刷"
                  f"（平台侧计数问题，程序侧已无手段）")
            return
        prev = total

        tasks = [ChapterTask(point=p, index=i, course=c)
                 for c, r in bad for i, p in enumerate(r)]
        if not tasks:
            return
        print(f"[复核] 第 {rnd} 轮补刷，共 {len(tasks)} 个章节任务点\n")
        proc = JobProcessor(chaoxing, tasks, config)
        state["job_processor"] = proc
        try:
            proc.run()
        except Exception as e:                                     # noqa: BLE001
            print(f"[复核] 补刷异常: {type(e).__name__}: {e}")
            return
        print(f"[复核] 第 {rnd} 轮补刷结束")


def _work(courses, config):
    # 🔴 2026-09-28：防睡眠请求**必须在这个长期线程里持有**，不能放 _launch()。
    # SetThreadExecutionState 的请求是线程级的，Flask 请求线程处理完 /api/start
    # 就结束，请求会被系统自动清除 —— 实测后果：关屏 6 分钟后平台进入
    # Modern Standby 的 Disconnected Standby，Win32 进程被冻结 + 网络断开
    # （手机连不上、日志整段空白，但进程没死）。
    _prevent_sleep(True)
    try:
        start_capture()
        # ---- 内容选择（2026-09-13 新增）----
        # video_course_ids 为 None 表示"未区分"（旧行为：全部课程都刷课）。
        # 两项都按 courseId 匹配，与恢复路径保持一致。
        video_ids = config.get("video_course_ids")
        work_ids = config.get("work_course_ids")
        quiz_ids = config.get("quiz_course_ids") or []
        # walk_ids：需要遍历章节的课（勾了视频或测验）。
        # 旧调用方没这个键 → 回退成"旧行为"（video_ids 为 None 时全遍历）。
        walk_ids = config.get("walk_course_ids")
        if walk_ids is None:
            walk_ids = None if video_ids is None else sorted(set(video_ids) | set(quiz_ids))
        # 运行看板（2026-09-14）：先把"这次一共要碰哪几门课"登记进去，
        # 界面才能显示"剩余 N 门（排队中）"，而不是只有一门在动、其余无影无踪。
        run_tracker.reset()
        for c in courses:
            cid = _course_id(c)
            do_walk = (walk_ids is None) or (cid in walk_ids)
            do_work = bool(work_ids) and (cid in work_ids)
            if do_walk:
                run_tracker.begin(cid, "video", c.get("title", ""))
            if do_work:
                run_tracker.begin(cid, "homework", c.get("title", ""))
            if not do_walk and not do_work:
                run_tracker.mark_skip(cid, "未勾选任何内容")
            elif not do_walk:
                run_tracker.mark_skip(cid, "只勾了作业，不遍历章节")
        tasks = []
        point_map = {}      # 任务点名 → courseId（供进度解析反查归属）
        for c in courses:
            cid = _course_id(c)
            do_walk = (walk_ids is None) or (cid in walk_ids)
            # 🔴 2026-09-28：**不遍历章节的课要在这里就跳过**。
            #    原先 `if not do_walk: continue` 写在拿到章节列表**之后**，于是：
            #      ① 白白为"只勾作业"的课（含全部额外课程）发一次章节请求
            #      ② 更糟：这些课若解析不到章节（额外课程本来就没有），
            #         会被下面的 `if not pts` 分支判成"未解析到章节任务点 → 中止"，
            #         用户在界面上看到课程被标红中止，而它其实只是不刷章节。
            if not do_walk:
                print(f"  [跳过章节] 《{c.get('title', '')}》"
                      f"（未勾视频/测验，作业在后续阶段处理）")
                continue
            print(f"读取章节: {c.get('title', '')}")
            try:
                points = state["chaoxing"].get_course_point(
                    c.get("courseId"), c.get("clazzId"), c.get("cpi"))
            except Exception as e:
                print(f"  [失败] 读取章节异常: {type(e).__name__}: {e}")
                # 读取失败 = 这门课的任务点一个都没拿到，绝不能报"已完成"
                run_tracker.abort(cid, "读取章节失败", "error")
                continue
            pts = (points or {}).get("points") or []
            print(f"  -> 解析到章节数: {len(pts)}")
            if not pts:
                print("  [提示] 该课程没解析到章节，可能章节未开放、已全部完成，或页面结构已变化")
                run_tracker.set_tasks(cid, 0)
                # "没解析到章节"有三种可能（全完成 / 未开放 / 结构变了），
                # 无法确认就是刷完了 → 归入中止，提示用户自己去核对，
                # 而不是替用户拍板"已完成"（2026-09-24 用户报障的正是这个）
                run_tracker.abort(cid, "未解析到章节任务点（请到学习通核对）", "error")
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
            # 🔴 2026-10-03（何同学要求）：任务结束 ≠ 平台侧真的刷完。
            #    "每次任务显示已经结束之后进行一次审查，从平台处核实是否全部刷完了；
            #     如果有没刷完的，再重新继续。"
            #    轻量实现见 `_audit_and_resume`：每轮仅"课程数"次请求，最多 2 轮，
            #    无进展即停；异常一律吞掉，**绝不影响后面的作业阶段**。
            try:
                _audit_and_resume(courses, config, walk_ids, state)
            except Exception as e:                                 # noqa: BLE001
                print(f"[复核] 异常（不影响主流程）: {type(e).__name__}: {e}")
        elif not work_ids:
            # 既没有刷课任务、也没勾选作业：保持旧行为（保留恢复标记后返回）
            print("没有可执行任务。常见原因：课程已全部完成、章节尚未开放、登录状态失效。")
            return
        else:
            print("没有需要刷课的章节任务（只勾选了作业，或课程已全部完成）")

        # ---- 作业阶段 ----
        if work_ids:
            # 🔴 2026-09-26 新增：把主界面「提交方式」现场套到题库对象上。
            # 为什么必须在这里套：`Tiku.SUBMIT` / `COVER_RATE` 是**登录那一刻**
            # 由 build_tiku() 从 ui_config 写死的，之后改界面上的开关
            # 完全影响不到它 —— 用户会看到"改了提交方式，结果还是被自动交卷"。
            # 只在本次启动显式带了这个键时才覆盖，自动恢复/旧调用方不带 → 行为不变。
            _apply_submit_cfg(config)
            _run_homeworks(courses, work_ids, work_items=config.get("work_items"))
        else:
            print("未勾选作业，跳过作业阶段")

        print("\n全部任务执行结束，请到学习通核对进度。")
        # 🔴 2026-10-27（何同学要求）：刷完课后重扫考试并接着做。
        #   课程考试要刷完才解锁，启动时那份清单的 enc_task 已经失效。
        if _maybe_run_exams_after_work(courses, config):
            state["exam_handoff"] = True
        _clear_resume()   # 正常跑完：清掉待恢复标记，下次启动不再自动续刷
    except RiskControlError as e:
        # 风控熔断：必须同时关掉自动恢复，否则看门狗/重启会立刻再撞上去，
        # 把"连续 3 次风控"变成"每次重启都刷一遍"，反而更危险。
        print(f"\n⚠ 已触发平台风控，本轮任务已中止：{e}")
        print("   已关闭自动恢复。请等待 10-30 分钟冷却后再手动点「开始刷课」，")
        print("   并先在学习通上手动完成反复报 403 的那个视频。")
        _clear_resume()
        state["paused"] = True   # 让前端停在"可继续"状态，而不是显示"已结束"
        # 熔断时刻所有在跑的课都是"被风控打断"，不是"刷完了" ——
        # 这里立刻落库，finally 才不会把它们误收口成绿色 done。
        n = run_tracker.abort_all_running("平台风控中止（未刷完）", "risk")
        if n:
            print(f"   {n} 门课程已标记为「风控中止」，未计入已完成。")
        state["risk_abort"] = str(e)   # 供前端出横幅（含冷却提示）
    except PauseInterrupt:
        print("\n已暂停：当前任务点在最近一次上报后中断，点击「继续」将从没刷完的任务接着跑。")
        state["paused"] = True
        # 暂停同理：没刷完的课不能显示"已完成"，但它是"可继续"的中途态，
        # 用 pending 留住（前端已另外显示"已暂停"），不当作中止。
        for cid, row in list(run_tracker.rows.items()):
            if row.get("status") == "running":
                run_tracker.finish(cid, row.get("active") or "video", "pending")
    except BaseException as e:
        traceback.print_exc()
        print("\n执行出错，详见上方堆栈。")
        run_tracker.abort_all_running(f"运行异常：{type(e).__name__}", "error")
        state["risk_abort"] = ""
    finally:
        # 收尾：把还挂在"运行中"的课程收成终态，否则看板上会永远留着
        # 几门"正在刷"的课（用户抱怨过"看不出剩下的课到底在不在跑"）。
        #
        # ⚠ 曾经的写法是「一律 finish(..., 'done')」，注释还写着"不会误导"——
        # 2026-09-24 实测恰恰就是它误导：4 门课全被风控挡下、一门没刷，
        # 界面却全绿显示"已完成"。正确语义是：
        #   真的跑到结尾 → done
        #   有任务点、但状态还是 running/pending → 说明是被打断的 → aborted
        #   从头到尾没被碰过 → skipped（本轮未执行）
        for cid, row in list(run_tracker.rows.items()):
            if row.get("status") not in ("running", "pending"):
                continue
            if row.get("status") == "pending" and not row.get("tasks") \
                    and not row.get("work"):
                # 从头到尾没被碰到的课程：明确标成"未执行"，别让它看起来还在排队
                run_tracker.mark_skip(cid, "本轮未执行")
                continue
            # 有任务却停在运行/排队 = 没跑完。有任务点却一个都没完成，更是明确没刷。
            tasks_n = int(row.get("tasks") or 0)
            done_n = int(row.get("done") or 0)
            if tasks_n and done_n >= tasks_n:
                run_tracker.finish(cid, row.get("active") or "video", "done")
            else:
                tail = f"（任务点 {done_n}/{tasks_n}）" if tasks_n else ""
                run_tracker.abort(cid, f"本轮未跑完{tail}", "error")
        _prevent_sleep(False)
        state["job_processor"] = None
        stop_capture()
        # 🔴 2026-10-27：若控制权已交给考试线程，这里**不能**把 running 置 False。
        #   否则前端会以为任务结束 → 看不到考试进度；考试线程自己结束时才置 False。
        if not state.pop("exam_handoff", False):
            state["running"] = False


@app.get("/api/live_params")
def api_live_params_get():
    """运行中快捷调参 —— 读当前值（2026-09-29）。

    用户诉求原话："加一个快捷调节这些东西的入口。要不然每次都需要退出去切换账号
    才能调节，还要重新登录，重新刷。"
    """
    cfg = state.get("last_config") or {}
    proc = state.get("job_processor")
    return jsonify(ok=True,
                   jobs=int(cfg.get("jobs") or 1),
                   speed=float(cfg.get("speed") or 1.0),
                   cover_rate=float(cfg.get("cover_rate") or 0.9),
                   auto_submit=bool(cfg.get("auto_submit", True)),
                   worker_num=int(getattr(proc, "worker_num", 0) or 0),
                   running=bool(state.get("running")),
                   paused=bool(state.get("paused")))


@app.post("/api/live_params")
def api_live_params_post():
    """运行中改参数（2026-09-29）。

    ⚠️ 关键事实（决定这个接口怎么实现）：
      · `jobs` / `speed` 是**任务级**参数 —— `main.py` 的 TaskHandler 只在启动时
        读一次 `config["jobs"]` 建线程池、读一次 `config["speed"]`，
        **运行中改内存不生效**，必须重建任务。
        这里用「改 last_config → pause → resume」实现，走的是和手动暂停/继续
        完全一样的路径，**进度不丢**（只是当前那个视频会重播一次）。
      · `cover_rate` / `auto_submit` 是每个题组**实时读**的，改完立即生效，不用重建。
      · speed 这里也按 1.0~2.0 钳制 —— 与 `main.py` 的钳制保持一致，
        否则会出现"接口放行了 3 倍速、任务里又被压回 2"的错觉。
    """
    data = request.get_json(force=True) or {}
    cfg = dict(state.get("last_config") or {})
    if not cfg:
        return jsonify(ok=False, msg="还没有任务配置（请先在选课页开始一次）")

    # ① 先算出「这次到底要改什么」，**暂时不落盘**
    plan = {}
    if data.get("jobs") is not None:
        j = max(1, min(8, int(data["jobs"])))
        if j != int(cfg.get("jobs") or 1):
            plan["jobs"] = j
    if data.get("speed") is not None:
        sp = max(1.0, min(2.0, float(data["speed"])))
        if abs(sp - float(cfg.get("speed") or 1.0)) > 1e-6:
            plan["speed"] = sp
    live = {}
    if data.get("cover_rate") is not None:
        live["cover_rate"] = max(0.0, min(1.0, float(data["cover_rate"])))
    if data.get("auto_submit") is not None:
        live["auto_submit"] = bool(data["auto_submit"])

    need_restart = bool(plan)
    want_apply = bool(data.get("apply_now", True))
    can_restart = need_restart and want_apply and state.get("running") and not state.get("paused")

    # ② 需要重建任务时：**先暂停、等真正停稳，再改配置**。
    #    用户明确要求："要注意虽然可以改但是不要影响，所以改的时候可以先暂停。"
    #    —— 所以暂停失败/没停稳就**一个字都不改**，绝不留下"配置改了、任务还在跑"
    #    这种两边不一致的中间态。
    if can_restart:
        try:
            api_pause()
        except Exception as e:
            return jsonify(ok=False, msg="暂停失败：%s（未做任何修改）" % e)
        for _ in range(60):                       # 最多等 12 秒让 worker 真停住
            if not state.get("running"):
                break
            time.sleep(0.2)
        if state.get("running"):
            return jsonify(ok=False,
                           msg="暂停未在 12 秒内完成，未做任何修改；请手动暂停后再试")

    # ③ 现在才落盘（此时任务已停稳，或本来就没在跑）
    LABEL = {"jobs": "并发", "speed": "倍速", "cover_rate": "门槛"}
    changed = []
    for k, v in list(plan.items()) + list(live.items()):
        cfg[k] = v
        if k == "auto_submit":
            changed.append("自动提交" + ("开" if v else "关"))
        else:
            changed.append("%s %s" % (LABEL.get(k, k), v))
    state["last_config"] = cfg
    _persist_live_params(cfg)

    restarted = False
    if can_restart:
        try:
            api_resume()
            restarted = True
        except Exception as e:
            return jsonify(ok=False, changed=changed, need_restart=need_restart,
                           msg="重建任务失败：%s（参数已保存，可手动点继续）" % e)
    elif need_restart:
        # 改了 jobs/speed 但没能立刻重建（未在跑 / 已暂停 / 明确不 apply_now）
        return jsonify(ok=True, changed=changed, need_restart=True, restarted=False,
                       msg="已保存；并发/倍速将在下次开始或点「继续」时生效")
    return jsonify(ok=True, changed=changed, need_restart=need_restart, restarted=restarted)


def _pause_now():
    """向任务线程注入暂停异常。返回 (ok, msg)。

    与 `/api/pause` 分离出来是给「添加课程」用：追加课程要重建任务，
    必须先停掉当前这轮（`_do_start` 在 running 时会直接拒绝）。
    """
    if not state["running"] or state["paused"]:
        return False, "当前没有可暂停的任务"
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
        # 🔴 2026-10-01 修：这里原本写的是 `return jsonify(...)` —— 返回的是
        #    Flask Response，而本函数契约是 `(ok, msg)`。调用方 `api_pause` 做
        #    `ok, msg = _pause_now()` 解包时会抛 TypeError → 暂停接口 500，
        #    「添加课程」和「运行参数」两条链路也会连带崩在这里。
        return False, "任务恰好已结束，无需暂停"

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
        return False, "暂停信号注入失败，请重试或直接关闭窗口"
    # 立即置位，避免 worker 退出与前端轮询之间出现"看着还在跑"的窗口
    state["paused"] = True
    return True, ""


@app.post("/api/pause")
def api_pause():
    ok, msg = _pause_now()
    return jsonify(ok=ok, msg=msg)


def _resume_now():
    """恢复暂停中的任务。返回 (ok, msg)。与 `/api/resume` 共用。"""
    if state["running"] or not state["paused"]:
        return False, "没有可恢复的任务"
    picked, config = state["last_courses"], state["last_config"]
    if not picked:
        return False, "找不到上次选课记录，请返回选课页重新开始"
    state["paused"] = False
    state["running"] = True
    state["risk_abort"] = ""
    th = threading.Thread(target=_work, args=(picked, config), daemon=True)
    state["work_thread"] = th
    th.start()
    return True, ""


@app.post("/api/resume")
def api_resume():
    ok, msg = _resume_now()
    return jsonify(ok=ok, msg=msg)


# ==================== 运行中「添加课程」（2026-10-01）====================
# 背景（用户原话）：「这个界面只有一个返回选课，如果我想添加课程接在上面的，
#   只能返回选课重新选，它不会显示，直接在这里加个添加的。」
# 即：进度页没有追加课的入口，退回选课页重选既麻烦、又容易漏掉原来在刷的课。
#
# 实现要点：TaskHandler 只在启动时读一次课程清单，所以追加**必须重建任务**
#   → 先 `_pause_now()` 停掉当前这轮（只中断当前任务点，已上报的进度不回滚），
#     再用「原清单 ∪ 新课」重新 `_do_start`。

def _current_pick_ids():
    """当前任务里的 courseId 集合（供「添加课程」弹窗标注"已在刷"）。"""
    return {_course_id(c) for c in (state.get("last_courses") or [])}


@app.get("/api/current_pick")
def api_current_pick():
    return jsonify(ok=True, course_ids=sorted(_current_pick_ids()),
                   running=bool(state["running"]))


@app.post("/api/append_courses")
def api_append_courses():
    """把新选的课程并进当前任务，不必退回选课页重选。

    新增课程按「视频 + 章节测验」加入；作业维度只在当前任务**不是**
    "精确清单模式"时才一并纳入（否则等于替用户决定做哪几份作业）。
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    add = [str(x) for x in (data.get("course_ids") or []) if str(x)]
    if not add:
        return jsonify(ok=False, msg="未选择任何课程")

    known = {_course_id(c) for c in _view_courses()}
    cur = _current_pick_ids()
    if not cur:
        return jsonify(ok=False, msg="当前没有正在跑的任务，请先回选课页开始")
    new_ids = [i for i in add if i in known and i not in cur]
    if not new_ids:
        return jsonify(ok=False, msg="选中的课程都已经在刷了")

    cfg = dict(state.get("last_config") or {})

    def _merge(key):
        return sorted({str(x) for x in (cfg.get(key) or [])} | set(new_ids))

    payload = {"video_course_ids": _merge("video_course_ids"),
               "quiz_course_ids": _merge("quiz_course_ids"),
               "jobs": cfg.get("jobs") or 1,
               "speed": cfg.get("speed") or 1.0}
    if not (cfg.get("work_items") or {}):
        payload["work_course_ids"] = _merge("work_course_ids")
    # 精确清单原样保留：不带它 `_run_homeworks` 会退化成"整门课全做"，
    # 等于把范围偷偷放大到用户没勾的作业上。
    if cfg.get("work_items"):
        payload["work_items"] = cfg["work_items"]
    if cfg.get("exam_items"):
        payload["exam_items"] = cfg["exam_items"]

    was_running = bool(state["running"])
    if was_running:
        ok, msg = _pause_now()
        if not ok:
            return jsonify(ok=False, msg=msg)
        deadline = time.time() + 30
        while state["running"] and time.time() < deadline:
            time.sleep(0.2)
        if state["running"]:
            # 暂停没落地就硬开会撞上"已有任务在运行"，先恢复原任务再报错
            state["paused"] = False
            return jsonify(ok=False, msg="暂停超时，请稍后重试")

    ok, msg = _do_start([], payload)
    if not ok:
        # 追加失败不能把任务就这么停着 —— 恢复原任务
        if was_running and state["paused"]:
            _resume_now()
        return jsonify(ok=False, msg=msg or "追加失败")
    return jsonify(ok=True, added=len(new_ids), total=len(cur) + len(new_ids))


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
    # 🔴 2026-10-03 修 bug：这里**必须回放 `quiz_course_ids`**。
    #    此前只回放 video_course_ids → `_do_start` 里 quiz_ids 变成 [] →
    #    only_kinds 只剩 {"video"} → `process_job` 把所有章节测验静默跳过（还按成功计），
    #    紧接着 `_save_last_pick` 回写又会把 last_pick 里的测验抹掉
    #    → 之后每次错峰启动 / 续跑都只刷视频，**自我强化、永久失效**。
    #    实测后果：2026-10-03 全部 8 个实例的 quiz_course_ids 均为空，
    #    被回溯后重刷的章节只补了视频、测验全程没做，平台上表现为"黄点"。
    ok, msg = _do_start([], {"video_course_ids": ids,
                             "quiz_course_ids": lp.get("quiz_course_ids") or [],
                             # 🔴 2026-10-03 补漏（第三方审计 P1-1）：原为硬编码 `[]`。
                             #    与 quiz 那次事故**完全同构**：`_do_start` 里
                             #    `work_ids` 直接取这个值，虽然后面有
                             #    `work_ids |= set(work_items.keys())` 兜底，但
                             #    "勾了作业课、没勾具体份"（work_items 为空）时，
                             #    作业阶段会被静默跳过，且 `_save_last_pick`
                             #    随即把 `work_course_ids: []` 回写固化。
                             "work_course_ids": lp.get("work_course_ids") or [],
                             # 🔴 2026-10-03：默认值由 2 改 1。
                             #    `last_pick` 缺 speed 时按 2 倍速跑会触发平台回溯，
                             #    与"全局 speed=1"策略冲突（`_do_start` 的
                             #    `min(2.0,max(1.0,…))` 夹不掉 2，会真的生效）。
                             "speed": lp.get("speed") or 1.0,
                             "jobs": lp.get("jobs", 1),
                             # 🔴 把"逐份作业清单"一并回放，否则错峰启动会
                             # 把精确选择放大成整门课全做
                             "work_items": lp.get("work_items") or {},
                             "exam_items": lp.get("exam_items") or {}})
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
    """一键全停：杀掉题库服务与自身（正在运行的任务一并中断，已上报进度不丢）。

    2026-09-15 修缺陷：多实例下网关「停止实例 N」也走这个端点，于是
    **停任意一个实例都会杀掉共享的 tikuAdapter**（8060）。后果是其它仍在
    运行的实例瞬间失去题库来源 —— 未命中全部转 AI，命中率暴跌，而界面上
    看不出任何异常（这正是"题库经常出问题"的隐蔽来源之一）。

    修复：默认只关自己；只有显式带 ?all=1（网关的"停止所有服务"/一键全停）
    才连题库一起杀。

    🔴 2026-10-04 新增 `?keep_resume=1`：网关的「停止所有服务」做了优雅化
    （先逐个 pause 再停），需要**保留续跑依据**，否则下次开机不会自动续刷。
    不带该参数时行为与原来完全一致（清 resume）。实例页自己的「退出服务」
    依旧不传 → 仍然是"我主动退出这一台，下次别自动跑"。
    """
    kill_tiku = str(request.args.get("all") or "").strip() in ("1", "true", "yes")
    keep_resume = str(request.args.get("keep_resume") or "").strip() in ("1", "true", "yes")

    def _bye():
        if not keep_resume:
            _clear_resume()   # 用户主动停止：下次启动不自动续刷
        if kill_tiku:
            try:
                subprocess.run(["taskkill", "/F", "/IM", "tikuAdapter.exe"],
                               capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            except Exception:
                pass
        os._exit(0)

    threading.Timer(0.8, _bye).start()
    return jsonify(ok=True)


@app.post("/api/diag/exec")
def api_diag_exec():
    """🔧 临时诊断：在**实例进程内**执行一段脚本并回传 stdout。

    存在理由：本机 Python 沙箱**直连外网不通**（RemoteDisconnected），
    想测 AI 平台的限流行为必须借助**实例进程已登录的网络栈**。
    只读用途，仅允许执行工作区下 `_` 前缀的临时脚本。
    """
    if not state["logged_in"]:
        return jsonify(ok=False, msg="请先登录")
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    import os as _os
    if not path or not _os.path.isfile(path):
        return jsonify(ok=False, msg=f"脚本不存在：{path}")
    # 🔴 2026-10-03 安全加固：原判据**只看 basename 前缀**，docstring 里声称的
    #    "仅允许工作区下"根本没实现 → 传 `D:\任意目录\_x.py` 照样执行
    #    （等价于任意代码执行入口）。现在要求脚本必须落在项目根内。
    #    注意：不删除本接口 —— `_audit_all.py` / `_chk_speed.py` / `_diag_silent.py`
    #    等运维工具全靠它在**实例进程内**读真实状态，删了整个巡检链路会失效。
    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))   # 项目根
    if not _os.path.realpath(path).startswith(_os.path.realpath(_root) + _os.sep):
        return jsonify(ok=False, msg="只允许执行项目目录内的临时脚本")
    if not _os.path.basename(path).startswith("_"):
        return jsonify(ok=False, msg="只允许执行以下划线开头的临时脚本")
    import io as _io
    import contextlib as _ctx
    buf = _io.StringIO()
    g = {"__name__": "__diag__", "__file__": path}
    try:
        src = open(path, encoding="utf-8").read()
        with _ctx.redirect_stdout(buf), _ctx.redirect_stderr(buf):
            exec(compile(src, path, "exec"), g)
        return jsonify(ok=True, out=buf.getvalue()[-20000:])
    except Exception as e:
        import traceback
        return jsonify(ok=False, out=buf.getvalue()[-8000:],
                       msg=f"{type(e).__name__}: {e}",
                       tb=traceback.format_exc()[-3000:])


def _port_listeners(port=None):
    """返回当前监听**本实例端口**的 PID 集合（中文系统 netstat 是 GBK 编码）。

    多实例支持：只认自己的端口，否则会把别的账号那份实例也杀掉。

    🔴 2026-10-08 加固（何同学遇到 `NETSTAT.EXE 应用程序无法正常启动(0xc0000142)`）：
      原实现**没有异常保护**，而本函数在 `__main__` 启动流程里被
      `_kill_other_instances()` 调用 ⇒ netstat 一旦启动失败（DLL 初始化失败，
      系统层面的偶发问题）就会把 OSError 抛进启动流程 ⇒ **实例直接起不来**。
      现与 `gateway.py` 的同名实现对齐：失败即返回空集合；
      并加 `CREATE_NO_WINDOW` 避免拉起控制台宿主。
    """
    port = port or PORT
    try:
        raw = subprocess.run(
            ["netstat", "-ano"], capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception as e:                                          # noqa: BLE001
        logger.warning(f"查询端口监听进程失败（忽略，不影响刷课）: {e}")
        return set()
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

    🔴 2026-10-08：整段加异常保护 —— 本函数在启动流程里调用，
    系统工具（netstat/taskkill）偶发启动失败时**不得**把实例拖垮。
    """
    killed = []
    try:
        listeners = _port_listeners()
    except Exception as e:                                          # noqa: BLE001
        logger.warning(f"枚举端口占用进程失败（忽略）: {e}")
        return killed
    for pid in listeners:
        if pid == os.getpid():
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:                                      # noqa: BLE001
            logger.warning(f"清理残留进程 {pid} 失败（忽略）: {e}")
            continue
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
