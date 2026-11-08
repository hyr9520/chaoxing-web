import configparser
import difflib
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from re import sub
from typing import Optional

import httpx
import requests
from openai import OpenAI
from urllib3 import disable_warnings, exceptions

from api.answer_check import check_answer, plausible_answer, is_listening_question
from api import ai_vote
from api.config import data_dir
from api.image_support import build_image_data_urls
from api.learned import LearnedAnswers, normalize_title
from api.logger import logger

# 关闭警告
disable_warnings(exceptions.InsecureRequestWarning)

__all__ = ["CacheDAO", "Tiku", "TikuFallback", "TikuYanxi", "TikuGo", "TikuLike", "TikuAdapter", "TikuHive",
           "TikuJszkk", "AI", "SiliconFlow", "TikuManual", "AIKeyExhaustedError"]


class AIKeyExhaustedError(Exception):
    """🔴 2026-09-26 23:40 新增：AI 题库的**全部 Key 都不可用**（额度耗尽/Key 无效）。

    为什么要单独一个异常类型：
      多 Key 池全部打光后，如果直接把 SDK 原始异常（401/429）抛出去，
      它会一路穿透 `_query_locked` → `_query`（降级分支）=无匹配 → `_call`
      → `TikuFallback._query` 的 `except Exception` 虽会兜住，
      但**日志语义混乱**，且 `_query` 的模型降级分支会先错误地尝试
      "换模型重试"（同一个死 Key，必然再次 401），白白多打一轮请求。
      ⇒ 用专用类型让上层**明确识别**"这是 Key 级故障、换模型没用、应直接
        回退到下一个题库"。
    """
    pass


class CacheDAO:
    """
    @Author: SocialSisterYi
    @Reference: https://github.com/SocialSisterYi/xuexiaoyi-to-xuexitong-tampermonkey-proxy
    """
    # 题库缓存随实例数据目录走（2026-09-14）：多账号实例各自一份，
    # 否则 A 账号的答案会被 B 账号直接命中复用，成绩来源就乱了
    DEFAULT_CACHE_FILE = os.path.join(data_dir(), "cache.json")

    def __init__(self, file: str = DEFAULT_CACHE_FILE):
        self.cache_file = Path(file)
        self._lock = threading.RLock()
        if not self.cache_file.is_file():
            self._write_cache({})

    def _read_cache(self) -> dict:
        # 新增缓存文件读取的异常处理
        try:
            with self._lock:
                if not self.cache_file.is_file():
                    return {}
                try:
                    with self.cache_file.open("r", encoding="utf8") as fp:
                        return json.load(fp)
                except json.JSONDecodeError as e:
                    logger.error(f"缓存文件 JSON 解析失败: {e}, 尝试恢复...")
                    # 尝试从原始二进制中以 utf-8 忽略错误地恢复有效 JSON 段
                    try:
                        raw = self.cache_file.read_bytes()
                        text = raw.decode("utf-8", errors="ignore")
                        start = text.find('{')
                        end = text.rfind('}')
                        if start != -1 and end != -1 and start < end:
                            try:
                                return json.loads(text[start:end + 1])
                            except Exception:
                                pass
                    except Exception:
                        pass
                    # 若无法恢复，备份损坏文件并返回空缓存
                    try:
                        bak_name = f"{self.cache_file.name}.bak.{int(time.time())}"
                        bak_path = self.cache_file.with_name(bak_name)
                        shutil.copy2(self.cache_file, bak_path)
                        logger.error(f"缓存文件已损坏，已备份为: {bak_path}，将使用空缓存继续运行")
                    except Exception as ex:
                        logger.error(f"备份损坏缓存失败: {ex}")
                    return {}
                except UnicodeDecodeError as e:
                    logger.error(f"缓存文件编码读取失败: {e}, 采用恢复策略...")
                    try:
                        raw = self.cache_file.read_bytes()
                        text = raw.decode("utf-8", errors="ignore")
                        start = text.find('{')
                        end = text.rfind('}')
                        if start != -1 and end != -1 and start < end:
                            try:
                                return json.loads(text[start:end + 1])
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        bak_name = f"{self.cache_file.name}.bak.{int(time.time())}"
                        bak_path = self.cache_file.with_name(bak_name)
                        shutil.copy2(self.cache_file, bak_path)
                        logger.error(f"缓存文件编码错误，已备份为: {bak_path}，将使用空缓存继续运行")
                    except Exception as ex:
                        logger.error(f"备份损坏缓存失败: {ex}")
                    return {}
        except Exception as e:
            logger.error(f"读取缓存异常: {e}")
            return {}

    def _write_cache(self, data: dict) -> None:
        # 为缓存写入加锁，防止并发写入损坏文件
        try:
            with self._lock:
                parent = self.cache_file.parent
                if not parent.exists():
                    parent.mkdir(parents=True, exist_ok=True)
                # 写入临时文件后原子替换，减少并发写入时的损坏风险
                fd, tmp_path = tempfile.mkstemp(prefix=self.cache_file.name, dir=str(parent))
                try:
                    with os.fdopen(fd, "w", encoding="utf8") as fp:
                        json.dump(data, fp, ensure_ascii=False, indent=4)
                        fp.flush()
                        os.fsync(fp.fileno())
                    os.replace(tmp_path, str(self.cache_file))
                except Exception as e:
                    # 清理临时文件
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
                    logger.error(f"Failed to write cache atomically: {e}")
        except IOError as e:
            logger.error(f"Failed to write cache: {e}")

    def get_cache(self, question: str) -> Optional[str]:
        data = self._read_cache()
        return data.get(question)

    def add_cache(self, question: str, answer: str) -> None:
        # 为缓存写入加锁，防止并发写入损坏文件
        with self._lock:
            data = self._read_cache()
            data[question] = answer
            self._write_cache(data)

    def remove_cache(self, question: str) -> bool:
        """从题库撤下某个条目的答案（2026-09-11 审查新增）。

        用途：平台判定"这个答案错了"时，若题库里存的正是该值，说明它不可靠，
        必须撤下 —— 否则会一直把错误答案当作标准答案复用（用户最初的问题）。
        返回是否确实删除了条目。
        """
        with self._lock:
            data = self._read_cache()
            if question not in data:
                return False
            data.pop(question, None)
            self._write_cache(data)
            return True


def _option_pool(q_info: dict) -> list:
    """从题目 options 里解析出可选字母池（如 ['A','B','C','D']）；解析不出用 A-D。"""
    letters = []
    raw = str((q_info or {}).get("options") or "")
    for m in re.finditer(r"(?:^|[\s,，;；(（\[])([A-H])(?=[\s.、)）\]：:])", raw):
        c = m.group(1)
        if c not in letters:
            letters.append(c)
    return letters or list("ABCD")


def _sub_count(q_info: dict) -> int:
    """题组含几个小问（从 answerField 的小问字段数推断，与 base 里的算法一致）。"""
    q = q_info or {}
    qid = str(q.get("id", ""))
    af = q.get("answerField") or {}
    subs = [k for k in af
            if k.startswith(f"answer{qid}") and k != f"answer{qid}"
            and not k.startswith("answertype")]
    return len(subs) if subs else 1


def _assemble_subs(sub_verified: dict, n: int):
    """把已确认的小问答案按顺序组装成完整连写答案；不齐或非法则返回 ''。"""
    if not n or n < 1:
        return ""
    chars = []
    for i in range(n):
        v = str((sub_verified or {}).get(str(i)) or "").strip()
        if not re.fullmatch(r"[A-Za-z]+", v):
            return ""
        chars.append(v[0].upper())
    return "".join(chars)


def _apply_verified_subs(ans: str, sub_verified: dict, wrong: list = None,
                         q_info: dict = None) -> str:
    """把"平台已确认正确的小问答案"填进题组答案的对应位置（2026-09-11）。

    背景：听力题一个题组含多个小问，平台按小问判对错。整组没全对时，此前会把
    所有小问答案一起丢掉；现在已确认的小问答案存在学习库 sub_verified 里，
    这里把它们覆盖到连写答案的对应位（如 "BA" 第 2 位确认是 A -> 修正为 ?A）。

    覆盖后若整体落回"历史判错的组合"，只调整**未确认**的那些位，
    找一个不在 wrong 里的组合（已确认的位不动）。
    """
    text = str(ans or "").strip()
    if not sub_verified or not re.fullmatch(r"[A-Za-z]+", text):
        return ans
    chars = list(text.upper())
    fixed = set()
    for k, v in (sub_verified or {}).items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        v = str(v or "").strip().upper()
        if 0 <= i < len(chars) and re.fullmatch(r"[A-Z]+", v):
            chars[i] = v[0]
            fixed.add(i)
    cand = "".join(chars)
    wrong_set = {str(w).strip().upper() for w in (wrong or [])}
    if cand not in wrong_set:
        return cand
    pool = _option_pool(q_info) if q_info else list("ABCD")
    for i in range(len(chars)):
        if i in fixed:
            continue
        for c in pool:
            if c == chars[i]:
                continue
            trial = "".join(chars[:i] + [c] + chars[i + 1:])
            if trial not in wrong_set:
                return trial
    return cand


# 🔴 2026-10-01：与 `api/ai_vote.py` 的 `JUDGE_T` / `JUDGE_F` **取并集**。
#    此前同一套"判断题等价表述"被定义了**两份且不一致**：
#      · 本处缺「对的 / 正确的」（肯定）
#      · ai_vote 缺「不对 / 不正确」（否定）
#    任一处遇到"对方认得、自己不认得"的形态就归一失败 → 已证伪的答案被漏放行，
#    或者投票时把同一个答案当成两票不同的意见。两份必须同源，这里补齐并集。
_JUDGE_T = {"对", "正确", "是", "√", "true", "t", "yes", "y", "1",
            "对的", "正确的"}
_JUDGE_F = {"错", "错误", "否", "×", "x", "false", "f", "no", "n", "0",
            "不对", "不正确", "错的", "错误的"}


def judge_key(s):
    """判断题答案归一 → True / False / None（无法判断）。2026-09-27 新增。

    必要性：平台展示用「正确/错误」，而学习库 wrong 列表里常存「对/错」，
    两者直接字符串比对**会漏判**（"错误" in ["错"] == False）→ 已证伪的答案
    被漏掉，又提交一遍。凡是判断类比对都要先过这里。

    🔴 2026-10-01：补 `_loose` 去标点，与 `ai_vote.judge_norm` 对齐。
        实测 `tiku.db` 里存的是「正确」、学习库存的是「对」——
        同一答案两种形态；带尾随标点（如「正确。」）时旧实现直接返回 None，
        闸门失效。两处归一函数必须用同一套预处理。
    """
    t = re.sub(r"[\s，,、;；。.：:·|/]+", "", str(s or "")).lower()
    if t in _JUDGE_T:
        return True
    if t in _JUDGE_F:
        return False
    return None


def _pick_alternative(ans: str, wrong: list, q_info: dict) -> str:
    """给出一个"不同于所有历史错误答案"的候选（2026-09-11 硬保证）。

    背景：用户要求"听力题 AI 答错后，下次重新来必须返回另一个答案，答对后再进题库"。
    只靠 prompt 里"请排除上述答案"是软性的 —— 模型经常原样返回已判错的答案，
    导致同一道错题被反复提交同一个错误值（听力题只能做一次，本账号无从纠正）。

    策略：
      - 判断（对/错）：直接取反（形态保持不变，如 正确↔错误）
      - 单字母（单选）：从选项池里挑第一个不在 wrong 中的
      - 连写字母（多选 / 听力多小问）：只改动其中一位，
        生成第一个不在 wrong 中的组合（从首位开始尝试）
      - 自由文本（填空/简答）：无候选池，返回空串（调用方据此放弃本题）
    都试不出来时返回空串（调用方据此放弃写入题库）。
    """
    text = str(ans or "").strip()
    wrong_set = {str(w).strip() for w in (wrong or [])}
    if not text:
        return ""

    text = str(text).strip()
    if not text:
        return ""

    # 🔴 2026-09-27 新增：判断题支持换候选（此前直接落到"非字母形态"分支被放弃）。
    k = judge_key(text)
    if k is not None:
        for _a, _b in (("对", "错"), ("正确", "错误"), ("是", "否"),
                       ("√", "×"), ("true", "false")):
            if text.lower() == _a:
                alt = _b
                break
            if text.lower() == _b:
                alt = _a
                break
        else:
            alt = "错" if k else "对"
        _k2 = judge_key(alt)
        if any(judge_key(w) is _k2 for w in wrong_set):
            return ""          # 反值也被判错过 → 无解
        return alt

    if not re.fullmatch(r"[A-Za-z]+", text):
        return ""          # 填空/自由文本无法安全构造候选
    text = text.upper()
    pool = _option_pool(q_info)
    is_multi = (q_info.get("type") == "multiple")

    if len(text) == 1:
        for c in pool:
            if c not in wrong_set:
                return c
        return ""

    # 连写字母：逐位尝试替换
    # 🔴 2026-09-27 修：多选替换后必须**无重复字母** ——
    #    旧逻辑会造出 "BBD" 这种非法多选答案（实测）。
    for i in range(len(text)):
        for c in pool:
            if c == text[i]:
                continue
            cand = text[:i] + c + text[i + 1:]
            if is_multi and len(set(cand)) != len(cand):
                continue
            if cand not in wrong_set:
                return cand
    return ""


# ── 2026-09-27 新增：填空答案字符规范化 + 选项一致性校验 ────────────────────
#    两个都是真机实测暴露出来的丢分点，详见各自 docstring。

_SUB_SUP = str.maketrans({
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
    "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
})

_FULLWIDTH_ALNUM = {}
for _i in range(10):
    _FULLWIDTH_ALNUM[chr(0xFF10 + _i)] = chr(0x30 + _i)      # ０-９
for _i in range(26):
    _FULLWIDTH_ALNUM[chr(0xFF21 + _i)] = chr(0x41 + _i)      # Ａ-Ｚ
    _FULLWIDTH_ALNUM[chr(0xFF41 + _i)] = chr(0x61 + _i)      # ａ-ｚ


def normalize_fill_answer(ans: str) -> str:
    """填空题答案字符规范化。

    真机实测（2026-09-27 A 卷 / 100 题作业第 62 题）：AI 把化学式写成
    `H₂O` `CO₂`（Unicode 下标数字），平台正确答案是 `H2O` `CO2`（普通数字）。
    平台对填空题做**字符串精确比较** → 界面显示"学生答案与正确答案一模一样"
    却判 0 分（用户截图实证：学生答案 H₂O/CO₂、正确答案 H2O/CO2、得分 0）。

    处理范围**刻意保守**，避免误伤中文答案：
      · Unicode 上下标数字 → 普通数字（H₂O → H2O）
      · 全角字母/数字 → 半角（只动 0-9 A-Z a-z，**不动中文标点**，
        否则"，（U+FF0C）"会被转成半角逗号，反而与平台答案失配）
      · 连续空白收敛 + 去首尾空白
    """
    s = str(ans or "")
    if not s:
        return ""
    s = s.translate(_SUB_SUP)
    s = "".join(_FULLWIDTH_ALNUM.get(ch, ch) for ch in s)
    s = re.sub(r"[ \t\u3000]+", " ", s).strip()
    return s


def _opt_list(options) -> list:
    """把选项拆成"纯内容"列表（去掉 A./B. 前缀与空白），**顺序敏感**。

    兼容 `"A. 21\\nB. 80"` / `["21", "80"]` / `"21|80"` 三种形态。
    """
    if isinstance(options, (list, tuple)):
        items = []
        for x in options:
            if isinstance(x, dict):
                # 🔴 2026-10-01 修：考试链路传的是 [{'letter','value','text'}]
                #    （见 api/exam.py）。直接 `str(dict)` 会把整个字典当成选项
                #    文本，于是与学习库里的纯文本选项**永不相等** ——
                #    `options_compatible` 在考试路径恒判 False，
                #    明明命中的学习库答案被错误否决、改去重新搜（更慢也更差）。
                items.append(str(x.get("text") or x.get("value") or ""))
            else:
                items.append(str(x))
    else:
        items = re.split(r"[\n|]+", str(options or ""))
    out = []
    for it in items:
        t = re.sub(r"^\s*[A-Ha-h]\s*[.、．:：]\s*", "", str(it).strip())
        t = re.sub(r"\s+", "", t)
        if t:
            out.append(t)
    return out


def options_compatible(cur_options, lib_options) -> bool:
    """按题干命中的**字母答案**能否用在当前题目上。

    学习库 / TikuAdapter 的字母，是"那道题在那份试卷里的选项编号"。
    当前题目选项若与之不同（数量、顺序或内容不同），字母必然错位。

    实测两例（2026-09-27 A 卷）：
      · 「下列属于中国四大名著的有？」库里 verified=ABC，答成 ABD
      · 「下列属于 HTTP 请求方法的有？」库里 verified=ABCD，答成 ABC

    返回 True = 兼容可用；False = 明确不兼容（调用方须放弃该答案）。
    任一侧取不到选项（老数据）时返回 True —— 不做过度拦截，保持旧行为。
    """
    cur = _opt_list(cur_options)
    lib = _opt_list(lib_options)
    if len(cur) < 2 or len(lib) < 2:
        return True
    return cur == lib


# ============ 本地题库直查（2026-10-01 新增）============
# 背景：`tiku.db` 是**全体实例共享**的已验证答案库（网关每 10 分钟把各实例学习库
#       回灌进去）。但取用它原来只有一条路 —— 经 8060 的 `TikuAdapter._query`，
#       而那条路按 **hash = md5(题干 + json(sorted(选项)) + 题型 + 平台)** 精确查，
#       **选项文本逐字参与**：标点、全/半角、空格只要有一处不同就 miss，
#       然后降级成模糊匹配 → 命中"另一道题" → 被选项校验拦掉
#       （3 天实测："选项集合与本题不符" 408 次，与成功命中 445 次基本持平）。
#
# 这里改成本地按「归一化题干」精确匹配 —— 完全绕开 hash 的脆弱性。
# 安全边界：**只做完全相等匹配，不做模糊**，所以不存在串味；
#          答案回填后仍走 `query_all` 原有的形态校验 + 选项映射，
#          对不上本题选项的会被自然舍弃（不是盲信）。

_LOCAL_TIKU = {"sig": None, "index": {}}
_LOCAL_TIKU_LOCK = threading.Lock()
_LOCAL_TIKU_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tikuAdapter", "tiku.db")


def _tiku_answer_text(raw) -> str:
    """tiku.db 的 answer 是 JSON 数组（['对'] / ['选项一','选项二']），转成文本答案。

    多选多项之间用换行连接 —— 与 TikuAdapter 返回 `bestAnswer` 的拼法一致，
    后续 `map_multiple_answer` 才能按选项内容映射回字母。
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if s.startswith("["):
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                return "\n".join(str(x).strip() for x in arr if str(x).strip())
        except Exception:
            pass
    return s


def _load_local_tiku() -> dict:
    """载入 tiku.db 的 {归一化题干: 答案文本} 索引，按 mtime 自动失效。

    网关每 10 分钟回灌一次，所以不能只加载一次；用 (mtime, size) 做签名即可。
    全库约 5.9k 行 / 3 MB，常驻内存最省事，查询退化成一次字典查找。
    读取失败时返回上一次的索引（而不是清空）—— 宁可稍旧也别把整条捷径关掉。
    """
    try:
        sig = (os.path.getmtime(_LOCAL_TIKU_PATH),
               os.path.getsize(_LOCAL_TIKU_PATH))
    except OSError:
        return _LOCAL_TIKU.get("index") or {}
    with _LOCAL_TIKU_LOCK:
        if _LOCAL_TIKU.get("sig") == sig:
            return _LOCAL_TIKU["index"]
        index = {}
        try:
            import sqlite3
            # 只读打开：避免和网关的回灌写操作抢锁
            con = sqlite3.connect(f"file:{_LOCAL_TIKU_PATH}?mode=ro",
                                  uri=True, timeout=5)
            try:
                for q, ans in con.execute("SELECT question, answer FROM tiku"):
                    key = normalize_title(q)
                    if not key or key in index:
                        continue
                    txt = _tiku_answer_text(ans)
                    if txt:
                        index[key] = txt
            finally:
                con.close()
        except Exception as e:                                     # noqa: BLE001
            logger.debug(f"载入本地题库失败（不影响其余题库）：{e}")
            return _LOCAL_TIKU.get("index") or {}
        _LOCAL_TIKU.update({"sig": sig, "index": index})
        logger.info(f"本地题库已载入 {len(index)} 条（跨实例共享，供 hash 未命中时兜底）")
        return index


def local_tiku_lookup(title: str) -> str:
    """按归一化题干直查本地题库，命中返回答案文本，否则空串。"""
    key = normalize_title(title)
    if not key:
        return ""
    return _load_local_tiku().get(key) or ""


def local_tiku_answer(title: str, options: str = "", qtype: str = None) -> str:
    """本地题库直查 + **已证伪否决**：命中但该值曾被平台判错 → 视为未命中。

    为什么必须补这道闸：
      `sync_learned_to_tiku` 回灌进 tiku.db 的记录**只插不更新**
      （`if q in exist2: return False`，没有 UPDATE/DELETE），
      而学习库在同一时刻会撤销自己的 `verified`。
      ⇒ 一个答案被判错之后：**学习库撤了、tiku.db 里那条还在**。
      不收口的话，本地直查会把刚被判错的值原样捞回来再交一次。

    与 `Tiku.query` 收口处的"已证伪答案拦截"同口径，只是更早生效（省一次网络）。

    🔴 2026-10-01：这一道闸**取代**了原先写在调用点的
    `if not work_feedback:` 判断 —— 无论是否重做模式都该否决"已被证伪的值"，
    而重做模式不该否决"仍然有效的老答案"（那才是整卷跳 AI 的病根）。

    ⚠️ **形态必须对齐**（这里踩过一次）：`wrong` 里存的是"我的答案"——
    单选/多选是**字母连写**（如 `ABD`），而 tiku.db 里存的是**选项原文**
    （如 `《史记》\\n《汉书》`）。直接拿原文比字母会**永远不命中**，闸门形同虚设。
    所以先试着把原文映射成字母，原文与字母两种形态都拿去比。
    """
    ans = local_tiku_lookup(title)
    if not ans:
        return ""
    try:
        _wrong = LearnedAnswers().get_wrong(title)
    except Exception as e:                                         # noqa: BLE001
        logger.debug(f"读取错题列表失败（本地库仍放行）：{e}")
        return ans
    if not _wrong:
        return ans
    probes = {ans.strip()}
    if qtype in ("single", "multiple") and options:
        try:
            from api.base import map_single_answer, map_multiple_answer
            _m = (map_multiple_answer(ans, options) if qtype == "multiple"
                  else map_single_answer(ans, options))
            if _m:
                probes.add(str(_m).strip())
        except Exception as e:                                     # noqa: BLE001
            logger.debug(f"本地库答案映射字母失败（只按原文比对）：{e}")
    if any(p and p in _wrong for p in probes):
        logger.info(f"本地题库答案曾被平台判错，跳过本题：{title[:40]} -> {ans[:40]}")
        return ""
    # 判断题再走一遍**等价形态**比对：`tiku.db` 存「正确」而学习库 `wrong` 存
    # 「对」，字符串比对必然不等（实测 5 条）。与 `Tiku.query` 收口处同口径。
    _k = judge_key(ans)
    if _k is not None and any(judge_key(w) is _k for w in _wrong):
        logger.info(f"本地题库答案（判断题等价形态）曾被平台判错，跳过本题："
                    f"{title[:40]} -> {ans[:40]}")
        return ""
    return ans


def local_tiku_round(q_list: list, results: list, pending_indices: list) -> list:
    """对仍未命中的题做一轮本地题库直查，命中即写入 results；返回仍未命中的下标。

    为什么**不**放在最前面（和学习库/cache 同级）：
      本地库里的答案是我们**历史提交并被平台判对**的那一份，而 8060 会聚合
      多个题库源、给出的答案往往更完整。实测同一道多选题：
          tiku.db     -> ["艺术的参与"]                    （只有 1 项）
          8060 聚合   -> ["艺术的参与", "不干扰…欣赏"]      （完整，answerKey=BC）
      所以顺序定为 **TikuAdapter → 本地直查 → AI**：
      先用最完整的，只有 8060 漏掉（hash 对选项文本敏感）时才用本地库兜底，
      最后才落 AI。
    """
    rest = []
    for idx in pending_indices:
        try:
            # ⚠️ 必须把 options / 题型一起传进去：wrong 存的是字母、
            #    本地库存的是选项原文，不映射成同一形态就比对不上（坑过一次）。
            _q = q_list[idx]
            ans = local_tiku_answer(_q.get("title") or "",
                                    _q.get("options") or "",
                                    _q.get("type"))
        except Exception as e:                                     # noqa: BLE001
            # 本地库只是"兜底加速"：任何异常都不能影响整批作答 ——
            # 这里吞掉并当作未命中，交给后面的 provider 链继续。
            logger.debug(f"本地题库直查失败（跳过本题）：{e}")
            ans = ""
        if ans:
            logger.info(f"从本地题库获取答案：{q_list[idx]['title']} -> {ans}")
            results[idx] = ans
        else:
            rest.append(idx)
    return rest


class Tiku(ABC):
    # 2026-09-15 修隐患：原来用 os.getcwd() 定位 config.ini，而这是**类属性、
    # 在模块导入时求值** —— 一旦进程从项目根以外的地方启动（多实例管理器、
    # 计划任务、双击 .bat 时 cwd 不同），CONFIG_PATH 就会指向错误的目录。
    # 后果不只是读不到配置：_get_conf() 捕获 KeyError 后会把 self.DISABLE
    # 置为 True，**整个题库功能被静默停用**，表现为"题库莫名不工作"。
    # 改为以源码根为基准（与 api/config.py:data_dir()、api/logger.py 一致）。
    # cwd 恰好在项目根时两种写法结果相同，故对现有启动方式零影响。
    CONFIG_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.ini")
    DISABLE = False  # 停用标志
    SUBMIT = False  # 提交标志
    COVER_RATE = 0.8  # 覆盖率
    # 🔴 2026-09-30 用户要求（原话）：
    #   「这种随机的极少数情况就可以直接提交，随机答案也可以填上去。
    #     但是为了避免大范围出现这种情况，还是要控制一下。」
    #
    # 语义：**随机兜底题目占全卷的比例上限**。
    #   · ≤ 该值 → 允许带随机答案提交（交比不交强：不交 = 平台显示"未完成"，
    #     用户还得手点进去自己蒙一次）
    #   · >  该值 → 仍按老规矩只暂存，避免"整卷靠蒙"交出去
    # 2026-09-30 用户定的阈值（原话）：
    #   「50 都高了，应该要在随机率低于百分之四十」
    #   —— 即随机兜底占比 **< 40%** 才允许带随机答案提交；≥ 40% 只暂存。
    # 用途见 base.py 的两道覆盖率闸门（章节测验 study_work / 作业 study_work_homework）。
    GUESS_MAX_RATIO = 0.4
    # 🔴 2026-09-26：默认**禁止**在"有题没拿到答案"时硬交。
    # 用户原话：「不会让这个类似的事情发生」。默认 False = 只要有题没答案
    # 就存草稿、不交卷（见 base.py study_work_homework 的拦截分支）。
    # 只有显式置 True 才允许带着蒙的答案交卷 —— 那是用户主动选择的冒险。
    FORCE_SUBMIT = False
    true_list = None
    false_list = None

    def __init__(self, config_path: Optional[str] = None) -> None:
        """
        初始化题库基类。

        Args:
            config_path: 配置文件路径，若为 None 则使用默认的 CONFIG_PATH。
        """
        self._name = None
        self._api = None
        self._conf = None
        self._config_path = config_path or self.CONFIG_PATH
        self.true_list = []
        self.false_list = []

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    @property
    def api(self):
        return self._api

    @api.setter
    def api(self, value):
        self._api = value

    @property
    def token(self):
        return self._token

    @token.setter
    def token(self, value):
        self._token = value

    def init_tiku(self):
        # 仅用于题库初始化, 应该在题库载入后作初始化调用, 随后才可以使用题库
        # 尝试根据配置文件设置提交模式
        if not self._conf:
            self.config_set(self._get_conf())
        if not self.DISABLE:
            # 设置提交模式
            self.SUBMIT = True if self._conf['submit'] == 'true' else False
            self.COVER_RATE = float(self._conf['cover_rate'])
            # config.ini 未配 guess_ratio_max 时保持类默认 0.5（不强制要求配置）
            try:
                self.GUESS_MAX_RATIO = float(self._conf['guess_ratio_max'])
            except Exception:
                pass
            self.true_list = self._conf['true_list'].split(',')
            self.false_list = self._conf['false_list'].split(',')
            # 调用自定义题库初始化
            self._init_tiku()

    def _init_tiku(self):
        # 仅用于题库初始化, 例如配置token, 交由自定义题库完成
        pass

    def config_set(self, config):
        self._conf = config

    def _get_conf(self):
        """
        从默认配置文件查询配置, 如果未能查到, 停用题库
        """
        try:
            config = configparser.ConfigParser()
            config.read(self._config_path, encoding="utf8")
            return config['tiku']
        except (KeyError, FileNotFoundError):
            logger.info("未找到tiku配置, 已忽略题库功能")
            self.DISABLE = True
            return None

    @property
    def _is_manual_mode(self) -> bool:
        return (
                getattr(self, 'is_manual', False) or
                self.__class__.__name__ == 'TikuManual' or
                (self.__class__.__name__ == 'TikuFallback' and any(
                    getattr(p, 'is_manual', False) or p.__class__.__name__ == 'TikuManual' for p in
                    getattr(self, 'providers', [])))
        )

    def query(self, q_info: dict) -> Optional[str]:
        if self.DISABLE:
            return None

        # 预处理, 去除【单选题】这样与标题无关的字段
        if not self._is_manual_mode:
            logger.debug(f"原始标题：{q_info['title']}")
        # 与题库 / 学习库共用同一套键归一（api/learned.py:normalize_title）。
        # 2026-09-14 修 bug：「题库命中 0」的根因在此 —— 原先只做「去首数字 +
        # 去尾分值」两步，而题库回灌时用的键是归一后的完整题干（含【单选题】
        # 这类题型标记）。两边规则不一致 → hash 永久对不上 → 全部落空。
        q_info['title'] = normalize_title(q_info['title'])
        q_info['title'] = sub(r'^\d+', '', q_info['title'])
        q_info['title'] = sub(r'（\d+\.\d+分）$', '', q_info['title'])
        if not self._is_manual_mode:
            logger.debug(f"处理后标题：{q_info['title']}")

        # 2026-09-14 【重要】学习库优先，与 query_all() 完全对齐。
        # 为什么必须加：作业（api/base.py:2420）走的是单题 query()，
        # 而 query() 此前**只查缓存、不查学习库** —— 于是同一道题哪怕
        # 学习库里已存着平台判对的 verified 答案（跨会话/跨账号沉淀），
        # 作业里再遇到时仍会重新走 TikuAdapter/AI，白白丢分。
        # 章节测验走 query_all() 有这层优先级，作业没有 → 两条路径行为
        # 不一致，属于明确缺陷。这里补齐，让"先纯本地、最后 AI"在
        # 单题路径上也成立。
        if not self._is_manual_mode:
            try:
                _rec = LearnedAnswers().get_verified_record(q_info['title'])
            except Exception as e:
                logger.warning(f"读取学习库失败（不影响作答）：{e}")
                _rec = {}
            _verified = (_rec or {}).get("answer") or ""
            # 2026-09-15 补：与 query_all() 对齐，学习库值也要过形态校验。
            # 此前 query() 直接返回 verified，若学习库里存的是脏值
            # （旧版程序写入的拒答文本、或题型变更导致形态不符），
            # 会被原样提交上去。query_all() 走的是"缓存层校验"，
            # 单题路径缺了这道闸。
            if _verified:
                _ok = plausible_answer(_verified, q_info.get('type'))
                # 🔴 2026-09-27 新增：**选项一致性闸门**。
                # 学习库按"题干"归一化匹配，不看选项 —— 同一题干在不同试卷里
                # 选项数/顺序不同时，库里的旧字母必然错位。实测 A 卷：
                # 「四大名著」库 verified=ABC 却答成 ABD；「HTTP 方法」库
                # verified=ABCD 却答成 ABC。两道都是这么错的。
                if _ok and not options_compatible(q_info.get('options'),
                                                  (_rec or {}).get("options")):
                    logger.warning(
                        f"学习库答案与本题选项不一致（按题干命中会字母错位），"
                        f"视为未命中改走重新查询：{q_info['title']} -> {_verified}")
                    _ok = False
                if _ok or getattr(self, 'is_manual', False):
                    logger.info(f"从学习库获取答案（已验证）：{q_info['title']} -> {_verified}")
                    return _verified
                logger.warning(
                    f"学习库答案形态与题型不符，视为未命中重新查询："
                    f"{q_info['title']} -> {_verified}")

        # 🔴 2026-09-26 修 bug：把"取到答案后的收口"抽成局部函数。
        # 原代码里 `answer = self._query(q_info)` 出现在两处（438 行「缓存被判错后
        # 重查」、443 行「缓存未命中」），**两处都没有 return**，控制流直接掉到
        # 函数末尾的 `return None` → AI/题库明明给出了答案却被丢掉。
        # 线上症状：日志里「AI大模型答题 命中答案」紧接着「题目 xxx 未能获取答案」
        # → 填空题被判空答案、判断题随机蒙 → 提交出去的是垃圾。
        def _accept(ans):
            """非空即返回（本类 skip_answer_validation=True，不再做形态校验）。

            🔴 2026-09-27 修：单选/多选补「选项内容 → 选项字母」映射。
            AI 提示词按设计要求返回**选项的具体内容**（内容比字母可靠，避免
            长选项时字母错位），但平台单选/多选只认字母 —— query_all() 有
            map_single/multiple_answer 兜底，本单题路径（作业走这条）此前
            没有 → 内容形态答案被原样提交 → 客观题全错。此处与 query_all 对齐。
            """
            if ans is None:
                return None
            ans = str(ans).strip()
            if not ans:
                return None
            _qtype = q_info.get('type')
            if (_qtype in ('single', 'multiple')
                    and not getattr(self, 'is_manual', False)):
                # 已是字母形态直接放行（single=单字母，multiple=连写/分隔字母串）
                _letter_form = (re.fullmatch(r'[A-Za-z]', ans) if _qtype == 'single'
                                else re.fullmatch(r'[A-Za-z]{1,8}', ans))
                if not _letter_form:
                    try:
                        # 延迟导入，避免循环依赖（同 query_all）
                        from api.base import map_single_answer, map_multiple_answer
                        mapped = (map_multiple_answer(ans, q_info.get('options') or '')
                                  if _qtype == 'multiple'
                                  else map_single_answer(ans, q_info.get('options') or '')) or ''
                    except Exception as e:
                        logger.debug(f"选项内容映射字母失败: {e}")
                        mapped = ''
                    if mapped:
                        logger.info(f"答案按选项内容映射为选项字母：{ans[:60]} -> {mapped}")
                        ans = mapped
                    else:
                        # 映射不出来的必是拒答文本/垃圾（如"我无法访问音频…"），
                        # 提交上去必判错还会污染真实答案率 → 与 query_all 一致舍弃。
                        logger.warning(f"答案无法映射为选项字母，已舍弃："
                                       f"{q_info['title'][:50]} -> {ans[:60]}")
                        return None
            if _qtype not in ('single', 'multiple'):
                # 🔴 2026-09-27 新增：填空/文本答案做字符规范化。
                # 真机实证（100 题作业第 62 题）：AI 答 `H₂O` `CO₂`（Unicode 下标），
                # 平台正确答案是 `H2O` `CO2` → 平台精确比较判 0 分，
                # 界面上"学生答案"和"正确答案"看起来一模一样（用户截图）。
                _n = normalize_fill_answer(ans)
                if _n and _n != ans:
                    logger.info(f"填空/文本答案字符规范化：{ans[:40]} -> {_n[:40]}")
                if _n:
                    ans = _n
            # 🔴 2026-09-27 新增：**已证伪答案拦截**（对所有来源生效）。
            # 背景（用户追问"题库命中但平台判错会怎么处理"查出来的真隐患）：
            #   · 学习库判错时会**撤销 verified**（learned.py:270 `rec.pop("verified")`），
            #     这条链路是通的；
            #   · 但回灌进 **tikuAdapter/tiku.db 的记录只插不更新**
            #     （sync_learned_to_tiku.py:478 `if q in exist2: return False`），
            #     没有 UPDATE / DELETE → 曾被判错的答案会**永久留在外部题库**里。
            # 结果：学习库撤销了、外部题库还留着 → 下次命中 → 又提交同一个错答案。
            # 这里在**统一收口**处补一道闸：答案落在 wrong 列表 → 换候选；
            # 换不出 → 放弃本题（宁空不污）。与用户 2026-09-11 提的
            # "第二次还返回相同答案就驳回"要求一致，且覆盖 AI/题库/学习库全来源。
            try:
                _wrong = LearnedAnswers().get_wrong(q_info['title'])
            except Exception as _e:                           # noqa: BLE001
                logger.debug(f"读取错题列表失败（忽略，不影响作答）：{_e}")
                _wrong = []
            if _wrong and not getattr(self, 'is_manual', False):
                _hit = ans in _wrong
                if not _hit:
                    # 判断题走等价比对：平台展示用「正确/错误」，
                    # 而库里 wrong 常存「对/错」→ 直接字符串比对会漏判。
                    _k = judge_key(ans)
                    if _k is not None:
                        _hit = any(judge_key(w) is _k for w in _wrong)
                if _hit:
                    _alt = _pick_alternative(ans, _wrong, q_info)
                    if _alt:
                        logger.warning(
                            f"答案曾被平台判错（{_wrong[:3]}），强制改用另一个候选："
                            f"{q_info['title'][:40]} {ans} -> {_alt}")
                        ans = _alt
                    else:
                        logger.warning(
                            f"答案曾被平台判错且无可替换候选，放弃本题（宁空不污）："
                            f"{q_info['title'][:40]} -> {ans}")
                        return None
            logger.info(f"从{self.name}获取答案：{q_info['title']} -> {ans}")
            if getattr(self, 'is_manual', False):
                CacheDAO().add_cache(q_info['title'], ans)
            return ans

        # 先过缓存。
        # 🔴 2026-10-01 改：原先这里是 `if not work_feedback: <走缓存> else: <直接问题库>`
        #    —— "重做模式跳过缓存"。**已删除该分支**，理由与 `query_all` 完全一致：
        #    · `work_feedback` 是**整卷**错题列表，拿它当开关会让每一道题都放弃缓存；
        #    · 真正要防的（缓存值恰是那次答错的答案）由下面的 `wrong` **按题**否决覆盖；
        #    · 且原 `else` 分支只是 `self._query()` 裸返回，**缺**字母映射 ——
        #      AI 按提示词返回"选项内容"时单选/多选不会被转成字母（比缓存路径更差）。
        cache_dao = CacheDAO()
        answer = cache_dao.get_cache(q_info['title'])
        # 2026-09-15 补：缓存形态校验（同 query_all() 的 plausible_answer 闸门），
        # 拦住历史写入的拒答文本/格式垃圾，避免脏缓存被直接交上去。
        if answer and not (plausible_answer(answer, q_info.get('type'))
                           or getattr(self, 'is_manual', False)):
            logger.warning(f"缓存答案形态非法，视为未命中重新查询："
                           f"{q_info['title']} -> {answer}")
            answer = None
        if answer:
            # 缓存值若曾被平台判错，不可信 —— 与 query_all 一致，改走
            # 重新分析（AI 会注入负反馈排除该错误答案）。
            try:
                _wrong = LearnedAnswers().get_wrong(q_info['title'])
            except Exception:
                _wrong = []
            if _wrong and answer.strip() in _wrong and not self._is_manual_mode:
                logger.info(f"缓存答案曾被平台判错，改走重新分析："
                            f"{q_info['title']} -> {answer}")
                # 🔴 2026-09-26 修：这里原先只赋值不返回，答案被
                # 函数末尾的 return None 吃掉。走统一收口。
                return _accept(self._query(q_info))
            logger.info(f"从缓存中获取答案：{q_info['title']} -> {answer}")
            return answer.strip()
        # 🔴 2026-09-26 修：缓存未命中 → 问题库。原先缺 return，
        # 导致**所有非重做模式的题目一律拿不到答案**（线上事故根因）。
        return _accept(self._query(q_info))

    def query_all(self, q_list: list[dict], query_delay: float = 0.0) -> list[Optional[str]]:
        if self.DISABLE:
            return [None] * len(q_list)

        results = [None] * len(q_list)
        pending_indices = []
        wrong_map = {}          # idx -> 该题历史被判错的答案列表（供强制换答案用）
        sub_map = {}            # idx -> 该题已确认正确的小问答案 {序号: 答案}

        cache_dao = CacheDAO()
        learner = LearnedAnswers()
        # 🔴 2026-10-01 改：**删掉 `skip_cache = bool(work_feedback)` 的全局跳过**。
        #
        #    原设计意图（何同学 2026-10-01 澄清）是对的：答错过一次后，把"错误答案"
        #    喂给 AI 参考，让**这道题**下次答对（多选题尤其明显）。
        #    但 `work_feedback` 是**整份试卷**的错题列表，而 skip_cache 一开就是
        #    **整卷所有题**跳过 cache + 本地题库 —— 连上次答对的题也被拖去问 AI，
        #    等于"一道题答错，全卷放弃已有答案"，代价与收益完全不成比例。
        #
        #    而且这层**本来就是冗余的**：判错时 `learned.record(..., "cuo"/"bandui")`
        #    已把该错误答案写进 `wrong` 列表，下面的 cache 分支会**按题**否决
        #    （`answer.strip() in wrong` → 走 AI），粒度精确到单题。
        #    ⇒ 需要防的场景已被覆盖，多余的全局开关只会造成退化，故删除。
        #    `work_feedback` 本身仍保留 —— 它给 AI 的 prompt 注入错题线索，是有效功能。
        for idx, q in enumerate(q_list):
            if not self._is_manual_mode:
                logger.debug(f"原始标题：{q['title']}")
            q['title'] = sub(r'^\d+', '', q['title'])
            q['title'] = sub(r'（\d+\.\d+分）$', '', q['title'])
            # 与学习库 / 题库共用同一键规范化（去 img 标签、空白归一），
            # 否则同一道题会分裂成多个键，负反馈与缓存互相命中不到。
            q['title'] = normalize_title(q['title'])
            if not self._is_manual_mode:
                logger.debug(f"处理后标题：{q['title']}")

            # 学习库优先（最高优先级）：该题曾被平台判定答对的答案。
            # 来源：提交后详情页解析（marking_dui）——跨会话/跨账号沉淀。
            # 手动模式（用户自己给答案）除外：不能覆盖用户的输入。
            # 🔴 2026-10-01 修：与 `query()`（单题路径）对齐，补上两道闸 ——
            #    · `plausible_answer`：形态校验（挡脏值/拒答文本）
            #    · `options_compatible`：**选项一致性**。学习库只按题干匹配，
            #      同一题干在不同试卷里选项数量/顺序不同时，库里存的旧字母必然错位
            #      （实测 A 卷：「四大名著」库 verified=ABC 却答成 ABD）。
            #    此前这两道闸只存在于单题路径，章节测验直接按旧字母提交 →
            #    系统性错判，属明确缺陷。
            _lrec = {}
            verified = None
            if not self._is_manual_mode:
                try:
                    _lrec = learner.get_verified_record(q['title']) or {}
                except Exception as e:                             # noqa: BLE001
                    logger.warning(f"读取学习库失败（不影响作答）：{e}")
                    _lrec = {}
                verified = (_lrec.get("answer") or "").strip() or None
            if verified:
                _lok = plausible_answer(verified, q.get('type'))
                if _lok and not options_compatible(q.get('options'),
                                                   _lrec.get("options")):
                    logger.warning(
                        f"学习库答案与本题选项不一致（按题干命中会字母错位），"
                        f"视为未命中改走重新查询：{q['title']} -> {verified}")
                    _lok = False
                if _lok:
                    logger.info(f"从学习库获取答案（已验证）：{q['title']} -> {verified}")
                    results[idx] = verified
                    continue
                logger.warning(
                    f"学习库答案形态与题型不符，视为未命中重新查询："
                    f"{q['title']} -> {verified}")

            # 小问答案已集齐 -> 直接组装提交，不必再问 AI（2026-09-11）。
            # 场景：听力题整组从没一次性全对，但多轮之后每个小问都被平台
            # 分别确认过了 —— 等价于"已全对"，无需再猜。
            if not self._is_manual_mode:
                _n_sub = _sub_count(q)
                if _n_sub > 1:
                    _sv_all = learner.get_sub_verified(q['title'])
                    if _sv_all and len(_sv_all) >= _n_sub:
                        _full_ans = _assemble_subs(_sv_all, _n_sub)
                        if _full_ans:
                            logger.info(
                                f"小问答案已集齐（{_n_sub}/{_n_sub}），直接组装提交："
                                f"{_full_ans} | {q['title'][:40]}")
                            results[idx] = _full_ans
                            continue

            # 该题历史被判错的答案（用于负反馈 + 否决被否的缓存值 + 强制换答案）
            wrong = learner.get_wrong(q['title'])
            if wrong:
                wrong_map[idx] = wrong
            # 该题组里"平台已确认正确"的小问答案（单独蒙对的小问也保留）
            if not self._is_manual_mode:
                _sv = learner.get_sub_verified(q['title'])
                if _sv:
                    sub_map[idx] = _sv

            answer = cache_dao.get_cache(q['title'])
            if answer and plausible_answer(answer, q.get('type')):
                if wrong and answer.strip() in wrong:
                    # 缓存值 = 历史被平台判错的答案：不可信，走 AI 重新分析
                    # （AI 查询会注入负反馈，排除该错误答案）
                    logger.info(f"缓存答案曾被平台判错，改走重新分析：{q['title']} -> {answer}")
                    pending_indices.append(idx)
                else:
                    logger.info(f"从缓存中获取答案：{q['title']} -> {answer}")
                    results[idx] = answer.strip()
            else:
                if answer:
                    # 缓存里有但形态非法（历史拒答文本/格式垃圾）：视为未命中，
                    # 走正常查询链路，新答案会覆盖这条脏缓存
                    logger.warning(f"缓存答案形态非法，视为未命中重新查询：{q['title']} -> {answer}")
                pending_indices.append(idx)

        if not pending_indices:
            return results

        sub_q_list = [q_list[idx] for idx in pending_indices]
        sub_results = self._query_all(sub_q_list, query_delay=query_delay)

        if not isinstance(sub_results, list):
            logger.error(f"{self.name} _query_all 返回结果格式异常，期望列表")
            sub_results = [None] * len(pending_indices)
        elif len(sub_results) != len(pending_indices):
            logger.error(
                f"{self.name} _query_all 返回结果长度不匹配，期望 {len(pending_indices)}，实际 {len(sub_results)}")
            # 补齐或截断 sub_results 防止错位
            sub_results = list(sub_results) + [None] * (len(pending_indices) - len(sub_results))
            sub_results = sub_results[:len(pending_indices)]

        for idx, ans in zip(pending_indices, sub_results):
            q_info = q_list[idx]
            if ans:
                ans = ans.strip()
                logger.info(f"从{self.name}获取答案：{q_info['title']} -> {ans}")

                # 硬保证（2026-09-11）：AI 若返回历史被平台判错过的答案，强制换成
                # 另一个候选。此前只靠 prompt 里"请排除上述答案"软性提示，模型
                # 经常照旧给出同一个值 → 同一道错题反复提交同一个错误答案
                # （听力题只能做一次，本账号无从纠正，只能等下次/换账号）。
                _wrong_here = wrong_map.get(idx) or []
                _subs_here = sub_map.get(idx) or {}
                if (_subs_here or _wrong_here) and not self._is_manual_mode:
                    if _subs_here:
                        # 优先用已确认正确的小问答案校正（比整体换答案更精准：
                        # 保住了蒙对的小问，只让未确认的小问换值）
                        _corrected = _apply_verified_subs(ans, _subs_here, _wrong_here)
                        if _corrected != ans:
                            logger.info(
                                f"按已确认的小问答案校正：{ans} -> {_corrected}"
                                f"（已确认 {len(_subs_here)} 个小问）")
                            ans = _corrected
                    elif ans in _wrong_here:
                        _alt = _pick_alternative(ans, _wrong_here, q_info)
                        if _alt:
                            logger.warning(
                                f"AI 返回历史错误答案 {ans}（该题曾错过：{_wrong_here}），"
                                f"强制改用另一个候选：{_alt}")
                            ans = _alt
                        else:
                            logger.error(
                                f"AI 返回历史错误答案 {ans} 且无其他候选可换，"
                                f"本次不写入题库（避免污染）：{q_info.get('title', '')[:50]}")
                            continue

                # 双重把关：check_answer 之外再用 plausible_answer 拦拒答整句——
                # skip_answer_validation 的包装器（如 TikuFallback）会跳过
                # check_answer，没有这道闸垃圾答案就会污染缓存（实测发生过）
                #   手动模式（is_manual）除外：答案由用户自己给，不做形态校验，
                #   否则用户输入的非字母形态答案会被误杀（与 skip_answer_validation
                #   只作用于 check_answer 不同，plausible_answer 原本无条件校验）。
                if check_answer(ans, q_info['type'], self) and (
                        plausible_answer(ans, q_info['type'])
                        or getattr(self, 'is_manual', False)):
                    # 关键（2026-09-11 用户反馈修复）：**不再把未验证的答案写进题库**。
                    # AI 与搜题服务给出的答案都没经过平台判定，直接写 cache.json 会把
                    # "猜测值"变成"标准答案"——用户实测 3.x 听力题题库里存的就是这些
                    # 猜测值，且下次会直接命中、连 AI 都不再重新分析。
                    # 现在只有两条路能写题库：
                    #   1) base._harvest_work_page_result —— 平台判定正确后沉淀
                    #   2) recycle_answers.py —— 回收已判对的历史题
                    # 手动模式例外：答案由用户自己给出，保留缓存意图。
                    if getattr(self, 'is_manual', False):
                        cache_dao.add_cache(q_info['title'], ans)
                    results[idx] = ans
                    continue
                # 映射兜底（2026-09-11 实测）：本项目 AI 提示词按设计要求
                # "输出选项的具体内容，而不是内容前的 ABCD"（见 AITiku 的
                # system_prompt），选项内容为整句时（长篇阅读/段落匹配题）
                # 会被 plausible_answer 以"非纯字母"拒 → 答案丢失 → 随机作答
                # （1.7/1.8 整章测验覆盖率 0% 的根因）。
                # 此处把选项内容映射回选项字母后接受（缓存存字母形态）。
                # 拒答文本（如"我无法访问音频文件…"）映射不出字母，仍会被拒。
                mapped = ""
                if q_info.get("type") in ("single", "multiple") and isinstance(ans, str):
                    try:
                        # 延迟导入，避免循环依赖
                        from api.base import map_single_answer, map_multiple_answer
                        if q_info.get("type") == "multiple":
                            mapped = map_multiple_answer(
                                ans, q_info.get("options", "")) or ""
                        else:
                            mapped = map_single_answer(
                                ans, q_info.get("options", "")) or ""
                    except Exception as e:
                        logger.debug(f"选项内容映射字母失败: {e}")
                if mapped:
                    logger.info(f"答案按选项内容映射为选项字母：{ans[:60]} -> {mapped}")
                    # 小问校正补做（2026-09-11 审查发现）：上面的校正在"选项内容"形态
                    # 上做不了（非纯字母），直到这里才映射成字母，必须再校正一次，
                    # 否则已确认的小问答案在"内容形态答案"的场景里完全失效。
                    _subs_map = sub_map.get(idx) or {}
                    if _subs_map and not self._is_manual_mode:
                        _fixed = _apply_verified_subs(
                            mapped, _subs_map, wrong_map.get(idx) or [], q_info)
                        if _fixed != mapped:
                            logger.info(f"映射后按已确认小问校正：{mapped} -> {_fixed}")
                            mapped = _fixed
                    # 同上：未经验证的答案不入题库，仅用于本次提交
                    if getattr(self, 'is_manual', False):
                        cache_dao.add_cache(q_info['title'], mapped)
                    results[idx] = mapped
                else:
                    logger.info(f"从{self.name}获取到的答案类型与题目类型不符，已舍弃")
            else:
                logger.error(f"从{self.name}获取答案失败：{q_info['title']}")

        return results

    @abstractmethod
    def _query(self, q_info: dict) -> Optional[str]:
        """
        查询接口, 交由自定义题库实现
        """
        pass


    def set_work_feedback(self, feedback) -> None:
        """
        设置上一轮章节检测的错误反馈，供支持反馈的大模型题库在重新作答时参考。

        Args:
            feedback: 错误反馈，可以是 str 或 list[str]（描述哪些题目答错、正确答案是什么）
        """
        pass

    def _query_all(self, q_list: list[dict], query_delay: float = 0.0) -> list[Optional[str]]:
        """
        批量查询的实现接口，默认循环调用单个查询 _query。
        子类若有批量查询或交互需求（如手动模式），可重写此方法。
        """
        results = []
        for q in q_list:
            if query_delay > 0:
                time.sleep(query_delay)
            try:
                results.append(self._query(q))
            except Exception as e:
                logger.error(f"{self.name} 查询单个题目发生异常: {e}")
                results.append(None)
        return results

    @staticmethod
    def get_tiku_from_config(config: Optional[dict] = None, config_path: Optional[str] = None):
        """
        从配置文件加载题库, 这个配置可以是用户提供, 可以是默认配置文件
        """
        conf = config
        path = config_path or Tiku.CONFIG_PATH
        if not conf:
            # 尝试从默认配置文件加载
            try:
                config_parser = configparser.ConfigParser()
                config_parser.read(path, encoding="utf8")
                conf = config_parser['tiku']
            except (KeyError, FileNotFoundError):
                logger.error("未找到题库配置, 已忽略题库功能")
                dummy = DummyTiku(config_path=path)
                return dummy

        try:
            cls_name = conf['provider']
            if not cls_name:
                raise KeyError
        except KeyError:
            logger.error("未找到题库配置, 已忽略题库功能")
            dummy = DummyTiku(config_path=path)
            return dummy

        providers = [name.strip() for name in cls_name.split(',') if name.strip()]
        if not providers:
            logger.error("题库provider配置为空, 已忽略题库功能")
            dummy = DummyTiku(config_path=path)
            return dummy

        invalid_providers = [name for name in providers if name not in PROVIDER_REGISTRY]
        if invalid_providers:
            logger.error(f"题库provider配置无效: {', '.join(invalid_providers)}")
            dummy = DummyTiku(config_path=path)
            return dummy

        if len(providers) == 1:
            provider_cls = PROVIDER_REGISTRY[providers[0]]
            if not isinstance(provider_cls, type) or not issubclass(provider_cls, Tiku):
                logger.error(f"题库provider配置无效: {providers[0]}")
                dummy = DummyTiku(config_path=path)
                return dummy
            new_cls = provider_cls(config_path=path)
            new_cls.config_set(conf)
            return new_cls

        chain_providers = []
        for provider_name in providers:
            provider_cls = PROVIDER_REGISTRY[provider_name]
            if not isinstance(provider_cls, type) or not issubclass(provider_cls, Tiku):
                logger.error(f"题库provider配置无效: {provider_name}")
                dummy = DummyTiku(config_path=path)
                return dummy
            provider = provider_cls(config_path=path)
            provider.config_set(conf)
            chain_providers.append(provider)
        fallback = TikuFallback(chain_providers, config_path=path)
        fallback.config_set(conf)
        return fallback

    def judgement_select(self, answer: str) -> bool:
        """
        这是一个专用的方法, 要求配置维护两个选项列表, 一份用于正确选项, 一份用于错误选项, 以应对题库对判断题答案响应的各种可能的情况
        它的作用是将获取到的答案answer与可能的选项列对比并返回对应的布尔值
        """
        if self.DISABLE:
            return False
        # 对响应的答案作处理
        answer = answer.strip().lower()

        # 内置的高频通用判断词规整
        if answer in ['true', 't', '1', '对', '正确', '√', '是', 'yes', 'y']:
            return True
        if answer in ['false', 'f', '0', '错', '错误', '×', '否', 'no', 'n', '不对', '不正确']:
            return False

        # 兼容自定义配置列表
        if answer in [x.lower() for x in self.true_list] or answer in self.true_list:
            return True
        elif answer in [x.lower() for x in self.false_list] or answer in self.false_list:
            return False
        else:
            # 无法判断, 随机选择
            logger.error(
                f'无法判断答案 -> {answer} 对应的是正确还是错误, 请自行判断并加入配置文件重启脚本, 本次将会随机选择选项')
            return random.choice([True, False])

    def get_submit_params(self):
        """
        这是一个专用方法, 用于根据当前设置的提交模式, 响应对应的答题提交API中的pyFlag值
        """
        # 留空直接提交, 1保存但不提交
        if self.SUBMIT:
            return ""
        else:
            return "1"

    def check_llm_connection(self) -> bool:
        """
        检查大模型连接是否可用
        默认返回 True（非大模型题库不需要检查）
        """
        return True


class TikuFallback(Tiku):
    # 多题库回退实现，按 provider 中配置顺序依次查询。
    def __init__(self, providers=None, config_path: Optional[str] = None):
        """初始化多题库回退."""
        super().__init__(config_path)
        self.name = '多题库回退'
        self.providers = providers or []
        self.skip_answer_validation = True

    def _init_tiku(self):
        active = []
        for provider in self.providers:
            try:
                provider.init_tiku()
                if not provider.DISABLE:
                    active.append(provider)
            except Exception as e:
                logger.error(f'初始化题库 {provider.name} 失败: {e}')
        self.providers = active
        if not self.providers:
            logger.error('多题库回退初始化失败: 没有可用题库')
            self.DISABLE = True
        else:
            logger.info(f"多题库回退已启用，查询顺序: {', '.join([p.__class__.__name__ for p in self.providers])}")

    def _query(self, q_info: dict) -> Optional[str]:
        _local_done = False
        for provider in self.providers:
            # 🔴 2026-10-01：与 `_query_all` 保持一致 —— 落 AI 之前先试本地题库。
            #    （原先还挂了 `not work_feedback` = "重做模式跳过"，已删除：
            #      该否决改由 `local_tiku_answer` 内部的 wrong 检查**按题**完成，
            #      不再因为卷子里有一道错题就把全卷的本地库都关掉。）
            if isinstance(provider, AI):
                _local_done = True
                try:
                    _la = local_tiku_answer(q_info.get("title") or "",
                                            q_info.get("options") or "",
                                            q_info.get("type"))
                except Exception as e:                             # noqa: BLE001
                    logger.debug(f"本地题库直查失败（跳过）：{e}")
                    _la = ""
                if _la:
                    logger.info(f"从本地题库获取答案：{q_info['title']} -> {_la}")
                    return _la
            try:
                answer = provider._query(q_info)
            except AIKeyExhaustedError as e:
                # 🔴 2026-09-26 23:40：Key 池全废是**预期内**的降级路径
                # （免费额度打光），用 warning + 不打堆栈，避免日志被
                # 长 traceback 淹没（真机踩坑：一题一个 60 行堆栈）。
                logger.warning(
                    f'{provider.name} 的 AI Key 已全部耗尽，'
                    f'回退到下一个题库：{e}')
                continue
            except Exception as e:
                provider_id = f'{provider.name}({provider.__class__.__name__})'
                logger.exception(f'{self.name} 查询时 {provider_id} 异常: {e}')
                continue
            if not answer:
                logger.info(f'{provider.name} 未命中，回退到下一个题库')
                continue

            # 若当前题库返回答案但类型不符，则继续回退。
            if check_answer(answer, q_info['type'], provider):
                logger.info(f'{provider.name} 命中答案')
                return answer

            # 🔴 2026-09-27：把「被拒的原始答案 + 判定上下文」打出来。
            # 起因：考试题日志只有「返回答案类型不符」，看不到 AI 究竟答了什么，
            # 完全没法定位（type 是数字还是字符串、答案是正文还是多行…）。
            logger.info(
                f'{provider.name} 返回答案类型不符，回退到下一个题库'
                f'｜type={q_info.get("type")!r} 原始答案={str(answer)[:150]!r}')

        # 链尾兜底：provider 链里**没有 AI** 时，上面那次本地直查不会触发 —— 补一次。
        if not _local_done:
            try:
                return local_tiku_answer(q_info.get("title") or "",
                                         q_info.get("options") or "",
                                         q_info.get("type")) or None
            except Exception as e:                                 # noqa: BLE001
                logger.debug(f"本地题库直查失败（跳过）：{e}")
        return None

    def _query_all(self, q_list: list[dict], query_delay: float = 0.0) -> list[Optional[str]]:
        results = [None] * len(q_list)
        pending_indices = list(range(len(q_list)))
        _local_done = False          # 本地直查是否已跑过（供链尾兜底判断）

        for provider in self.providers:
            if not pending_indices:
                break
            if provider.DISABLE:
                continue

            # 🔴 2026-10-01：落 AI 之前先试本地题库（tiku.db）兜底。
            #    8060(TikuAdapter) 按 hash 精确查，而 hash 把**选项文本逐字**算进去 ——
            #    标点/全半角差一点就 miss（3 天实测被拒 408 次），
            #    本地按归一化题干匹配，专门捞这批漏网的。
            #    排在 AI **之前**：本地答案是平台判对过的，比 AI 猜的可信。
            #    ⚠️ 原先这里还挂了 `not work_feedback`（重做模式跳过），已删除：
            #       "库里的值正好是刚被判错的那个"由 `local_tiku_answer` 内部的
            #       wrong 检查**按题**处理，不必因此关掉整卷。
            if isinstance(provider, AI):
                _local_done = True
                pending_indices = local_tiku_round(q_list, results, pending_indices)
                if not pending_indices:
                    break

            sub_q_list = [q_list[idx] for idx in pending_indices]
            try:
                sub_results = provider.query_all(sub_q_list, query_delay=query_delay)
            except AIKeyExhaustedError as e:
                logger.warning(
                    f'{provider.name} 的 AI Key 已全部耗尽，'
                    f'批量查询跳过该题库：{e}')
                continue
            except Exception as e:
                provider_id = f'{provider.name}({provider.__class__.__name__})'
                logger.exception(f'{self.name} 批量查询时 {provider_id} 异常: {e}')
                continue

            if not isinstance(sub_results, list):
                logger.error(f"{provider.name} 批量查询返回数据格式异常（非列表），跳过该题库")
                continue

            if len(sub_results) != len(pending_indices):
                logger.error(
                    f"{provider.name} 批量查询返回结果长度（{len(sub_results)}）与请求题目数（{len(pending_indices)}）不匹配，跳过该题库以防答案错位")
                continue

            next_pending_indices = []
            for orig_idx, ans in zip(pending_indices, sub_results):
                if ans:
                    logger.info(f'{provider.name} 命中答案: {q_list[orig_idx]["title"]} -> {ans}')
                    results[orig_idx] = ans
                else:
                    logger.info(f'{provider.name} 未命中或返回答案无效，将回退')
                    next_pending_indices.append(orig_idx)
            pending_indices = next_pending_indices

        # 链尾兜底：provider 链里**没有 AI** 时（例如只配了 TikuAdapter），上面那次
        # 本地直查根本不会触发（它挂在 `isinstance(provider, AI)` 分支上）——
        # 这里补一次，保证"只要还有未命中的题，本地库就一定被问过一遍"。
        if pending_indices and not _local_done:
            local_tiku_round(q_list, results, pending_indices)

        return results

    def set_work_feedback(self, feedback) -> None:
        """把上一轮章节检测的错题反馈转发给支持它的子题库（如 AI 大模型）。

        🔴 2026-10-01：**不再在本层保留副本**。
            原先存一份是为了让 `_query_all` 判断"是否重做模式"，好决定要不要
            跳过本地题库 —— 但那个整卷级的跳过已删除（改为 `local_tiku_answer`
            里的 wrong **按题**否决，见 `answer.py` 上方），本层就没有读者了。
            留着副本只会让人误以为它还有作用。`work_feedback` 现在的唯一用途是
            **给 AI 的提示词注入错题线索**，由 `AI` 子题库自己持有。
        """
        for provider in self.providers:
            if hasattr(provider, 'set_work_feedback'):
                try:
                    provider.set_work_feedback(feedback)
                except Exception as e:
                    logger.warning(f"向子题库 {provider.name} 设置错误反馈失败: {e}")

    def set_session(self, session) -> None:
        """把超星登录会话转发给支持读图的子题库（2026-09-13）。

        为什么必须有：默认配置是 provider="TikuAdapter,AI"（免费题库 +
        AI 兜底），外层是 TikuFallback。若不在这里转发，Chaoxing 注入的
        session 只会停在 TikuFallback 上，内层 AI 拿不到 → 题目配图
        永远下载不了，且是**静默失效**（不报错，只是没有图）。
        """
        for provider in self.providers:
            if hasattr(provider, 'set_session'):
                try:
                    provider.set_session(session)
                except Exception as e:
                    logger.warning(f"向子题库 {provider.name} 注入会话失败: {e}")

    def check_llm_connection(self) -> bool:
        for provider in self.providers:
            if not provider.check_llm_connection():
                logger.error(f'{provider.name} 连接检查失败')
                return False
        return True


# 按照以下模板实现更多题库

class TikuYanxi(Tiku):
    # 言溪题库实现
    def __init__(self, config_path: Optional[str] = None) -> None:
        """初始化言溪题库实例."""
        super().__init__(config_path)
        self.name = '言溪题库'
        self.api = 'https://tk.enncy.cn/query'
        self._token = None
        self._token_index = 0  # token队列计数器
        self._times = 100  # 查询次数剩余, 初始化为100, 查询后校对修正

    def _query(self, q_info: dict):
        res = requests.get(
            self.api,
            params={
                'question': q_info['title'],
                'token': self._token,
                # 'type':q_info['type'], #修复478题目类型与答案类型不符（不想写后处理了）
                # 没用，就算有type和options，言溪题库还是可能返回类型不符，问了客服，type仅用于收集
            },
            verify=False,
            # 🔴 2026-10-01 修：补 timeout。这是全项目**唯一**漏掉超时的题库请求
            #    （同文件其余 8 处调用都带 timeout）。缺失时若 tk.enncy.cn 挂起，
            #    `study_work → tiku.query_all → _query` 会永久阻塞 → worker 线程挂死。
            timeout=(5, 20),
        )
        if res.status_code == 200:
            res_json = res.json()
            if not res_json['code']:
                # 如果是因为TOKEN次数到期, 则更换token
                if self._times == 0 or '次数不足' in res_json['data']['answer']:
                    logger.info('TOKEN查询次数不足, 将会更换并重新搜题')
                    self._token_index += 1
                    self.load_token()
                    # 重新查询
                    return self._query(q_info)
                logger.error(
                    f'{self.name}查询失败:\n\t剩余查询数{res_json["data"].get("times", f"{self._times}(仅参考)")}:\n\t消息:{res_json["message"]}')
                return None
            self._times = res_json["data"].get("times", self._times)
            return res_json['data']['answer'].strip()
        else:
            logger.error(f'{self.name}查询失败:\n{res.text[:500]}')
        return None

    def load_token(self):
        token_list = self._conf['tokens'].split(',')
        if self._token_index == len(token_list):
            # TOKEN 用完
            logger.error('TOKEN用完, 请自行更换再重启脚本')
            raise PermissionError(f'{self.name} TOKEN 已用完, 请更换')
        self._token = token_list[self._token_index]

    def _init_tiku(self):
        self.load_token()


class TikuGo(Tiku):
    # GO题（网课小工具题库）实现
    def __init__(self, config_path: Optional[str] = None) -> None:
        """初始化GO题实例."""
        super().__init__(config_path)
        self.name = 'GO题（网课小工具题库）'
        self.api = 'https://q.icodef.com/wyn-nb?v=4'
        self._headers = {
            'Authorization': '',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        self._request_lock = threading.Lock()
        self._last_request_time = 0.0
        self._min_interval = 1.0
        self._retry_times = 3
        self._retry_backoff = 1.2

    def _sleep_for_next_request(self) -> None:
        with self._request_lock:
            now = time.time()
            wait_time = max(0.0, self._last_request_time + self._min_interval - now)
            self._last_request_time = now + wait_time
        if wait_time > 0:
            time.sleep(wait_time)

    def _mark_request_finished(self) -> None:
        with self._request_lock:
            self._last_request_time = time.time()

    def _request_question(self, question: str, attempt: int) -> Optional[requests.Response]:
        try:
            self._sleep_for_next_request()
            res = requests.post(
                self.api,
                data={'question': question},
                headers=self._headers,
                verify=True,
                timeout=15
            )
            self._mark_request_finished()
            return res
        except requests.exceptions.RequestException as e:
            logger.error(f'{self.name}查询异常 ({attempt}/{self._retry_times}): {e}')
            self._mark_request_finished()
            return None

    def _parse_response(self, res: requests.Response) -> Optional[dict]:
        if res.status_code != 200:
            logger.error(f'{self.name}查询失败: 状态码 {res.status_code}, 响应: {res.text[:500]}')
            return None

        try:
            res_json = res.json()
        except ValueError:
            logger.error(f'{self.name}查询失败: 返回内容不是有效JSON, 响应: {res.text[:500]}')
            return None

        try:
            code = int(str(res_json.get('code', '')).strip())
        except ValueError:
            code = 0

        answer = str(res_json.get('data', '')).strip()
        msg = str(res_json.get('msg', '')).strip()
        raw_text = f'{answer} {msg}'
        is_throttled = any(key in raw_text for key in ['流控限制', '速度太快', '并发限制', '忙不过来'])
        return {
            'code': code,
            'answer': answer,
            'msg': msg,
            'is_throttled': is_throttled,
        }

    def _sleep_retry(self, attempt: int, reason: str, include_min_interval: bool = False) -> None:
        if include_min_interval:
            sleep_seconds = max(self._min_interval, self._retry_backoff * attempt)
        else:
            sleep_seconds = self._retry_backoff * attempt
        logger.warning(f'{self.name}{reason}，{sleep_seconds:.1f}s 后重试 ({attempt}/{self._retry_times})')
        time.sleep(sleep_seconds)

    @staticmethod
    def _is_placeholder_answer(answer: str, msg: str) -> bool:
        return '李恒雅' in answer or '李恒雅' in msg

    def _query(self, q_info: dict):
        title = q_info.get('title', '')
        candidates = [
            title,
            re.sub(r'^【[^】]+】\s*', '', title).strip(),
            re.sub(r'^\[[^\]]+\]\s*', '', title).strip(),
        ]
        seen = set()
        normalized_titles = []
        for item in candidates:
            if item and item not in seen:
                seen.add(item)
                normalized_titles.append(item)

        for query_title in normalized_titles:
            answer = self._query_once(query_title)
            if answer:
                return answer
        return None

    def _query_once(self, question: str) -> Optional[str]:
        for attempt in range(1, self._retry_times + 1):
            res = self._request_question(question, attempt)
            if res is None:
                if attempt < self._retry_times:
                    self._sleep_retry(attempt, '查询异常', include_min_interval=True)
                    continue
                break

            parsed = self._parse_response(res)
            if not parsed:
                return None

            code = parsed['code']
            answer = parsed['answer']
            msg = parsed['msg']
            is_throttled = parsed['is_throttled']

            if code != 1:
                if is_throttled and attempt < self._retry_times:
                    self._sleep_retry(attempt, '触发流控')
                    continue
                logger.info(f"{self.name}未命中或失败: {msg or '未知错误'}")
                return None

            if not answer:
                return None

            # GO题库在未搜到时可能在 data/msg 中返回“李恒雅正在努力撰写中...”。
            if self._is_placeholder_answer(answer, msg):
                if is_throttled and attempt < self._retry_times:
                    self._sleep_retry(attempt, '命中流控提示')
                    continue
                return None

            return answer

        return None

    def _init_tiku(self):
        self._headers['Authorization'] = self._conf.get('go_authorization', self._headers['Authorization'])
        try:
            min_interval = float(self._conf.get('go_min_interval', self._min_interval))
            if min_interval < 0:
                raise ValueError('go_min_interval must be non-negative')
            self._min_interval = min_interval
        except (TypeError, ValueError):
            logger.warning(f'{self.name}配置 go_min_interval 无效，使用默认值 {self._min_interval}')

        try:
            retry_times = int(self._conf.get('go_retry_times', self._retry_times))
            if retry_times < 1:
                raise ValueError('go_retry_times must be >= 1')
            self._retry_times = retry_times
        except (TypeError, ValueError):
            logger.warning(f'{self.name}配置 go_retry_times 无效，使用默认值 {self._retry_times}')

        try:
            retry_backoff = float(self._conf.get('go_retry_backoff', self._retry_backoff))
            if retry_backoff < 0:
                raise ValueError('go_retry_backoff must be non-negative')
            self._retry_backoff = retry_backoff
        except (TypeError, ValueError):
            logger.warning(f'{self.name}配置 go_retry_backoff 无效，使用默认值 {self._retry_backoff}')


class TikuLike(Tiku):
    # LIKE知识库实现 参考 https://www.datam.site/
    def __init__(self, config_path: Optional[str] = None) -> None:
        """初始化LIKE知识库实例."""
        super().__init__(config_path)
        self.name = 'LIKE知识库'
        self.ver = '2.0.0'  # 对应官网API版本
        self.query_api = 'https://app.datam.site/api/v1/query'
        self.models_api = 'https://app.datam.site/api/v1/query/models'
        self.balance_api = 'https://app.datam.site/api/v1/balance'
        self.homepage = 'https://www.datam.site'
        self._model = None
        # 🔴 2026-10-12：300 秒 → **15 秒**。
        #    真机事故（acc2《植物知道生命的答案》19:05）：上游题库/大模型不可用时，
        #    一次调用最长卡 **300 秒**，且槽位轮换还会再来一轮 ⇒ 单题能耗 4~5 分钟，
        #    考试线程被拖到"4 分钟不推进"（表现为"任务在跑但永远不结束"）。
        #    实测正常作答只需 3~6 秒，15 秒足够；上游挂掉时快速失败、换下一个源。
        self._timeout = 15
        self._retry = True
        self._retry_times = 3
        self._tokens = []
        self._balance = {}
        self._search = False
        self._vision = True
        self._count = 0
        self._headers = {"Content-Type": "application/json"}

    def _query(self, q_info: dict = None):
        if not q_info:
            logger.error("当前无题目信息，请检查")
            return ""

        q_info_map = {"single": "【单选题】", "multiple": "【多选题】", "completion": "【填空题】", "judgement": "【判断题】"}
        q_info_prefix = q_info_map.get(q_info['type'], "【其他类型题目】")
        options = ', '.join(q_info['options']) if isinstance(q_info['options'], list) else q_info['options']
        question = f"{q_info_prefix}{q_info['title']}\n"

        if q_info['type'] in ['single', 'multiple']:
            question += f"选项为: {options}\n"

        # 随机选择一个token进行查询
        token = random.choice(self._tokens)

        # 检查该token是否有余额
        if self._balance.get(token, 0) <= 0:
            logger.error(f'{self.name}当前Token查询次数不足: ...{token[-5:]}')
            # 尝试选择其他有余额的token
            available_tokens = [t for t in self._tokens if self._balance.get(t, 0) > 0]
            if available_tokens:
                token = random.choice(available_tokens)
            else:
                logger.error(f'{self.name}所有Token查询次数都不足')
                return None

        ans = None
        try_times = 0

        # 尝试查询，直到成功或达到重试次数
        while not ans and self._retry and try_times < self._retry_times:
            ans = self._query_single(token, question)
            try_times += 1
            if ans:  # 如果查询成功，减少余额
                self._balance[token] -= 1
                logger.info(f'使用Token ...{token[-5:]} 查询成功，剩余次数: {self._balance[token]}')
                break
            elif try_times < self._retry_times:
                logger.warning(f'使用Token ...{token[-5:]} 查询失败，进行第 {try_times + 1} 次重试...')

        # 10次查询后更新余额
        self._count = (self._count + 1) % 10
        if self._count == 0:
            self.update_times()

        return ans

    def _query_single(self, token: str = "", query: str = "") -> str:
        """
        查询单个问题的答案
        
        Args:
            token: API访问令牌
            query: 查询的问题内容
            
        Returns:
            查询到的答案，如果失败则返回None
        """
        # 验证输入参数
        if not token:
            logger.error(f'{self.name}查询失败: 未提供有效的token')
            return None

        if not query:
            logger.error(f'{self.name}查询失败: 查询内容为空')
            return None

        # 设置请求头
        temp_headers = self._headers.copy()
        temp_headers['Authorization'] = f'Bearer {token}'

        # 准备请求数据
        request_data = {
            'query': query,
            'model': self._model if self._model else '',
            'search': self._search,
            'vision': self._vision
        }

        # 发送API请求
        try:
            res = requests.post(
                self.query_api,
                json=request_data,
                headers=temp_headers,
                verify=False,
                timeout=self._timeout  # 添加超时设置
            )
        except requests.exceptions.Timeout:
            logger.error(f'{self.name}查询超时: 请求超过300秒')
            return None
        except requests.exceptions.ConnectionError:
            logger.error(f'{self.name}网络连接错误: 无法连接到API服务器')
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f'{self.name}查询异常: \n{e}')
            return None
        except Exception as e:
            logger.error(f'{self.name}查询发生未知错误: \n{e}')
            return None

        # 处理HTTP响应
        if res.status_code == 200:
            return self._parse_response(res)
        elif res.status_code == 401:
            logger.error(f'{self.name}认证失败: 请检查Token是否正确或已过期')
        elif res.status_code == 429:
            logger.error(f'{self.name}请求过于频繁: 已达到API速率限制')
        elif res.status_code == 500:
            logger.error(f'{self.name}服务器内部错误: API服务暂时不可用')
        elif res.status_code == 400:
            logger.error(f'{self.name}请求参数错误: 请检查查询内容格式')
        elif res.status_code == 403:
            logger.error(f'{self.name}访问被拒绝: 可能是Token权限不足')
        else:
            logger.error(f'{self.name}查询失败: 状态码 {res.status_code}, 响应内容: \n{res.text[:500]}')

        return None

    def _parse_response(self, response):
        """
        解析API响应
        
        Args:
            response: HTTP响应对象
            
        Returns:
            解析后的答案，如果解析失败则返回None
        """
        try:
            res_json = response.json()
        except json.JSONDecodeError:
            logger.error(f'{self.name}响应解析失败: 响应不是有效的JSON格式')
            return None
        except Exception as e:
            logger.error(f'{self.name}响应解析异常: {e}')
            return None

        # 记录响应消息
        msg = res_json.get('message', '')
        if msg:
            logger.info(f'{self.name}响应消息: {msg}')

        results = res_json.get('results', {})
        if not results or not isinstance(results, dict):
            logger.error(f'{self.name}查询结果格式错误: API返回结果中results字段格式不正确')
            return None

        output = results.get('output', None)
        if output is None or not isinstance(output, dict):
            logger.error(f'{self.name}查询结果中output字段格式错误或不存在')
            return None

        q_type = output.get('questionType', None)
        if q_type is None:
            logger.error(f'{self.name}查询结果中questionType字段不存在')
            return None

        answer = output.get('answer', None)
        if answer is None:
            logger.error(f'{self.name}查询结果中answer字段不存在')
            return None

        # 根据题目类型提取答案
        return self._extract_answer_by_type(q_type, answer)

    def _extract_answer_by_type(self, q_type: str, answer: dict) -> str:
        """
        根据题目类型提取答案
        
        Args:
            q_type: 题目类型
            answer: 答案字典
            
        Returns:
            提取的答案文本
        """
        if not isinstance(answer, dict):
            logger.error(f'{self.name}答案格式错误: 不是有效的字典格式')
            return None

        if q_type == "CHOICE":
            selected_options = answer.get('selectedOptions', None)
            if selected_options is not None:
                if isinstance(selected_options, list) and selected_options:
                    # 过滤掉None和空字符串
                    valid_options = [opt for opt in selected_options if opt is not None and str(opt).strip()]
                    if valid_options:
                        return '\n'.join(str(opt) for opt in valid_options)
                    else:
                        logger.error(f'{self.name}CHOICE类型题目没有有效的选项内容')
                else:
                    logger.error(f'{self.name}CHOICE类型题目没有有效的选项内容')
            else:
                logger.error(f'{self.name}CHOICE类型题目缺少selectedOptions字段')
        elif q_type == "FILL_IN_BLANK":
            blanks = answer.get('blanks', None)
            if blanks is not None:
                if isinstance(blanks, list) and blanks:
                    # 过滤掉None和空字符串
                    valid_blanks = [blank for blank in blanks if blank is not None and str(blank).strip()]
                    if valid_blanks:
                        return "\n".join(str(blank) for blank in valid_blanks)
                    else:
                        logger.error(f'{self.name}FILL_IN_BLANK类型题目没有有效的填空内容')
                else:
                    logger.error(f'{self.name}FILL_IN_BLANK类型题目没有有效的填空内容')
            else:
                logger.error(f'{self.name}FILL_IN_BLANK类型题目缺少blanks字段')
        elif q_type == "JUDGMENT":
            is_correct = answer.get('isCorrect', None)
            if is_correct is not None:
                return "正确" if is_correct else "错误"
            else:
                logger.error(f'{self.name}JUDGMENT类型题目缺少isCorrect字段')
        else:
            otherText = answer.get('otherText', None)
            if otherText is not None:
                return str(otherText)
            else:
                logger.error(f'{self.name}未知题目类型{q_type}且缺少otherText字段')

        return None

    def get_api_balance(self, token: str = ""):
        if not token:
            logger.error(f'{self.name}获取余额失败: 未提供有效的token')
            return 0

        temp_headers = self._headers.copy()
        temp_headers['Authorization'] = f'Bearer {token}'
        try:
            res = requests.get(
                self.balance_api,
                headers=temp_headers,
                verify=False,
                timeout=self._timeout
            )
            if res.status_code == 200:
                res_json = res.json()
                return int(res_json.get("balance", 0))
            else:
                logger.error(f'{self.name}请求余额接口失败，状态码: {res.status_code}')
                return 0
        except requests.exceptions.Timeout:
            logger.error(f'{self.name}获取余额超时: 请求超过30秒')
            return 0
        except requests.exceptions.ConnectionError:
            logger.error(f'{self.name}网络连接错误: 无法连接到余额查询API服务器')
            return 0
        except ValueError:  # json解析错误或int转换错误
            logger.error(f'{self.name}余额响应解析失败: 响应格式不正确')
            return 0
        except Exception as e:
            logger.error(f'{self.name}Token余额查询过程中出现错误: {e}')
            return 0

    def update_times(self) -> None:
        if not self._tokens:
            logger.warning(f'{self.name}未加载任何Token, 无法更新余额')
            return
        for token in self._tokens:
            balance = self.get_api_balance(token)
            self._balance[token] = balance
            logger.info(
                f"当前LIKE知识库Token: ...{token[-5:]} 的剩余查询次数为: {balance} (仅供参考, 实际次数以查询结果为准)")

    def load_tokens(self) -> None:
        tokens_str = self._conf.get('tokens')
        if not tokens_str:
            logger.error(f'{self.name}配置中未找到tokens')
            self._tokens = []
            return
        if ',' in tokens_str:
            tokens = [token.strip() for token in tokens_str.split(',') if token.strip()]
        else:
            tokens = [tokens_str.strip()] if tokens_str.strip() else []
        self._tokens = tokens
        if not self._tokens:
            logger.warning(f'{self.name}未加载任何有效的Token')

    def load_config(self) -> None:
        # 从配置中获取参数，提供默认值
        self._search = self._conf.get('likeapi_search', False)
        self._model = self._conf.get('likeapi_model', None)
        self._vision = self._conf.get('likeapi_vision', True)
        self._retry = self._conf.get("likeapi_retry", True)
        self._retry_times = int(self._conf.get("likeapi_retry_times", 3))

    def _init_tiku(self) -> None:
        self.load_config()
        self.load_tokens()
        if self._tokens:
            self.update_times()
        else:
            logger.error(f'{self.name}初始化失败: 未加载任何有效的Token')
            self.DISABLE = True


class TikuAdapter(Tiku):
    # TikuAdapter题库实现 https://github.com/DokiDoki1103/tikuAdapter
    def __init__(self, config_path: Optional[str] = None) -> None:
        """初始化TikuAdapter题库实例."""
        super().__init__(config_path)
        self.name = 'TikuAdapter题库'
        self.api = ''

    def _query(self, q_info: dict):
        # 判断题目类型
        if q_info['type'] == "single":
            type = 0
        elif q_info['type'] == 'multiple':
            type = 1
        elif q_info['type'] == 'completion':
            type = 2
        elif q_info['type'] == 'judgement':
            type = 3
        else:
            type = 4

        # 选项按行拆开，去掉 "A." / "A、" 前缀。
        # 2026-09-14 修 bug：原来空 options("") 经 split('\n') 得到 ['']，
        # 传给 TikuAdapter 后 hash 变成 md5(q + '[""]' + type + plat)，
        # 与库里的 md5(q + "[]" + type + plat) 不一致 —— 听力题（选项都嵌在
        # 题干里、options 为空）因此永远搜不到。空串必须产出 []。
        # 🔴 2026-09-27 修 bug：考试链路传的 options 是 **list[dict]**
        #    （{letter, value, text}），此前无条件当字符串 `.split('\n')`
        #    → AttributeError，导致**考试每一题的 TikuAdapter 查询都崩**。
        _raw_opts = q_info.get('options')
        if isinstance(_raw_opts, (list, tuple)):
            _lines = []
            for _o in _raw_opts:
                if isinstance(_o, dict):
                    _L = str(_o.get('letter') or _o.get('value') or '').strip()
                    _T = str(_o.get('text') or '').strip()
                    if _L or _T:
                        _lines.append(f"{_L}. {_T}" if _T else _L)
                elif str(_o).strip():
                    _lines.append(str(_o))
            options = [sub(r'^[A-Za-z]\.?、?\s?', '', o) for o in _lines if o.strip()]
        else:
            options = [sub(r'^[A-Za-z]\.?、?\s?', '', option)
                       for option in (_raw_opts or '').split('\n') if option.strip()]
        # 2026-09-14 【听力题专项】type=4 一律不带选项查询。
        # 实测（3.5 题）：正确答案 BAD 就在题库里，但查询侧把每道小题的
        # A/B/C/D 拼成 12 个选项发出去，而题库里听力题的 options 存的是 []，
        # 两边 md5 不等 → 查不到 → 转 AI 瞎猜（CDC）→ 判错。
        # 22 条听力题实测：[题干 + [] + 4 + 0] 与库中 hash 100% 一致。
        if type == 4:
            options = []
        # 2026-09-15 修 bug：此前 requests.post 没有 timeout —— TikuAdapter
        # 一旦卡住（进程假死 / 端口被占但无响应），整个答题流程会无限期挂起，
        # 表现为作业页一直转圈。加超时 + 异常兜底：超时就当未命中，回退下一个
        # 题库（正常链路是 TikuAdapter,AI，AI 会兜底）。
        try:
            res = requests.post(
                self.api,
                json={
                    'question': q_info['title'],
                    'options': options,
                    'type': type
                },
                verify=False,
                timeout=(5, 20),   # (连接, 读取) 秒
            )
        except requests.RequestException as e:
            logger.warning(f"{self.name} 请求异常（按未命中处理）: {e}")
            return None
        if res.status_code == 200:
            try:
                res_json = res.json()
                best = (res_json.get('answer') or {}).get('bestAnswer') or []
            except Exception as e:
                logger.warning(f"{self.name} 响应解析失败（按未命中处理）: {e}")
                return None
            # plat 无论搜没搜到答案都返回 0；该字段是 tikuadapter 用来
            # 设定自定义平台类型的，这里用不到。
            if not len(best):
                logger.debug("未命中，返回：" + res.text[:200])
                return None
            # 🔴 2026-09-27 新增：**选项集合校验**（拦"串味命中"）。
            # 背景：自制题实测 —— AI 21/21 全对，TikuAdapter 27 题错 11 题，
            # 全部是"题库返回了另一道题的答案"（模糊匹配所致）：
            #   "下列哪个**不是**哺乳动物" → 返回"鲸"；"四大发明" → 返回"针灸"；
            #   "长江是中国第一长河" → 返回"错误"（命中了"黄河…"那道题）。
            # ⚠️ **实测结论（很重要，别重复踩）**：对当前这套 TikuAdapter，
            #    **这道校验拦不到任何串味**（错题与好题的 options 集合 100% 一致）
            #    —— 因为它把 `options` 原样回显我们传入的值，而不是回库中题的选项。
            #    但实测也 **0 误伤**，成本极低，故保留：换题库源/换实现时可能有用。
            #    真正可靠的兜底仍是 AI（见 _accept 的映射兜底与 query 的 provider 链）。
            # 🔴 2026-10-12 修：**判断题/填空题根本不能做这个校验**。
            #   实测（acc2《机器的征途》85 题得分 82）：30 道判断题
            #   **全部**落到「全能搜题」，TikuAdapter 一道都没供 ——
            #   日志显示原因全是
            #     `选项集合与本题不符（串味命中）→ 拒绝`
            #   而判断题的 `options` 是**空列表**，库里回显的是该题在
            #   题库系统里的选项（`A.A / B.B`），两侧集合**必然不等**
            #   ⇒ 校验把判断题**全部误杀**。
            #   ⇒ 判断题（及任何无选项题型）直接跳过本校验。
            #   注：本校验对选择题**实测 0 命中**（原注释已记录），
            #   故放过判断题不会削弱防串味能力。
            _qtype = str(q_info.get("type") or "")
            _skip_optcheck = _qtype in ("judgement", "completion", "shortanswer")
            _lib_opts = res_json.get("options")
            if (not _skip_optcheck and isinstance(_lib_opts, list)
                    and _lib_opts and options):
                def _oset(xs):
                    return {re.sub(r"[\s\u3000,，。.、;；:：!！?？\"'“”‘’()（）\[\]【】]+",
                                   "", str(x)).lower()
                            for x in xs if str(x).strip()}
                _a, _b = _oset(_lib_opts), _oset(options)
                if _a != _b:
                    logger.warning(
                        f"{self.name} 选项集合与本题不符（串味命中，库中是另一道题）"
                        f"→ 拒绝并交下一个题库：{q_info['title'][:40]}")
                    return None
            # 🔴 2026-09-27 新增：**答案歧义拒绝**。
            # 真机实证（A 卷第 4 题「下列哪个不是哺乳动物？」）：题库里同时存在
            # "鲸" 与 "企鹅" 两条互相冲突的记录 ——
            # 响应 allAnswer=[["鲸"],["企鹅"]]，作业那次选中 "鲸" → 判错；
            # 事后重查同一接口又选中 "企鹅"。这种"同题多答案"的记录不可采信：
            # 按未命中处理，交给后续题库/AI（腾讯额度充足，宁可多问一次也要答对）。
            try:
                _ans_obj = res_json.get("answer") or {}
                _all = _ans_obj.get("allAnswer") or []
                _uniq = set()
                for _x in _all:
                    _t = ("".join(_x) if isinstance(_x, (list, tuple))
                          else str(_x)).strip()
                    # 🔴 规范化后再比对：题库里常有"长度单位"与"长度单位,"这类
                    # **只差标点**的成对记录（实测误报两例：光年 / 物种起源），
                    # 那不是真冲突。必须先去标点空白再判重，否则会把好答案拒掉、
                    # 白白多问一次 AI。
                    _t = re.sub(r"[\s,，。.、;；:：!！?？\"'“”‘’]+", "", _t)
                    if _t:
                        _uniq.add(_t)
                # 🔴 2026-10-01 修 bug（实测 3 天 77 次误拒）：
                #    `allAnswer` 是**按完整度排序的候选清单**，同一道题常同时给出
                #    "部分匹配"和"完整匹配"两种粒度。实测多选题原响应：
                #        answerKey=BC, bestAnswer=[艺术的参与, 不干扰…欣赏],
                #        allAnswer=[ [艺术的参与], [艺术的参与, 不干扰…欣赏] ]
                #    —— 第 2 个候选是第 1 个的**超集**，本是同一个答案。
                #    先前只判"去重后是否 >1"，于是这种子集/超集关系被当成
                #    互相冲突 → **正确率 100% 的答案被拒**，转头去问 AI。
                #    现在只有出现**互不包含**的两个候选才算真冲突
                #    （真冲突样例：鲸 / 企鹅 —— 两个都不是对方的子串）。
                # 🔴🔴 2026-10-12 修：**判断题必须先按"真假"归一**再判冲突。
                #   真机证据（《中国历史人文地理》85 题，本场 23 次冲突）：
                #     ['×','错','错误']  ['√','对','正确']  ['对','正确']
                #     ['×','错误']        ['√','对','正确']
                #   这些是**同一个答案的多种写法**，但 `√` 与 `对` 互不包含
                #   ⇒ 被判"冲突" → **判断题被成批误杀** → 转去全能搜题。
                #   （与 10-12 修的「选项集合校验误杀判断题」是同一类 bug：
                #     校验对无选项/多写法题型不成立。）
                #   修法：先把判断题候选统一成 True/False 再比较，
                #        只有归一后**仍互不包含**才是真冲突。
                #   ⚠️ 只对判断题生效，不动其他题型的原有判据。
                _jnorm = {"对", "正确", "是", "√", "true", "t", "1", "yes"}
                _jnorm |= {"错", "错误", "否", "×", "false", "f", "0", "no"}
                if _qtype == "judgement" and _uniq:
                    _low = {str(x).strip().lower() for x in _uniq}
                    if _low <= _jnorm or {str(x).strip() for x in _uniq} <= _jnorm:
                        _uniq = {"正确" if str(x).strip().lower() in
                                 ("对", "正确", "是", "√", "true", "t", "1",
                                  "yes") else "错误"
                                 for x in _uniq}
                        logger.info(
                            f"{self.name} 判断题答案为同一判断的多种写法"
                            f"（{sorted(_uniq)}）→ 已归一，不算冲突")
                _u = sorted(_uniq, key=len)
                if any(not (a in b)
                       for i, a in enumerate(_u) for b in _u[i + 1:]):
                    logger.warning(
                        f"{self.name} 题库答案存在冲突（{sorted(_uniq)[:4]}），"
                        f"不可采信 → 按未命中处理交下一个题库："
                        f"{q_info['title'][:40]}")
                    return None
            except Exception as _e:                       # noqa: BLE001
                logger.debug(f"答案歧义检测失败（忽略，不影响作答）：{_e}")
            sep = "\n"
            return sep.join(best).strip()
        logger.debug(f'{self.name} 查询返回非 200: {res.status_code}')
        return None

    def _init_tiku(self):
        # self.load_token()
        url = self._conf['url']
        # 2026-09-15 【关键修复】必须显式带 use=local，否则本地题库查不到。
        #
        # 官方文档（github.com/DokiDoki1103/tikuAdapter）在「URL 请求参数」里写明：
        #     use  你想要使用哪些题库，不填写默认使用所有免费题库
        #         示例值：local,icodef,buguake,wanneng
        # 即 **不传 use 时，默认题库集合里没有 local** —— 我们自己写进
        # tikuAdapter/tiku.db 的那 170 多条答案根本不会被查询，
        # /search 只会去问 icodef / 不挂科 这些在线免费题库。
        #
        # 后果：表现为"答案明明回灌进库了、管理页也能看到，就是搜不到"，
        # 而且 TikuAdapter 照样会为通用常识题（"中国的首都是哪里"）返回答案 ——
        # 因为那是在线题库给的。极具迷惑性，排查时很容易误判成
        # "库坏了 / hash 不对 / 需要重启"，实际只差这一个 URL 参数。
        #
        # 实测（2026-09-15，同一道题同一时刻）：
        #     POST /adapter-service/search              -> bestAnswer = []          ✗
        #     POST /adapter-service/search?use=local    -> ['让数据分析结果更直观清晰'] ✓
        #
        # 兼容处理：
        #   - 配置里若已带 "use=" → 原样使用，尊重用户配置（可自定义 local,icodef 组合）
        #   - 已带其它 query（含 "?"）→ 用 & 追加
        #   - 干净 URL → 用 ? 追加
        if 'use=' not in url:
            url = url + ('&' if '?' in url else '?') + 'use=local'
        self.api = url


class TikuHive(Tiku):
    """OnlineCourseAPI（hive-net.cn）—— 第三个免费题库兜底（2026-09-29 接入）。

    定位：`TikuAdapter`（本地库 + 不挂科）未命中时，再问一次这个免费网课题库，
    最后才落到 AI。用户原话：「多个题库对应一个题库」「首要是不付费的」。

    实测（2026-09-29，40 道超星真题对拍）：
      · 题干完全一致（相似度 ≥0.95）：10/40 → **覆盖面约 25%**
      · 其中答案与本地库一致 8/10；另 2 道是**多选题库只存了部分选项**，
        外部源给的 ABC/ABCD 反而更完整 → 实际准确率 ≈ 10/10
      · 免费额度 10000 次/天（token=free），实测消耗后余 8900+

    两个**必须**的工程处理（少一个就会引入错误答案）：
      1. **浏览器 UA**：不带 UA 会被 Cloudflare 拦（HTTP 403, error code 1010）
      2. **题干相似度闸门（≥0.95）**：它是模糊搜索，实测最佳匹配相似度
         均值只有 0.599 —— 大量"同类不同题"（问"耳聋基因编码什么"却返回
         生物学分类词）。不过滤就会把**别的题**的答案填上来。

    安全策略：
      · 相似度不足 → 按未命中处理（绝不"猜一个"）
      · 答案字母 → 文本 → **在本题选项里反查**；反查不到也按未命中
        （避免"外部源与本题选项顺序不同"导致字母错位）
      · 返回的是**本题选项原文**（与已验证的 TikuAdapter 返回形态一致），
        避免下游"文本→字母"映射因细微差异失败
      · 任何异常（超时/解析失败/额度用尽）静默降级，交给下一个 provider
    """

    DISABLE = False
    _DEFAULT_URL = "https://www.hive-net.cn/backend/course/search"
    _DEFAULT_TOKEN = "free"
    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
    _MIN_SIM = 0.95

    def __init__(self, config_path: Optional[str] = None) -> None:
        super().__init__(config_path)
        self.name = 'OnlineCourse题库'

    def _init_tiku(self):
        # 端点 / 令牌允许用 config.ini [tiku] hive_url / hive_token 覆盖
        try:
            self.api = (self._conf.get('hive_url') or '').strip() or self._DEFAULT_URL
            self._token = (self._conf.get('hive_token') or '').strip() or self._DEFAULT_TOKEN
        except Exception:
            self.api = self._DEFAULT_URL
            self._token = self._DEFAULT_TOKEN

    # ---------- 内部工具 ----------
    @staticmethod
    def _clean_title(s) -> str:
        """去掉与题干无关的装饰：题型前缀 / 题号 / 尾部分值。

        ⚠️ **必须做**（2026-09-29 实测踩坑）：外部源是**关键词模糊搜索**，
        题干里带着「【单选题】」这类前缀会显著拉低匹配质量 ——
        同一道题带前缀查相似度仅 0.14~0.20，剥掉前缀后是 **1.00**
        （「五胡十六国的作战旗号是」实测对比）。
        """
        s = str(s or "").strip()
        s = re.sub(r"^【[^】]{1,8}】\s*", "", s)
        s = re.sub(r"^\d+[.、]\s*", "", s)
        s = re.sub(r"（\d+(?:\.\d+)?分）\s*$", "", s)
        return s.strip()

    @staticmethod
    def _norm(s) -> str:
        """题干/选项归一：去题型前缀、题号、空白与标点，统一小写。"""
        s = str(s or "").strip().lower()
        s = re.sub(r"^【[^】]{1,8}】\s*", "", s)
        s = re.sub(r"^\d+[.、]\s*", "", s)
        return re.sub(r"[\s\u3000,，。;；:：'\"“”‘’()（）\[\]【】_\-—\.、]+", "", s)

    @staticmethod
    def _parse_opts(text) -> dict:
        """'A.xxx,B.yyy' / 'A:xxx,B:yyy' / 换行分隔 → {'A': 'xxx', 'B': 'yyy'}。

        ⚠️ 分隔符必须包含**冒号**（2026-09-29 实测踩坑）：外部源的
        `answer_options` 实际格式是 `A:埃斯库罗斯,B:索福克勒斯,...`，
        只写 `[.、)．]` 会导致**整条解析为空** → 答案明明命中却被判成
        "选项无法对应本题"而丢弃。
        """
        out = {}
        for seg in re.split(r"[,\n，;；]", str(text or "")):
            m = re.match(r"\s*([A-Za-z])\s*[.、\)．:：]\s*(.+?)\s*$", seg.strip())
            if m:
                out[m.group(1).upper()] = m.group(2)
        return out

    @staticmethod
    def _q_opts(q_info: dict) -> list:
        """取本题选项文本（兼容 str / list[str] / list[dict] 三种形态）。"""
        raw = q_info.get("options")
        out = []
        if isinstance(raw, (list, tuple)):
            for o in raw:
                if isinstance(o, dict):
                    out.append(str(o.get("text") or o.get("value") or "").strip())
                else:
                    out.append(str(o).strip())
        else:
            out = [x.strip() for x in str(raw or "").split("\n") if x.strip()]
        return [re.sub(r"^[A-Za-z][.、\)．]\s*", "", x) for x in out if x]

    # ---------- 主查询 ----------
    def _query(self, q_info: dict) -> Optional[str]:
        title = self._clean_title(q_info.get("title"))
        if len(title) < 6:
            return None
        try:
            res = requests.get(
                self.api,
                params={"token": self._token, "question": title},
                headers={"User-Agent": self._UA,
                         "Referer": "https://www.hive-net.cn/"},
                verify=False,
                timeout=(5, 12),
            )
        except requests.RequestException as e:
            logger.debug(f"{self.name} 请求异常（按未命中处理）: {e}")
            return None
        if res.status_code != 200:
            logger.debug(f"{self.name} 返回非 200: {res.status_code}")
            return None
        try:
            data = res.json().get("data") or {}
        except Exception as e:
            logger.debug(f"{self.name} 响应解析失败（按未命中处理）: {e}")
            return None

        lst = data.get("list") or []
        if not lst:
            return None

        # ① 题干相似度闸门
        tgt = self._norm(title)
        best, best_sim = None, 0.0
        for it in lst:
            s = difflib.SequenceMatcher(
                None, self._norm(it.get("question")), tgt).ratio()
            if s > best_sim:
                best, best_sim = it, s
        if best is None:
            return None
        if best_sim < self._MIN_SIM:
            logger.info(
                f"{self.name} 最高相似度仅 {best_sim:.2f}（需 ≥{self._MIN_SIM}），"
                f"判为误匹配 → 按未命中处理交下一个题库：{title[:36]}")
            return None

        # ② 答案字母 → 文本 → 本题选项里反查
        their = self._parse_opts(best.get("answer_options"))
        letters = re.sub(r"[^A-Za-z]", "", str(best.get("answer") or "")).upper()
        their_texts = {self._norm(their[ch]) for ch in letters if ch in their}
        mine = self._q_opts(q_info)
        picks = [mine[i] for i, o in enumerate(mine) if self._norm(o) in their_texts]

        # 🔴 2026-09-29：**只接受"完整对应"**，不做部分采信、也不做
        # "条数一致就按字母硬对"。
        # 理由（实测）：外部源的选项**顺序与本题不同**是常态
        #   （「情境」题：库=以上都不是/特定的事件/特定的人物关系/特点的时空环境，
        #     外部源=特点的时空环境/特定的事件/特定的人物关系）。
        # 一旦按字母直接对应就会全盘错位；而部分对应则会把多选**答漏**
        # （外部源给 3 项、只对上 2 项 → 提交出去就是错的）。
        # 所以：对不上就整题放弃，宁可留给 AI。
        if not picks or len(picks) != len(letters):
            logger.info(
                f"{self.name} 命中题干但选项与本题无法完整对应"
                f"（相似度 {best_sim:.2f}，外部 {len(letters)} 项 / 对上 {len(picks)} 项）"
                f"→ 按未命中处理：{title[:34]}")
            return None

        logger.info(
            f"{self.name} 命中答案（相似度 {best_sim:.2f}）：{title[:36]} "
            f"-> {len(picks)} 项")
        # 返回**本题选项原文**，与 TikuAdapter 的返回形态保持一致
        return "\n".join(picks)


class TikuJszkk(Tiku):
    """「全能搜题」(study.jszkk.com) —— **免费网课题库，免 token**（2026-09-29 接入）。

    来源：扩大搜索时发现（开源项目 study.zmide.com，MIT）。
    接口：GET `https://study.jszkk.com/api/open/seek?q=<题干>`
    响应：`{"code":200,"data":{"content":"题目原文","answer":"答案"}}`
          —— 多选用 `#` 分隔；判断题给 `√` / `×`。

    实测（2026-09-29，40 道超星中文真题）：
      · 题干命中（相似度 ≥0.95）: **27/40 = 67.5%**
      · 答案能对上本题选项: **20/40 = 50%**
      · 其中与本地库一致 18 / 不一致 2 —— **而那 2 个都是它对**：
          ① 1971 年 7 月秘密访华的美国总统特使 → 它答"基辛格"（对），
             本地库是"卡特"（错）
          ② 1950 年工商业合理调整的重点关系 → 它答"公私关系"（对），
             本地库是"劳资关系"（错）
      → 本样本 **20/20 全对**，质量明显优于 OnlineCourseAPI
        （后者题干命中仅 17.5%、与库一致率仅 57%）。

    因为质量不低于 AI（91%）且**完全免费**，所以放在 **AI 之前**：
    能接住的题就不烧 token 了（用户明确在意消耗）。

    工程要点：
      · 它**不返回选项列表** → 只能靠「题干相似度 ≥0.95」+「答案文本能对上
        本题选项」两道闸，缺一不可
      · 会返回垃圾（"版本需要更新<a href=...>"）→ 必须过滤
      · 英文题覆盖差（子代理实测）→ 这层主要吃中文题
    """

    DISABLE = False
    _DEFAULT_URL = "https://study.jszkk.com/api/open/seek"
    _UA = TikuHive._UA
    _MIN_SIM = 0.95
    # 命中这些字样说明返回的是提示/报错而非答案
    _JUNK = ("版本需要更新", "<a href", "请先", "无法", "不存在", "登录",
             "充值", "过期", "http://", "https://")

    def __init__(self, config_path: Optional[str] = None) -> None:
        super().__init__(config_path)
        self.name = '全能搜题'

    def _init_tiku(self):
        try:
            self.api = (self._conf.get('jszkk_url') or '').strip() or self._DEFAULT_URL
        except Exception:
            self.api = self._DEFAULT_URL

    def _query(self, q_info: dict) -> Optional[str]:
        title = TikuHive._clean_title(q_info.get("title"))
        if len(title) < 6:
            return None
        try:
            res = requests.get(
                self.api,
                params={"q": title},
                headers={"User-Agent": self._UA},
                verify=False,
                timeout=(5, 12),
            )
        except requests.RequestException as e:
            logger.debug(f"{self.name} 请求异常（按未命中处理）: {e}")
            return None
        if res.status_code != 200:
            logger.debug(f"{self.name} 返回非 200: {res.status_code}")
            return None
        try:
            data = res.json().get("data") or {}
        except Exception as e:
            logger.debug(f"{self.name} 响应解析失败（按未命中处理）: {e}")
            return None

        content = str(data.get("content") or "").strip()
        answer = str(data.get("answer") or "").strip()
        if not content or not answer:
            return None
        if any(bad in answer for bad in self._JUNK):
            logger.info(f"{self.name} 返回的是提示而非答案，按未命中处理：{answer[:40]}")
            return None

        # ① 题干相似度闸门
        sim = difflib.SequenceMatcher(
            None, TikuHive._norm(content), TikuHive._norm(title)).ratio()
        if sim < self._MIN_SIM:
            logger.info(
                f"{self.name} 最高相似度仅 {sim:.2f}（需 ≥{self._MIN_SIM}）"
                f"→ 按未命中处理交下一个题库：{title[:34]}")
            return None

        qtype = q_info.get("type")
        # ② 判断题：√ / × → 平台认识的「正确 / 错误」
        if qtype == "judgement":
            a = TikuHive._norm(answer)
            if a in ("√", "对", "正确", "是", "t", "true", "1"):
                logger.info(f"{self.name} 命中判断题（相似度 {sim:.2f}）：{title[:32]} -> 正确")
                return "正确"
            if a in ("×", "x", "错", "错误", "否", "f", "false", "0"):
                logger.info(f"{self.name} 命中判断题（相似度 {sim:.2f}）：{title[:32]} -> 错误")
                return "错误"
            return None

        # ③ 单选/多选：答案文本 → 本题选项（只接受完整对应）
        mine = TikuHive._q_opts(q_info)
        if not mine:
            return None
        parts = {TikuHive._norm(x) for x in re.split(r"[#|]", answer) if x.strip()}
        picks = [o for o in mine if TikuHive._norm(o) in parts]
        if not picks:
            logger.info(
                f"{self.name} 命中题干但答案对不上本题选项（相似度 {sim:.2f}）"
                f"→ 按未命中处理：{title[:34]}")
            return None
        logger.info(
            f"{self.name} 命中答案（相似度 {sim:.2f}）：{title[:34]} -> {len(picks)} 项")
        return "\n".join(picks)


class AI(Tiku):
    # AI大模型答题实现
    # 支持读图的模型（2026-09-13 实测）：agnes-3.0-flash 可正确读图；
    # agnes-2.5-flash / 2.0-flash 返回空，故默认只信这一档。
    # 命中规则：模型名里含 "flash" 且含 "3.0"，或含 "vl"/"vision"/"gpt-4o"/
    # "claude-3"/"gemini" 等多模态关键字。可用 set_vision_enabled() 强制开关。
    _VISION_HINTS = ("3.0", "vl", "vision", "gpt-4o", "gpt-4.1", "claude-3",
                     "claude-4", "gemini", "qwen-vl", "glm-4v")

    # 🔴 2026-09-27（用户明确要求）：**一次性额度平台**的端点特征。
    #   这些平台的免费额度是「每个模型一份、用完即无」，所以**额度真耗尽**时
    #   必须永久弃用该槽位，绝不能像永久免费档那样记冷却又切回来
    #   （否则会反复撞同一个已耗尽的槽位，永远递补不到下一个模型）。
    #   与之相对：agnes（apihub.agnes-ai.cn）、智谱 GLM 免费档是**按窗口限流**，
    #   等一会儿会恢复，冷却复活是正确的。
    #
    # 🔴🔴 2026-09-27 二次修正（用户追问"额度没用完、只是网络不畅，会不会被
    #   当成用完了"）：原实现对一次性平台**一刀切**——只要落进"配额/限流"
    #   分支就永久弃用。后果：腾讯**只是 QPS/并发限流**回一个 429
    #   （额度其实还剩几十万）也会被永久丢掉，白扔一个满额度槽位。
    #   现在只对"**明确额度耗尽**"永久弃用；纯限流（rate limit / too many
    #   requests）给**有限次**冷却复活机会，反复失败才升级为永久。
    _ONESHOT_HOSTS = ("tokenhub.tencentmaas.com",)
    # 一次性平台"疑似限流"的冷却时长（比普通 45s 长，且逐次递增）
    _ONESHOT_COOLDOWN = 180.0
    # 一次性平台"疑似限流"最多复活几次；超过即永久弃用（防反复撞墙）
    _ONESHOT_MAX_REVIVE = 2
    # 🔴 2026-10-12 新增：**「本 Key 的该模型」额度耗尽**后的冷却时长（6 小时）。
    #   真机依据（`_probe_slots_now.py`，腾讯 TokenHub 39 槽位直连）：
    #     402 "The free trial quota for the service has been exhausted and
    #          postpaid billing is not enabled" —— **按模型**发放的试用额度。
    #     同一把 Key 换模型仍 200 可用（实测 31/39 槽位存活）。
    #   ⇒ 槽位签名 `key|endpoint|model` 里坏的只是**这一个组合**，
    #     永久弃用它等于白扔一个「额度重置后就能用」的模型。
    #   ⇒ 改长冷却：本场考试不再试它，下次会话自动复活。
    _ONESHOT_EXHAUSTED_COOLDOWN = 21600.0
    # 永久免费平台滑动窗口限流的冷却时长（类属性，`_init_tiku` 里也会覆盖一份）
    _COOLDOWN_SECONDS = 45.0

    # 🔴 2026-09-27 新增：**纯网络/传输层**故障的异常类型名。
    #   这些异常跟"Key 有没有废"完全无关，必须**先于任何额度判据**排除掉，
    #   否则一次网络抖动就可能被误判成"额度耗尽 → 永久弃用"。
    _NETWORK_ERR_NAMES = (
        'APITimeoutError', 'APIConnectionError',
        'ConnectError', 'ConnectTimeout', 'ConnectTimeoutError',
        'ReadTimeout', 'ReadTimeoutError', 'WriteTimeout', 'WriteError',
        'ReadError', 'PoolTimeout', 'RemoteProtocolError',
        'LocalProtocolError', 'ProxyError', 'SSLError', 'SSLEOFError',
        'NetworkError', 'NewConnectionError', 'MaxRetryError',
        'ProtocolError', 'ConnectionError', 'Timeout', 'TimeoutException',
        'ChunkedEncodingError', 'IncompleteRead', 'RemoteDisconnected',
        'ClosedResourceError', 'BrokenResourceError',
    )

    def __init__(self, config_path: Optional[str] = None) -> None:
        """初始化AI大模型答题实现."""
        super().__init__(config_path)
        self.name = 'AI大模型答题'
        self.last_request_time = None
        self._lock = threading.Lock()
        self.work_feedback = None  # 章节检测错误反馈（重做时参考）
        self._session = None       # 超星登录会话（用于下载题目配图）
        self._vision_override = None  # None=自动判断, True/False=强制

        # 🔴 2026-09-28 **AI 答案二次确认（多槽位投票）** ——
        #    起因：日志归因发现"仅由 AI 作答"的题错误率明显高于题库命中的题
        #    （得 10 分那次 3 题全走 AI、几乎全错）。用户要求做二次确认，
        #    并提出关键质疑"两个模型返回不一样怎么办" ⇒ 改成**奇数票多数决**，
        #    细节见 api/ai_vote.py 的模块 docstring。
        #    🔴 2026-09-29 **默认改为关闭**（用户拍板）：用真实回执量化后发现
        #    投票把正确率从 92.3% 提到 92.4%（169 题里只救回 1 题，+0.6pp，
        #    带偏 0 题），而多出来的 hy3 / hy4-preview / v4-pro 调用占了总
        #    token 的约 49% —— 属于纯白消费。要开启就配 `ai_vote = true`。
        #    量化脚本：`_analyze_vote_accuracy.py` / `_analyze_vote_net.py`。
        self._vote_enabled = False
        self._vote_stat = {"voted": 0, "high": 0, "majority": 0,
                           "merged": 0, "fallback": 0, "single": 0}

    def set_session(self, session) -> None:
        """注入超星登录会话，供下载带登录态的题目配图（2026-09-13）。"""
        self._session = session

    def set_vision_enabled(self, enabled: Optional[bool]) -> None:
        """强制开启/关闭读图（None 恢复自动判断）。"""
        self._vision_override = enabled

    def _vision_enabled(self) -> bool:
        """当前模型是否支持读图。"""
        if self._vision_override is not None:
            return bool(self._vision_override)
        return self._model_is_vision(self.model)

    @classmethod
    def _model_is_vision(cls, model: str) -> bool:
        """判断某个模型名是否支持读图（供多槽位选择视觉槽位复用）。"""
        m = (model or "").lower()
        if not m:
            return False
        if any(h in m for h in cls._VISION_HINTS):
            return True
        # 🔴 2026-09-26 23:35 补充：硅基流动实测可用的 VL 命名都带 `-vl-`
        #    （Qwen/Qwen3-VL-8B-Instruct 等），上面的 "vl" 已能覆盖；
        #    这里再兜一层常见变体，避免大小写/分隔符差异漏判。
        return any(k in m for k in ("-vl-", "vl-", "-v-", "omni"))

    def _vision_slot_idx(self) -> int:
        """找一个**支持视觉**的可用槽位下标；没有则返回 -1。

        用途（2026-09-26 新增）：图片题落在非视觉模型上时（例如
        Qwen2.5-7B），平台会直接回 `The model is not a VLM` —— 此时
        应自动切到池里支持视觉的槽位（如 Qwen3-VL-8B），而不是丢题。

        ⚠️ 判定"已在用的槽位"要比 **(Key, Model)**，不能只比 Key ——
        同一个 Key 可以配不同模型（文本用 7B、图片用 VL），
        只比 Key 会把唯一可用的视觉槽位误判成"就是当前这个"而跳过。
        """
        for i, s in enumerate(getattr(self, "_slots", []) or []):
            model = s.get("model") or self._conf.get("model") or ""
            key = s.get("key") or ""
            # 🔴 2026-09-27：`_key_dead` 已是**冷却表**且键为**槽位签名**，
            #    用 `_sig_alive` 判定（临时限流到点自动复活；同 Key 不同模型互不牵连）。
            if not key or not self._sig_alive(self._sig(s)):
                continue
            if not self._model_is_vision(model):
                continue
            # 已是当前生效的 (Key, Model) 组合 → 跳过（切了也没变）
            if key == self.key and model == self.model:
                continue
            return i
        return -1

    def set_work_feedback(self, feedback) -> None:
        """
        设置章节检测上一轮的错误反馈，供重新作答时参考。

        Args:
            feedback: str 或 list[str]，描述答错的题目与正确答案
        """
        self.work_feedback = feedback

    @staticmethod
    def _looks_like_listening(q_info: dict) -> bool:
        """判断是否听力题：与 answer_check.is_listening_question 保持同一判定。"""
        return is_listening_question(q_info)

    def _build_work_feedback_text(self) -> str:
        """将 work_feedback 转为提示词文本."""
        fb = self.work_feedback
        if not fb:
            return ""
        if isinstance(fb, str):
            return fb
        lines = [
            "你上一次作答的章节检测中有以下题目回答错误，"
            "请根据题目与正确答案仔细思考错因，纠正你的判断，本次作答务必保证每道题都正确："
        ]
        for item in fb:
            lines.append(str(item))
        return "\n".join(lines)

    def _is_deepseek_v4(self) -> bool:
        return (
                'api.deepseek.com' in (self.endpoint or '').lower()
                and (self.model or '').lower().startswith('deepseek-v4')
        )

    def _completion_kwargs(self, **kwargs):
        if self._is_deepseek_v4():
            # DeepSeek V4 defaults to thinking mode, which can leave message.content empty.
            kwargs['extra_body'] = {'thinking': {'type': 'disabled'}}
        return kwargs

    def _wait_for_interval(self):
        """🔴🔴🔴 2026-09-27 重写：**跨线程串行化**，把并发放大项消掉。

        🔴 实测结论（进程内探针，2026-09-27，**已确证**）：
            · **单 Key 串行连打，第 11 次起 429** ⇒ agnes 免费档是
              **「约 10 次/分钟」的滑动窗口限流**，**串行也照样触发**！
            · 429 后 **约 32 秒自动恢复**（+15s 仍 429、+32s 已放行）
            · 并发 4 → 全 200；并发 8 → 前 8 次 429
        ⇒ 并发**不是根因**，只是让窗口**更快打满**。
          降并发**解决不了**限流，唯一解是**多 Key 轮换**（见 `_key_dead` 冷却表）。

        那这个函数还有用吗？**有**：`last_request_time` 是**实例级单值**，
        多线程读到同一个旧值 → 间隔判断形同虚设 → 瞬时并发冲上 8+ →
        本来能撑 10 次的窗口**几秒就烧完**。加锁串行化让配额**按时间摊开**，
        配合 45s 冷却表，整体从"秒烧完"变成"稳定轮转"。

        修法：把"检查+更新 last_request_time"整段放进 `self._rate_lock`，
        配合 `time.sleep()` 让**同一时刻只有一个线程在真正发请求**。
        sleep 也在锁内 → 后续线程排队等待，天然把并发压到 1。
        """
        with self._rate_lock:
            if self.last_request_time:
                interval_time = time.time() - self.last_request_time
                if interval_time < self.min_interval_seconds:
                    sleep_time = self.min_interval_seconds - interval_time
                    logger.debug(f"API请求间隔过短, 等待 {sleep_time} 秒")
                    time.sleep(sleep_time)
            self.last_request_time = time.time()

    def _query_locked(self, q_info: dict, extra_hint: str = ""):
        """单题问答。

        `extra_hint`（2026-09-28）：追加到 system prompt 末尾的额外说明。
        目前只被**投票的第 3 票（仲裁票）**使用 —— 把前两票的分歧告诉模型，
        要它重新审题并判断哪个对（提高平局收敛率，减少"三票全不同"）。
        """
        def remove_md_json_wrapper(md_str):
            # 使用正则表达式匹配Markdown代码块并提取内容
            pattern = r'^\s*```(?:json)?\s*(.*?)\s*```\s*$'
            match = re.search(pattern, md_str, re.DOTALL)
            return match.group(1).strip() if match else md_str.strip()

        # 🔴 2026-09-26 23:05：client 改为在 `_call()` 里**按当前 Key 现建**
        #    （多 Key 轮换需要每次重建；这里不再预先构造）
        # 去除选项字母，防止大模型直接输出字母而非内容
        # 🔴 2026-09-27 修 bug：考试的 `q_info['options']` 是 **list（且可能为空）**，
        #    此前无条件当字符串 `.split('\n')` → `AttributeError: 'list' object has
        #    no attribute 'split'`，导致**考试每一题的 AI 兜底全部异常失败**
        #    （作业路径传的是字符串，所以这个 bug 一直没暴露；今天第一次真跑考试才现形）。
        _raw_options = q_info.get('options')
        options_list = []
        if isinstance(_raw_options, (list, tuple)):
            # 考试链路传的是 list[dict]：{letter(显示字母), value(提交值), text(正文)}
            # → 拼成 AI 能读的「A. 圣诞节」
            for _o in _raw_options:
                if isinstance(_o, dict):
                    _L = str(_o.get('letter') or _o.get('value') or '').strip()
                    _T = str(_o.get('text') or '').strip()
                    if _L or _T:
                        options_list.append(f"{_L}. {_T}" if _T else _L)
                elif str(_o).strip():
                    options_list.append(str(_o))
        elif isinstance(_raw_options, str):
            options_list = _raw_options.split('\n')
        options_list = [x for x in options_list if str(x).strip()]
        cleaned_options = [re.sub(r"^[A-Z]\s*", "", option) for option in options_list]
        options = "\n".join(cleaned_options)

        # 上一轮章节检测的错误反馈（若有）
        feedback_text = self._build_work_feedback_text()

        # 学习库负反馈（跨会话/跨账号）：该题历史上被平台判错的答案，
        # 指导模型排除错误项、重新独立分析（2026-09-11 错题学习机制）。
        # 注意 q_info['title'] 此时已被 query_all 规范化（去序号），与学习库键一致。
        try:
            _wrong = LearnedAnswers().get_wrong(q_info.get('title', ''))
        except Exception:
            _wrong = []
        if _wrong:
            _wrong_text = (
                "注意：本题历史上曾被作答为 " + "、".join(_wrong) +
                "，均已被平台判定为错误（该判定可信）。请排除上述答案，重新独立分析，"
                "给出一个不同的答案。"
            )
            feedback_text = (feedback_text + "\n" + _wrong_text) if feedback_text else _wrong_text

        # 🔴 2026-09-29 新增（用户要求）：「部分正确」线索 —— 主要针对多选题。
        # 平台判 bandui 时**不会告诉你哪个选项对**，但"得了部分分"本身就有信息量：
        # 所选选项里至少有一个是正确的，只是组合不完整。把这条线索交给模型，
        # 让它"在已有基础上补全"，而不是每轮从零重新猜（实测多选题是最大失分项）。
        _qtype_s = str((q_info or {}).get('type') or '')
        _is_multi = ('多选' in str((q_info or {}).get('title') or '')
                     or _qtype_s in ('multiple', '3'))
        if _is_multi:
            try:
                _parts = LearnedAnswers().get_partials(q_info.get('title', ''))
            except Exception:
                _parts = []
            if _parts:
                _lines, _cand = [], set()
                for _p in _parts[:4]:
                    _a = str(_p.get('ans') or '').strip()
                    _s = _p.get('score')
                    _sc = ("得 %.1f 分" % _s) if isinstance(_s, (int, float)) else "获得部分分"
                    _lines.append("答「%s」-> %s" % (_a, _sc))
                    if isinstance(_s, (int, float)) and _s > 0:
                        _cand |= set(re.findall(r"[A-Za-z]", _a))
                _cand_txt = ((" 由此可推断 " + "、".join(sorted(_cand)) +
                              " 之中**至少有一个选项确实是对的**。") if _cand else "")
                _partial_text = (
                    "【多选题线索】本题你之前作答过：" + "；".join(_lines) +
                    "。平台判定为**部分正确** —— 这说明所选选项里**至少有一个是正确的**，"
                    "但整体组合不完整或含有错误项。" + _cand_txt +
                    " 请**在已有基础上补全**，给出完整正确答案；"
                    "正确选项的数量可能多于你上次所选。不要重复提交上面这些已知不通过的组合。"
                )
                feedback_text = ((feedback_text + "\n" + _partial_text)
                                 if feedback_text else _partial_text)

        # 已确认正确的小问答案（2026-09-11 用户要求）：把"平台已经判对的小问"告诉
        # 模型，明确要求**原样保留**、只重新分析其余小问。否则模型会把已确认的
        # 小问也一起改掉 —— 即便提交前有 _apply_verified_subs 兜底校正，
        # 也白丢了一次有效信息、且可能把整组带回已判错的组合。
        try:
            _subs_known = LearnedAnswers().get_sub_verified(q_info.get('title', ''))
        except Exception:
            _subs_known = {}
        if _subs_known:
            _n = _sub_count(q_info)
            _items = "、".join(
                f"第{k + 1}小问={_subs_known[str(k)]}"
                for k in sorted(int(x) for x in _subs_known if str(x).isdigit()))
            _sub_text = (
                f"重要：本题组共 {_n} 个小问，需按小问顺序连写作答。"
                f"其中 {_items} 已由平台确认正确，**必须原样保留、不得改动**。"
                f"你只需重新分析剩余 {max(0, _n - len(_subs_known))} 个小问，"
                f"最后按小问顺序输出完整的连写答案（长度必须为 {_n}，例如 4 个小问"
                f"就输出 4 个字母）。"
            )
            feedback_text = (feedback_text + "\n" + _sub_text) if feedback_text else _sub_text

        # 课程上下文（由 base.study_work 注入到 q_info["_course"]）：给模型领域定位
        # 与术语要求。实测同一批 SketchUp 填空题：无课程背景 0/5 → 有课程背景 2/5。
        # 题库全没命中时全靠 AI 兜底，它若不知道这是哪门课，只能拿通用知识硬凑，
        # 专业术语极易答偏（如把 SketchUp 的"反转平面"答成"反转法线"）。
        _course = str((q_info or {}).get("_course") or "").strip()
        _ctx = (f"本题出自《{_course}》课程的章节测验。"
                "请使用该学科的标准术语（软件类课程用该软件官方中文界面的原话）作答，"
                "答案保持简短（通常 1-6 个字），不要写解释。\n") if _course else ""

        def _make_messages(system_content: str, user_content: str,
                           images: Optional[list] = None) -> list:
            """构造带反馈上下文的消息列表.

            images（2026-09-13 新增）：题目配图的 data URL 列表。非空时
            user 消息改用多模态 content 数组（text + image_url 交替），
            视觉模型即可"看到"题干里的图片。
            """
            messages = [
                {
                    "role": "system",
                    "content": system_content
                },
            ]
            if feedback_text:
                messages.append(
                    {
                        "role": "system",
                        "content": feedback_text
                    }
                )
            if images:
                content = [{"type": "text", "text": user_content}]
                for _img in images:
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": _img},
                    })
                messages.append({"role": "user", "content": content})
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": user_content
                    }
                )
            return messages

        def _make_client():
            """按**当前** self.key 建 client（多 Key 轮换时必须每次重建）。

            🔴 2026-09-27 加 timeout：OpenAI SDK 默认超时 600 秒，上游一旦挂起
            （真机实测硅基流动挂了 8 分钟不返回）会堵死整个作业线程。90 秒覆盖
            实测最慢的视觉模型（读图 ~14s）+ 限流重试余量。
            """
            if self.http_proxy:
                return OpenAI(http_client=httpx.Client(proxy=self.http_proxy, timeout=20),
                              base_url=self.endpoint, api_key=self.key, timeout=20)
            return OpenAI(base_url=self.endpoint, api_key=self.key, timeout=20)

        def _call(system_content: str, user_content: str):
            """发一次问答请求（统一入口）。

            带图失败时自动去掉图片重试一次（2026-09-13 加固）：
            模型名里带视觉关键字、但实际后端不支持图片时（例如 agnes 的
            图片能力未开通），带图请求会直接 400 —— 若不放行就会让整道题
            拿不到答案。去图重试保证"至少能按纯文本作答"，与用户要求的
            "自动降级"一致。

            🔴🔴🔴 2026-09-26 23:05 **多 Key 轮换（新增）**：
            遇到 429 / 额度用尽 → 把当前 Key 标记 dead → 换下一个 Key 重试。
            最多轮遍整个 Key 池。这样"一个 Key 免费额度用尽"不再导致整份
            作业大面积丢题（真机踩坑：单 Key 429 → 29/100 题无答案）。
            """
            last_err = None
            tried = 0
            # 仲裁票的额外说明（本函数是闭包，直接读外层的 extra_hint）。
            # 拼到 system prompt 末尾 —— 所有题型分支与"去图重试"都自动带上。
            _sys_extra = ("\n\n" + extra_hint.strip()) if (extra_hint or "").strip() else ""
            # 🔴 2026-09-27：按**槽位**计数（同一把 Key 可配多个模型，
            #    只数去重后的 Key 会漏掉槽位 → 提前放弃重试）
            total_keys = max(1, len(self._slots))
            while tried < total_keys:
                tried += 1
                client = _make_client()          # 用当前 Key 重建
                try:
                    _ok = client.chat.completions.create(**self._completion_kwargs(
                        model=self.model,
                        messages=_make_messages(_ctx + system_content + _sys_extra,
                                                user_content,
                                                images=_images)))
                    # 🔴 2026-09-27：成功即清零该槽位的"限流复活计数"——
                    #    限流是一次性事件，稳定工作过就不该再因为历史计数被永久弃用。
                    try:
                        self._revive_count.pop(self._cur_sig(), None)
                    except Exception:
                        pass
                    return _ok
                except Exception as e:
                    # ⓪ 🔴 2026-09-26 23:35 图片题专用修复：
                    #    当前模型不是视觉模型（平台回 "not a VLM"）→ **不要**
                    #    直接把图丢掉降级成纯文本（那样等于瞎猜，必错），
                    #    而是切到池里支持视觉的槽位重试。
                    #    真机踩坑：Qwen2.5-7B 收到图直接 400 not a VLM。
                    if _images and self._is_vlm_error(e):
                        _vi = self._vision_slot_idx()
                        if _vi >= 0:
                            logger.warning(
                                f"⚠ 当前模型 {self.model} 不支持读图，"
                                f"自动切到视觉槽位重试本题")
                            self._apply_slot(_vi, announce=True)
                            self._key_idx = _vi + 1
                            continue
                        logger.warning(
                            f"⚠ 当前模型 {self.model} 不支持读图，"
                            f"且池内无可用视觉模型 → 退化为纯文本作答")
                        _images.clear()
                        continue

                    # ① 带图失败 → 去图重试一次（同 Key）
                    if _images:
                        logger.warning(
                            f"带图请求失败（{type(e).__name__}: {e}），"
                            f"已自动降级为纯文本重试")
                        try:
                            return client.chat.completions.create(
                                **self._completion_kwargs(
                                    model=self.model,
                                    messages=_make_messages(
                                        _ctx + system_content + _sys_extra,
                                        user_content)))
                        except Exception as e2:
                            e = e2
                            _images.clear()      # 去图后仍失败 → 后续不再带图

                    # ② 额度/限流/鉴权类 → 标记该槽位死亡并切下一个（Key+Endpoint+Model 整组）
                    if self._is_quota_error(e) and len(self._slots) > 1:
                        dead = self._cur_sig()      # ⚠️ 槽位签名，不是 Key 字符串
                        # 🔴 2026-09-27 修：区分**临时限流**与**永久废号**。
                        #    临时（agnes 免费档 429）→ 记 45s 冷却，到期自动复活；
                        #    永久（欠费/Key 无效）→ 记 +inf 真正弃用。
                        #    原实现一律永久拉黑 → 长作业中段全部 Key 阵亡。
                        if dead not in self._key_dead:
                            # 🔴 2026-09-27（用户明确要求）：**一次性额度平台**
                            #    （腾讯 TokenHub）额度用完就真没了 → 永久弃用、
                            #    不冷却复活（否则反复撞墙、永远递补不到下一个模型）。
                            #
                            # 🔴🔴 2026-09-27 二次修正（用户追问"额度没用完、
                            #    只是网络不畅会不会被当成用完"）：
                            #    原实现在这个分支里对一次性平台**一刀切永久弃用**，
                            #    于是腾讯**仅因 QPS/并发限流**回一个 429（额度还
                            #    剩很多）也会被永久丢掉。现在细分三类：
                            #      ① 额度/余额真耗尽（quota / 余额不足 / 用完…）
                            #         → 永久弃用（用户要求的语义）
                            #      ② 纯限流（rate limit / too many requests）
                            #         → 冷却（腾讯 180s/360s，普通 45s）后复活，
                            #           但腾讯侧**复活次数有上限**，反复限流即永久
                            #      ③ Key 无效/无权 → 永久弃用（与额度无关）
                            #    ⚠️ 纯网络故障（超时/断连/DNS/SSL/代理）**根本
                            #    进不来这里** —— `_is_quota_error` 已把它们排除。
                            #    决策逻辑抽到 `_decide_dead_action`（纯函数，可单测）。
                            _act, _cool, _why2, _bump = self._decide_dead_action(
                                self.endpoint or "", e,
                                self._revive_count.get(dead, 0))
                            if _act == 'none':
                                # 网络抖动：不处置槽位，原样抛出（上层走降级）
                                raise e
                            if _act == 'permanent':
                                self._key_dead[dead] = float('inf')
                                # ⚠️ 2026-10-12：`len(self._key_dead)` 含冷却中的
                                #   槽位，写成"个已耗尽"会把「暂时冷却」也说成废掉。
                                #   这里只数真正 +inf 的。
                                _n_perm = sum(
                                    1 for v in self._key_dead.values()
                                    if v == float('inf'))
                                logger.warning(
                                    f"🔑 AI 槽位（{self._sig_masked(dead)}）不可用"
                                    f"（{_why2}），已永久弃用、不再切回"
                                    f"（{_n_perm}/{total_keys} 个已废）")
                            else:
                                if _bump:
                                    self._revive_count[dead] = \
                                        self._revive_count.get(dead, 0) + 1
                                self._key_dead[dead] = time.time() + _cool
                                _n_trans = sum(
                                    1 for v in self._key_dead.values()
                                    if v != float('inf'))
                                logger.warning(
                                    f"🔑 AI 槽位（{self._sig_masked(dead)}）触发"
                                    f"{_why2}（{self._sig_cooldown_left(dead):.0f}s "
                                    f"后自动恢复）｜当前冷却中 "
                                    f"{_n_trans}/{total_keys}")
                        # 从**当前槽位**的下一个开始找
                        cur = self._slot_of_sig(dead)
                        self._key_idx = (cur + 1) if cur >= 0 else self._key_idx
                        self._next_key()
                        # `_next_key()` 已把「找到的可用槽位」应用到生效值；
                        # 若一个都没找到则不切换 → 此时当前槽位仍是刚判死的那个。
                        # 因此这里判「当前生效槽位」是否真的可用。
                        if not self._sig_alive(self._cur_sig()):
                            # 全部冷却中 → 睡到最早解冻的那一刻再试一次
                            _wait = self._min_cooldown_left()
                            if _wait > 0 and tried < total_keys:
                                logger.warning(
                                    f"⏳ 全部 {total_keys} 个槽位均在冷却，"
                                    f"等待 {_wait:.0f}s 后重试")
                                time.sleep(min(_wait, 60.0))
                                continue
                            last_err = e
                            break
                        logger.info(
                            f"🔁 换用下一个 AI 槽位重试本题"
                            f"（{self._sig_masked(self._cur_sig())}）")
                        continue

                    # ③ 其他异常 → 原样抛出
                    raise
            if last_err is not None:
                # 🔴 2026-09-26 23:40 修复：**所有 Key 都死了**是终态，
                # 必须抛出可被上层识别的"账号级/Key 级"异常，让
                # TikuFallback 回退到下一个题库（如 TikuAdapter），
                # 而不是把原始 SDK 异常（401/429）抛穿整条作业链 ——
                # 真机踩坑：假 Key 401 直接炸穿 study_work_homework，
                # 整份 100 题作业中止（而非逐题降级）。
                raise AIKeyExhaustedError(
                    f"AI 全部 {total_keys} 个 Key 均不可用：{last_err}") from last_err
            raise RuntimeError("AI 请求失败：所有 Key 均不可用")

        # 判断题目类型
        self._wait_for_interval()
        self.last_request_time = time.time()

        # 题目配图（2026-09-13 新增）：题干含 <img> 时，带登录态下载并转
        # data URL，随题干一起提交给视觉模型。非视觉模型 / 下载失败时
        # images 为空列表，链路完全退回原来的纯文本行为。
        _images = []
        _title_text = q_info['title']
        if self._vision_enabled():
            try:
                _imgs, _stripped = build_image_data_urls(
                    self._session, q_info['title'])
                if _imgs:
                    _images = _imgs
                    # 图片已随消息发出，题干里的 URL 文本没有意义，去掉更干净
                    _title_text = _stripped
            except Exception as _e:
                logger.warning(f"题目配图处理失败，按纯文本作答：{_e}")

        # 听力题：题干只有选项、没有音频原文，模型看不到听力材料。
        # 不特别说明时极易退化成"全部选同一个字母"，这里改用专门的解题策略。
        if self._looks_like_listening(q_info) and q_info['type'] in ('single', 'multiple'):
            completion = _call(
                    "这是一道英语听力选择题，通常包含多个小问（如 Questions 19 to 21）。"
                    "你无法听到音频，题干中也不包含听力原文，必须基于以下线索作答：\n"
                    "1. 选项之间的语义差异与常识逻辑；\n"
                    "2. 四级听力常见规律（正确项多是对原文的转述；含 only/never/all/"
                    "must 等绝对化措辞的选项通常错误）；\n"
                    "3. 每个小问必须独立判断，禁止将所有小问都选同一个字母。\n"
                    "输出要求：按小问顺序给出每个小问所选选项的字母，去掉题号后连写，"
                    "例如第一个小问选 C、第二个选 A、第三个选 D，就输出 {\"Answer\": [\"CAD\"]}。"
                    "只输出这一串字母，不要输出选项内容、题号、空格或任何解释，"
                    "也不要使用MD语法。",
                    f"题目：{_title_text}\n选项：{options}"
            )
        elif q_info['type'] == "single":
            completion = _call(
                    "本题为单选题，你只能选择一个选项，请根据题目和选项回答问题，以json格式输出正确的选项内容，示例回答：{\"Answer\": [\"答案\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料",
                    f"题目：{_title_text}\n选项：{options}"
            )
        elif q_info['type'] == 'multiple':
            completion = _call(
                    "本题为多选题，你必须选择两个或以上选项，请根据题目和选项回答问题，以json格式输出正确的选项内容，示例回答：{\"Answer\": [\"答案1\",\n\"答案2\",\n\"答案3\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料",
                    f"题目：{_title_text}\n选项：{options}"
            )
        elif q_info['type'] == 'completion':
            _blank_n = int(q_info.get('blankCount') or 0)
            _blank_hint = ""
            if _blank_n > 1:
                # 多空题（2026-09-13 新增，同日实测后强化）：
                # 平台按空分字段提交，必须给出与空数等量的答案且顺序对应。
                # 实测 agnes-3.0-flash 常把多个空的答案**拼成一个字符串**
                # 放进数组唯一元素（如 ["it can only be applied to ...it produces ..."]），
                # 导致其余空全空 → 判错。因此在提示词里把要求写到最死。
                _blank_hint = (
                    f'\n⚠ 本题有 {_blank_n} 个空格（按题干里下划线的先后顺序）。'
                    f'必须输出 {_blank_n} 个**互相独立**的元素，格式严格为：'
                    f'{{"Answer": ["第1空答案", "第2空答案", ...]}}。\n'
                    f'硬性要求：数组长度必须正好等于 {_blank_n}；'
                    f'每个元素只放对应那一个空的内容；'
                    f'**绝对不要**把两个空的答案拼接在同一个元素里；'
                    f'不要用换行或任何分隔符把它们合起来。'
                )
            completion = _call(
                    "本题为填空题，你必须根据语境和相关知识填入合适的内容，请根据题目回答问题，以json格式输出正确的答案，示例回答：{\"Answer\": [\"答案\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
                    + _blank_hint,
                    f"题目：{_title_text}"
            )
        elif q_info['type'] == 'judgement':
            completion = _call(
                    "本题为判断题，你只能回答正确或者错误，请根据题目回答问题，以json格式输出正确的答案，示例回答：{\"Answer\": [\"正确\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料",
                    f"题目：{_title_text}"
            )
        elif q_info['type'] == 'unknown':
            # 未知题型（2026-09-13）：超星支持 18 种题型，本程序只映射了
            # 0-4 与 19，其余（排序题、完型填空、名词解释、论述、计算、
            # 分录题、资料题…）都落到这里。用通用提示词让模型自己判断
            # 该给字母还是给文字 —— 比硬套"简答题"提示词准确。
            completion = _call(
                    "本题题型未能自动识别，可能是排序题、完型填空、名词解释、"
                    "论述题、计算题或资料题中的一种。请根据题干（和选项）判断"
                    "最合理的作答形式，以json格式输出答案，示例回答："
                    "{\"Answer\": [\"答案\"]}。若题目要求选择或排序，只输出对应的"
                    "选项字母（如 \"ACBD\"，按最终顺序连写）；若要求文字作答，"
                    "给出简洁准确的内容。除此之外不要输出任何多余的内容，"
                    "也不要使用MD语法。如果你使用了互联网搜索，也请不要返回"
                    "搜索的结果和参考资料",
                    f"题目：{_title_text}\n选项：{options}" if options else f"题目：{_title_text}"
            )
        else:
            completion = _call(
                    "本题为简答题，你必须根据语境和相关知识填入合适的内容，请根据题目回答问题，以json格式输出正确的答案，示例回答：{\"Answer\": [\"这是我的答案\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料",
                    f"题目：{_title_text}"
            )

        try:
            response = json.loads(remove_md_json_wrapper(completion.choices[0].message.content))
            sep = "\n"
            return sep.join(response['Answer']).strip()
        except:
            logger.error("无法解析大模型输出内容")
            return None

    # ══ 🗳 AI 答案二次确认（2026-09-28）══════════════════════════════
    #
    # 背景：日志归因显示「**仅由 AI 作答**」的题错误率明显高于题库命中的题
    #      （某次测验 3 题全走 AI → 只拿了 10 分）。
    # 做法：**奇数票多数决**。用户提的关键质疑是"两个模型答案不一样怎么办" ——
    #      2 票（偶数）平票时信息量为零，所以用 3 票：必有严格多数。
    #      票必须来自**不同槽位**（不同模型），同一模型重问等于没投票。
    # 平票怎么裁决、多选为什么"争议选项倾向不选"：见 api/ai_vote.py 的 docstring。

    # 允许投票的题型（2026-10-07 收敛）。判断题净亏 11 题、填空题答案形态
    # 不唯一，都不投；只保留"答案可穷举成选项字母"的单选与多选。
    _VOTE_TYPES = frozenset({"single", "multiple"})

    def _vote_applicable(self, q_info: dict) -> bool:
        """本题是否值得投票（按题型）。

        依据 `_analyze_vote_by_type.py` 的全量拆账：
        judgement 净亏 11 题、single 持平、completion 无意义。
        """
        return str(q_info.get("type") or "") in self._VOTE_TYPES

    def _vote_candidates(self) -> list:
        """挑可用于投票的槽位，**优先"独立的第二意见"**。

        排序（越靠前越先被用来投票）：
          1. 不同模型、且不是明显的小参数量模型（→ 真正的独立判断）
          2. 不同模型但是小模型（如 4B/7B、mini/tiny）
          3. 同模型但不同 Key（同源采样，价值最低，作为最后手段）

        排除冷却中/永久弃用的槽位 —— 它们发出去必然失败，白等一轮。
        """
        cur_model = self.model
        out = []
        for i, s in enumerate(self._slots or []):
            sig = self._sig(s)
            if not self._sig_alive(sig):
                continue
            md = s.get("model") or self._conf.get("model") or ""
            out.append({
                "idx": i, "sig": sig, "tag": self._sig_masked(sig),
                "rank": (0 if md != cur_model else 2)
                        + (1 if ai_vote.is_weak_model(md) else 0),
            })
        out.sort(key=lambda x: x["rank"])
        return out

    # 投票时最多尝试几个候选槽位（前几个都失败就不硬撑，避免整题卡住）
    _VOTE_MAX_TRY = 4

    def _to_uniform(self, q_info: dict, raw):
        """把 AI 原始输出统一成**可比较形态**再投票。

        必要性：提示词要求"输出选项内容"，但模型有时给内容、有时给字母。
        不统一的话，两票会因为**表述形态不同**被判成"分歧"（假平票），
        白白触发第三次请求甚至走兜底。这里用的映射函数与后续真正提交时
        用的是同一套（`map_single/map_multiple_answer`），所以投出来的票
        就是最终要提交的形态。
        """
        if raw is None:
            return ""
        t = str(raw).strip()
        if not t:
            return ""
        q_type = q_info.get("type")
        if q_type in ("single", "multiple"):
            try:
                from api.base import map_single_answer, map_multiple_answer
                opts = q_info.get("options", "")
                if q_type == "multiple":
                    return map_multiple_answer(t, opts) or ""
                return map_single_answer(t, opts) or ""
            except Exception as e:
                logger.debug(f"投票前「选项内容→字母」映射失败：{e}")
                return ""
        # 判断题保留原文（正确/对/false…），比较时有 ai_vote.judge_norm 归一；
        # 保留原文能保证最终提交的表述与模型输出一致
        # （平台的 true_list/false_list 两种写法都认）。
        return t

    @staticmethod
    def _arbitration_hint(votes, q_type) -> str:
        """第 3 票（**仲裁票**）的额外说明：把前两票的分歧摆出来。

        为什么不能让第 3 票也"盲答"：
        它的**唯一职责就是打破平局**。若三票彼此独立，在 4 选项的单选题上
        "三票全不同"的概率并不低 —— 那结果又变成抛硬币（只能退回主槽位）。
        把分歧明确告诉它，能大幅提高它落到前两票之一的概率
        （= 真正把平局解成了 2:1）。

        判断题只有两个选项，三票必然 2:1，用不上这条；
        多选题走逐选项裁决，也不需要。
        """
        if q_type != "single":
            return ""
        picks = [a for _, a in votes[:2]]
        if len(picks) < 2 or picks[0] == picks[1]:
            return ""
        return (f"注意：本题此前已由另外两个模型独立作答，它们给出了**不同**的答案："
                f"{picks[0]} 与 {picks[1]}。请你重新仔细审题，"
                f"独立判断其中哪一个才是正确的；"
                f"如果你认为两个都不对，也可以给出你自己的答案。")

    def _vote_query(self, q_info: dict, first):
        """已拿到第 1 票，追加第 2/3 票并裁决（返回最终答案）。"""
        q_type = str(q_info.get("type") or "unknown")

        # 自由文本题型不投票：两个模型答"同义不同字"的句子会被判成分歧，
        # 强行投票只会把第 1 票的答案换掉，得不偿失。
        if q_type in ("completion", "shortanswer"):
            return first

        # 图片题不投票：池内视觉槽位可能唯一，换槽位会切到非视觉模型
        # （报 not a VLM 后退化成纯文本作答）→ 两票不同源、比较无意义；
        # 且每票都要重新下载题干配图，代价高。
        if self._vision_enabled():
            try:
                _imgs, _ = build_image_data_urls(self._session,
                                                 q_info.get("title", ""))
                if _imgs:
                    return first
            except Exception:
                pass

        base = self._to_uniform(q_info, first)
        if not base:
            return first          # 第 1 票转不出统一形态 → 没有可比对的基础

        votes = [(self._sig_masked(self._cur_sig()), base)]
        used = {self._cur_sig()}
        # 投票期间会切走槽位，结束后**必须原样恢复**，否则影响后续所有题目
        saved = (self.key, self.endpoint, self.model,
                 self._key_idx, self._model_idx, list(self._model_chain))

        def _restore():
            (self.key, self.endpoint, self.model, self._key_idx,
             self._model_idx, self._model_chain) = (
                saved[0], saved[1], saved[2], saved[3], saved[4], list(saved[5]))

        try:
            _tried = 0
            for cand in self._vote_candidates():
                # **票数上限 3**：2 票一致就停；不一致才追加第 3 票。
                # （奇数票保证必有严格多数，不会像 2 票那样平票无解。）
                if len(votes) >= 3 or _tried >= self._VOTE_MAX_TRY:
                    break
                if cand["sig"] in used:
                    continue
                _tried += 1
                # 第 3 票 = 仲裁票：带上前两票的分歧，别让它盲答
                hint = self._arbitration_hint(votes, q_type) if len(votes) >= 2 else ""
                try:
                    self._apply_slot(cand["idx"], announce=False)
                    # 只在真有仲裁说明时才传 extra_hint —— 保持"无 hint 走原调用"
                    # 的路径不变，避免任何覆盖了 `_query_locked` 的实现被签名变更打坏
                    raw = (self._query_locked(q_info, extra_hint=hint) if hint
                           else self._query_locked(q_info))
                except AIKeyExhaustedError:
                    break                      # 池子耗尽 → 用手上的票裁决
                except Exception as e:
                    logger.debug(f"投票请求失败，跳过该槽位："
                                 f"{type(e).__name__}: {e}")
                    continue
                finally:
                    # 每次投票后立刻恢复：`_call` 内部失败时会自行切槽位，
                    # 不还原就会把下一次投票的起点带偏
                    _restore()
                ans = self._to_uniform(q_info, raw)
                if not ans:
                    continue
                used.add(cand["sig"])
                votes.append((cand["tag"], ans))
                if ai_vote.same(ans, base, q_type):
                    break                      # 已一致 → 省掉第三次请求
        finally:
            _restore()

        res = ai_vote.decide(votes, q_type)
        conf = res.get("confidence") or "single"
        self._vote_stat["voted"] = self._vote_stat.get("voted", 0) + 1
        self._vote_stat[conf] = self._vote_stat.get(conf, 0) + 1

        tags = "、".join(t for t, _ in votes)
        _tail = f"| 原标题：{str(q_info.get('title') or '')[:40]} | 票源：{tags}"
        if conf in ("high", "majority", "merged"):
            logger.info(f"🗳 AI 二次确认[{conf}] {res.get('detail')} {_tail}")
        else:
            # 兜底说明这题的 AI 答案本身不可靠，值得在日志里醒目一点
            logger.warning(f"🗳 AI 二次确认[{conf}] {res.get('detail')} {_tail}")

        # 留痕（jsonl）：供事后核对"投票到底有没有让正确率变好"
        try:
            ai_vote.log_vote(data_dir(), {
                "title": str(q_info.get("title") or "")[:120],
                "type": q_type,
                "votes": [{"tag": t, "ans": a} for t, a in votes],
                "answer": res.get("answer"),
                "confidence": conf,
                "detail": res.get("detail"),
            })
        except Exception:
            pass

        return res.get("answer") or first

    def _query(self, q_info: dict):
        with self._lock:
            try:
                first = self._query_locked(q_info)
            except AIKeyExhaustedError:
                # 🔴 2026-09-26 23:40：Key 池全废 → **绝不能换模型重试**
                # （同一个死 Key，换模型必然再次 401/429，白打一轮请求且
                # 会让日志看起来像"模型有问题"）。直接抛出去，让
                # TikuFallback 回退到下一个题库（如 TikuAdapter）。
                raise
            except Exception as e:
                # 模型级故障（模型下线/无权限/余额不足）自动降级重试一次
                # （2026-09-13，用户明确要求"到时候自动降级"）。
                # 只对"该模型不可用"类错误降级；网络抖动/限流不降级 ——
                # 换模型解决不了限流，反而会白丢一个可用的档位。
                if self._is_model_fatal(e) and self._switch_to_fallback():
                    logger.warning(f"已切换到备用模型重试本题：{e}")
                    first = self._query_locked(q_info)
                else:
                    raise
            # 🗳 二次确认（2026-09-28）：能走到这里说明**题库/学习库都没命中**，
            #    正是"仅由 AI 作答"、错误率最高的那批题，值得多花 1–2 次请求。
            # 🔴 2026-10-07 **按题型收敛**（用户拍板，有实测数据支撑）：
            #    全量投票跑 1043 次后按题型拆账，结论是题型差异极大：
            #      judgement 647 票 → 救回 3 / 带偏 14 = **净亏 11 题**
            #      single    247 票 → 救回 1 / 带偏  1 = 净持平
            #    判断题只有"对/错"两种取值，多模型投票等于互相拆票：
            #    本已 50% 起步，投出来的多数决反而频繁偏离正确项。
            #    填空题（completion）答案形态不唯一（同义表述、标点、字序），
            #    多票几乎必然"表述不同→判成分歧"，投票在此无意义。
            #    ⇒ 只对 **单选 / 多选** 投票；判断、填空、未知一律不投。
            #    量化脚本：_analyze_vote_by_type.py
            if self._vote_enabled and first and self._vote_applicable(q_info):
                try:
                    first = self._vote_query(q_info, first)
                except AIKeyExhaustedError:
                    pass                # 投票途中池子耗尽 → 保留第 1 票，不丢题
                except Exception as e:
                    logger.warning(f"AI 二次确认异常，本次改用第 1 票："
                                   f"{type(e).__name__}: {e}")
            # 🔴 2026-09-29：**答案可用性自检 + 换模型重答**（详见方法注释）。
            # 放在最后：投票/降级都做完后，再确认"这个答案平台认不认"。
            return self._ensure_usable(q_info, first)

    # ---- 答案可用性自检（2026-09-29，用户明确要求）----
    _RETRY_HINT = (
        "⚠️ 你上一次的回答**无法对应本题的选项**，系统识别不了。请重新作答：\n"
        "1) 必须从给定选项中作答，**逐字照抄选项原文**，禁止翻译、改写、概括、意译；\n"
        "2) 题目与选项若是英文，必须原样输出英文，**禁止翻译成中文**；\n"
        "3) 单选：给一个字母或一项原文；多选：给全部正确项（字母连写，或每项一行）。"
    )

    def _answer_usable(self, q_info: dict, ans) -> bool:
        """这个答案平台认不认？单选/多选必须能落到**选项字母**上。

        （不能只用 `plausible_answer` —— 它对"中文文本"也放行，而平台
          单选/多选只认字母，文字交上去必判错。）
        """
        a = str(ans or "").strip()
        if not a:
            return False
        t = q_info.get("type")
        if t in ("single", "multiple"):
            if re.fullmatch(r"[A-Za-z]{1,8}", a):
                return True
            try:
                from api.base import map_single_answer, map_multiple_answer
                opts = q_info.get("options") or ""
                if isinstance(opts, (list, tuple)):
                    opts = "\n".join(
                        (str(o.get("text") or o.get("value") or "") if isinstance(o, dict)
                         else str(o)) for o in opts if o)
                fn = map_multiple_answer if t == "multiple" else map_single_answer
                return bool(fn(a, opts))
            except Exception as e:                            # noqa: BLE001
                logger.debug(f"答案可用性检查异常（放行，不因自检丢答案）：{e}")
                return True
        return True

    def _ensure_usable(self, q_info: dict, ans):
        """答案不可用就**换第二个 AI 重答**，尽量别走到随机兜底。

        背景（2026-09-29 实测）：acc2 出现 2 次"随机选择"兜底 ——
        题目与选项都是英文，AI 语义答对了，但**把选项翻译成了中文**
        （"大数据技术" vs 选项 "Big Data Technology"）→ 无法映射成选项字母
        → 上层的 `_accept` 舍弃 → `random_answer()` 蒙。

        用户原话：「一个 AI 返回识别不了，就调第二个 AI，保证这个题返回的
        答案是正常的。随机的话风险就大了，只能是最下下下策。」

        策略：最多换 3 个槽位（模型）重试，并附加强制提示词；任一槽位给出
        可用答案即返回。全都不可用 → **返回原答案**（交给上层，本层不吞）。
        """
        if not ans or self._answer_usable(q_info, ans):
            return ans
        slots = getattr(self, "_slots", None) or []
        limit = min(3, max(0, len(slots) - 1))
        if limit <= 0:
            logger.warning(
                f"AI 答案不可用且池中无备用槽位（仅 {len(slots)} 个），原样交上层："
                f"{str(ans)[:40]!r}")
            return ans
        logger.warning(
            f"AI 答案无法对应本题选项（{str(ans)[:40]!r}），换模型重答，最多 {limit} 次")
        for i in range(limit):
            try:
                # ⚠️ 用 `_next_key()`（切到下一个可用**槽位**：Key+Endpoint+Model），
                #    **不是** `_switch_to_fallback()` —— 后者是"当前模型下线时
                #    切备用模型"，返回 bool，本场景下恒为 False（实测踩坑：
                #    第一次注入用了它，导致重试循环一次都没跑）。
                if not self._next_key():
                    break
            except Exception as e:                            # noqa: BLE001
                logger.debug(f"切换槽位失败：{e}")
                break
            try:
                again = self._query_locked(q_info, extra_hint=self._RETRY_HINT)
            except AIKeyExhaustedError:
                break
            except Exception as e:                            # noqa: BLE001
                logger.debug(f"重答异常（继续换下一个）：{type(e).__name__}: {e}")
                continue
            if again and self._answer_usable(q_info, again):
                logger.warning(
                    f"换模型重答成功（第 {i + 1} 次）：{str(ans)[:30]!r} -> "
                    f"{str(again)[:30]!r}")
                return again
        logger.warning(f"换模型重答仍未得到可用答案，保留原答案交上层：{str(ans)[:40]!r}")
        return ans

    def force_retry(self, q_info: dict) -> Optional[str]:
        """**强制**换槽位重答（供上层在"即将随机蒙"之前调用）。

        为什么需要它（2026-09-29 实测）：`_ensure_usable` 只在 AI **自己的**
        `_query` 内生效；而**章节检测/作业的随机兜底在 `api/base.py` 三处**
        （`1663` / `1770` / `3308`），走的是"query 返回空"或"答案映射失败"
        —— 那些情况下 AI 层已经交过答案、不会再重试，于是照样落到随机。

        用户原话：「随机的话风险就大了，只能是最下下下策」。
        这里给它一道最后挣扎：换最多 3 个模型，任一给出**可用**答案即返回。
        """
        with self._lock:
            for _ in range(3):
                try:
                    if not self._next_key():
                        break
                except Exception:                            # noqa: BLE001
                    break
                try:
                    ans = self._query_locked(q_info, extra_hint=self._RETRY_HINT)
                except AIKeyExhaustedError:
                    break
                except Exception as e:                        # noqa: BLE001
                    logger.debug(f"force_retry 异常：{type(e).__name__}: {e}")
                    continue
                if ans and self._answer_usable(q_info, ans):
                    logger.warning(f"随机兜底前换模型重答成功 -> {str(ans)[:40]!r}")
                    return ans
        return None

    # ---- 模型自动降级（2026-09-13）----
    @staticmethod
    def _is_model_fatal(e) -> bool:
        """是否属于"当前模型不可用"（可降级），而非网络抖动/限流。

        只对"模型本身没了"的情况降级：模型下线/改名、无权限、余额不足。
        超时、断连、限流、5xx 都是暂态或账号级问题 —— 换个模型同样是
        同一个 Key、同一个账号，降级解决不了，反而白丢一个可用档位。
        """
        name = type(e).__name__
        # 1) 明确的"模型不可用"SDK 错误类型（注意它们是 APIStatusError 的子类，
        #    必须在下面的暂态判断之前拦下）
        if name in ('NotFoundError', 'PermissionDeniedError', 'AuthenticationError'):
            return True
        # 2) 暂态/账号级问题：不降级
        if name in ('APITimeoutError', 'APIConnectionError', 'RateLimitError',
                    'InternalServerError'):
            return False
        # 3) 其余（含各类 APIStatusError 与未知异常）：按错误消息特征判断
        msg = str(e).lower()
        kw = ('model not found', 'does not exist', 'no such model',
              'unsupported model', 'invalid model', 'model_not_found',
              'insufficient', 'quota', 'balance', 'not available',
              '余额', '无权限', '权限不足', '模型不存在', '模型不可用')
        return any(k in msg for k in kw)

    @staticmethod
    def _build_model_chain(primary: str, conf: dict) -> list:
        """构造备用模型链：主模型 → 备用1 → 备用2 …

        优先读配置 fallback_models（逗号/空格/分号分隔，中英文都认）；
        没配则用内置规则：agnes 免费档 3.0-flash 挂了自动退到 2.5-flash
        （同 endpoint / key）。
        """
        chain = [primary] if primary else []
        raw = (conf.get('fallback_models') or '').strip()
        if raw:
            for m in re.split(r'[,，;；、\s]+', raw):
                m = m.strip()
                if m and m not in chain:
                    chain.append(m)
            return chain
        ep = (conf.get('endpoint') or '').lower()
        if 'agnes' in ep and primary == 'agnes-3.0-flash':
            chain.append('agnes-2.5-flash')
        return chain

    def _switch_to_fallback(self) -> bool:
        """切换到下一个备用模型；返回是否切换成功。"""
        chain = getattr(self, '_model_chain', None) or []
        idx = getattr(self, '_model_idx', 0)
        if idx + 1 >= len(chain):
            logger.error(
                f"AI 模型已无可用备用档（当前 {self.model}），"
                f"请检查 Key / 余额 / 模型名")
            return False
        self._model_idx = idx + 1
        old = self.model
        self.model = chain[self._model_idx]
        logger.warning(
            f"⚠ AI 主模型不可用，已自动降级：{old} → {self.model}"
            f"（后续题目均使用降级模型，直到程序重启）")
        return True

    def _init_tiku(self):
        self.endpoint = self._conf['endpoint']
        self.key = self._conf['key']
        self.model = self._conf['model']
        self.http_proxy = self._conf['http_proxy']
        self.min_interval_seconds = int(self._conf['min_interval_seconds'])
        # 备用模型链（2026-09-13）
        self._model_chain = self._build_model_chain(self.model, self._conf)
        self._model_idx = 0
        if len(self._model_chain) > 1:
            logger.info(f"AI 模型降级链：{' → '.join(self._model_chain)}")

        # 🔴🔴🔴 2026-09-26 23:05 **多 Key 池（解决 429 额度用尽）**
        # 🔴🔴🔴 2026-09-26 23:25 **升级为「多槽位」：Key + Endpoint + Model 三元组**
        #
        # 背景：免费额度是**按 Key 独立计算**的。单个 Key 用尽后必然 429，
        #       而"额度用尽"不是模型问题、也不是网络问题 —— 换个 Key 立刻满血。
        #       ⇒ 配置多个 Key，遇到 429（额度/限流）自动切下一个。
        #
        # 🔴 为什么要连 Endpoint/Model 一起换（用户 2026-09-26 明确要求
        #    「换 API Key 的时候把对应的也换掉，确保可以直接通畅」）：
        #      不同平台的 Key **不能混用同一个 Endpoint** —— 把硅基流动的 Key
        #      发到 agnes 的端点必然 401。所以一个"槽位"必须是三元组，
        #      切换时整组替换，否则跨平台配置会互相撞死。
        #
        # 配置来源（按优先级）：
        #   ① `ai_keys`：每行一个槽位（新增，推荐）
        #        格式1  sk-bbb...                              → 沿用主 Endpoint/模型
        #        格式2  sk-ccc...|https://api.siliconflow.cn/v1|Qwen/Qwen2.5-7B-Instruct
        #               （用 `|` 分隔，只写 Key|Endpoint 也可，模型沿用）
        #      兼容旧的逗号分隔写法（全部沿用主 Endpoint/模型）。
        #   ② `ai_key` ：单个 Key（兼容旧配置），作为**首个**槽位
        #
        # 行为：
        #   · 每个槽位被标记 `dead`（额度用尽/鉴权失败）后不再使用
        #   · 全部 dead → 抛 AIKeyExhaustedError，让上层回退到别的题库
        # 🔴 2026-09-27：跨线程限流锁（根治并发 429，见 _wait_for_interval）
        self._rate_lock = threading.Lock()
        self._slots: list = []      # [{"key","endpoint","model","tag"}]
        self._key_idx = 0
        # 🔴 2026-09-27 改造：由 `set` 升级为 **冷却表** {槽位签名: 解冻时间戳}。
        #    原因：agnes 免费档的 429 是**账号级滑动窗口限流**，过一会儿自动恢复
        #    （真机验证：K1 429 的同时 K2/K3/K4 全部正常）。
        #    原 `set` 语义是"永久拉黑"，一份长作业跑到中段就把所有 Key 拉黑，
        #    后面几十题全部拿不到答案。
        #    现在：临时限流 → 记冷却 45s（到期自动复活）；永久废号 → 记 +inf。
        #
        # 🔴🔴 2026-09-27 二次改造：键从 **Key 字符串** 改为 **槽位签名**
        #    `key|endpoint|model`。原因：腾讯 TokenHub 的免费额度是**按模型**
        #    发放的（每模型 100 万 token，一把 Key 可调全部模型）。原先按 Key
        #    记冷却 ⇒ 一个模型额度耗尽就把整把 Key 拉黑，同 Key 的其他模型一起废。
        #    改为签名后，**一个模型用完只会弃用那一个槽位**，同 Key 的其他模型
        #    继续可用 ⇒ 20 个模型各 100 万可以逐个吃完，无需为每个模型建一把 Key。
        #    （这也顺带修好了"同一把 Key 配文本槽 + 视觉槽"时互相牵连的问题。）
        self._key_dead: dict = {}
        # 🔴 2026-09-27：槽位签名 → 已"疑似限流复活"次数。
        #   仅一次性额度平台（腾讯）使用：限流不等于额度耗尽，所以先给冷却复活；
        #   连续复活 _ONESHOT_MAX_REVIVE 次仍失败 → 升级为永久弃用。
        #   一次请求成功即清零（见 `_call` 成功分支）。
        self._revive_count: dict = {}
        self._COOLDOWN_SECONDS = 45.0
        try:
            raw = str(self._conf.get('keys') or '').strip()
            if raw:
                for line in re.split(r'[\r\n]+', raw):
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = [p.strip() for p in line.split('|')]
                    key = parts[0] if parts else ''
                    if not key:
                        continue
                    # 旧格式兼容：一行里塞多个 Key（逗号分隔）
                    if '|' not in line and re.search(r'[,，;；\s]', key):
                        for k in re.split(r'[,，;；\s]+', key):
                            k = k.strip()
                            if k:
                                self._slots.append(
                                    {"key": k, "endpoint": "", "model": "", "tag": "池"})
                        continue
                    self._slots.append({
                        "key": key,
                        "endpoint": parts[1] if len(parts) > 1 else "",
                        "model": parts[2] if len(parts) > 2 else "",
                        "tag": "池",
                    })
        except Exception:
            pass
        # 主 Key 作为**首个**槽位（沿用主 Endpoint/模型）
        if self.key and all(s.get("key") != self.key for s in self._slots):
            self._slots.insert(0, {"key": self.key, "endpoint": "",
                                   "model": "", "tag": "主"})
        if not self._slots and self.key:
            self._slots = [{"key": self.key, "endpoint": "",
                            "model": "", "tag": "主"}]
        # 旧属性保留（`_next_key`/`_key_alive` 等仍按 Key 字符串工作）
        self._keys = [s["key"] for s in self._slots]
        # 去重（同 Key 只留一个；保序）
        # 🔴 2026-09-26 23:38 修缺陷：去重键必须是 **(Key, Endpoint, Model) 三元组**，
        #    不能只用 Key。原因：同一家的同一个 Key 可以调**不同模型**
        #    （典型场景：文本题用 Qwen2.5-7B、图片题用 Qwen3-VL-8B，
        #      但 API Key 就一个）。只按 Key 去重会把 VL 槽位吃掉 →
        #    图片题永远找不到视觉模型（真机踩坑）。
        _seen = set()
        _uniq = []
        for s in self._slots:
            sig = (s["key"], s.get("endpoint") or "", s.get("model") or "")
            if sig in _seen:
                continue
            _seen.add(sig)
            _uniq.append(s)
        self._slots = _uniq
        self._keys = [s["key"] for s in self._slots]

        if len(self._slots) > 1:
            eps = {s["endpoint"] for s in self._slots if s["endpoint"]}
            extra = f"，跨 {len(eps) + 1} 个平台" if eps else ""
            logger.info(f"🔑 AI 多 Key 轮换已启用：共 {len(self._slots)} 个 Key"
                        f"{extra}（429/401 时自动切换）")
        # 把首个槽位应用到当前生效属性
        self._apply_slot(0, announce=False)

        # 🔴 2026-09-29 二次确认开关：**默认关**，只有显式配 `ai_vote = true`
        #    才启用（语义从"默认可关"反转过来，原因见 `__init__` 处的注释：
        #    实测只提升 0.6pp，却多花约一半 token）。
        _av = str((self._conf or {}).get("ai_vote") or "").strip().lower()
        self._vote_enabled = _av in ("1", "true", "on", "yes", "y")
        if not self._vote_enabled:
            logger.info("🗳 AI 二次确认：已关闭（默认）。实测开启只提升 0.6 个百分点，"
                        "却多消耗约一半 token；要开启请配 ai_vote = true")
        elif len(self._slots) > 1:
            logger.info(f"🗳 AI 二次确认已启用：共 {len(self._slots)} 个槽位可交叉验证"
                        f"（题库/学习库命中的题不参与，只复核 AI 单独作答的题）")
        else:
            logger.info("🗳 AI 二次确认：池中只有 1 个槽位，无法交叉验证 → "
                        "退化为单次作答（加一个 Key 即可启用）")

    # ── 🔑 多 Key 轮换（2026-09-26）──────────────────────────────────
    def _apply_slot(self, idx: int, announce: bool = True) -> None:
        """把第 idx 个槽位的 Key/Endpoint/Model 应用为**当前生效**值。

        🔴 用户要求「换 API Key 的时候把对应的也换掉」：跨平台时三者必须
        同时切换，否则会出现"用 A 家的 Endpoint 发 B 家的 Key" → 必然 401。
        """
        if not self._slots:
            return
        s = self._slots[idx % len(self._slots)]
        old_ep, old_key, old_model = self.endpoint, self.key, self.model
        self.key = s["key"]
        self.endpoint = s["endpoint"] or self._conf.get('endpoint') or ""
        self.model = s["model"] or self._conf.get('model') or ""
        # 模型变了 → 重建降级链与索引，避免指向旧平台的模型名
        if self.model != old_model:
            self._model_chain = self._build_model_chain(self.model, self._conf)
            self._model_idx = 0
        if announce and (self.endpoint != old_ep or self.model != old_model):
            logger.warning(
                f"🔑 已切换 AI 槽位 → Key（…{self.key[-6:]}）"
                f" | Endpoint={self.endpoint}"
                f" | Model={self.model}")

    # ── 🔑 槽位签名（2026-09-27 新增）────────────────────────────────
    #
    # 冷却表原先以 **Key 字符串** 为键 → 同一把 Key 配多个模型会**连带死亡**：
    # hy3 额度耗尽 → 整把 Key 被拉黑 → 同 Key 的其他模型槽位一起废。
    #
    # 而腾讯 TokenHub 的免费额度是**按模型**发放的（每模型 100 万 token），
    # 一把 Key 就能调全部模型。要让「一个模型用完 → 自动换下一个模型」生效，
    # 冷却必须细化到**槽位**粒度 ⇒ 改用 (Key | Endpoint | Model) 签名。
    #
    # ⚠️ 签名的 endpoint/model 取**实际生效值**（空则回退主配置），
    #    保证 `_cur_sig()` 与 `_sig(槽位)` 严格一致。
    def _sig(self, s: dict) -> str:
        ep = (s.get("endpoint") or self._conf.get("endpoint") or "").strip()
        md = (s.get("model") or self._conf.get("model") or "").strip()
        return f"{s.get('key') or ''}|{ep}|{md}"

    def _cur_sig(self) -> str:
        """当前生效槽位的签名。"""
        return f"{self.key}|{self.endpoint}|{self.model}"

    @staticmethod
    def _sig_masked(sig: str) -> str:
        """日志用掩码：Key 末 6 位 + 模型名。"""
        parts = (sig or "").split("|")
        key = parts[0] if parts else ""
        md = parts[2] if len(parts) > 2 else ""
        return f"…{key[-6:]}@{md}" if md else f"…{key[-6:]}"

    def _slot_of_sig(self, sig: str) -> int:
        """按**槽位签名**找下标（原 `_slot_of_key` 的升级版）。

        同 Key 多模型时，只按 Key 找会返回第一个匹配 → 切换起点错位。
        """
        for i, s in enumerate(self._slots):
            if self._sig(s) == sig:
                return i
        return -1

    def _sig_cooldown_left(self, sig: str) -> float:
        """某个**槽位**还需要冷却多久（秒）。0 = 可用，inf = 永久废。"""
        until = self._key_dead.get(sig)
        if until is None:
            return 0.0
        if until == float('inf'):
            return float('inf')
        return max(0.0, until - time.time())

    def _min_cooldown_left(self) -> float:
        """池里**最早解冻**还需要多久；若全是永久废槽位则返回 -1。"""
        best = -1.0
        for s in self._slots:
            left = self._sig_cooldown_left(self._sig(s))
            if left == float('inf'):
                continue
            if left <= 0:
                return 0.0
            if best < 0 or left < best:
                best = left
        return best

    def _sig_alive(self, sig: str) -> bool:
        """🔴 2026-09-27：冷却表语义 —— 到期的临时冷却自动复活。

        按**槽位签名**判定：同 Key 不同模型**互不影响**。
        这样"一个模型额度耗尽"只会弃用那一个槽位，同 Key 的其他模型照常可用
        ⇒ 腾讯每个模型各 100 万 token 可以逐个吃完，无需为每个模型各建一把 Key。
        """
        if not sig:
            return False
        until = self._key_dead.get(sig)
        if until is None:
            return True
        if until == float('inf'):        # 永久废号
            return False
        if time.time() >= until:          # 冷却结束 → 复活
            del self._key_dead[sig]
            logger.info(f"🔑 AI 槽位（{self._sig_masked(sig)}）冷却结束，重新启用")
            return True
        return False

    def _next_key(self) -> str:
        """取下一个可用**槽位**并整体应用（Key+Endpoint+Model）；全不可用返回 ""。

        🔴 2026-09-27 修下标错位：
        `_keys` 是**去重后的 Key 列表**，而切换要的是 `_slots`（可能同一 Key
        占多个槽位，如"同 Key 7B 文本 + VL 视觉"）。原实现用
        `self._key_idx % len(self._keys)` 取下标再 `self._keys[i]`，得到的 Key
        再喂给 `_apply_slot(i)` —— **下标是 `_keys` 的，却拿去索引 `_slots`**，
        跨平台/同 Key 多槽位时必然切错。
        现在直接以 `_slots` 为索引空间，用 `_apply_slot` 的取模自理。
        """
        if not self._slots:
            return self.key
        n = len(self._slots)
        for _ in range(n):
            i = self._key_idx % n
            self._key_idx += 1
            # 按**槽位签名**判定可用性（同 Key 不同模型互不牵连）
            if self._sig_alive(self._sig(self._slots[i])):
                # 一律走 `_apply_slot`（它会自行判断 Key/Endpoint/Model 是否
                # 真的变了，并决定 announced 文案）—— 不在这里用 `k != self.key`
                # 决定路径：同 Key 不同模型（文本槽位 → 视觉槽位）也必须切换。
                self._apply_slot(i, announce=True)
                return self._slots[i].get("key") or ""
        # 🔴 2026-09-27 修：全不可用时返回空串。
        #    原实现 `return self.key` 会让上层 `_key_alive(nxt)` 判 false 之后
        #    仍然把当前（已死的）Key 当成"切换成功"，下一轮又打同一个死 Key。
        return ""

    @classmethod
    def _is_network_error(cls, err: BaseException) -> bool:
        """🔴 2026-09-27 新增：是不是**纯网络/传输层**故障（跟 Key 好坏无关）。

        用户追问"额度没用完、只是网络连接不畅，会不会被当成用完了"——
        实测（`_probe_net_fault.py`）12 种网络报文：连接失败/超时/断连/
        DNS/SSL/代理/网关 HTML 错误页**都不会**被判成额度错误。
        但为杜绝边缘误伤（例如报文里恰好含 "40123"、"403 bytes"、
        "Max retries exceeded"），这里再立一道**显式闸门**：
        凡属网络层异常，直接退出"弃用槽位"的判定链。

        判定顺序：异常类型名 → httpx 传输层基类 → requests 网络基类。
        """
        if type(err).__name__ in cls._NETWORK_ERR_NAMES:
            return True
        try:
            import httpx
            if isinstance(err, (httpx.TransportError, httpx.TimeoutException)):
                return True
        except Exception:
            pass
        try:
            import requests
            if isinstance(err, (requests.exceptions.ConnectionError,
                                requests.exceptions.Timeout,
                                requests.exceptions.ProxyError,
                                requests.exceptions.SSLError)):
                return True
        except Exception:
            pass
        return False

    @classmethod
    def _is_quota_error(cls, err: BaseException) -> bool:
        """判断异常的 Key 是不是**已经废了**（换 Key 可解）。

        🔴 2026-09-26 23:40 改名语义（原为"额度/限流"）：
        实测发现"该换 Key"的情况有**两类**，行为上都是"弃用这个 Key、换下一个"：
          ① 额度/限流：429 / rate limit / quota / token plan（免费额度打光）
          ② Key 无效  ：401 Invalid token / AuthenticationError（Key 打错/被吊销）

        两类都"换 Key 就能解决"，所以归到同一个判据里；
        但日志里要区分（见 `_key_fault_kind`），否则用户看到"额度耗尽"
        实际是 Key 打错，会去充值而解决不了问题。

        依据真机报文：
            429: "You've reached the API rate limit for free users.
                  Upgrade to a Token Plan ..."  (type: AgnesAI_error)
            401: "Invalid token (request id: ...)"  (type: AgnesAI_error)

        🔴 2026-09-27 二次加固（用户追问"网络不畅会不会被误判成额度用完"）：
          ① **纯网络故障直接返回 False**（见 `_is_network_error`）——
             一次网络抖动绝不能让一个满额度的槽位"永久弃用"。
          ② 裸数字状态码 `401/403/429` 改为**带上下文的正则**匹配：
             原实现是 `'401' in s` 子串匹配，实测会被
             `127.0.0.1:40123`（端口号）、`403 bytes of garbage`
             （字节数）这类网络报文误命中 → 白扔一个好槽位。
          ③ 删掉裸词 `'exceeded'`：它会命中经典的
             `Max retries exceeded with url: ...`（纯网络故障报文）。
             真额度报文的 "exceeded your current quota" 由 `quota` 兜住。
        """
        if cls._is_network_error(err):
            return False
        name = type(err).__name__
        if name in ('RateLimitError', 'AuthenticationError', 'PermissionDeniedError'):
            return True
        s = str(err).lower()
        # 状态码只在"HTTP 状态码上下文"里认（避免端口号 / 字节数误伤）
        if re.search(r'(?:http|status|error)[ _-]*code[:= ]{0,2}(?:401|403|429)\b', s):
            return True
        if re.search(r'\bhttp[/ ]?1\.[01]["\s]{1,3}(?:401|403|429)\b', s):
            return True
        if re.search(r'\b(?:401|403|429)\s+'
                     r'(?:unauthorized|forbidden|too many requests)', s):
            return True
        # JSON 形态：{"code":429,...} / {"status":429,...}
        if re.search(r'["\'](?:code|status)["\']\s*:\s*(?:401|403|429)\b', s):
            return True
        # 🔴 2026-10-12 修正：**复用 `_key_fault_kind` 作语义兜底**。
        #   起因（本轮实测抓到）：腾讯把鉴权失败抛成 `APIStatusError`
        #   + 报文 `"The API Key does not exist..."`，既不是
        #   `AuthenticationError`、报文里也没有"401"字样（状态码在
        #   `e.response.status_code` 里，不在 str 里）⇒ 上面所有判据全落空
        #   ⇒ `_is_quota_error` 返回 False ⇒ `_decide_dead_action` 走
    #   `'none'`（"不弃用槽位"）⇒ **401 废号永远留在池里，每题都重试**。
        #   改为：报文级判据只维护一份（在 `_key_fault_kind`），此处直接复用。
        #   ⚠️ 只兜「明确的 Key 无效 / 额度耗尽」，不引入任何宽泛词 ——
        #   宁可漏判（该题换题库），不可误判（白扔满额度槽位）。
        if cls._key_fault_kind(err) != "不可用":
            return True
        markers = ('rate limit', 'rate_limit', 'quota', 'insufficient',
                   'balance', '欠费', '额度', '余额不足',
                   'too many requests', 'token plan', 'invalid token',
                   'invalid api key', 'incorrect api key', 'unauthorized',
                   'authentication', 'per minute', 'per second')
        # ⚠️ 这里**故意不含**裸数字 '429'：实测它会被端口号/时间戳等误命中，
        #    而"误判成额度耗尽的代价（永久丢一个满额度槽位）"远大于
        #    "漏判的代价（该题换题库/失败）"。宁可漏判，不可误判。
        return any(m in s for m in markers)

    @classmethod
    def _is_transient_limit(cls, err: BaseException) -> bool:
        """🔴 2026-09-27 新增：区分**临时限流**（可冷却复活）与**永久废号**。

        真机证据（agnes 免费档）：
            K1 报 429 "You've reached the API rate limit for free users"，
            但 **K2/K3/K4 同一时刻正常** → 说明这个 429 是**按账号的滑动窗口**
            限流，不是"额度永久打光"。等一段时间会自动恢复。

        原实现把 429 一律 `_key_dead.add()` 永久拉黑 —— 后果：
            一份 100 题作业跑到一半所有 Key 都被拉黑，
            后面 70 题全部无答案（真机踩坑：23:55 之后大面积"AI Key 已全部耗尽"）。

        判据：报文里出现"限流/稍后再试"语义 → 临时（冷却可复活）。
              出现"欠费/余额不足/quota/Key 无效"语义 → 永久。

        🔴 2026-09-27 二次加固：
          · 纯网络故障不参与本判据（`_is_network_error` 前置排除）；
          · 补中文额度说法（"免费额度已用完"/"额度用尽"/"已达上限"…）→ 永久；
          · 补 Key 无效类（invalid token / unauthorized / permission denied）
            → 永久 —— Key 打错了不该给它冷却反复复活。
        """
        if cls._is_network_error(err):
            return False
        s = str(err).lower()
        # 永久的：余额/额度真没了，或 Key 本身无效
        # 🔴 2026-09-27 补漏（用户专门问"hy3 用完后能不能正常切换"）：
        #   OpenAI / 腾讯 TokenHub 兼容格式的「额度耗尽」标准报文是
        #     HTTP 429 + "You exceeded your current quota, please check
        #                 your plan and billing details"  (insufficient_quota)
        #   它**不含** "quota exceeded"（词序相反），原本一条都匹配不上，
        #   于是落到最后的类型兜底 —— 429 被 openai SDK 抛成 RateLimitError，
        #   判定为"临时限流"→ 45 秒后又切回已耗尽的槽位 → **反复撞墙、
        #   永远递补不到下一个模型**。加一个宽判据 `quota` 覆盖全部变体。
        _permanent = (
            'insufficient balance', 'balance', '欠费', '余额不足',
            'quota exceeded', 'no quota', 'billing', 'payment required',
            'arrears', 'quota', 'insufficient_quota',
            'exceeded your current quota',
            # 中文"额度用完"类说法（2026-09-27 补）
            '额度已用完', '额度用尽', '额度不足', '额度耗尽', '免费额度用完',
            '免费额度已用完', '用量已满', '已达上限', '超出配额', '配额不足',
            '套餐已用完', '已无可用额度',
            # 英文"额度用完"类说法
            'exhausted', 'used up', 'out of credit', 'credit exhausted',
            'quota exhausted', 'no remaining',
            # Key 无效/无权（永久，与额度无关）
            'invalid token', 'invalid api key', 'incorrect api key',
            'unauthorized', 'authentication', 'permission denied', 'key 无效',
        )
        if any(m in s for m in _permanent):
            return False
        # 临时的：限流 / 稍后再试
        if any(m in s for m in ('rate limit', 'rate_limit', 'too many requests',
                                'tpm', 'rpm', 'try again later', 'retry after',
                                'slow down', '频率', '请求过于频繁', '稍后再试',
                                'overloaded', '过载', 'busy')):
            return True
        # 类型兜底：RateLimitError 多数是滑动窗口。
        # ⚠️ 注意顺序：必须在"额度/Key 无效"判据**之后**才用兜底，否则会把
        #    "额度耗尽(429)"误判成临时限流（上面那条漏就是这么来的）。
        return type(err).__name__ == 'RateLimitError'

    @staticmethod
    def _is_vlm_error(err: BaseException) -> bool:
        """异常是否表示"当前模型不支持读图"（图片题专用判据）。

        真机报文（硅基流动）：
            Qwen/Qwen2.5-7B-Instruct + 图片
            → 400 {"code":20041,
                   "message":"The model is not a VLM (Vision Language Model).
                              Please use text-only prompts."}
        识别出它才能"换视觉槽位"而不是"丢图瞎猜"。
        """
        s = str(err).lower()
        return ('not a vlm' in s or 'not a vision' in s
                or 'vision language model' in s
                or 'does not support image' in s
                or 'not support image' in s
                or 'image input' in s
                or ('image' in s and 'not supported' in s)
                or ('不支持' in s and ('图' in s or '视觉' in s)))

    @staticmethod
    def _key_fault_kind(err: BaseException) -> str:
        """把 Key 故障分类成人类可读的原因（**决定处置方式，不只是文案**）。

        🔴 2026-10-12 修正：原实现只认 `type(err).__name__`
        （`AuthenticationError` / `PermissionDeniedError`）或几个英文短词。
        但腾讯 TokenHub 把鉴权失败抛成**普通异常 + JSON 报文**：
            401 {"error":{"type":"authentication_error","code":"401002",
                 "message":"The API Key does not exist or signature
                            verification failed..."}}
        SDK 在部分路径下不包装成 `AuthenticationError` ⇒ 原实现落到最后
        的 `return "不可用"` ⇒ **401 被误当成"额度耗尽"**。
        （实测：`_key_fault_kind(E_401)` 返回 `'不可用'`。）

        ⇒ 增补报文级判据，并**优先于**额度类判据（401 与 402 处置方式不同，
        必须先把"Key 报废"摘出来，否则永远走错分支）。
        """
        name = type(err).__name__
        s = str(err).lower()
        # ① Key 无效/无权 —— 先判，且**必须早于**额度判据
        if name in ('AuthenticationError', 'PermissionDeniedError') \
                or 'invalid token' in s or 'invalid api key' in s \
                or 'incorrect api key' in s or 'unauthorized' in s \
                or 'authentication_error' in s \
                or 'api key does not exist' in s \
                or 'signature verification failed' in s:
            return "Key 无效/无权"
        # ② 额度/限流
        if name == 'RateLimitError' or '429' in s or 'rate limit' in s \
                or 'quota' in s or 'token plan' in s \
                or 'free trial quota' in s:
            return "额度/限流用尽"
        return "不可用"

    @classmethod
    def _decide_dead_action(cls, endpoint: str, err: BaseException,
                            n_revive: int) -> tuple:
        """🔴 2026-09-27 新增：槽位报错后的处置决策（**纯函数**，单独可测）。

        抽出来的目的：这段"永久弃用 / 冷却复活"的判断直接决定"会不会白扔
        一个满额度槽位"，属高风险逻辑，必须能脱离整条作业链单测。
        单测见 `_verify_keypool.py`。

        Args:
            endpoint: 当前槽位的 endpoint（判断是否一次性额度平台）
            err: 平台抛出的异常
            n_revive: 该槽位已"限流复活"过的次数

        Returns:
            (action, seconds, reason, bump_revive)
            · action ∈ {'permanent', 'cooldown', 'none'}
              'none' = 网络抖动，**不处置**（原样抛出，槽位保持可用）
            · seconds: 冷却秒数（permanent 时为 inf）
            · reason : 人类可读原因（写日志）
            · bump_revive: 是否要把该槽位的复活计数 +1
        """
        # ⓪ 纵深防御：**只有"换 Key 能解决"的错误才允许处置槽位**。
        #    网络抖动（超时/断连/DNS/SSL）与服务端 5xx、请求参数错误等
        #    跟 Key 好坏无关，一律放行、保持槽位可用。
        #    正常路径上 `_is_quota_error` 已挡住这些，这里再兜一层 ——
        #    万一将来有人绕过那道闸门直接调本函数，也不会误伤好槽位。
        if cls._is_network_error(err):
            return ('none', 0.0, "网络抖动（不弃用槽位）", False)
        if not cls._is_quota_error(err):
            return ('none', 0.0, "非额度/鉴权类错误（不弃用槽位）", False)
        oneshot = any(h in (endpoint or "") for h in cls._ONESHOT_HOSTS)
        _kind = cls._key_fault_kind(err)
        _limited = cls._is_transient_limit(err)
        # ① Key 无效/无权（401）→ **永久弃用**。
        #    这是唯一该永久的：Key 本身不存在/被吊销，换任何模型都没用。
        #    ⚠️ 与「额度耗尽」严格区分 —— 2026-10-12 实测腾讯同一批 Key 里，
        #    4 个报 401（真废）、2 个报 402（只是该模型额度用完，同 Key 换模型可用）。
        #    早前把两者都写成"额度已耗尽，永久丢弃"，日志误导成"整把 Key 报废"。
        if _kind == "Key 无效/无权":
            return ('permanent', float('inf'), "Key 无效/无权", False)
        # ② 疑似限流（429 / rate limit / try again later）→ 短冷却复活，逐次递增。
        #    限流是**滑动窗口**，额度多半还剩很多 —— 与「真耗尽」分开处置。
        if _limited:
            if oneshot:
                if n_revive >= cls._ONESHOT_MAX_REVIVE:
                    return ('permanent', float('inf'),
                            f"已 {n_revive} 次限流复活后仍不可用"
                            f"（一次性额度平台）", False)
                return ('cooldown', cls._ONESHOT_COOLDOWN * (n_revive + 1),
                        f"疑似限流（额度未必耗尽，第 {n_revive + 1}/"
                        f"{cls._ONESHOT_MAX_REVIVE} 次复活）", True)
            return ('cooldown', cls._COOLDOWN_SECONDS, "临时限流", False)
        # ③ 🔴 2026-10-12 修正（本次实测驱动）：**额度耗尽 ≠ 整把 Key 报废**。
        #    腾讯 TokenHub 的免费额度**按模型发放**，402 只说明
        #    「key|endpoint|model」这一个组合用完了，同 Key 换模型仍可用。
        #    原实现对 oneshot 平台的非限流额度错误一律 `permanent`（+inf），
        #    等于永久扔掉一个「额度重置后就能用」的模型 —— 实测 39 槽位里
        #    有 31 个仍是 200 可用，误杀面很大。
        #    改为长冷却（默认 6h）：本场考试不再用它，下次会话自动复活。
        if oneshot:
            return ('cooldown', cls._ONESHOT_EXHAUSTED_COOLDOWN,
                    "该模型额度耗尽（同 Key 其他模型不受影响，冷却后自动恢复）",
                    False)
        # ④ 其他平台的额度/余额真耗尽 → 永久弃用
        return ('permanent', float('inf'), _kind, False)

    def check_llm_connection(self) -> bool:
        """
        检查大模型连接是否可用
        发送一个简单的测试请求来验证 API 配置
        """
        with self._lock:
            logger.info(f'正在检查 {self.name} 连接...')
            try:
                # 初始化客户端（timeout 同 _make_client，防上游挂起堵死）
                if self.http_proxy:
                    httpx_client = httpx.Client(proxy=self.http_proxy, timeout=20)
                    client = OpenAI(http_client=httpx_client, base_url=self.endpoint,
                                    api_key=self.key, timeout=20)
                else:
                    client = OpenAI(base_url=self.endpoint, api_key=self.key, timeout=20)

                # 限流等待
                self._wait_for_interval()
                self.last_request_time = time.time()

                # 发送测试请求
                completion = client.chat.completions.create(**self._completion_kwargs(
                    model=self.model,
                    messages=[
                        {
                            'role': 'user',
                            'content': '你好，请回答：1+1 等于几？只回答数字。'
                        }
                    ],
                    max_tokens=200  # 增大以支持可能返回的 reasoning_content
                ))

                # 统一检查响应
                if completion.choices:
                    msg = completion.choices[0].message
                    if msg.content or getattr(msg, 'reasoning_content', None):
                        logger.info(f'{self.name} 连接检查成功')
                        return True

                logger.error(f'{self.name} 连接检查失败：未收到响应')
                return False

            except Exception as e:
                logger.error(f'{self.name} 连接检查失败：{e}')
                return False

class SiliconFlow(Tiku):

    def __init__(self, config_path: Optional[str] = None):
        """初始化硅基流动大模型题库."""
        super().__init__(config_path)
        self.name = '硅基流动大模型'
        self.last_request_time = None
        self._lock = threading.Lock()

    @staticmethod
    def _looks_like_listening(q_info: dict) -> bool:
        """与 AI 类保持一致：听力题需走专门提示词策略。"""
        return AI._looks_like_listening(q_info)

    def _wait_for_interval(self):
        if self.last_request_time:
            interval = time.time() - self.last_request_time
            if interval < self.min_interval:
                sleep_time = self.min_interval - interval
                logger.debug(f"API请求间隔过短, 等待 {sleep_time} 秒")
                time.sleep(sleep_time)

    def _query(self, q_info: dict):
        with self._lock:
            return self._query_locked(q_info)

    def _query_locked(self, q_info: dict):
        def remove_md_json_wrapper(md_str):
            # 解析可能存在的JSON包装
            pattern = r'^\s*```(?:json)?\s*(.*?)\s*```\s*$'
            match = re.search(pattern, md_str, re.DOTALL)
            return match.group(1).strip() if match else md_str.strip()

        # 构造请求头
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        # 构造系统提示词
        system_prompt = ""
        if q_info['type'] == "single":
            system_prompt = "本题为单选题，你只能选择一个选项，请根据题目和选项回答问题，输出的是选项的具体内容（必须与选项原文逐字一致，含标点符号），而不是内容前的ABCD，并以JSON格式输出：示例回答：{\"Answer\": [\"正确选项内容\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
        elif q_info['type'] == 'multiple':
            # 🔴 2026-09-27 对齐 query_all 的批量版提示词：那边写了"必须选择
            # 两个或以上选项"，这边漏了 → AI 常只回一个选项（真机实测：
            # "下列属于二十四史的有？"只答《史记》）→ 多选题白丢分。
            system_prompt = "本题为多选题，你必须选择两个或以上选项，逐个判断每个选项，不要漏选。输出的是选项的具体内容（必须与选项原文逐字一致，含标点符号），而不是内容前的ABCD，以JSON格式输出：示例回答：{\"Answer\": [\"选项1的内容\",\n\"选项2的内容\"]}。每个正确选项作为数组的一个元素，不要合并成一段文字。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
        elif q_info['type'] == 'completion':
            system_prompt = "本题为填空题，请直接给出填空内容，以JSON格式输出：示例回答：{\"Answer\": [\"答案文本\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
        elif q_info['type'] == 'judgement':
            system_prompt = "本题为判断题，请回答'正确'或'错误'，以JSON格式输出：示例回答：{\"Answer\": [\"正确\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"

        # 课程上下文（由 base.study_work 注入到 q_info["_course"]）：给出领域定位。
        # ⚠️ 用独立变量而不是 `system_prompt +=`：下面的**听力分支会整体覆盖**
        # system_prompt，+= 的内容会被吞掉。统一在 payload 里拼 _ctx_prefix。
        _course = str((q_info or {}).get("_course") or "").strip()
        _ctx_prefix = (f"本题出自《{_course}》课程的章节测验。"
                       "请使用该学科（或该软件官方中文界面）的标准术语作答，"
                       "答案保持简短（通常 1-6 个字），不要写解释。\n") if _course else ""

        # 听力题：题干里只有选项、没有音频原文，模型看不到听力材料，
        # 直接问极易退化成"全部选同一个字母"的瞎猜。这里明确告知这一限制，
        # 并要求基于选项语义与常识推断，禁止无依据地重复同一选项。
        if self._looks_like_listening(q_info):
            system_prompt = (
                "这是一道英语听力选择题（四级/六级听力篇章）。你无法听到音频，"
                "题干中也不包含听力原文，因此必须基于以下线索作答：\n"
                "1. 选项之间的语义差异与常识逻辑；\n"
                "2. 四级听力常见出题规律（正确选项往往是对原文的转述，含绝对化词语"
                "如 only/never/all 的选项通常错误）；\n"
                "3. 若一题含多个小问，各小问答案应独立判断，禁止全部填同一个字母。\n"
                "输出格式：{\"Answer\": [\"选项完整内容1\", \"选项完整内容2\", ...]}，"
                "按小问顺序给出，不要输出 ABCD 字母，不要输出任何解释或多余内容。"
            )

        # 构造请求体
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": _ctx_prefix + system_prompt
                },
                {
                    "role": "user",
                    "content": f"题目：{q_info['title']}\n选项：{q_info['options']}"
                }
            ],
            "stream": False,

            "max_tokens": 4096,

            "temperature": 0.7,
            "top_p": 0.7,
            "response_format": {"type": "text"}
        }

        self._wait_for_interval()

        try:
            response = requests.post(
                self.api_endpoint,
                headers=headers,
                json=payload,
                timeout=30
            )
            self.last_request_time = time.time()

            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                parsed = json.loads(remove_md_json_wrapper(content))
                return "\n".join(parsed['Answer']).strip()
            else:
                logger.error(f"API请求失败：{response.status_code} {response.text}")
                return None

        except Exception as e:
            logger.error(f"硅基流动API异常：{e}")
            return None

    def _init_tiku(self):
        # 从配置文件读取参数
        self.api_endpoint = self._conf.get('siliconflow_endpoint', 'https://api.siliconflow.cn/v1/chat/completions')
        self.api_key = self._conf['siliconflow_key']

        self.model_name = self._conf.get('siliconflow_model', 'deepseek-ai/DeepSeek-V3')

        self.min_interval = int(self._conf.get('min_interval_seconds', 3))

    def check_llm_connection(self) -> bool:
        """
        检查硅基流动大模型连接是否可用
        发送一个简单的测试请求来验证 API 配置
        """
        with self._lock:
            logger.info(f'正在检查 {self.name} 连接...')
            try:
                headers = {
                    'Authorization': f'Bearer {self.api_key}',
                    'Content-Type': 'application/json'
                }

                payload = {
                    'model': self.model_name,
                    'messages': [
                        {
                            'role': 'user',
                            'content': '你好，请回答：1+1 等于几？只回答数字。'
                        }
                    ],
                    'stream': False,
                    'max_tokens': 10,
                    'temperature': 0.7,
                    'top_p': 0.7,
                    'response_format': {'type': 'text'}
                }

                # 在测试 API 连接时同样执行节流校验
                self._wait_for_interval()

                response = requests.post(
                    self.api_endpoint,
                    headers=headers,
                    json=payload,
                    timeout=30
                )
                self.last_request_time = time.time()

                if response.status_code == 200:
                    result = response.json()
                    if result.get('choices') and result['choices'][0]['message']['content']:
                        logger.info(f'{self.name} 连接检查成功')
                        return True
                    else:
                        logger.error(f'{self.name} 连接检查失败：未收到有效响应')
                        return False
                else:
                    logger.error(f'{self.name} 连接检查失败：{response.status_code} {response.text}')
                    return False

            except Exception as e:
                logger.error(f'{self.name} 连接检查失败：{e}')
                return False


class TikuManual(Tiku):
    _manual_lock = threading.Lock()
    is_manual = True

    def __init__(self, config_path: Optional[str] = None) -> None:
        """初始化手动题库实例."""
        super().__init__(config_path)
        self.name = '手动输入题库'
        self.default_mode = 'batch'
        self.skip_answer_validation = True

    @staticmethod
    def _extract_option_letters(ans: str) -> list[str]:
        cleaned = re.sub(r'[\s,，;；、]+', '', ans)
        if not cleaned or not re.fullmatch(r'[A-Za-z]+', cleaned):
            return []
        return [c.upper() for c in cleaned]

    def _init_tiku(self):
        self.default_mode = self._conf.get('manual_mode_default', 'batch').strip().lower()
        if self.default_mode not in ['batch', 'single']:
            self.default_mode = 'batch'

        self.separator = self._conf.get('manual_mode_separator', ';')
        if self.separator.lower() in ['\\n', 'newline', '换行']:
            self.separator = '\n'
        elif self.separator.lower() in ['space', '空格']:
            self.separator = ' '
        elif self.separator.lower() in ['tab', '制表符']:
            self.separator = '\t'

    @staticmethod
    def _safe_close_tqdm_bars():
        """安全地清除并关闭所有活动的 tqdm 进度条，防止私有属性变更引发异常."""
        try:
            from tqdm import tqdm
            if hasattr(tqdm, '_instances') and hasattr(tqdm._instances, '__iter__'):
                # 复制一份以防遍历时容器大小发生变化 (WeakSet/list)
                instances = list(tqdm._instances)
                for instance in instances:
                    try:
                        if hasattr(instance, 'leave'):
                            instance.leave = False
                        if hasattr(instance, 'clear'):
                            instance.clear()
                        if hasattr(instance, 'close'):
                            instance.close()
                    except Exception as ie:
                        logger.debug(f"清理单个 tqdm 实例失败: {ie}")
        except Exception as e:
            logger.debug(f"获取/清理 tqdm 实例列表失败: {e}")

    def _query(self, q_info: dict) -> Optional[str]:
        # 强行关闭清除所有当前活动的 tqdm 进度条
        self._safe_close_tqdm_bars()

        with self._manual_lock:
            ans = self._single_query(q_info)
        logger.debug("手动答题结束，冲刷缓存日志")
        return ans

    def _query_all(self, q_list: list[dict], query_delay: float = 0.0) -> list[Optional[str]]:
        # 强行关闭清除所有当前活动的 tqdm 进度条
        self._safe_close_tqdm_bars()

        with self._manual_lock:
            print(f"\n{'=' * 20} 手动输入题库 (共 {len(q_list)} 题) {'=' * 20}")
            if self.default_mode == 'batch':
                ans_list = self._batch_query_flow(q_list)
            else:
                ans_list = [self._single_query(q) for q in q_list]
        logger.debug("手动答题结束，冲刷缓存日志")
        return ans_list

    @staticmethod
    def _get_type_display(type_str: str) -> str:
        type_map = {
            'single': '单选题',
            'multiple': '多选题',
            'completion': '填空题',
            'judgement': '判断题',
            'shortanswer': '简答题',   # 2026-09-13 补：含名词解释/论述/计算/分录等
            'unknown': '未知题型',
        }
        return type_map.get(type_str, '其他类型')

    def _single_query(self, q: dict) -> Optional[str]:
        type_str = self._get_type_display(q['type'])
        if q['type'] in ['single', 'multiple'] and q.get('options'):
            options = q['options']
            parts = []
            if isinstance(options, str):
                parts = [o.strip() for o in options.split('\n') if o.strip()]
                if len(parts) <= 1:
                    from api.answer_check import cut
                    cut_parts = cut(options)
                    if cut_parts:
                        parts = cut_parts
            elif isinstance(options, list):
                parts = [str(o).strip() for o in options if str(o).strip()]

            options_text = "  ".join(parts)
            print(f"\n【{type_str}】 {q['title']} 选项: {options_text}")
        elif q['type'] == 'judgement':
            print(f"\n【{type_str}】 {q['title']} 选项: 正确 / 错误")
        else:
            print(f"\n【{type_str}】 {q['title']}")

        while True:
            ans = input("请输入答案 (直接回车表示跳过/无答案): ").strip()
            if not ans:
                print(f"  [已记录] 题目: {q['title']} ---> 答案: [跳过/随机]")
                return None

            # 即时校验
            ok, err_msg = self._validate_user_input(ans, q)
            if not ok:
                print(f"  \033[31m[输入错误] {err_msg}\033[0m")
                continue

            normalized_ans = self._normalize_user_input(ans, q)
            print(f"  [已记录] 题目: {q['title']} ---> 答案: {normalized_ans}")
            return normalized_ans

    def _validate_user_input(self, ans: str, q: dict) -> tuple[bool, str]:
        """
        验证用户手动输入的答案是否合规.
        """
        if not ans:
            return True, ""

        ans = ans.strip()
        if not ans:
            return True, ""

        q_type = q.get('type')
        if q_type == 'judgement':
            return self._validate_judgement_input(ans)
        elif q_type in ['single', 'multiple']:
            return self._validate_choice_input(ans, q)
        return True, ""

    def _validate_judgement_input(self, ans: str) -> tuple[bool, str]:
        """验证判断题手动输入是否合规."""
        val = ans.lower()
        valid_judgements = [
            'true', 't', '1', '对', '正确', '√', '是', 'yes', 'y',
            'false', 'f', '0', '错', '错误', '×', '否', 'no', 'n', '不对', '不正确'
        ]
        if val not in valid_judgements:
            return False, f"无法识别的判断词 '{ans}'，请输入：对/错、正确/错误、T/F、1/0"
        return True, ""

    def _validate_choice_input(self, ans: str, q: dict) -> tuple[bool, str]:
        """
        验证选择题手动输入是否合规.
        """
        options = q.get('options', '')
        parts = self._parse_options(options)
        valid_keys = self._extract_valid_keys(parts)

        if not valid_keys:
            return True, ""

        letters = self._extract_option_letters(ans)
        if not letters:
            return self._validate_text_match(ans, parts)

        invalid_letters = [letter for letter in letters if letter not in valid_keys]
        if invalid_letters:
            return False, f"输入包含无效的选项字母 {invalid_letters}，当前题目的可用选项为: {', '.join(valid_keys)}"

        if q.get('type') == 'single' and len(letters) > 1:
            return False, "当前是单选题，但输入了多个选项字母！"

        return True, ""

    def _parse_options(self, options) -> list[str]:
        """
        解析选项.
        """
        parts = []
        if isinstance(options, str):
            parts = [o.strip() for o in options.split('\n') if o.strip()]
            if len(parts) <= 1:
                from api.answer_check import cut
                cut_parts = cut(options)
                if cut_parts:
                    parts = cut_parts
        elif isinstance(options, list):
            parts = [str(o).strip() for o in options if str(o).strip()]
        return parts

    def _extract_valid_keys(self, parts: list[str]) -> list[str]:
        """
        提取合法的选项字母.
        """
        valid_keys = []
        for p in parts:
            first_char = p[:1].upper()
            if first_char.isalpha():
                valid_keys.append(first_char)
        return valid_keys

    def _validate_text_match(self, ans: str, parts: list[str]) -> tuple[bool, str]:
        """
        验证用户输入的文本是否和选项文本匹配.
        """
        from api.answer_check import cut
        split_ans = cut(ans)
        if split_ans:
            for item in split_ans:
                matched = False
                for p in parts:
                    p_norm = re.sub(r'^[A-Za-z]\s*[.、:：)?）]?\s*', '', p).strip().lower()
                    if item.strip().lower() in p_norm or p_norm in item.strip().lower():
                        matched = True
                        break
                if not matched:
                    return False, f"输入的文本 '{item}' 在所有选项中均无法匹配，请输入合法的选项文本或字母"
        return True, ""

    def _normalize_user_input(self, ans: str, q: dict) -> Optional[str]:
        """
        规整化用户的手动输入答案.
        """
        if not ans:
            return None

        ans = ans.strip()
        if not ans:
            return None

        q_type = q.get('type')
        if q_type == 'judgement':
            return self._normalize_judgement_input(ans)
        elif q_type in ['single', 'multiple']:
            return self._normalize_choice_input(ans, q)
        return ans

    def _normalize_judgement_input(self, ans: str) -> str:
        """
        规整化判断题的手动输入.
        """
        val = ans.lower()
        if val in ['true', 't', '1', '对', '正确', '√', '是', 'yes', 'y']:
            return "正确"
        elif val in ['false', 'f', '0', '错', '错误', '×', '否', 'no', 'n', '不对', '不正确']:
            return "错误"
        return ans

    def _normalize_choice_input(self, ans: str, q: dict) -> str:
        """
        规整化选择题的手动输入.
        """
        options = q.get('options', '')
        parts = self._parse_options(options)
        valid_keys = self._extract_valid_keys(parts)

        letters = self._extract_option_letters(ans)
        if letters and all(letter in valid_keys for letter in letters):
            unique_ordered_letters = []
            for letter in letters:
                if letter not in unique_ordered_letters:
                    unique_ordered_letters.append(letter)
            return "\n".join(unique_ordered_letters)

        from api.answer_check import cut
        split_ans = cut(ans)
        if split_ans:
            return "\n".join(split_ans)
        return ans

    def _batch_query_flow(self, q_list: list[dict]) -> list[Optional[str]]:
        """
        执行批量手动搜题交互.
        """
        self._print_batch_questions(q_list)

        sep_desc = self.separator
        if self.separator == '\n':
            sep_desc = '换行 (每题一行)'
        elif self.separator == ' ':
            sep_desc = '空格'
        elif self.separator == '\t':
            sep_desc = 'Tab制表符'

        self._print_batch_instructions(sep_desc)

        while True:
            answers = []
            if self.separator == '\n':
                print(f"请直接粘贴或依次输入各题答案（每行一个，共 {len(q_list)} 行）：")
                for i in range(len(q_list)):
                    try:
                        ans = input(f"  第 {i + 1} 题答案: ").strip()
                    except EOFError:
                        ans = ""
                    answers.append(ans)
            else:
                raw_input = input(f"\n请一次性输入所有题目的答案 (使用 '{sep_desc}' 分割): ").strip()
                answers = self._split_batch_answers(raw_input, len(q_list))

            has_error, temp_answers = self._parse_and_validate_batch(q_list, answers)

            if has_error:
                print("\033[31m检测到存在不合规的答案，已拒绝确认，请重新输入！\033[0m")
                continue

            confirm = input("确认使用上述答案？[Y/n]: ").strip().lower()
            if confirm in ['', 'y', 'yes']:
                return temp_answers
            elif confirm == 'switch':
                return [self._single_query(q) for q in q_list]
            else:
                print("已取消，请重新输入，或输入 'switch' 切换为单题输入模式。")

    def _print_batch_questions(self, q_list: list[dict]) -> None:
        """批量打印题目内容及选项."""
        for idx, q in enumerate(q_list):
            type_str = self._get_type_display(q['type'])
            if q['type'] in ['single', 'multiple'] and q.get('options'):
                options = q['options']
                parts = []
                if isinstance(options, str):
                    parts = [o.strip() for o in options.split('\n') if o.strip()]
                    if len(parts) <= 1:
                        from api.answer_check import cut
                        cut_parts = cut(options)
                        if cut_parts:
                            parts = cut_parts
                elif isinstance(options, list):
                    parts = [str(o).strip() for o in options if str(o).strip()]

                options_text = "  ".join(parts)
                print(f"\n[{idx + 1}] 【{type_str}】 {q['title']} 选项: {options_text}")
            elif q['type'] == 'judgement':
                print(f"\n[{idx + 1}] 【{type_str}】 {q['title']} 选项: 正确 / 错误")
            else:
                print(f"\n[{idx + 1}] 【{type_str}】 {q['title']}")

    def _print_batch_instructions(self, sep_desc: str) -> None:
        """打印批量输入的使用引导说明."""
        print("\n" + "=" * 50)
        print("请依次输入每道题的答案。")
        print(f"格式要求：当前配置要求使用【{sep_desc}】分割各题的答案。")
        print("如果是多选题，答案中的多个选项直接连着写即可（例如：AB 或 AC）。")
        print("直接按回车或输入空格跳过的题，对应的答案将为空（会触发随机答题）。")
        if self.separator == '\n':
            print("粘贴多行时，每行会被解析为对应一题的答案。")
        else:
            print(f"示例输入: A{self.separator} B{self.separator} 正确{self.separator} 答案1, 答案2{self.separator} 错")
        print("=" * 50)

    def _split_batch_answers(self, raw_input: str, expected_len: int) -> list[str]:
        """根据配置的分割符将批量的答案进行分拆和补齐."""
        if not raw_input:
            return [''] * expected_len

        if self.separator in [';', '；']:
            raw_input = raw_input.replace('；', ';')
            answers = [ans.strip() for ans in raw_input.split(';')]
        elif self.separator in [',', '，']:
            raw_input = raw_input.replace('，', ',')
            answers = [ans.strip() for ans in raw_input.split(',')]
        else:
            answers = [ans.strip() for ans in raw_input.split(self.separator)]

        if len(answers) < expected_len:
            answers.extend([''] * (expected_len - len(answers)))
        elif len(answers) > expected_len:
            answers = answers[:expected_len]
        return answers

    def _parse_and_validate_batch(self, q_list: list[dict], answers: list[str]) -> tuple[bool, list[Optional[str]]]:
        """批量解析用户输入并进行合法性校验."""
        print("\n--- 解析答案结果 ---")
        has_error = False
        temp_answers = []
        for idx, (q, ans) in enumerate(zip(q_list, answers)):
            ok, err_msg = self._validate_user_input(ans, q)
            if not ok:
                has_error = True
                print(f"第 {idx + 1} 题: {q['title']} ---> \033[31m[错误: {err_msg}]\033[0m")
                temp_answers.append(None)
            else:
                normalized_ans = self._normalize_user_input(ans, q)
                temp_answers.append(normalized_ans)
                print(f"第 {idx + 1} 题: {q['title']} ---> 答案: {normalized_ans if normalized_ans else '[跳过/随机]'}")
        print("-------------------")
        return has_error, temp_answers


class DummyTiku(Tiku):
    def __init__(self, config_path: Optional[str] = None) -> None:
        """初始化空题库."""
        super().__init__(config_path)
        self.name = '空/禁用题库'
        self.DISABLE = True

    def _query(self, q_info: dict) -> Optional[str]:
        return None


PROVIDER_REGISTRY = {
    'TikuYanxi': TikuYanxi,
    'TikuGo': TikuGo,
    'TikuLike': TikuLike,
    'TikuAdapter': TikuAdapter,
    'TikuHive': TikuHive,
    'TikuJszkk': TikuJszkk,
    'AI': AI,
    'SiliconFlow': SiliconFlow,
    'TikuManual': TikuManual,
}
