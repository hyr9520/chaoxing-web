# -*- coding: utf-8 -*-
import functools
import json
import os
import random
import secrets
import re
import threading
import time
from difflib import SequenceMatcher
from enum import Enum, IntEnum
from hashlib import md5
from typing import Optional, Literal
from typing_extensions import Self

import requests
from loguru import logger
from requests import RequestException
from requests.adapters import HTTPAdapter
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception
from tqdm import tqdm

from api.answer import Tiku, TikuManual, CacheDAO, _assemble_subs
from api.answer_check import (cut, check_submittable, is_listening_question,
                              _is_letter_only, set_option_letters as _sol)
from api.learned import LearnedAnswers, normalize_title
from api.exceptions import RiskControlError, PauseInterrupt
from api.cipher import AESCipher
from api.config import GlobalConst as gc
from api.cookies import save_cookies, use_cookies
from api.decode import (
    decode_course_list,
    decode_course_point,
    decode_course_card,
    decode_course_folder,
    decode_questions_info,
)
from api.homework import (
    scan_works,
    fetch_work,
    submit_work,
    fetch_work_detail,
    redo_work,
    fetch_work_page,
    submit_dohw,
    iter_dohw_questions,
    fetch_all_works_once,
    fetch_all_works_detail,
    fetch_redo_info_batch,
    HomeworkNeedsAttention,
    HomeworkUnavailable,
    _WORK_STATUS_DONE,
    PAGE_WORK_DOHW,
)
from api.exam import (
    fetch_course_tokens,
    fetch_exam_list,
    parse_exam_list,
    is_exam_done,
)


def get_timestamp():
    return str(int(time.time() * 1000))


def _option_letters_of(options) -> set:
    """从题目的 options 文本里抽出合法选项字母集（如 {"A","B","C","D"}）。

    供 answer_check._is_letter_only 判定"连写字母"是否合法 —— 有了字母集，
    "review"/"origin" 这类英文单词（含选项外的字母）就能被正确排除，
    而 "ABC" 这种合法多选答案仍能通过。options 可能为空（听力/判断题），
    此时返回空集合，调用方 set_option_letters 会退回长度兜底行为。
    """
    if not options:
        return set()
    text = str(options)
    # 兼容 "A. xxx\nB. yyy" 与 "A、xxx" 两种前缀形态
    return {m.group(1).upper() for m in re.finditer(r"(?m)^\s*([A-Za-z])[.、)．:：]?", text)}


class SessionManager:
    _instance = None
    _login_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # ⚠️ 2026-10-03：曾尝试给这里加"只初始化一次"的守卫（第三方审计 B-08
        #    指出每次 `cls()` 都会重建 Session），但**已回退**：
        #    原行为"每次重建"同时意味着"每次只加载磁盘 cookie"，
        #    是**多账号切换时清干净旧 cookie 的隐式保证**。
        #    改成复用后，`cookies.update()` 只覆盖同名、不会删掉多余项，
        #    切换账号会残留上一个账号的 `_uid` → 有真实的串号风险。
        #    收益（连接复用）远小于该风险，故保持原样不动。
        self._session = requests.Session()
        self._session.mount("https://", HTTPAdapter(max_retries=10))
        self._session.mount("http://", HTTPAdapter(max_retries=10))
        self._session.request = functools.partial(self._session.request, timeout=5)
        # For debug purposes
        # self._session.verify=False
        self._session.headers.clear()
        self._session.headers.update(gc.HEADERS)
        self._session.cookies.update(use_cookies())

    @classmethod
    def get_instance(cls) -> Self:
        return cls()

    @classmethod
    def get_session(cls) -> requests.Session:
        instance = cls.get_instance()
        return instance._session

    @classmethod
    def update_cookies(cls):
        cls.get_instance()._session.cookies.update(use_cookies())

    @classmethod
    def relogin_if_needed(cls, chaoxing_instance) -> bool:
        with cls._login_lock:
            # 检查 cookie 会话是否仍然无效
            if chaoxing_instance._validate_cookie_session():
                return True

            logger.info("Cookie session invalid, attempting thread-safe relogin...")
            if chaoxing_instance.account and chaoxing_instance.account.username and chaoxing_instance.account.password:
                login_result = chaoxing_instance.login(login_with_cookies=False)
                if login_result.get("status"):
                    cls.update_cookies()
                    logger.info("Thread-safe relogin succeeded")
                    return True
                else:
                    logger.warning(f"Thread-safe relogin failed: {login_result.get('msg')}")
            return False


# ---- 自动避风控：**只认真实风控信号**（2026-09-29）----
# 只有平台明确给出风控信号（403 / 验证码拦截）才计数 —— 这些是硬信号。
# 累计 2 次就先冷却 10 分钟给账号喘息，把 `_note_risk_control` 原有的
# "3 次熔断中止"挡在前面：冷却期内风控消退，成功上报会把计数清零。
#
# ⚠️ 为什么**不**用"wait_video_passed 判定超时"当触发：平台判定慢是常态
# （实测正常视频要 9~19 分钟才通过），拿它当风控信号会误伤正常刷课。
_RISK_HITS = 0              # 连续真实风控信号次数
_COOLDOWN_UNTIL = 0.0       # 冷却截止时间戳
_RISK_THRESHOLD = 2         # 累计几次真实风控后冷却（熔断阈值是 3，留一次余量）
_COOLDOWN_SECS = 600        # 冷却时长（秒）
_RISK_LOCK = threading.Lock()


def note_risk_cooldown(where: str = ""):
    """记一次真实风控信号；达到阈值就开启冷却。返回是否触发。"""
    global _RISK_HITS, _COOLDOWN_UNTIL
    with _RISK_LOCK:
        _RISK_HITS += 1
        hits = _RISK_HITS
        if hits >= _RISK_THRESHOLD:
            _RISK_HITS = 0
            _COOLDOWN_UNTIL = time.time() + _COOLDOWN_SECS
            logger.warning("连续 {} 次真实风控信号（{}）-> 冷却 {} 分钟（挡在熔断中止之前）",
                           hits, where or "未知", _COOLDOWN_SECS // 60)
            return True
    return False


def note_risk_clear():
    """一次成功上报：清零风控计数。"""
    global _RISK_HITS
    with _RISK_LOCK:
        _RISK_HITS = 0


def cooldown_wait():
    """worker 开始新任务点前调用：冷却期内主动等待，冷却结束自动继续。"""
    while True:
        remain = _COOLDOWN_UNTIL - time.time()
        if remain <= 0:
            return
        logger.info("风控冷却中，剩余 {} 秒后自动继续", int(remain))
        time.sleep(min(remain, 30))


def cooldown_state() -> int:
    """给界面/诊断用：还剩多少秒冷却。"""
    return max(0, int(_COOLDOWN_UNTIL - time.time()))


class Account:
    username = None
    password = None
    last_login = None
    isSuccess = None

    def __init__(self, _username, _password):
        self.username = _username
        self.password = _password


class RateLimiter:
    def __init__(self, call_interval):
        self.last_call = time.time()
        self.lock = threading.Lock()
        self.call_interval = call_interval

    def limit_rate(self, random_time=False, random_min=0.0, random_max=1.0):
        with self.lock:
            now = time.time()
            base_wait = max(self.last_call + self.call_interval - now, 0)
            extra_wait = random.uniform(random_min, random_max) if random_time else 0
            call_wait = base_wait + extra_wait
            self.last_call = now + call_wait

        time.sleep(call_wait)


class StudyResult(Enum):
    SUCCESS = 0
    FORBIDDEN = 1  # 403
    ERROR = 2
    TIMEOUT = 3
    PENDING = 4  # 视频已播完但平台未判通过：不阻塞，交后台线程等判定

    def is_success(self):
        return self == StudyResult.SUCCESS

    def is_failure(self):
        # PENDING 不是失败：它表示"工作已做完，等平台判定"，由后台线程收尾
        return self not in (StudyResult.SUCCESS, StudyResult.PENDING)


class SignType(IntEnum):
    NORMAL = 0
    GESTURE = 3
    LOCATION = 4


class ActivityStatus(IntEnum):
    ACTIVE = 1
    INACTIVE = 2


class ActivityType(IntEnum):
    SIGNIN = 2


def multi_cut(answer: str, origin_html_content="", logger=logger):
    """
    将多选题答案字符串按特定字符进行切割, 并返回切割后的答案列表
    """
    res = cut(answer)
    if res is None:
        logger.warning(
            f"未能从网页中提取题目信息, 以下为相关信息：\n\t{answer}\n\n{origin_html_content}\n"
        )
        logger.warning("未能正确提取题目选项信息! 请反馈并提供以上信息")
        return None
    else:
        return res


def clean_res(res):
    cleaned_res = []
    if isinstance(res, str):
        res = [res]
    for c in res:
        # 仅在字符串长度大于1时才尝试去除开头的字母编号，防止误删单个字母答案
        cleaned = re.sub(r'^[A-Za-z]\s*[.、:：)?）]?\s*|[.,!?;:，。！？；：]', '', c) if len(c) > 1 else c
        cleaned_res.append(cleaned.strip())
    return cleaned_res


def normalize_text(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    # 统一常见异体字符，降低“风/⻛”类差异导致的匹配失败。
    char_map = str.maketrans({
        '⻛': '风',
        '⻔': '门',
        '⻋': '车',
        '⻢': '马',
    })
    normalized = text.translate(char_map)
    normalized = re.sub(r'^[A-Za-z]\s*[.、:：)?）]?\s*', '', normalized)
    normalized = re.sub(r'\s+', '', normalized)
    normalized = re.sub(r'[，。！？；：,.!?;:()（）\[\]【】"“”‘’\-_/\\|]', '', normalized)
    return normalized.lower()


def get_option_text(option: str) -> str:
    return re.sub(r'^[A-Za-z]\s*[.、:：)?）]?\s*', '', option).strip()


def best_option_by_similarity(target: str, options: list, threshold: float = 0.8) -> str:
    if not target or not options:
        return ""
    target_norm = normalize_text(target)
    if not target_norm:
        return ""

    best_letter = ""
    best_score = 0.0
    for option in options:
        option_text = get_option_text(option)
        option_norm = normalize_text(option_text)
        if not option_norm:
            continue
        score = SequenceMatcher(None, target_norm, option_norm).ratio()
        if score > best_score:
            best_score = score
            best_letter = option[:1]

    if best_score >= threshold:
        logger.info(f"相似度兜底匹配成功: {best_letter} (score={best_score:.2f}, threshold={threshold:.2f})")
        return best_letter
    return ""


def is_subsequence(a, o):
    iter_o = iter(o.lower())
    return all(c in iter_o for c in a.lower())


def normalize_listening_answer(answer) -> str:
    """把听力题的多小问答案规范成平台要求的连写字母形式。

    平台的 answer 字段是"一道题一个字符串"，听力题（一个题组含多个小问，
    answertype=19）需要按小问顺序连写字母，如 "CADB"。

    而题库/AI 可能返回多种形态，这里统一转换：
      - "22.C 23.A 24.D 25.B"        -> "CADB"（带题号）
      - "16.B、17.C、18.D"            -> "BCD"
      - ["C", "A", "D"]              -> "CAD"（已经是字母列表）
    若无法识别成多小问形态，原样返回，避免误伤普通单选/多选答案。
    """
    if not answer:
        return answer

    # 已经是连写字母（如 "CADB"），直接规范化大小写
    if isinstance(answer, str) and re.fullmatch(r"[A-Za-z]{2,}", answer.strip()):
        return answer.strip().upper()

    # 列表形式：逐项取字母
    if isinstance(answer, (list, tuple)):
        letters = []
        for item in answer:
            m = re.search(r"(?<![A-Za-z])([A-Za-z])(?![A-Za-z])", str(item))
            if m:
                letters.append(m.group(1).upper())
        if len(letters) >= 2:
            return "".join(letters)
        return answer

    text = str(answer).strip()
    # "序号 + 字母" 形态（如 "22.C 23.A 24.D 25.B"）
    pairs = re.findall(r"(?<![A-Za-z])\d{1,3}\s*[.、,，:：)\]]?\s*([A-Za-z])(?![A-Za-z])", text)
    if len(pairs) >= 2:
        return "".join(p.upper() for p in pairs)
    return answer


def _coerce_options_str(options) -> str:
    """把各种形态的 options 统一成字符串（2026-09-27 新增）。

    起因（真机实证）：考试链路传的 options 是 **list[dict]**
    `[{letter:'A', value:'C', text:'圣诞节'}, …]`，而选项内容→字母的映射
    函数一律按 **str** 处理 → `multi_cut` 拿到 list 直接失败 → 映射结果为空
    → 日志刷「答案无法映射为选项字母，已舍弃」→ **考试每道选择题的答案全被丢**。
    （作业链路传的是字符串，所以这个坑一直没暴露。）

    - str          → 原样
    - list[dict]   → "A. 圣诞节\nB. 端午节"（给 map_* 做文本匹配用）
    - list[str]    → 直接拼接
    """
    if isinstance(options, (list, tuple)):
        lines = []
        for o in options:
            if isinstance(o, dict):
                letter = str(o.get("letter") or o.get("value") or "").strip()
                text = str(o.get("text") or "").strip()
                if letter or text:
                    lines.append(f"{letter}. {text}" if text else letter)
            elif str(o).strip():
                lines.append(str(o))
        return "\n".join(lines)
    return str(options or "")


def map_single_answer(res, options_str: str, origin_html_content="") -> str:
    """普通单选：把题库/AI 返回的「选项内容（词）」映射回「选项字母」。

    平台对单选题只认选项字母（如 "C"）。题库/AI 常返回选项内容本身
    （如 "review"），直接提交会被平台拒绝（"作业提交失败！"，2026-09-11 实测）。

    返回映射出的字母；无法映射时返回空串（调用方走随机作答），
    避免把非法格式提交给平台。
    """
    options_str = _coerce_options_str(options_str)   # ← 2026-09-27 兼容 list[dict]
    res_text = str(res).strip()
    # 已是单个选项字母：直接采用。
    # 关键：不能再做子序列匹配——单字母会误命中"第一个含该字母的选项"
    # （如 "C" 会撞上 "A commitment"，把答案从 C 改错成 A）
    if re.fullmatch(r"[A-Za-z]", res_text):
        return res_text.upper()

    options_list = multi_cut(options_str, origin_html_content)
    if not options_list:
        return ""

    def _norm(s: str) -> str:
        """匹配用规范化：剥离选项编号前缀（仅在"字母+分隔符"同时存在时，
        避免把词首字母误删，如 "review"→"eview"）、去空白标点、统一小写。"""
        s = str(s).strip()
        m = re.match(r"^[A-Za-z][\s.、,，:：)）\]】|]+(.+)$", s)
        if m:
            s = m.group(1)
        s = re.sub(r"[\s，。！？；：,.!?;:()（）\[\]【】\"“”‘’\-_/\\|&]+", "", s)
        return s.lower()

    target_norm = _norm(res_text)
    if not target_norm:
        return ""
    # ① 精确匹配：选项文本（去编号前缀后）与答案一致
    for o in options_list:
        if _norm(o) == target_norm:
            return o[:1]
    # ② 子序列兜底（容忍标点/多余字符；过短的目标跳过，防误匹配）
    if len(target_norm) >= 3:
        for o in options_list:
            opt_norm = _norm(o)
            if opt_norm and is_subsequence(target_norm, opt_norm):
                return o[:1]
    # ③ 相似度兜底
    return best_option_by_similarity(res_text, options_list, threshold=0.8)


def map_multiple_answer(res, options_str: str, origin_html_content="") -> str:
    """普通多选：把题库/AI 返回的多段「选项内容」映射回「选项字母串」（如 "ABC"）。

    与 map_single_answer 同源问题（2026-09-14 实测）：AI 提示词按设计要求
    "输出选项内容而非字母"，而平台多选题只认连写字母。映射兜底此前只覆盖
    single，多选题答案被 plausible_answer 以"非纯字母"拒掉 → 整题丢弃 →
    随机作答（实测 3.2 三道多选题全部随机，白丢分）。

    返回排序去重后的字母串；无法映射时返回空串（调用方走随机作答）。
    """
    options_str = _coerce_options_str(options_str)   # ← 2026-09-27 兼容 list[dict]
    res_text = str(res).strip()
    if not res_text:
        return ""

    options_list = multi_cut(options_str, origin_html_content) or []
    if not options_list:
        return ""

    # ① 已是「字母串」形态（"ABC" / "A B" / "A,B"）：直接规范化。
    #    不能再走内容匹配 —— 单字母段会误命中首个含该字母的选项。
    parts = [p for p in re.split(r"[\s,，、;；|/]+", res_text) if p]
    if parts and all(re.fullmatch(r"[A-Za-z]", p) for p in parts):
        return "".join(sorted({p.upper() for p in parts}))
    #    连写形态（"ABC"）：靠"字母是否都在选项字母集里"排除英文单词
    #    （"review" 含 R/E/V/I/W，必然落在选项集外）。
    if re.fullmatch(r"[A-Za-z]{2,8}", res_text):
        _letters = {o[:1].upper() for o in options_list}
        if all(c.upper() in _letters for c in res_text):
            return "".join(sorted({c.upper() for c in res_text}))

    # ② 逐段内容映射（复用"内容 → 字母"的单选逻辑）
    segments = [s for s in re.split(r"[\n|｜、;；]+", res_text) if s.strip()]
    letters = "".join(filter(None, (map_single_answer(s, options_str,
                                                      origin_html_content)
                                    for s in segments)))
    # ③ 逐段全失败则整串再试一次（防止答案被误切碎）
    if not letters:
        letters = map_single_answer(res_text, options_str, origin_html_content) or ""
    return "".join(sorted(set(letters))) if letters else ""


def random_answer(options: str, q_type: str) -> str:
    answer = ""
    # 填空 / 简答：没有选项可随机，返回一个"看起来像答案"的占位值。
    # 2026-09-13 补：此前无分支且被上面的 `if not options` 提前拦截，
    # 导致这两类题遇题库未命中时填空值恒为空串 —— 平台侧等于没作答，
    # 拉低覆盖率还可能触发"未作答不可提交"。填占位至少保留得分机会
    # （作业/测验可重做，下一轮用平台反馈的正确答案覆盖）。
    if q_type in ("completion", "shortanswer"):
        answer = random.choice(["略", "无", "待补充"])
        logger.info(f"随机选择（{q_type} 占位）-> {answer}")
        return answer
    # 未知题型且没有选项：答案大概率是文字（名词解释/论述/计算…），
    # 用文字占位；随机字母对这类题毫无意义（2026-09-13 补）。
    if q_type == "unknown" and not options:
        answer = random.choice(["略", "无", "待补充"])
        logger.info(f"随机选择（未知题型占位）-> {answer}")
        return answer
    if not options:
        return answer

    if q_type == "multiple":
        logger.debug(f"当前选项列表[cut前] -> {options}")
        _op_list = multi_cut(options)
        logger.debug(f"当前选项列表[cut后] -> {_op_list}")

        if not _op_list:
            logger.error(
                "选项为空, 未能正确提取题目选项信息! 请反馈并提供以上信息"
            )
            return answer

        available_options = len(_op_list)
        select_count = 0

        # 根据可用选项数量调整可能选择的选项数
        if available_options <= 1:
            select_count = available_options
        else:
            max_possible = min(4, available_options)
            min_possible = min(2, available_options)

            weights_map = {
                2: [1.0],
                3: [0.3, 0.7],
                4: [0.1, 0.5, 0.4],
                5: [0.1, 0.4, 0.3, 0.2],
            }

            weights = weights_map.get(max_possible, [0.3, 0.4, 0.3])
            possible_counts = list(range(min_possible, max_possible + 1))

            weights = weights[:len(possible_counts)]

            weights_sum = sum(weights)
            if weights_sum > 0:
                weights = [w / weights_sum for w in weights]

            select_count = random.choices(possible_counts, weights=weights, k=1)[0]

        selected_options = random.sample(_op_list, select_count) if select_count > 0 else []

        for option in selected_options:
            answer += option[:1]  # 取首字为答案，例如A或B

        answer = "".join(sorted(answer))
    elif q_type == "single":
        answer = random.choice(options.split("\n"))[:1]  # 取首字为答案, 例如A或B
    # 判断题处理
    elif q_type == "judgement":
        answer = "true" if random.choice([True, False]) else "false"
    logger.info(f"随机选择 -> {answer}")
    return answer


def _random_exam_answer(q: dict, q_type: str) -> str:
    """考试题随机兜底（何同学要求：**考试链路不能空着**）。

    与 `random_answer()` 的区别 —— 考试题的 `options` 是 **list[dict]**
    （`{"letter", "value", "text"}`，见 `api/exam.py` 的解析；且平台会把选项
    乱序，`letter` ≠ `value`），而 `random_answer()` 是按字符串
    `split("\\n")` 取首字母的 —— 直接把 list 传进去会 AttributeError。

    所以这里直接按**显示字母**随机取，返回值交给 `exam.build_answer_form`
    的 `_as_choice()` 映射成真实提交值 ⇒ 平台乱序也不会错位。

    返回空串表示"确实无选项可随机"（调用方据此决定是否算跳过）。
    """
    letters: list = []
    opts = q.get("options")
    if isinstance(opts, (list, tuple)):
        for o in opts:
            if isinstance(o, dict):
                L = str(o.get("letter") or "").strip().upper()
                if L and L not in letters:
                    letters.append(L)
    elif isinstance(opts, str) and opts.strip():
        for m in re.finditer(r"(?m)^\s*([A-Za-z])[\.、:：]?\s", opts):
            L = m.group(1).upper()
            if L not in letters:
                letters.append(L)

    if not letters:
        # 填空 / 简答 / 未知题型没有选项可随机 → 复用占位词（与 random_answer 一致），
        # 至少避免"平台侧完全未作答"。
        if q_type in ("completion", "shortanswer", "unknown"):
            ans = random.choice(["略", "无", "待补充"])
            logger.info(f"随机兜底（{q_type} 占位）-> {ans}")
            return ans
        return ""

    if q_type == "judgement":
        ans = random.choice(["true", "false"])
    elif q_type == "multiple" and len(letters) > 1:
        ans = "".join(sorted(random.sample(letters, min(2, len(letters)))))
    else:
        ans = random.choice(letters)
    logger.info(f"随机兜底（{q_type}，{len(letters)} 个选项）-> {ans}")
    return ans


_TRUE_WORDS = ("true", "t", "对", "正确", "是", "√", "1", "yes", "y", "ri")
_FALSE_WORDS = ("false", "f", "错", "错误", "否", "×", "0", "no", "n", "wr")


def _norm_true(x) -> bool:
    """判断题答案归一化成 True/False —— 只用于"库 vs AI 是否一致"的比较。

    ⚠️ 与 `api/exam.py:_as_judge` 的区别：那个识别不了时**默认 true**（提交用，
       偏保守地给个合法值）；这里识别不了**一律 False**（比较用，宁可判"不一致"
       也不要误判成"一致"从而跳过核对）。
    """
    s = str(x or "").strip().lower()
    if s in _TRUE_WORDS:
        return True
    if s in _FALSE_WORDS:
        return False
    if len(s) <= 8:                     # 只对短文本做包含判断，避免长句误伤
        if "正确" in s or "对" in s:
            return True
        if "错误" in s or "错" in s:
            return False
    return False


def _judge_from_text(text: str) -> str:
    """从**任意文本**里提取判断题答案，返回 `正确`/`错误`；提取不出返回 ""。

    与 `_norm_true` 的区别（必须分清）：
      · `_norm_true` 识别不了时返回 **False** —— 用于"库 vs AI 比较"，
        宁可判"不一致"也不要漏掉复核。
      · 本函数识别不了时返回 **""** —— 用于**提交前格式闸门**，
        宁可交回原值让人看出来，也不能凭空造一个判断值。

    实测事故（2026-10-12 acc2 第 71 题）：AI 对判断题返回了
    `这是人类航天史上的里程碑` —— 这是**解释**不是答案。
    旧逻辑直接 return，于是平台收到非法值。
    """
    s = str(text or "").strip().lower()
    if not s:
        return ""
    # 判断题的 A/B 约定：A=正确、B=错误（与考试页选项一致）。
    # ⚠️ 必须**先于**词表查 —— `_TRUE_WORDS`/`_FALSE_WORDS` 里没有 a/b。
    if s in ("a", "t"):
        return "正确"
    if s in ("b", "f"):
        return "错误"
    if s in _TRUE_WORDS:
        return "正确"
    if s in _FALSE_WORDS:
        return "错误"
    # 否定式**必须先于**肯定式判断，否则「不正确」会被 "正确" 命中成"正确"
    if any(k in s for k in ("不正确", "不对", "并非", "不是", "没有", "错误")):
        return "错误"
    if any(k in s for k in ("正确", "是对的", "没错", "成立")):
        return "正确"
    return ""


def _vote_raw(tiku, q: dict, n: int = 2) -> list:
    """连续调 n 次 `force_retry`（每次**内部换槽位** ⇒ 不同模型），返回原始答案列表。

    这是所有"多模型投票"的公共底座（判断题、多选题共用），避免重复实现。

    实现要点：
      · 必须对**同一个 provider 连续调 n 次**，不能"每个 provider 各取一票"：
        `force_retry` 每次都会 `_next_key()` 切到下一个槽位 ⇒ 拿到不同模型。
        （自测抓到：按 provider 循环时只有 1 个 provider，只得到 1 票。）
      · 任一票拿不到就**提前返回已有票数**，不阻塞主流程。
    """
    out = []
    providers = getattr(tiku, "providers", None) or [tiku]
    ai = next((p for p in providers
               if callable(getattr(p, "force_retry", None))), None)
    if ai is None:
        return out
    for _ in range(n):
        try:
            a = ai.force_retry(q)
        except Exception:                                        # noqa: BLE001
            break
        if not (a and str(a).strip()):
            break
        out.append(a)
    return out


def _judge_vote(tiku, q: dict, n: int = 2) -> list:
    """判断题：返回 `['正确'/'错误', ...]`（基于 `_vote_raw` 归一）。"""
    return ["正确" if _norm_true(a) else "错误" for a in _vote_raw(tiku, q, n)]


def _ai_last_resort(tiku, q: dict) -> str:
    """**随机蒙之前的最后一道**：让 AI 换个模型再答一次（2026-09-29 新增）。

    用户原话：「随机的话风险就大了，只能是最下下下策」。

    为什么要在这一层补：`AI._ensure_usable` 只覆盖 AI 自己的 `_query` 路径，
    而**章节检测/作业的随机兜底在本文件三处**（`1663` / `1770` / `3308`），
    触发条件是"query 返回空"或"答案映射失败" —— 那时 AI 层已经交过答案、
    不会再重试，于是照样随机。实测：18:50 acc5 就出过 1 次随机
    （补丁已注入，但拦不到这条路径）。

    返回：可用答案（非空字符串）；拿不到则返回 ""，调用方照常随机。
    任何异常一律吞掉 —— 它只是"加分项"，绝不能影响主流程。
    """
    try:
        cands = getattr(tiku, "providers", None) or [tiku]
        for p in cands:
            fn = getattr(p, "force_retry", None)
            if callable(fn):
                ans = fn(q)
                if ans and str(ans).strip():
                    return _map_rescue_answer(str(ans).strip(), q)
    except Exception as e:                                     # noqa: BLE001
        logger.debug(f"随机前 AI 重答失败（忽略，照常随机）：{e}")
    return ""


def _map_rescue_answer(ans: str, q: dict) -> str:
    """把 AI「救援答案」从**选项内容**映射回**选项字母**（2026-09-30 修）。

    为什么必须有这一步 —— 4.3「下列不属于国内探方编码的是」实证：
    `force_retry` 换模型重答**成功**，返回 `'a\\nI\\nα'`，但那正是 AI 按提示词
    设计要求输出的「选项内容」；而**救援分支在 `if not res:` 里，完全跳过了
    主流程的"内容→字母"映射**，于是原样填进表单 →
    `check_submittable('a\\nI\\nα', 'multiple')` 判 False → 整题被判"不可提交"
    → 覆盖率跌破阈值 → 只暂存不提交（用户看到的就是"这一节像没做"）。

    返回：映射成功 → 字母串；映射不出且原格式不合法 → 空串（让上层走随机，
    至少保证提交格式合法）；其余情况原样返回。
    """
    t = q.get("type")
    if t not in ("single", "multiple"):
        return ans
    opts = q.get("options") or ""
    # 🔴 2026-10-12 真机事故（acc2《机器的征途》第 50 题）：**这里必须先 coerce**。
    #   `map_multiple_answer` 内部有 `_coerce_options_str`（能把 list[dict] 转成
    #   "A. xxx\nB. xxx"），但**本行的校验没有** —— 直接把 `list[dict]` 喂给
    #   `multi_cut`（它按 HTML/文本解析）⇒ 解析出 `["'", '[', '{']` 这种垃圾
    #   ⇒ `_valid` 全废 ⇒ 映射结果被判"含非法选项字母"而**丢弃**
    #   ⇒ 原样返回 `NMD`（选项内容）→ 平台判 0 分。
    #   与 2026-09-27 那次是同一个病的另一处漏网（那次只修了 map_* 内部）。
    opts_str = _coerce_options_str(opts)
    try:
        mapped = (map_multiple_answer(ans, opts_str) if t == "multiple"
                  else map_single_answer(ans, opts_str))
        # ⚠ 映射结果必须"每个字母都落在本题选项字母集里"才算可信。
        #   实测（4.3 的单字符选项 `A a / B I / C α / D T1`）map_multiple_answer
        #   会吐出 `'ACI'` —— 而 'I' 根本不是本题的选项字母，属误映射。
        #   这种情况宁可弃用、走随机（随机出来的格式一定合法）。
        if mapped:
            _valid = {o[:1].upper() for o in (multi_cut(opts_str, "") or [])
                      if o[:1].strip()}
            if not _valid or all(c in _valid for c in mapped):
                logger.info(f"救援答案已映射为选项字母：{ans[:40]!r} -> {mapped!r}")
                return mapped
            logger.warning(
                f"救援答案映射结果 {mapped!r} 含非法选项字母"
                f"（本题合法 {sorted(_valid)}），弃用改走随机")
        # 映射不出来：若原答案格式对平台就不合法（如多选题带着内容串），
        # 交上去必然解析不了 —— 返回空串，交由上层随机（格式一定合法）。
        if not check_submittable(ans, t):
            logger.warning(
                f"救援答案无法映射为选项字母，改走随机兜底：{ans[:40]!r}")
            return ""
    except Exception as e:                                     # noqa: BLE001
        logger.debug(f"救援答案映射异常（原样返回）：{e}")
    return ans


def _parse_work_record_list(html_text: str) -> list[tuple[int, float]]:
    """
    解析章节检测作答记录列表页面（/work/record-list）。

    Args:
        html_text: record-list 页面 HTML

    Returns:
        作答记录列表，元素为 (作答序号times, 成绩score)，例如 [(0, 80.0), (1, 100.0)]
    """
    records = []
    times_list = re.findall(r'viewNum">第(\d+)次', html_text)
    scores = re.findall(r'viewScore">([\d.]+)分', html_text)
    # 🔴 2026-10-08 移植上游 PR #626（2026-09-28 已合并）：
    #   次数/成绩数量不齐时，`zip` 会**静默错位**（把第 N 次的成绩配到第 M 次上），
    #   导致后续"分数捷径"判断拿到错误的历史最高分。页面改版或部分成绩未出时即触发。
    #   兜底：数量不齐时成绩全部按 0 处理 —— 放弃分数捷径，仅做逐题比对（更保守）。
    if len(times_list) != len(scores):
        logger.warning(f"作答记录次数/成绩数量不匹配({len(times_list)}/{len(scores)})，成绩按0处理")
        scores = ['0'] * len(times_list)
    for t, s in zip(times_list, scores):
        try:
            records.append((int(t), float(s)))
        except ValueError:
            continue
    return records


def _parse_work_record_detail(html_text: str) -> list[dict]:
    """
    解析章节检测单次作答详情页面（/work/record-detail）。

    Args:
        html_text: record-detail 页面 HTML

    Returns:
        每题信息列表：{id, title, type_label, my_answer, correct_answer, parse_ok}
        parse_ok=False 表示"我的答案/正确答案"至少一个未能从页面提取出来
        （页面结构变体），调用方**不得**据此判定对错（否则两侧同为 '' 会被误判"全对"）。
    """
    questions = []
    # 兼容两种详情页结构：class 在前（"TiMu ... singleQuesId"）或 data 在前
    # （"singleQuesId" 紧跟前缀，如 data 属性排在 class 之前）。旧正则只认前者，
    # 漏匹配时 detail 为空 -> 成绩检查被跳过 -> 0 分也被当成"通过"。
    _q_pattern = re.compile(
        r'<div[^>]*class="[^"]*singleQuesId[^"]*"[^>]*data="(\d+)"[^>]*>'
        r'|<div[^>]*data="(\d+)"[^>]*class="[^"]*singleQuesId[^"]*"[^>]*>',
        re.S,
    )
    _starts = [(m.start(), m.group(1) or m.group(2)) for m in _q_pattern.finditer(html_text)]
    for idx, (pos, qid) in enumerate(_starts):
        end = _starts[idx + 1][0] if idx + 1 < len(_starts) else len(html_text)
        qb = html_text[pos:end]

        # 题型 + 题目
        tm = re.search(r'newZy_TItle">(.*?)</span>(.*?)</div>', qb, re.S)
        if tm:
            type_label = re.sub(r'<[^>]+>', '', tm.group(1)).strip()
            title = re.sub(r'<[^>]+>', '', tm.group(2))
        else:
            type_label = ""
            title = ""
        title = re.sub(r'\s+', ' ', title).strip()

        # 我的答案/正确答案：多模式匹配，兼容属性顺序变化与额外 class 的页面变体
        #
        # 🔴 2026-10-08 移植上游 PR #626（2026-09-28 已合并）：
        #   原为**单模式** `我的答案：</span><div class="fl answerCon">`。
        #   页面变体（class 带额外后缀、换成 span 承载）时两个字段**同时**提取失败 ⇒
        #   两侧都是空串 ⇒ 调用方逐题比对时 `'' == ''` ⇒ **误判"全部正确"**
        #   —— 这是"0 分也当通过"的假成功路径。
        #   改为多模式 + `parse_ok` 标记，把"没解析出来"与"解析出来是空"区分开。
        _ans_patterns = [
            r'{label}[:：]</span>\s*<div class="fl answerCon">\s*(.*?)\s*</div>',
            r'{label}[:：]\s*</span>\s*<div[^>]*class="[^"]*answerCon[^"]*"[^>]*>\s*(.*?)\s*</div>',
            r'{label}[:：]\s*</span>\s*<span[^>]*>\s*(.*?)\s*</span>',
        ]

        def _extract_answer(label):
            """返回提取到的答案文本；**未匹配到返回 None**（与"匹配到但为空"区分）。"""
            for _pat in _ans_patterns:
                _m = re.search(_pat.format(label=label), qb, re.S)
                if _m:
                    return re.sub(r'<[^>]+>', '', _m.group(1)).strip()
            return None

        my_raw = _extract_answer('我的答案')
        correct_raw = _extract_answer('正确答案')

        questions.append({
            "id": qid,
            "title": title,
            "type_label": type_label,
            "my_answer": my_raw or '',
            "correct_answer": correct_raw or '',
            # 两侧都提取到才算可信；任一为 None ⇒ 该题不参与对错判定
            "parse_ok": my_raw is not None and correct_raw is not None,
        })
    # 🔴🔴 2026-10-08 **修复本项目丢失的返回值**（上游一直有 `return questions`）：
    #   本项目该函数从 2026-10-03（更早的备份同样）起**缺少本行** ⇒ 函数恒返回 None ⇒
    #   所有调用方都拿不到数据：
    #     · `_verify_submit_accepted`（提交后自检）→ 恒判"解析为空，跳过自检"
    #       ⇒ **「答案是否真被平台接收」这道保护形同虚设**（本项目自己注释里
    #         明确说"接口成功 ≠ 答案被接收"，这道检查正是为此而设）。
    #     · `_check_work_result` 主路线 → 恒走"回退到题目页作答块解析"分支。
    #   注意：返回空列表 `[]`（页面确实无题目）与返回 `None` 都会让调用方走
    #   `if not detail` 分支，行为一致；只有**页面确实解析出题目**时才会改变走向。
    return questions


# 🔴 2026-10-08 移植上游 PR #626（2026-09-28 已合并）：答案文本归一化。
#
#   背景：`_check_work_result` 的老结构详情页路径用 `my_answer != correct_answer`
#   逐题比对。但两处文本常因**纯格式差异**不等，导致"实际全对却判定有错" ⇒
#   触发**无谓重做**（重做会重新提交，增加请求量与风控暴露；对"仅允许作答一次"
#   的课程还可能直接失败）。
#
#   实测差异形态：
#     · 判断题：页面写"对"，答案是"true"/"T"/"√"/"正确"
#     · 选项：页面写"A."，答案写"A"
#     · 标点：全角"、"vs 半角","、句末多余标点
#
#   归一化是**确定性**的：两侧同格式必然相等；跨格式差异（字母 vs 选项全文）
#   不在此处理，交由调用方的满分/分数兜底。
_JUDGE_TRUE_SET = {'TRUE', 'T', '1', '对', '正确', '√', '是', 'YES', 'Y'}
_JUDGE_FALSE_SET = {'FALSE', 'F', '0', '错', '错误', '×', 'X', '否', 'NO', 'N',
                    '不对', '不正确'}


def normalize_answer_text(ans) -> str:
    """作答详情答案归一化：消除 对/错 与 true/false、全半角标点等格式差异。"""
    s = re.sub(r'\s+', '', str(ans or '')).upper()
    if s in _JUDGE_TRUE_SET:
        return 'TRUE'
    if s in _JUDGE_FALSE_SET:
        return 'FALSE'
    if re.fullmatch(r'[A-Z0-9]+', s):
        return s
    # 形如 "A." / "A、" / "A:" / "A)" 的**单个选项字母**：归一化为字母本身。
    #   上游原实现会先删掉 "A." 前缀得到**空串** ⇒ 与 "A" 不相等 ⇒ 误判为错。
    #   本项目详情页常见 "A"、"A."、"A、" 混用，故补这条（只命中"字母+一个标点"）。
    m_opt = re.fullmatch(r'([A-Z])[.、:：?？)）]', s)
    if m_opt:
        return m_opt.group(1)
    # 去掉开头的选项字母前缀（"A." "A、" "A:" "A)" "A？" "A）"）
    s = re.sub(r'^[A-Z][.、:：)?）]', '', s)
    # 去掉句中/句末标点（全半角）
    s = re.sub(r'[，。、！？；：,.!?;:()（）\[\]【】"“”‘’\-_/\\|]', '', s)
    return s


def _parse_answer_blocks(html_text: str) -> list[dict]:
    """
    解析"已完成"测验详情页（mooc-ans/api/work）的逐小问作答块。

    详情页每个小问渲染为一个 <div class="newAnswerBx">，内含：
      - <div class="fl answerCon">  -> 我的答案文本
      - <span class="scoreNum">     -> 该小问得分
      - <span class="marking_dui|marking_cuo|marking_bandui"> -> 对错标记

    该结构在"平台不返回正确答案"的测验上同样可用（record-detail 路线取不到时的
    唯一数据源，2026-09-11 实测）。据此可实现"错题学习"：答对沉淀正确答案、
    答错记录负反馈。

    Returns:
        按页面顺序的列表：{"my_answer": str, "score": float|None, "mark": "dui|cuo|bandui|"}
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    try:
        soup = BeautifulSoup(html_text, "html.parser")
    except Exception:
        return []
    out = []
    for b in soup.find_all("div", class_="newAnswerBx"):
        mya = b.find("div", class_="answerCon")
        sc = b.find("span", class_="scoreNum")
        mark = ""
        for e in b.find_all(True):
            cls = " ".join(e.get("class") or [])
            if "marking_bandui" in cls:      # 必须先于 dui 判断（"bandui" 含子串 "dui"）
                mark = "bandui"
                break
            if "marking_dui" in cls:
                mark = "dui"
                break
            if "marking_cuo" in cls:
                mark = "cuo"
                break
        score = None
        if sc is not None:
            try:
                score = float(sc.get_text(strip=True))
            except (TypeError, ValueError):
                score = None
        out.append({
            "my_answer": mya.get_text(strip=True) if mya is not None else "",
            "score": score,
            "mark": mark,
        })
    return out


def _group_blocks_by_question(questions: list, blocks: list) -> list[dict]:
    """把"小问级"作答块按题组聚合（用 answerField 的小问字段数切分），与 questions 对齐。

    返回 [{"title", "my_answer", "mark", "score"}]：
      - 单选/听力：my_answer 为各小问字母连写（与提交形态一致，如 "BA"）
      - 其他题型：按小问全文用 " | " 连接
      - mark 聚合：全 dui 才算 dui；任一 cuo 为 cuo；否则 bandui/unknown
    数量不匹配（页面结构异常）时返回空列表，调用方跳过学习记录。
    """
    def _sub_field_count(q: dict) -> int:
        qid = str(q.get("id", ""))
        af = q.get("answerField") or {}
        subs = [k for k in af
                if k.startswith(f"answer{qid}") and k != f"answer{qid}"
                and not k.startswith("answertype")]
        return len(subs) if subs else 1

    needed = sum(_sub_field_count(q) for q in questions)
    if not questions or needed != len(blocks):
        return []

    result = []
    cursor = 0
    for q in questions:
        n = _sub_field_count(q)
        group = blocks[cursor:cursor + n]
        cursor += n
        qtype = str(q.get("type") or "")
        if qtype in ("single", "multiple"):
            my_answer = "".join((b.get("my_answer") or "").strip()[:1] for b in group)
        else:
            my_answer = " | ".join((b.get("my_answer") or "").strip() for b in group if b.get("my_answer"))
        marks = [b.get("mark") or "" for b in group]
        if marks and all(m == "dui" for m in marks):
            mark = "dui"
        elif marks and all(m == "cuo" for m in marks):
            mark = "cuo"
        elif any(m in ("dui", "cuo", "bandui") for m in marks):
            # 有对有错（或含半对）：标记为"部分正确"，对 AI 的反馈更精准
            mark = "bandui"
        else:
            mark = "unknown"
        scores = [b.get("score") for b in group if b.get("score") is not None]
        # 小问级明细（2026-09-11 新增）：题组内每个小问各自的对错与作答。
        # 听力题常有"整组没全对、但个别小问蒙对了"的情况，这些单独正确的小问
        # 答案此前被一起丢掉；保留明细后可以把它们单独沉淀、下次直接复用。
        subs = []
        for b in group:
            raw = (b.get("my_answer") or "").strip()
            subs.append({
                "answer": raw[:1] if qtype in ("single", "multiple") else raw,
                "mark": b.get("mark") or "unknown",
                "score": b.get("score"),
            })
        result.append({
            "title": q.get("title", ""),
            "my_answer": my_answer,
            "mark": mark,
            "score": sum(scores) if scores else None,
            "subs": subs,
            # 选项（2026-09-14 新增）：学习库原来只存题干→答案，导致"以后想把
            # 已验证答案回灌 TikuAdapter 题库"时算不出 hash（题库 hash =
            # md5(题干+选项JSON+题型+平台)）。这里顺手把选项带上并存进学习库，
            # 补全长期缺失的一环。q["options"] 是换行分隔的字符串（含 "A) xxx"）。
            "options": str(q.get("options") or ""),
            "qtype": qtype,
        })
    return result

    return questions


class Chaoxing:
    def __init__(self, account: Account = None, tiku: Tiku = None, **kwargs):
        self.account = account
        self.cipher = AESCipher()
        self.tiku = tiku
        self.kwargs = kwargs
        self.rollback_times = 0
        self.rate_limiter = RateLimiter(0.5)  # 其他接口速率限制比较松
        self.video_log_limiter = RateLimiter(2)  # 上报进度极其容易卡验证码，限制2s一次
        # 判定等待的上下文（study_video -> worker 传递）。必须 thread-local：
        # 多个 worker 共享同一个 Chaoxing 实例，普通属性会互相覆盖
        self._verdict_ctx = threading.local()

        # ---- 风控熔断器 ----
        # 平台对视频上报返回 403/验证码时，本地换会话/刷新令牌都救不回来，
        # 重试只会累加风控计数。连续 RISK_LIMIT 次风控信号即判定账号已被盯上，
        # 抛 RiskControlError 让整个任务停下（由上层关闭自动恢复）。
        self.RISK_LIMIT = 3
        self._risk_hits = 0
        self._risk_lock = threading.Lock()
        # 本轮被 403 反复拦截的 (jobid, playingTime) 组合。
        # 2026-09-24 加：日志实测同一个视频从 0s 被反复重刷（403 → 跳过 →
        # 进度没推进 → 下次又从 0s 撞同一面墙），每撞一次风控等级加重一级。
        # 记住"这个视频在这个进度点被拦过"，本轮直接跳过，不再重复试探。
        self._blocked_probes: set[tuple[str, int]] = set()
        self._blocked_lock = threading.Lock()

    def _mark_blocked(self, jobid, playing_time):
        key = (str(jobid), int(playing_time))
        with self._blocked_lock:
            self._blocked_probes.add(key)

    def is_blocked(self, jobid, playing_time) -> bool:
        """该视频在这个进度点是否已被 403 拦过（本轮内不再重试）。"""
        with self._blocked_lock:
            return (str(jobid), int(playing_time)) in self._blocked_probes

    def _clear_blocked(self):
        """新一轮任务开始时清空屏蔽名单（冷却过后可以重新尝试）。"""
        with self._blocked_lock:
            self._blocked_probes.clear()

        # 题目配图下载（2026-09-13）：把登录会话注入 AI 题库，使其能带
        # cookies 抓取题干里的 <img>。非 AI 题库没有该方法，忽略即可。
        try:
            if hasattr(self.tiku, "set_session"):
                self.tiku.set_session(self._session)
        except Exception:
            pass

    def _note_risk_control(self, where: str):
        """记录一次平台风控信号；达到阈值即熔断（抛出 RiskControlError）。

        阈值取 3 是有意的：403/验证码是明确的风控信号（不是网络抖动），
        连续 3 次说明账号已被限流，继续试探只会加重处置。
        """
        with self._risk_lock:
            self._risk_hits += 1
            hits = self._risk_hits
        # 真实风控信号同时驱动"软冷却"：在 3 次熔断中止之前先暂停喘息（2026-09-29）
        note_risk_cooldown(where)
        if hits >= self.RISK_LIMIT:
            logger.error(
                f"连续 {hits} 次触发平台风控（{where}）—— 已熔断并中止本轮任务。"
                f"继续上报只会加重风控，请等待 10-30 分钟后再试，"
                f"并手动在学习通完成触发风控的视频。")
            raise RiskControlError(f"连续 {hits} 次风控信号（{where}）")

    def _note_risk_ok(self):
        """一次成功的上报，说明风控已解除，重置计数。"""
        with self._risk_lock:
            self._risk_hits = 0
        note_risk_clear()

    def login(self, login_with_cookies=False):
        if login_with_cookies:
            logger.info("Logging in with cookies")
            SessionManager.update_cookies()
            # 🔴 2026-10-03（第三方审计 B-10）：原样打印 cookies 会把 `_uid`/`fid`/`_d`
            #    等登录票据写进 chaoxing.log（该 sink 是 TRACE 级、10MB 轮转）
            #    —— 日志文件即账号凭证。只打印 cookie **名**，不打印值。
            logger.debug("Logged in with cookies: %s"
                         % ", ".join(sorted(SessionManager.get_instance()
                                            ._session.cookies.keys())))
            if not self._validate_cookie_session():
                logger.warning("Cookie 登录校验失败，尝试使用账号密码重新登录")
                if self.account and self.account.username and self.account.password:
                    return self.login(login_with_cookies=False)
                return {"status": False, "msg": "cookies 已失效，请更新 cookies 或提供账号密码"}
            logger.info("登录成功...")
            try:
                realname = self.get_name()
                if realname:
                    logger.info(f"当前登录用户: {realname}")
            except Exception as e:
                logger.debug(f"获取当前登录用户名失败: {e}")
            return {"status": True, "msg": "登录成功"}

        _session = requests.Session()
        _url = "https://passport2.chaoxing.com/fanyalogin"
        _data = {
            "fid": "-1",
            "uname": self.cipher.encrypt(self.account.username),
            "password": self.cipher.encrypt(self.account.password),
            "refer": "https%3A%2F%2Fi.chaoxing.com",
            "t": True,
            "forbidotherlogin": 0,
            "validate": "",
            "doubleFactorLogin": 0,
            "independentId": 0,
        }
        logger.trace("正在尝试登录...")
        # 🔴 2026-10-01 修：补 timeout。这是全项目**唯一**漏掉超时的关键请求，
        #    而且它被类级 `_login_lock` 包着（见 relogin_if_needed）——
        #    一旦服务端建连后不回包（半开连接/丢包/限速），该线程会永久阻塞在
        #    socket recv 上，**所有** 后续 worker 全部卡在这把锁上 → 整个实例停摆。
        resp = _session.post(_url, headers=gc.HEADERS, data=_data,
                             timeout=(5, 20))   # (连接, 读取)
        if resp and resp.json()["status"] == True:
            save_cookies(_session)
            SessionManager.update_cookies()
            logger.info("登录成功...")
            try:
                realname = self.get_name()
                if realname:
                    logger.info(f"当前登录用户: {realname}")
            except Exception as e:
                logger.debug(f"获取当前登录用户名失败: {e}")
            return {"status": True, "msg": "登录成功"}
        else:
            return {"status": False, "msg": str(resp.json()["msg2"])}

    @staticmethod
    def get_name() -> str:
        _session = SessionManager.get_session()
        try:
            resp = _session.get("https://passport2.chaoxing.com/mooc/accountManage", timeout=10)
            if resp.status_code == 200:
                match = re.search(r'id="messageName"\s+value="([^"]*)"', resp.text)
                if match:
                    return match.group(1).strip()
        except Exception as e:
            logger.debug(f"获取用户名失败: {e}")
        return ""

    def _validate_cookie_session(self) -> bool:
        session = SessionManager.get_instance()._session
        if not session.cookies.get("_uid"):
            return False

        test_session = requests.Session()
        test_session.headers.update(gc.HEADERS)
        test_session.cookies.update(session.cookies.get_dict())

        try:
            resp = test_session.post(
                "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata",
                data={"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0},
                timeout=8,
            )
        except RequestException as exc:
            logger.debug("Cookie validation request failed: {}", exc)
            return False

        if resp.status_code != 200:
            return False

        if "passport2.chaoxing.com" in resp.text or "login" in resp.text.lower():
            return False

        return True

    def get_fid(self):
        _session = SessionManager.get_session()
        return _session.cookies.get("fid", 1024)

    def get_uid(self):
        s = SessionManager.get_session()
        if "_uid" in s.cookies:
            return s.cookies["_uid"]
        if "UID" in s.cookies:
            return s.cookies["UID"]
        raise ValueError("Cannot get uid !")

    def get_course_list(self):
        _session = SessionManager.get_session()
        _url = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
        _data = {"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0}
        logger.trace("正在读取所有的课程列表...")

        # 接口突然抽风, 增加headers
        # 有可能只是referer的问题
        _headers = {
            "Referer": "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction?moocDomain=https://mooc1-1.chaoxing.com/mooc-ans",
        }
        _resp = _session.post(_url, headers=_headers, data=_data)
        # logger.trace(f"原始课程列表内容:\n{_resp.text}")
        logger.info("课程列表读取完毕...")
        course_list = decode_course_list(_resp.text)

        _interaction_url = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction"
        _interaction_resp = _session.get(_interaction_url)
        course_folder = decode_course_folder(_interaction_resp.text)
        for folder in course_folder:
            _data = {
                "courseType": 1,
                "courseFolderId": folder["id"],
                "query": "",
                "superstarClass": 0,
            }
            _resp = _session.post(_url, data=_data)
            course_list += decode_course_list(_resp.text)
        return course_list

    def get_activity_list(self, course: dict) -> list[dict]:
        s = SessionManager.get_session()
        url = "https://mobilelearn.chaoxing.com/v2/apis/active/student/activelist"
        params = {
            "fid": self.get_fid(),
            "courseId": course["courseId"],
            "classId": course["clazzId"],
            "showNotStartedActive": 0,
            "_": get_timestamp()
        }
        resp = s.get(url, params=params, allow_redirects=False)
        if resp.status_code != 200:
            logger.error("Failed to get activity list, return code: " + str(resp.status_code))
            logger.debug("Request url: " + resp.url)
            return []

        data = resp.json()
        if data["result"] != 1:
            logger.error("Unknown status: {} {}", data["result"], data["errorMsg"])
            logger.debug("Request url: " + resp.url)
            return []

        return data["data"]["activeList"]

    def pre_sign(self, course: dict, activity_id):
        s = SessionManager.get_session()
        params = {
            "general": 1,
            "sys": 1,
            "ls": 1,
            "appType": 15,
            "tid": '',
            "ut": 's',
            "uid": self.get_uid(),
            "activePrimaryId": activity_id,
            "courseId": course["courseId"],
            "classId": course["clazzId"],
        }
        resp = s.get('https://mobilelearn.chaoxing.com/newsign/preSign', params=params)
        resp_txt = resp.text
        logger.debug("Request url" + resp.url)
        if resp.status_code != 200:
            logger.error("Failed to get sign in, return code: " + str(resp.status_code) + "message: " + resp_txt)

        return resp_txt

    def sign_in_normal(self, course: dict, activity_id, name="", obj_id="aaa", lat=-1, lon=-1, type_=SignType.NORMAL):
        s = SessionManager.get_session()
        params = {
            "activeId": activity_id,
            "uid": self.get_uid(),
            "fid": self.get_fid(),
            "courseId": course["courseId"],
            "classId": course["clazzId"],
            "clientip": "",
            "objectId": obj_id,
            "name": name,
            "useragent": "",
            "latitude": lat,
            "longitude": lon,
            "appType": "15",
        }

        resp = s.get("https://mobilelearn.chaoxing.com/pptSign/stuSignajax", params=params)

        resp_txt = resp.text
        if resp.status_code != 200:
            logger.error("Failed to get sign in, return code: " + str(resp.status_code) + "message: " + resp_txt)

        if type_ != SignType.LOCATION:
            return resp_txt

        pattern = r"[^0-9\.]*(.+)米[^0-9\.]*"
        msg = re.match(pattern, resp_txt)
        logger.warning(f"距离签到位置 {msg}m")
        # TOD0: Implement triangulation for location signs
        return resp_txt

    def get_course_point(self, _courseid, _clazzid, _cpi):
        _session = SessionManager.get_session()
        _url = f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?courseid={_courseid}&clazzid={_clazzid}&cpi={_cpi}&ut=s"
        logger.trace("URL: " + _url)
        logger.trace("开始读取课程所有章节...")
        _resp = _session.get(_url)

        # 🔴 2026-09-29 截断：原样打印整页 HTML，而平台进度采集线程每 60 秒
        # 调一次本方法 → 日志每分钟被灌 ~200KB HTML，把有用的刷课日志全冲掉
        # （真机踩坑：acc5 日志近 900KB 里 0 条真实日志、71% 是 HTML，
        #   排查"卡住"时完全看不到日志流）。保留前 200 字足够定位问题。
        logger.trace(f"原始章节列表内容(前200字):\n{_resp.text[:200]}")
        logger.info("课程章节读取成功...")
        return decode_course_point(_resp.text)

    def get_job_list(self, course: dict, point: dict) -> tuple[list[dict], dict]:
        _session = SessionManager.get_session()
        # 🔴 2026-10-02 P1：开启随机抖动。原为固定 0.5s 间隔 —— 外部调研显示
        #   「过于均匀/规律的请求间隔会被判定为自动化脚本」（TrueSight 分析报告），
        #   而 `RateLimiter` 本身就支持随机抖动，只是这里没开。0~0.5s 的抖动
        #   不影响吞吐（原本就有 0.5s 基础间隔），只消除"机械规律"这个特征。
        self.rate_limiter.limit_rate(random_time=True, random_max=0.5)
        job_list = []
        job_info = {}

        # 章节列表偶有解析不到 id 的条目（结构变化/未开放节点），直接取键会 KeyError
        # 抛到 worker 里，表现为"切到下一章就卡住"。这里提前拦下，按未开放处理。
        if not point.get("id"):
            logger.warning(f"章节缺少 id，跳过该任务点 -> {point.get('title', '(无标题)')}")
            return [], {"notOpen": True}

        cards_params = {
            "clazzid": course["clazzId"],
            "courseid": course["courseId"],
            "knowledgeid": point["id"],
            "ut": "s",
            "cpi": course["cpi"],
            "v": "2025-0424-1038-3",
            "mooc2": 1
        }

        # 🔴 2026-10-03（审核后定稿）：分页探测
        #
        #   坑 A —— **判"该页无内容"不能看解码后的任务列表**。
        #     已完成的卡会被 `_process_attachment_cards` 按 isPassed 正常过滤掉，
        #     "过滤后为空" ≠ "页面无卡"。旧的 `_empty_streak` 正是这么算的，
        #     于是会**提前 break**，后面的页再也不被请求。
        #     ⇒ 现在**不做提前退出**（num 范围已经很窄，多试几次成本可忽略）。
        #
        #   坑 B —— **同一张卡可能被多个 num 重复返回**，必须去重，
        #     否则同一个视频会被重复处理/播放。
        #
        #   ⚠️ 关于 num 的取值范围（**实测结论，别再猜**）：
        #     对 acc2 安全人机的节点逐一试过 num=0..24：
        #       · **只有 `0` 和 `1` 有卡**，`2` 及以上全部返回 0 张
        #       · 非数字（`a`/`b`/`f`）被服务端当作 `0`，返回与 num=0 相同的内容
        #     ⇒ 用 `"012"` 即可覆盖（0/1 有效 + 1 次冗余探测）。
        #       曾经写成 `"0123456789abcdef"`（误以为 num 是十六进制），
        #       那会把每章节的请求数从 ~2-3 次放大到 **16 次**，
        #       在批量刷课场景下是无谓的**请求量放大**（风控隐患），故收窄。
        #       —— 收窄后每章节请求数与改动前基本持平甚至更少。
        # 🔴 2026-10-03（何同学："一定要保持界面展示的真实性" +
        #   "不要每次没读取到全部的就结束了"）：本方法有两条**不能撒谎**的约束：
        #     1) 节点被平台标为「未完成」时，卡上的 `isPassed=true` 不可信
        #        ⇒ 放行 `job=True` 的真任务点让它**重做**（见 `_process_attachment_cards`）
        #     2) `job_list` 为空时**不能无条件**上报"空页面完成" ——
        #        那会让界面显示已完成、而平台侧永远黄点。
        _force_redo = not point.get("has_finished", False)
        _seen_jobs = set()
        _raw_total = 0          # 本次请求到的**原始卡片总数**（含已完成/附属卡）

        # 🔴🔴 2026-10-07 何同学定案：**`num` 是「节点内页签序号」，必须自适应扫完所有页签**。
        #
        #   背景（acc10《生态文明建设》28 个黄点，跨账号复现，与账号无关）：
        #     一个节点可含**多个页签**（tab），每个页签一个 cardid，`num=0,1,2,...` 依次取：
        #       0=学前任务(常空) / 1=学习视频(video) / 2=课件资料(document)
        #       / 3=**课后小测(workid, job=true 真任务点)** / 4=拓展学习(插入图书附属卡)
        #     旧代码硬编码 `for _possible_num in "012"` —— **只扫 3 个页签**，
        #     于是 28 个「5 页签」节点的 `num=3 课后小测` **从未被请求、从未被做**
        #     ⇒ 节点永远黄点（实测：已完成节点全是 1~2 页签；28 个未完成节点全是 5 页签）。
        #   何同学原话：「它应该是把所有卡片全部列出来，而不是只固定读取卡片」
        #                「这种多张卡片的，需要把全部卡片都过一遍才会变绿。
        #                  但是一般也只用看视频和做章节测验，其他的只需要过一遍就行」
        #
        #   实现：从 num=0 起**连续向后探测**，直到**连续 `_EMPTY_STREAK_STOP` 次**取不到
        #   任何原始卡为止（页签是连续的，遇到空即到头；连续判空是为了容忍中间偶发空页签）。
        #   · 页签 ≤3 的节点：请求数与旧实现相同（num=0/1/2 后即停）
        #   · 5 页签节点：多 2~3 个请求（num=3 有卡、num=4 有卡、num=5 空）
        #   · 上限 `_NUM_MAX` 兜底，防御服务端异常时无限循环
        #
        #   附：`num` 与页签的对应关系是**实测**结论 —— 非数字入参（`a`/`b`）被服务端
        #       当作 `0`，返回与 num=0 相同内容；故只递增数字。
        _NUM_MAX = 10              # 页签序号上限（防御性兜底）
        _EMPTY_STREAK_STOP = 2     # 连续 N 次取不到原始卡即判定"到头"
        _empty_streak = 0
        for _num_i in range(_NUM_MAX):
            _possible_num = str(_num_i)

            logger.trace("开始读取章节所有任务点...")

            cards_params.update({"num": _possible_num})
            _resp = _session.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params=cards_params)
            if _resp.status_code != 200:
                logger.error(f"未知错误: {_resp.status_code} 正在跳过")
                logger.error(_resp.text[:500])
                return [], {}

            _job_list, _job_info = decode_course_card(_resp.text,
                                                      keep_passed_task=_force_redo)
            if _job_info.get("notOpen", False):
                # 直接返回, 节省一次请求
                logger.info("该章节未开放")
                return [], _job_info

            # 🔴 2026-10-07 移植上游 PR #630：`noCard` = 该 num 没有对应标签页
            #   （页面里 `mArg = $mArg` 未被填充）⇒ 页签到头，**立即**停止探测。
            #   这是**加速**判据：更早知道"没有了"，少发请求。
            #   ⚠️ 保守加法：即便 noCard 判据失效（上游语义变化），
            #      下面的 `_empty_streak` 兜底仍然照常工作 ⇒ 不会漏扫页签。
            if _job_info.get("noCard", False):
                logger.trace(f"num={_possible_num} 无对应标签页(noCard)，停止探测")
                break

            _raw = _job_info.get("raw_cards", 0)
            _raw_total += _raw
            for _j in _job_list:
                _key = (_j.get("type"), str(_j.get("jobid") or _j.get("mid") or ""))
                if _key in _seen_jobs:
                    continue
                _seen_jobs.add(_key)
                job_list.append(_j)
            job_info.update(_job_info)

            # 自适应收尾：该页签无原始卡 → 累加空计数；有卡则清零
            if _raw <= 0:
                _empty_streak += 1
                if _empty_streak >= _EMPTY_STREAK_STOP:
                    break
            else:
                _empty_streak = 0

        if not job_list:
            if _raw_total > 0:
                # 页面上**确实有卡**，却解析不出任何可执行任务 ⇒ 异常。
                # 绝不能上报"空页面完成"（那是**假成功**：界面会显示已完成，
                # 而平台侧的任务点一个都没达成）。打 blocked 标记交上层判定的。
                logger.error(f"章节「{point.get('title')}」取到 {_raw_total} 张原始卡片，"
                             f"但没有任何可执行任务 —— 不上报完成，交上层处理")
                job_info["blocked"] = True
            else:
                # 页面上确实一张卡都没有（纯目录/标题节点）⇒ 才允许上报空页面完成
                self.study_emptypage(course, point)

        # 🔴 2026-09-29 截断（同 get_course_point）：本方法**每刷一个章节任务点
        # 都会调用**，原样打印整页 HTML → 日志被彻底淹没，排查"卡住"时看不到
        # 任何有效日志（实测：日志 100% 是 HTML、每分钟涨 24KB）。
        logger.trace(f"原始任务点列表内容(前200字):\n{_resp.text[:200]}")
        logger.info("章节任务点读取成功...")

        return job_list, job_info

    def get_enc(self, clazzId, jobid, objectId, playingTime, duration, userid):
        return md5(
            f"[{clazzId}][{userid}][{jobid}][{objectId}][{playingTime * 1000}][d_yHJ!$pdA~5][{duration * 1000}][0_{duration}]"
            .encode()).hexdigest()

    def video_progress_log(
            self,
            _session,
            _course,
            _job,
            _job_info,
            _dtoken,
            _duration,
            _playingTime,
            _type: str = "Video",
            _isdrag: int = 3,
            headers: Optional[dict] = None,
    ) -> tuple[bool, int]:

        if headers is None:
            logger.warning("null headers")
            headers = gc.VIDEO_HEADERS

        self.video_log_limiter.limit_rate(random_time=True, random_max=2)

        if "courseId" in _job["otherinfo"]:
            logger.error(_job["otherinfo"])
            raise RuntimeError("this is not possible")

        enc = self.get_enc(_course["clazzId"], _job["jobid"], _job["objectid"], _playingTime, _duration, self.get_uid())
        params = {
            "clazzId": _course["clazzId"],
            "playingTime": _playingTime,
            "duration": _duration,
            "clipTime": f"0_{_duration}",
            "objectId": _job["objectid"],
            "otherInfo": _job["otherinfo"],
            "courseId": _course["courseId"],
            "jobid": _job["jobid"],
            "userid": self.get_uid(),
            "isdrag": _isdrag,
            "view": "pc",
            "enc": enc,
            "dtype": _type
        }

        _url = (
            f"https://mooc1.chaoxing.com/mooc-ans/multimedia/log/a/"
            f"{_course['cpi']}/"
            f"{_dtoken}"
        )

        # 这三个字段并非每个任务点都返回（实测写作类视频章节就没有
        # videoFaceCaptureEnc），用下标取值会抛 KeyError。异常会一路冒泡到
        # worker 线程，表现为"切到下一章就卡住"——必修。
        face_capture_enc = _job.get("videoFaceCaptureEnc")
        att_duration = _job.get("attDuration")
        att_duration_enc = _job.get("attDurationEnc")

        if face_capture_enc:
            params["videoFaceCaptureEnc"] = face_capture_enc
        if att_duration:
            params["attDuration"] = att_duration
        if att_duration_enc:
            params["attDurationEnc"] = att_duration_enc

        def perform_request(rt_val):
            params.update({"rt": rt_val, "_t": get_timestamp()})
            res = _session.get(_url, params=params, headers=headers)
            if res.status_code == 403 or '验证码' in res.text or 'validate' in res.text:
                logger.warning("检测到验证码拦截，正在尝试自动通过验证码...")
                try:
                    from api.captcha import CxCaptcha
                    cookies_str = "; ".join([f"{k}={v}" for k, v in _session.cookies.items()])
                    ua = headers.get("User-Agent", gc.HEADERS.get("User-Agent"))
                    ocr_inst = getattr(self, '_ocr', None)
                    if ocr_inst is None:
                        from api.captcha import ocr_init
                        ocr_inst = ocr_init()
                        if ocr_inst:
                            self._ocr = ocr_inst
                    captcha_solver = CxCaptcha(user_agent=ua, cookies=cookies_str, ocr=ocr_inst,
                                               limiter=self.video_log_limiter)
                    # ⚠ 只试一次（2026-09-24 改）：
                    # 原来失败后重试 3 次，日志实测是「第1/2/3次尝试通关 → 验证码验证失败
                    # → 多次验证码通关失败」，连续 3 轮全失败还在撞。验证码拦下来说明
                    # 平台已经在盯这个账号，**每一次重试都在加重风控**，收益却是 0。
                    # 正确做法：试一次，不行就把它当成一次风控信号交给熔断器处理。
                    solved = False
                    logger.info("尝试通关验证码（单次，失败即计入风控）...")
                    if captcha_solver.try_pass():
                        logger.success("验证码通关成功！")
                        solved = True
                    else:
                        logger.warning("验证码验证失败，不再重试（重试会加重风控）")
                    if solved:
                        _session.cookies.update(captcha_solver.s.cookies)
                        res = _session.get(_url, params=params, headers=headers)
                    else:
                        logger.error("验证码未能自动通过 —— 计入风控信号，需人工干预。")
                        self._note_risk_control("验证码自动识别连续失败")
                except RiskControlError:
                    raise       # 熔断必须透传，不能被下面的 except Exception 吞掉
                except Exception as e:
                    logger.error(f"验证码通关逻辑异常: {e}")
            return res

        rt = _job['rt']
        if not rt:
            rt_search = re.search(r"-rt_([1d])", _job['otherinfo'])
            if rt_search:
                rt_char = rt_search.group(1)
                rt = "0.9" if rt_char == "d" else "1"
                logger.trace(f"Got rt from otherinfo: {rt}")

        if rt:
            logger.trace(f"Got rt: {rt}")
            _job['rt'] = rt
            resp = perform_request(rt)
        else:
            logger.warning("Failed to get rt")
            for rt in [0.9, 1]:
                resp = perform_request(rt)
                if resp.status_code == 200:
                    logger.trace(resp.text[:200])
                    return resp.json()["isPassed"], 200
                elif resp.status_code == 403:
                    logger.warning("出现403报错, 正常尝试切换rt")
                else:
                    logger.warning("未知错误 jobid={}, status_code={}, 摘要:\n{}",
                                   _job.get("jobid"),
                                   resp.status_code,
                                   resp.text[:200])
                    break

        if resp.status_code == 200:
            # ⚠️ 2026-09-29：这里原本是 `logger.trace(resp.text)` —— 上报**成功**的主路径，
            # 每次视频进度上报都打一整页响应 HTML（日志 24KB/分钟、真实日志全被冲掉）。
            # 上一轮 replace_all 只匹配到 20 空格缩进的那处，**12 空格缩进的这处漏了**
            # （靠 `_verify_source_features.py` 发布前核验才抓到）。必须截断。
            logger.trace(resp.text[:200])
            self._note_risk_ok()   # 上报成功 -> 风控计数归零
            return resp.json()["isPassed"], 200

        elif resp.status_code == 403:
            logger.debug(
                "视频进度上报返回403, jobid={}, 摘要={}",
                _job.get("jobid"),
                resp.text[:200],
            )

            # 若出现两个rt参数都返回403的情况, 则跳过当前任务
            logger.error("出现403报错, 尝试修复无效, 正在跳过当前任务点...")
            logger.error("请求url: {}", resp.url)
            # 403 是平台级风控信号：本地换会话/刷新令牌/换 rt 都救不回来。
            # 这里累加计数，达到阈值会抛 RiskControlError 直接终止整个任务，
            # 而不是按章节逐个重试（那会把风控次数放大十几倍直至封号）。
            #
            # 同时把这个 (视频, 进度点) 记进屏蔽名单：本轮不再碰它。
            # 2026-09-24 加 —— 实测同一视频从 0s 被反复撞是风控升级的主因。
            self._mark_blocked(_job.get("jobid"), _playingTime)
            self._note_risk_control("视频进度上报被 403 拦截")
            return False, 403

        logger.error(f"未知错误: {resp.status_code}")
        logger.error("请求url:", resp.url)
        logger.error("请求头：", dict(_session.headers) | headers)
        return False, resp.status_code

    def _refresh_video_status(self, session: requests.Session, job: dict, _type: Literal["Video", "Audio"]) \
            -> Optional[dict]:
        self.rate_limiter.limit_rate(random_time=True, random_max=0.2)
        headers = gc.VIDEO_HEADERS if _type == "Video" else gc.AUDIO_HEADERS
        info_url = (
            f"https://mooc1.chaoxing.com/ananas/status/{job['objectid']}?"
            f"k={self.get_fid()}&flag=normal"
        )
        try:
            resp = session.get(info_url, timeout=8, headers=headers)
        except RequestException as exc:
            logger.debug("刷新视频状态失败: {}", exc)
            return None

        if resp.status_code != 200:
            logger.debug("刷新视频状态返回码异常: {}" % resp.status_code)
            logger.debug(resp.text[:200])
            return None

        try:
            data = resp.json()
        except ValueError as exc:
            logger.debug("解析视频状态响应失败: {}", exc)
            return None

        if data.get("status") == "success":
            return data

        return None

    def _recover_after_forbidden(self, session: requests.Session, job: dict, _type: Literal["Video", "Audio"]):
        SessionManager.update_cookies()
        refreshed = self._refresh_video_status(session, job, _type)
        if refreshed:
            return refreshed

        if SessionManager.relogin_if_needed(self):
            return self._refresh_video_status(session, job, _type)

        return None

    @staticmethod
    def _close_pbar_safe(pbar_ref):
        if pbar_ref is not None:
            try:
                pbar_ref.leave = False
                pbar_ref.close()
            except Exception as e:
                logger.trace(f"关闭进度条失败: {e}")
        return None

    def study_video(self, _course, _job, _job_info, _speed: float = 1.0,
                    _type: Literal["Video", "Audio"] = "Video") -> StudyResult:
        # 风控冷却：上一轮连续判定超时会在这一行挡住，冷却结束自动继续（2026-09-29）
        cooldown_wait()
        _session = SessionManager.get_session()

        headers = gc.VIDEO_HEADERS if _type == "Video" else gc.AUDIO_HEADERS
        _info_url = f"https://mooc1.chaoxing.com/ananas/status/{_job['objectid']}?k={self.get_fid()}&flag=normal"
        _video_info = _session.get(_info_url, headers=headers).json()

        if _video_info["status"] != "success":
            logger.error(f"Unknown status: {_video_info['status']}")
            return StudyResult.ERROR

        _dtoken = _video_info["dtoken"]

        # crc / key 是早期版本用于视频校验的字段，平台现已改用 dtoken 上报
        # （见下方 video_progress_log 传的就是 _dtoken）。这里仍取用是为了
        # 接口字段缺失时能尽早暴露变化，**当前不参与任何计算**，勿当成漏用。
        _crc = _video_info["crc"]
        _key = _video_info["key"]

        # Time in the real world: last_iter, gc.THRESHOLD
        # Time in the video (can be scaled with the speed factor): duration, play_time, last_log_time, wait_time

        duration = int(_video_info["duration"])
        play_time = int(_job["playTime"]) // 1000
        last_log_time = 0
        last_iter = time.time()
        wait_time = int(random.uniform(30, 90))

        logger.info(f"开始任务: {_job['name']}, 总时长: {duration}s, 已进行: {play_time}s")

        # 本轮已被 403 拦过的 (视频, 进度点) 直接跳过（2026-09-24 加）。
        # 实测死循环：403 → 跳过任务点 → 视频进度没真的推进（仍 0s）→
        # 下次重启又从 0s 刷同一个视频 → 再 403。这里从源头掐掉：
        # 同一面墙本轮不撞第二次。冷却后 _clear_blocked() 会放行。
        if self.is_blocked(_job.get("jobid"), play_time):
            logger.warning(
                "跳过已被风控拦截的视频（本轮不再重试，避免加重风控）: {} @ {}s",
                _job["name"], play_time)
            return StudyResult.FORBIDDEN

        forbidden_retry = 0
        max_forbidden_retry = 2

        # 仅当本地已有进度时才尝试"瞬间完成"。从 0 开始的新视频若一上来就把
        # playingTime 推到 duration，服务器比对真实耗时与进度增量会发现严重不匹配，
        # 判定为拖拽作弊，isPassed 迟迟不给 true（实测 4.1/4.2 要干等 5-10 分钟）。
        passed = False
        # ⛔ 2026-10-04 19:5x 实测记录：`forced_redo`（对该类节点走"重置进度、
        #   真实重播"）**试过、无效** —— acc4 劳动教育 11 个黄点节点全部真实重播
        #   并"任务完成"，平台 `has_finished` **一个都没变**（仍 11 个）。
        #   同批还试过 isdrag=4 瞬间完成 20 次、isdrag=3 满进度上报，全都无效。
        #   结论：这类节点（平台标未完成、卡内却全 `isPassed=true`）属**平台侧
        #   数据不一致** —— 页面汇总已 41/41 满，平台自己也不把它们算作缺口，
        #   程序无法通过学习行为消除，留着重播只会白耗时间。
        #   ⇒ 保持原实现（瞬间完成），不再走重播。
        if play_time > 0:
            passed, state = self.video_progress_log(_session, _course, _job, _job_info, _dtoken, duration, duration,
                                                    _type, headers=headers, _isdrag=4)
            if passed:
                logger.info("任务瞬间完成: {}", _job['name'])
                return StudyResult.SUCCESS

        # 进度已满却仍未通过：满了就再也产生不了增量，继续上报服务端不会认可，
        # 必须把播放位置重置回 0 重播一遍，用真实的时间推进换取通过判定。
        if play_time >= duration:
            logger.warning(
                f"进度已满({play_time}/{duration}s)但平台未判定通过，判定为拖拽记录；"
                f"重置进度重新播放以产生真实增量 -> {_job['name']}")
            play_time = 0
            last_log_time = 0

        pbar = None
        try:
            while not passed:
                # Sometimes the last request needs to be sent several times to complete the task
                if play_time - last_log_time >= wait_time or play_time == duration:

                    passed, state = self.video_progress_log(_session, _course, _job, _job_info, _dtoken, duration,
                                                            int(play_time), _type, headers=headers)

                    if state == 403:
                        if forbidden_retry >= max_forbidden_retry:
                            logger.warning("403重试失败, 跳过当前任务")
                            return StudyResult.FORBIDDEN
                        forbidden_retry += 1
                        # ⚠️ 403 退避必须分级（2026-09-24 改）：
                        # 原来固定 sleep(2~4) 秒 —— 平台已经明确回 403 了，
                        # 睡 2 秒就再来一次，在风控语境下等于没退避，
                        # 实测 4.2 秒就打下一发，28 秒内打满 30 个请求。
                        # 改为指数退避：第 1 次 30s，第 2 次 120s。
                        backoff = 30 if forbidden_retry == 1 else 120
                        logger.warning(
                            "出现403报错, 正在尝试刷新会话状态 (第{}次, 退避{}s)",
                            forbidden_retry, backoff,
                        )
                        time.sleep(backoff)
                        refreshed_meta = self._recover_after_forbidden(_session, _job, _type)
                        if refreshed_meta and refreshed_meta.get("dtoken") and refreshed_meta.get(
                                "duration") is not None:
                            _dtoken = refreshed_meta["dtoken"]
                            duration = int(refreshed_meta["duration"])
                            refreshed_play_time = refreshed_meta.get("playTime")
                            if refreshed_play_time is not None:
                                play_time = int(refreshed_play_time)

                            logger.debug("刷新后的令牌: {}, 持续时间: {}, 播放时间: {}", _dtoken, duration, play_time)
                            pbar = self._close_pbar_safe(pbar)
                            continue
                        else:
                            logger.error("会话恢复失败，刷新后的元数据缺少必要字段 (dtoken, duration)")
                            return StudyResult.ERROR

                    elif not passed and state != 200:
                        return StudyResult.ERROR

                    wait_time = int(random.uniform(30, 90))
                    last_log_time = play_time

                    # 播完但平台未判通过：不再原地阻塞等待（那会占住 worker、
                    # 拖慢后续章节）。把等待所需上下文存到线程本地，返回 PENDING，
                    # 由 JobProcessor 委托后台线程沿用原 30 秒上报节奏等判定 ——
                    # 判定与这些周期上报相关，不能改成纯被动查询。
                    if play_time >= duration and not passed:
                        self._verdict_ctx.data = {
                            "session": _session, "course": _course, "job": _job,
                            "job_info": _job_info, "dtoken": _dtoken,
                            "duration": duration, "type": _type, "headers": headers,
                        }
                        logger.info(
                            "视频已播完({}s)，平台判定中 —— 委托后台等待，worker 继续后续章节",
                            duration)
                        return StudyResult.PENDING

                    logger.trace("Progress logged")

                # Uploading the progress takes time, we assume that the video is still playing in the background, this manually calculates the time elapsed
                dt = (time.time() - last_iter) * _speed
                last_iter = time.time()
                play_time = min(duration, play_time + dt)

                # 检查手动模式锁是否被锁定
                manual_locked = False
                try:
                    manual_locked = TikuManual._manual_lock.locked()
                except Exception as e:
                    logger.trace(f"无法检查手动锁状态: {e}")

                if manual_locked:
                    pbar = self._close_pbar_safe(pbar)
                else:
                    if pbar is None:
                        pbar = tqdm(total=duration, initial=int(play_time), desc=_job["name"],
                                    unit_scale=True, bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt}', leave=False)
                    pbar.n = int(play_time)
                    pbar.refresh()

                time.sleep(gc.THRESHOLD)
        finally:
            pbar = self._close_pbar_safe(pbar)

        logger.info("任务完成: {}", _job['name'])
        return StudyResult.SUCCESS

    def wait_video_passed(self, _session, _course, _job, _job_info, _dtoken,
                          duration: int, _type: str = "Video", headers=None,
                          max_checks: int = 40, check_interval: int = 30) -> StudyResult:
        """后台判定等待：视频已播到 100% 但平台未判通过时，由独立线程调用。

        沿用原等待循环的节奏（每 ~30 秒发一次 100% 进度上报）——实测平台判定
        与这些周期上报相关（4.1/4.3/4.4 均在上报期通过），不能改成纯被动查询。
        通过返回 SUCCESS；超上限返回 ERROR（走既有重试→重播恢复路径）。
        本方法在后台线程运行，不占用 worker。
        """
        for i in range(1, max_checks + 1):
            passed, state = self.video_progress_log(
                _session, _course, _job, _job_info, _dtoken, duration, duration,
                _type, headers=headers)
            if passed:
                logger.info("平台已判通过(第{}次上报): {}", i, _job.get('name', ''))
                return StudyResult.SUCCESS
            if state == 403:
                logger.warning("判定等待期上报返回403，交回主流程恢复会话")
                return StudyResult.FORBIDDEN
            if state != 200:
                logger.warning("判定等待期上报异常 state={}，交回主流程", state)
                return StudyResult.ERROR
            logger.info("等待平台判定 (第{}次核查，每{}秒上报)", i, check_interval)
            time.sleep(check_interval)
        logger.warning("平台判定等待超时(约{}分钟)，交回重试队列（重跑会自动重播）",
                       max_checks * check_interval // 60)
        return StudyResult.ERROR

    def study_document(self, _course, _job) -> StudyResult:
        """
        Study a document in Chaoxing platform.

        This method makes a GET request to fetch document information for a given course and job.

        Args:
            _course (dict): Dictionary containing course information with keys:
                - courseId: ID of the course
                - clazzId: ID of the class
            _job (dict): Dictionary containing job information with keys:
                - jobid: ID of the job
                - otherinfo: String containing node information
                - jtoken: Authentication token for the job

        Returns:
            requests.Response: Response object from the GET request

        Note:
            This method requires the following helper functions:
            - init_session(): To initialize a new session
            - get_timestamp(): To get current timestamp
            - re module for regular expression matching
        """
        _session = SessionManager.get_session()
        _url = f"https://mooc1.chaoxing.com/ananas/job/document?jobid={_job['jobid']}&knowledgeid={re.findall(r'nodeId_(.*?)-', _job['otherinfo'])[0]}&courseid={_course['courseId']}&clazzid={_course['clazzId']}&jtoken={_job['jtoken']}&_dc={get_timestamp()}"
        _resp = _session.get(_url)
        if _resp.status_code != 200:
            return StudyResult.ERROR
        else:
            return StudyResult.SUCCESS

    def study_work(self, _course, _job, _job_info) -> StudyResult:
        # 🔴 2026-10-04（何同学要求：界面必须真实反映平台完成度，不能"没做完就报成功"）：
        #   原实现 `return StudyResult.SUCCESS` —— 题库不可用时**一个字符都没提交，
        #   却按"成功"计**，上层据此认为该章节已完成 ⇒ 界面显示完成、平台仍是黄点。
        #
        #   真机证据（acc10 / 习近平经济思想概论 / 10-04 13:25 那轮）：
        #     · `识别到章节检测任务` 104 次
        #     · 紧随其后全部是 `Task success`（同秒），中间**无任何答题日志**
        #     · 全时段 `章节检测题库覆盖率` / `提交答题成功` = **0 条**
        #     · 平台进度停在 23/49（视频做了、测验一个没做）
        #   ⇒ 26 个测验全部被这里静默吞掉。
        #
        #   现改为**如实返回 ERROR**：没做成就是没做成，宁可让上层记为失败
        #   （复核会给出"仍有 N 个未完成"的明确结论），也不伪装成成功。
        #   ⚠️ 顺带修：原写法 `self.tiku.DISABLE or not self.tiku` 在 `tiku is None`
        #   时会先求值 `.DISABLE` 而抛 AttributeError（顺序反了）。改为先判空。
        if not self.tiku or getattr(self.tiku, "DISABLE", False):
            logger.warning(
                f"[章节检测] 题库不可用（DISABLE={getattr(self.tiku, 'DISABLE', '?')}），"
                f"该测验**未作答**，如实上报失败 -> {(_course or {}).get('title')}"
            )
            return StudyResult.ERROR

        # 每个章节检测独立计数：rollback_times 若跨章节残留，会让后续所有章节
        # 满足 "rollback_times >= 1" 从而绕过覆盖率检查强制提交（无答案也提交）。
        self.rollback_times = 0

        _session = SessionManager.get_session()
        _url = "https://mooc1.chaoxing.com/mooc-ans/api/work"

        def is_not_permission_error(exception):
            return not isinstance(exception, PermissionError)

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_fixed(1),
            retry=retry_if_exception(is_not_permission_error),
            reraise=True
        )
        def fetch_response_with_retry():
            _resp = _session.get(
                _url,
                params={
                    "api": "1",
                    "workId": _job["jobid"].replace("work-", ""),
                    "jobid": _job["jobid"],
                    "originJobId": _job["jobid"],
                    "needRedirect": "true",
                    "skipHeader": "true",
                    "knowledgeid": str(_job_info["knowledgeid"]),
                    "ktoken": _job_info["ktoken"],
                    "cpi": _job_info["cpi"],
                    "ut": "s",
                    "clazzId": _course["clazzId"],
                    "type": "",
                    "enc": _job["enc"],
                    "mooc2": "1",
                    "courseid": _course["courseId"],
                }
            )

            # 未创建完成该测验则不进行答题，目前遇到的情况是未创建完成等同于没题目
            if '教师未创建完成该测验' in _resp.text:
                raise PermissionError("教师未创建完成该测验")

            questions = decode_questions_info(_resp.text)

            if _resp.status_code == 200 and questions.get("questions"):
                return _resp, questions

            logger.warning(
                f"无效响应 (Code: {getattr(_resp, 'status_code', 'Unknown')}), 重试中...")
            raise RuntimeError(f"请求返回无效数据 (Code: {_resp.status_code})")

        # 章节检测最大重做次数（答错后收集错误反馈并重新提交，直到全对）
        try:
            max_retries = max(1, int(self.kwargs.get("work_max_retries", 3)))
        except (TypeError, ValueError):
            max_retries = 3
        query_delay = self.kwargs.get("query_delay", 0)
        feedback_history = None

        for attempt in range(max_retries + 1):
            if attempt > 0:
                logger.warning(
                    f"章节检测重做第 {attempt}/{max_retries} 轮，携带上一轮错误反馈重新作答")
                time.sleep(2)

            # 1. 获取题目
            final_resp = {}
            questions = {}
            try:
                final_resp, questions = fetch_response_with_retry()
            except PermissionError as e:
                logger.warning(f"跳过章节检测: {e}")
                return StudyResult.SUCCESS
            except Exception as e:
                logger.error(f"获取章节检测题目失败, 达到最大重试次数: {e}")
                return StudyResult.ERROR

            _ORIGIN_HTML_CONTENT = final_resp.text  # 用于配合输出网页源码, 帮助修复#391错误

            # 2. 设置上一轮错误反馈（供 AI 重新作答时参考）
            #    这是**有效功能**（何同学 2026-10-01 澄清）：把自己上次答错的题
            #    连带那个错误答案一起喂给 AI，让它这次避开 —— 多选题重做时尤其有用
            #    （平台判 `bandui` 不会说哪个选项对，但"这个组合不通过"本身有信息量）。
            #
            #    🔴 但必须**每轮无条件调用**（原代码写作 `if feedback_history and ...`，
            #       只在非空时设置，且全项目没有任何复位点）：
            #         · 第 0 轮 `feedback_history` 是 None → 顺带清掉**上一个测验**的残留；
            #         · 重做轮（attempt≥1）拿到真实反馈 → 正常注入。
            #       否则一次答错的反馈会永久残留，被塞进后续无关题目的 prompt。
            #
            #    ⚠️ `work_feedback` 现在**只影响 AI 的提示词**，不再充当
            #       "跳过 cache / 跳过本地题库"的开关 —— 那个**整卷级**的全局跳过
            #       已删除，改为由 `wrong` 列表**按题**否决（见 answer.py 的
            #       `local_tiku_answer` 与 `query_all` 的 cache 分支）。
            #       病根是：一道题答错就把整卷所有题都拖去问 AI，代价与收益完全不成比例。
            #
            #    任务结束后的清空见 `main.py` 的 workid 分支（唯一生产调用点）。
            if hasattr(self.tiku, 'set_work_feedback'):
                try:
                    self.tiku.set_work_feedback(feedback_history or None)
                except Exception as e:
                    logger.warning(f"设置题库错误反馈失败: {e}")

            # 3. 搜题
            total_questions = len(questions["questions"])
            found_answers = 0
            # 把课程名注入到每道题上：题库全没命中时走 AI 兜底，AI 若不知道
            # 这是哪门课，只能拿通用知识硬凑，专业术语极易答偏
            # （实测同一批 SketchUp 填空题：无课程背景 0/5 → 有课程背景 2/5）
            for _q in questions["questions"]:
                _q["_course"] = (_course or {}).get("title", "")
            answers = self.tiku.query_all(questions["questions"], query_delay=query_delay)

            if not isinstance(answers, list):
                logger.error("题库 query_all 返回的数据格式异常，期望列表。将采用随机答案答题")
                answers = [None] * total_questions
            elif len(answers) != total_questions:
                logger.error(
                    f"题库返回的答案数量（{len(answers)}）与题目数量（{total_questions}）不匹配，正在补齐或截断以防错位！")
                answers = list(answers) + [None] * (total_questions - len(answers))
                answers = answers[:total_questions]

            for q, res in zip(questions["questions"], answers):
                logger.debug(f"当前题目信息 -> {q}")
                answer = ""
                if not res:
                    # 🔴 随机前最后挣扎：让 AI 换模型再答一次（2026-09-29）
                    _rescue = _ai_last_resort(self.tiku, dict(q))
                    if _rescue:
                        answer = _rescue
                        q[f'answerSource{q["id"]}'] = "ai-retry"
                    else:
                        # 随机答题
                        answer = random_answer(q["options"], q["type"])
                        q[f'answerSource{q["id"]}'] = "random"
                else:
                    # 根据响应结果选择答案
                    if q["type"] == "multiple":
                        # 已映射好的纯字母串（题库/AI 侧映射，2026-09-14）：直接采用。
                        # 不能再走下面"内容→字母"匹配 —— clean_res 会把 "ABC"
                        # 当成带编号的内容剥成 "BC" → 匹配失败 → 答案丢失走随机
                        # （实测 3.2 三道多选题全部随机，正是这条路径）
                        #
                        # 2026-09-15 修 bug：_is_letter_only 依赖模块级"选项字母集"
                        # 上下文，而主流程从未调用 set_option_letters() → 上下文
                        # 恒为 None → 只能靠 len<=8 兜底，于是 AI 返回的英文单词
                        # （如 "review"、"origin"）会被误判成选项字母直接提交。
                        # 这里在判定前注入本题的合法选项字母集，判完立即清空，
                        # 避免串题。
                        _letters = _option_letters_of(q.get("options"))
                        _sol(_letters)
                        try:
                            _is_pure_letters = _is_letter_only(res)
                        finally:
                            _sol(None)
                        if _is_pure_letters:
                            answer = "".join(sorted(set(str(res).strip().upper())))
                        else:
                            # 多选处理
                            options_list = multi_cut(q["options"], _ORIGIN_HTML_CONTENT)
                            res_list = multi_cut(res, _ORIGIN_HTML_CONTENT)
                            if res_list is not None and options_list is not None:
                                for _a in clean_res(res_list):
                                    matched = False
                                    for o in options_list:
                                        if (
                                                is_subsequence(_a, o)  # 去掉各种符号和前面ABCD的答案应当是选项的子序列
                                        ):
                                            answer += o[:1]
                                            matched = True
                                            break  # 找到匹配项后立即停止，防止重复添加
                                    if not matched:
                                        best_letter = best_option_by_similarity(_a, options_list, threshold=0.8)
                                        if best_letter:
                                            answer += best_letter
                                # 对答案进行排序, 否则会提交失败
                                answer = "".join(sorted(set(answer)))
                            # else 如果分割失败那么就直接到下面去随机选
                    elif q["type"] == "single":
                        # 听力题（多小问）：按题干特征判定，答案规范化为连写字母。
                        if is_listening_question(q):
                            answer = normalize_listening_answer(res)
                        else:
                            # 普通单选：题库/AI 可能返回「选项内容（词）」而不是
                            # 「选项字母」，必须先映射回字母（2026-09-11 实测：
                            # 词答案直接提交会被平台拒绝"作业提交失败！"）。
                            # 注意不能用 "normalize(res) != res" 做判定——小写词
                            # "review" 会被 normalize 大写化，导致跳过映射逻辑。
                            answer = map_single_answer(res, q["options"], _ORIGIN_HTML_CONTENT)
                    elif q["type"] == "judgement":
                        answer = "true" if self.tiku.judgement_select(res) else "false"
                    elif q["type"] == "completion":
                        # 填空题（2026-09-13 重写）：平台按空分字段提交
                        # （answer{id}1 / answer{id}2 …），所以这里必须保留
                        # "每空一个值"的结构，不能拼成一整串。
                        # 此前用 "".join(res) 把多空答案粘成一个串，提交时
                        # 所有空拿到同一个拼接结果 —— 平台判错。
                        _n_blank = int(q.get("blankCount") or 0)
                        if isinstance(res, list):
                            _parts = [str(x).strip() for x in res if str(x).strip()]
                        else:
                            # 模型常返回 "a | b" / "a\nb" / "a，b" 形式，按分隔符拆
                            _parts = [p.strip() for p in
                                      re.split(r"\s*[|｜\n]\s*|\s*[，,]\s*", str(res))
                                      if p.strip()]
                        # ⚠️ 2026-09-16 修复「填空题整题提交为空」：此前条件是 _n_blank > 1，
                        # 单空（_n_blank == 1）落进下面的 else，**不写 _blank_parts**；
                        # 提交阶段于是走 `answer{qid}` 兜底分支 —— 而 PC 版页面根本没有
                        # answer{qid} 这个可提交字段（它只是回显用），_set_field 命中不到
                        # 就静默丢弃 → 平台收到空值 → 整题 0 分且不报任何错。
                        # 实测对照：光纤 7.9 的多空题（5/3/2 空）字段是 answerEditor{qid}N，
                        # 能提交；区域经济学 1.6/3.8 的单空题全是「我的答案：(1)」空 = 0 分。
                        # 单空与多空的真实字段同为 answerEditor{qid}1，故统一走该分支。
                        if _n_blank >= 1:
                            # 兜底（2026-09-13 实测补充）：模型偶尔仍把 N 个空的答案
                            # 拼成一整串放进数组唯一元素。此时按句末标点再拆一次；
                            # **只在拆分结果正好等于空数时**采用，避免误拆。
                            if len(_parts) == 1 and _n_blank > 1:
                                _cand = [c.strip() for c in
                                         re.split(r"(?<=[.。;；])\s*", _parts[0])
                                         if c.strip()]
                                if len(_cand) == _n_blank:
                                    logger.info(
                                        f"多空答案无分隔符，已按句末标点拆成 {_n_blank} 段")
                                    _parts = _cand
                            if len(_parts) < _n_blank:
                                # 空数不足：不足的位留空（后续轮次靠平台反馈补）
                                _parts = _parts + [""] * (_n_blank - len(_parts))
                            elif len(_parts) > _n_blank:
                                _parts = _parts[:_n_blank]
                            q["_blank_parts"] = _parts
                            answer = "".join(_parts)
                        else:
                            answer = _parts[0] if _parts else ""
                    else:
                        # 其他类型直接使用答案 （目前仅知有简答题，待补充处理）
                        answer = res

                    if not answer:  # 检查 answer 是否为空
                        logger.warning(f"找到答案但答案未能匹配 -> {res}\t随机选择答案")
                        # 🔴 随机前最后挣扎：让 AI 换模型再答一次（2026-09-29）。
                        # 这条路径最容易漏 —— 答案是**拿到了但映射失败**
                        # （如 AI 把英文选项翻成中文），AI 层不会重试。
                        _rescue = _ai_last_resort(self.tiku, dict(q))
                        if _rescue:
                            answer = _rescue
                            q[f'answerSource{q["id"]}'] = "ai-retry"
                        else:
                            answer = random_answer(q["options"], q["type"])  # 如果为空，则随机选择答案
                            q[f'answerSource{q["id"]}'] = "random"
                    else:
                        # ⚠ 只对**听力题 / 题组题**做连写字母规范化（2026-09-13 修复）
                        # 此前对所有题型无条件调用 normalize_listening_answer，
                        # 该函数会把 2 个以上字母的答案整体 .upper() ——
                        # 判断题的 "true"/"false" 因此变成 "TRUE"/"FALSE"，
                        # 平台不认，实测导致整卷"作业提交失败！"（重做也一样）。
                        # 填空题若答案是纯字母（如 "ai"/"h2o"）同样会被错误大写。
                        if is_listening_question(q) or q.get("is_group"):
                            raw_answer = answer
                            answer = normalize_listening_answer(answer)
                            if answer != raw_answer:
                                logger.info(f"题组答案规范化：{raw_answer!r} -> {answer!r}")
                        logger.info(f"成功获取到答案：{answer}")
                        q[f'answerSource{q["id"]}'] = "cover"
                        found_answers += 1
                # 填充答案
                # 填空/简答等非选择题若最终答案为空，不能标记为"有效答案"：
                # completion 的 check_submittable 只判非空，空串必须降级为随机来源，
                # 否则覆盖率会把"什么都没填"算成已答。
                if not str(answer).strip() and q.get(f'answerSource{q["id"]}') == "cover":
                    logger.warning(f"答案为空，降级为随机来源 -> {q['title'][:60]}")
                    q[f'answerSource{q["id"]}'] = "random"
                    found_answers -= 1

                # 题组题（听力 19 / 阅读理解 15）：answerField 里含每个小问的真实
                # 提交字段（answer{qid}{GUID}），按文档顺序（= 小问顺序）把连写答案
                # 逐字母分配到各小问字段；基础名 answer{id} 保留完整连写串（向后兼容）。
                # 2026-09-13：判定从"仅听力"放宽为"听力 or is_group"，让阅读理解
                # 题组也能正确拆分（此前只认听力，阅读理解的多个小问只会填第一个）。
                sub_fields = [k for k in q["answerField"]
                              if k.startswith("answer") and k != f'answer{q["id"]}'
                              and not k.startswith("answertype")]
                if ((is_listening_question(q) or q.get("is_group"))
                        and len(sub_fields) > 1 and isinstance(answer, str) and answer):
                    letters = list(answer)
                    if len(letters) < len(sub_fields):
                        # 补齐（2026-09-11 审查发现）：留空的小问平台必然判错，
                        # 此前直接留空 = 白丢分。这里优先用"平台已确认正确的小问答案"
                        # 补位，其余用选项字母随机补，至少保留得分机会。
                        try:
                            _sv = LearnedAnswers().get_sub_verified(
                                str(q.get("title") or ""))
                        except Exception:
                            _sv = {}
                        _pool = sorted(set(re.findall(r"[A-D]", str(q.get("options") or "")))) \
                            or list("ABCD")
                        _filled = []
                        for _i in range(len(sub_fields)):
                            if _i < len(letters):
                                _filled.append(letters[_i])
                                continue
                            _v = str(_sv.get(str(_i)) or "").strip().upper()
                            _filled.append(_v if len(_v) == 1 and _v in "ABCD"
                                           else random.choice(_pool))
                        logger.warning(
                            f"听力答案 {answer} 字母数({len(letters)})少于小问数"
                            f"({len(sub_fields)})，已补齐为 {''.join(_filled)}")
                        letters = _filled
                    for fname, letter in zip(sub_fields, letters):
                        q["answerField"][fname] = letter
                    logger.info(f"听力题按小问拆分提交: {answer} -> {len(sub_fields)} 个小问字段")
                q["answerField"][f'answer{q["id"]}'] = answer
                logger.info(f'{q["title"]} 填写答案为 {answer}')
            # 覆盖率只统计"答案确实可用"的题：
            # 之前只看 found_answers（搜到即算），导致格式非法（如带题号的听力答案、
            # 空串）也被算作有效，覆盖率虚高到 100% 后直接提交 —— 平台解析不了 → 0 分。
            # 2026-09-30 用户要求：**随机兜底的答案也算「填了」**。
            # 原逻辑只认 answerSource=="cover"，于是「题库没命中 → AI 换模型 →
            # 随机」这条链填进去的答案被判为"不可提交" → 覆盖率跌破阈值 →
            # 只暂存不提交 → 平台侧这一节永远显示"未完成"，用户还得手点进去
            # 自己蒙一次（用户原话：「看着这一节就像没做一样」）。
            # 现改为 cover / random / ai-retry 三类都算 usable，
            # 但**单独统计随机占比**，由下面的闸门限制"整卷靠蒙"。
            usable = 0
            _hit = 0          # 真实命中（题库 / 学习库 / AI 直接给出，格式合法）
            _guessed_n = 0    # 随机兜底（random / ai-retry）
            for q in questions["questions"]:
                filled = q["answerField"][f'answer{q["id"]}']
                _src = q.get(f'answerSource{q["id"]}')
                if (_src in ("cover", "random", "ai-retry")
                        and check_submittable(filled, q.get("type"))):
                    usable += 1
                    if _src == "cover":
                        _hit += 1
                    else:
                        _guessed_n += 1
                else:
                    logger.warning(
                        f"答案不可提交，本应放弃该题 -> {q['title'][:60]} 填写值={filled!r}")
            cover_rate = (_hit / total_questions) * 100
            guess_rate = (_guessed_n / total_questions) * 100
            fill_rate = (usable / total_questions) * 100
            logger.info(
                f"章节检测题库覆盖率： {cover_rate:.0f}%（可用答案 {usable}/{total_questions}，"
                f"填充率 {fill_rate:.0f}%；其中真实命中 {_hit}、随机兜底 {_guessed_n} = {guess_rate:.0f}%）")
            # 提交模式  现在与题库绑定,留空直接提交, 1保存但不提交
            is_manual_mode = (
                    getattr(self.tiku, 'is_manual', False) or
                    self.tiku.__class__.__name__ == 'TikuManual' or
                    (self.tiku.__class__.__name__ == 'TikuFallback' and any(
                        getattr(p, 'is_manual', False) or p.__class__.__name__ == 'TikuManual' for p in
                        getattr(self.tiku, 'providers', [])))
            )
            if self.tiku.get_submit_params() == "1":
                questions["pyFlag"] = "1"
            elif is_manual_mode or cover_rate >= self.tiku.COVER_RATE * 100 or self.rollback_times >= 1:
                questions["pyFlag"] = ""
            elif (fill_rate >= self.tiku.COVER_RATE * 100
                  and guess_rate <= getattr(self.tiku, "GUESS_MAX_RATIO", 0.5) * 100):
                # 覆盖率没达标，但**每题都填上了**（填充率达标），只是其中一部分是
                # 随机兜底补的、且占比在容忍范围内 → 按用户要求**照常提交**：
                # 交上去至少有蒙对的机会；不交则平台侧这一节显示"未完成"，
                # 用户还得手点进去自己选一次。
                #
                # ⚠ 必须先卡 fill_rate：否则"一题都没答出来"时 guess_rate = 0
                #   反而满足"随机占比低"的条件 → 空卷被提交（09-26 的 0 分就是这类）。
                questions["pyFlag"] = ""
                logger.info(
                    f"覆盖率 {cover_rate:.0f}% 低于 {self.tiku.COVER_RATE * 100:.0f}%，"
                    f"但填充率 {fill_rate:.0f}% 达标、随机兜底只占 {guess_rate:.0f}%"
                    f"（≤ {getattr(self.tiku, 'GUESS_MAX_RATIO', 0.5) * 100:.0f}%）"
                    f"-> 按用户要求照常提交")
            else:
                questions["pyFlag"] = "1"
                logger.warning(
                    f"覆盖率 {cover_rate:.0f}% 不足（填充率 {fill_rate:.0f}%），"
                    f"随机兜底占 {guess_rate:.0f}% "
                    f"-> 不予提交（避免空卷或整卷靠蒙）")
            # 组建提交表单
            if questions["pyFlag"] == "1":
                for q in questions["questions"]:
                    questions.update(
                        {
                            f'answer{q["id"]}':
                                q["answerField"][f'answer{q["id"]}'] if q[f'answerSource{q["id"]}'] == "cover" else '',
                            f'answertype{q["id"]}': q["answerField"][f'answertype{q["id"]}'],
                        }
                    )
                    # 听力题组的小问字段：answerField 保存着按文档顺序拆分好的单字母
                    for k, v in q["answerField"].items():
                        if k.startswith("answer") and k != f'answer{q["id"]}' and not k.startswith("answertype"):
                            questions[k] = v if q[f'answerSource{q["id"]}'] == "cover" else ''
            else:
                for q in questions["questions"]:
                    questions.update(
                        {
                            f'answer{q["id"]}': q["answerField"][f'answer{q["id"]}'],
                            f'answertype{q["id"]}': q["answerField"][f'answertype{q["id"]}'],
                        }
                    )
                    # 同上：带上听力题组的每个小问字段（缺它们"我的答案"就是空）
                    for k, v in q["answerField"].items():
                        if k.startswith("answer") and k != f'answer{q["id"]}' and not k.startswith("answertype"):
                            questions[k] = v

            q_list = questions["questions"]   # 表单回显提交也要遍历题目，先留引用
            del questions["questions"]

            # 4. 提交 / 保存
            # 协议为 2026-09-11 Playwright 真实浏览器抓包实证（非推测）：
            #   保存: POST /work/addStudentWorkNew
            #     ?_classId={clazzId}&courseid={courseId}&token={enc_work值}
            #     &totalQuestionNum={值}&ua=pc&formType=post&saveStatus=1&version=1&tempsave=1
            #   提交: POST /work/addStudentWorkNewWeb + 表单 action 原查询串
            #     + &ua=pc&formType=post&saveStatus=1&pos=&version=1
            #   两者 body 相同 = 表单 serialize（DOM 顺序、同名多值保留），其中：
            #     - 每小问字段 answer{qid}{短GUID} = 单字母
            #     - 基础字段 answer{qid} = JSON [{"{短GUID}":{"type":N,"answer":"X"},...}]
            #       （这是浏览器 setReadComprehensionAnswer() 的写入格式；
            #         我们此前发空串/连写串 → 服务端报"无效的参数"或静默丢弃）
            #     - answerwqbid = "{qid},{qid2},"（全部题ID 逗号拼接）
            #     - pyFlag = "1"(保存) / ""(提交)
            form_submit_ok = False
            pairs = None
            try:
                from bs4 import BeautifulSoup
                _soup = BeautifulSoup(_ORIGIN_HTML_CONTENT, "html.parser")
                _form = _soup.find("form", id="form1") or _soup.find(
                    "form", action=re.compile("addStudentWorkNew"))
                if _form is not None and _form.get("action"):
                    pairs = []
                    # input 与 textarea 都要收集（2026-09-13 实测修复）：
                    # 填空题的逐空答案字段是
                    #   <textarea name="answerEditor{qid}{N}">
                    # 只遍历 input 会把它整个漏掉 —— 填空题永远提交空值，
                    # 也就是"整题未作答"（0 分），而且不会报任何错。
                    for _el in _form.find_all(["input", "textarea"]):
                        nm = _el.get("name")
                        if not nm:
                            continue
                        if _el.name == "textarea":
                            pairs.append([nm, _el.get_text() or ""])
                        else:
                            pairs.append([nm, _el.get("value", "") or ""])

                    # 未命中的字段名先收集，提交前汇总告警一次。
                    # 「静默跳过」曾把"字段名拼错"伪装成"提交成功"——填空题 0 分
                    # 的根因就是这个，藏了很久才被翻出来。
                    _missed_fields = []

                    def _set_field(name, val):
                        for _i2, (_nm, _v) in enumerate(pairs):
                            if _nm == name:
                                pairs[_i2][1] = val
                                return
                        _missed_fields.append(name)

                    wqbid_ids = []
                    for q in q_list:
                        qid = str(q["id"])
                        cover = q.get(f'answerSource{qid}') == "cover"
                        blank = questions["pyFlag"] == "1" and not cover
                        subs = []
                        for k in q["answerField"]:
                            m2 = re.match(rf"^answer{re.escape(qid)}([0-9a-f\-]{{36}})$", k)
                            if m2:
                                subs.append((k, m2.group(1)))
                        sub_types = q.get("sub_types") or []
                        _parts = q.get("_blank_parts")
                        if subs:
                            obj = {}
                            for _idx3, (fname, guid) in enumerate(subs):
                                letter = "" if blank else q["answerField"].get(fname, "")
                                tval = sub_types[_idx3] if _idx3 < len(sub_types) else 0
                                obj[guid] = {"type": tval, "answer": letter}
                                _set_field(fname, letter)
                            _set_field(f'answer{qid}', json.dumps([obj], ensure_ascii=False))
                            wqbid_ids.append(qid)
                        elif _parts is not None:
                            # 填空题（2026-09-13 校正）：逐空字段名由**页面实际 DOM**
                            # 决定，不能自己拼：
                            #   PC 版章节测验 -> answerEditor{qid}1/2/…（textarea）
                            #   手机端作业   -> answer{qid}1/2/…（input）
                            # decode 已按真实结构把字段名记进 answerField，这里照用。
                            # 此前固定拼 answer{qid}N，与 PC 版页面对不上，
                            # 结果填空题整题提交为空（0 分且无报错）。
                            _n = len(_parts)
                            _set_field(f'tiankongsize{qid}', str(_n))
                            _prefix = (f'answerEditor{qid}'
                                       if f'answerEditor{qid}1' in q["answerField"]
                                       else f'answer{qid}')
                            for _bi in range(_n):
                                _set_field(f'{_prefix}{_bi + 1}',
                                           "" if blank else str(_parts[_bi]))
                            wqbid_ids.append(qid)
                        else:
                            _set_field(f'answer{qid}',
                                       "" if blank else q["answerField"].get(f'answer{qid}', ""))
                            wqbid_ids.append(qid)
                    _set_field("answerwqbid", "".join(x + "," for x in wqbid_ids))
                    _set_field("pyFlag", questions["pyFlag"])

                    # 排错开关（默认关闭，不影响生产）：把填空相关字段的最终提交值
                    # 打出来。填空题"整题提交为空却毫无报错"这类问题只有看到它才能定位。
                    if os.environ.get("CK_DUMP_FORM") == "1":
                        _dump = [(n, (v or "")[:36]) for n, v in pairs
                                 if n.startswith(("answerEditor", "tiankongsize"))]
                        logger.info(f"[表单dump] 填空相关字段 {len(_dump)} 个 -> {_dump}")

                    if _missed_fields:
                        logger.warning(
                            f"以下 {len(_missed_fields)} 个字段在提交表单中不存在，"
                            f"已跳过（若为 answer{{qid}} 属正常回显字段）："
                            f"{_missed_fields[:12]}")

                    if questions["pyFlag"] == "1":
                        enc_work = next((v for nm, v in pairs if nm == "enc_work"), "")
                        tqn = next((v for nm, v in pairs if nm == "totalQuestionNum"), "")
                        post_url = (
                            "https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew"
                            f"?_classId={_course['clazzId']}&courseid={_course['courseId']}"
                            f"&token={enc_work}&totalQuestionNum={tqn}"
                            "&ua=pc&formType=post&saveStatus=1&version=1&tempsave=1")
                    else:
                        action = _form["action"]
                        post_url = action if action.startswith("http") else \
                            "https://mooc1.chaoxing.com/mooc-ans/work/" + action
                        post_url += "&ua=pc&formType=post&saveStatus=1&pos=&version=1"

                    res = _session.post(post_url, data=[tuple(x) for x in pairs], headers={
                        "Host": "mooc1.chaoxing.com",
                        "X-Requested-With": "XMLHttpRequest",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "Origin": "https://mooc1.chaoxing.com",
                        "Referer": "https://mooc1.chaoxing.com/mooc-ans/work/doHomeWorkNew",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    })
                    if res.status_code == 200:
                        try:
                            rj = res.json()
                            if rj.get("status"):
                                form_submit_ok = True
                                logger.info(
                                    f'{"提交" if questions["pyFlag"] == "" else "保存"}答题成功(表单协议) -> {rj.get("msg")}')
                            else:
                                logger.warning(f'表单协议被拒，将退回旧端点 -> {rj.get("msg")}')
                        except ValueError:
                            logger.warning(f"表单协议响应非 JSON: {res.text[:120]}")
            except Exception as e:
                logger.warning(f"表单协议提交异常，退回旧端点: {type(e).__name__}: {e}")

            if not form_submit_ok:
                res = _session.post(
                    "https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew",
                    data=questions,
                    headers={
                        "Host": "mooc1.chaoxing.com",
                        "X-Requested-With": "XMLHttpRequest",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "Origin": "https://mooc1.chaoxing.com",
                    },
                )
                if res.status_code == 200:
                    res_json = res.json()
                    if res_json["status"]:
                        logger.info(f'{"提交" if questions["pyFlag"] == "" else "保存"}答题成功 -> {res_json["msg"]}')
                    else:
                        logger.error(f'{"提交" if questions["pyFlag"] == "" else "保存"}答题失败 -> {res_json["msg"]}')
                        return StudyResult.ERROR
                else:
                    logger.error(f'{"提交" if questions["pyFlag"] == "" else "保存"}答题失败 -> {res.text[:500]}')
                    return StudyResult.ERROR

            # 4.1 提交后自检：接口返回成功不等于答案被接收，回查服务端回显
            #     ⚠️ 必须传 q_list —— 此刻 questions["questions"] 已被 del，
            #        不传的话自检会读到空列表并静默跳过（见 _verify_submit_accepted 说明）
            _work_id = str(questions.get("workId", "") or _job["jobid"].replace("work-", ""))
            _work_answer_id = str(questions.get("workAnswerId", "") or "")
            if self._verify_submit_accepted(
                    _session, _course, _job, _job_info, questions,
                    _work_id, _work_answer_id, q_list) is False:
                return StudyResult.ERROR

            # 5. 若只是保存未提交，无法判断成绩，保持原有行为
            if questions["pyFlag"] == "1":
                return StudyResult.SUCCESS

            # 6. 提交后检查成绩：若未全部正确，则收集错误反馈并重新作答提交
            result_info = self._check_work_result(_session, _course, _job, _job_info, questions, q_list)
            if result_info is None:
                # 无法获取成绩详情（如接口异常），按原行为返回成功，避免误判失败
                return StudyResult.SUCCESS

            if result_info.get("all_correct", False):
                if result_info.get("pending_review"):
                    # 平台不自动判分（简答/填空类测验待教师批阅），
                    # 没有"已被判定错误"的题，按通过处理
                    logger.info("章节检测已提交，平台状态『待批阅』，按通过处理")
                else:
                    logger.info(
                        f"章节检测全部正确（成绩 {result_info.get('score', '?')} 分），通过！")
                return StudyResult.SUCCESS

            # 6.1 已提交完成、不可重做（如本课程听力测验只能做一次）：
            # 不再盲目重做（重做会被平台拒绝并触发无意义的章节重试），
            # 成绩与错题信息已沉淀进学习库，直接按通过处理。
            if result_info.get("redoable", True) is False:
                logger.warning(
                    f"章节检测未全对（得分 {result_info.get('score', '?')}），"
                    f"但该测验已提交且不可重做；错题信息已沉淀到学习库，跳过重做"
                )
                return StudyResult.SUCCESS

            # 7. 未全对：收集错误反馈，进入下一轮重做
            feedback_history = result_info.get("feedback", [])
            wrong_count = len(feedback_history)
            logger.warning(
                f"章节检测有 {wrong_count}/{total_questions} 题回答错误（成绩 {result_info.get('score', '?')} 分），"
                f"已将错误反馈给AI，准备重新作答提交 (第 {attempt + 1}/{max_retries + 1} 轮)"
            )
            self.rollback_times += 1

        # 达到最大重试次数仍未全对
        logger.error(f"章节检测重试 {max_retries + 1} 次仍未全部正确，请人工检查处理")
        return StudyResult.ERROR

    def _verify_submit_accepted(self, _session, _course, _job, _job_info,
                                questions, work_id, work_answer_id,
                                q_list=None) -> Optional[bool]:
        """提交后自检：平台这次到底收没收到答案。

        之前只看接口返回的 status，接口成功 ≠ 答案被接收（听力题提交带题号的
        原始文本时接口照样返回成功，但成绩是 0）。这里回查最新一次作答详情，
        用服务端回显的"我的答案"来确认。

        Args:
            q_list: 提交前的题目列表引用。

        🔴🔴 2026-10-08 **修复自检的第二重失效**（第一重是 `_parse_work_record_detail`
            缺 `return questions`）：
            本方法原先用 `questions.get("questions")` 取题目 —— 但调用点在此之前
            已执行 `del questions["questions"]`（构造提交表单时删的，题目引用存进
            `q_list`）⇒ 取到**空列表** ⇒ `needs_detail` 恒为 False ⇒
            **自检在"提交后自检"这行之前就 return None 了，从不发请求、不打日志**。
            实测证据：全部实例日志中「自检」出现 **0 次**（连 debug 都没有）。
            修法：显式接收 `q_list`；未传时回退旧行为（保持向后兼容）。

        Returns:
            True  - 服务端回显的作答内容非空（答案已生效）
            False - 服务端回显为空（答案未被接收）
            None  - 查不到详情，无法判断（按旧行为放行，避免误判）
        """
        # 题目列表：优先用调用方传进来的提交前引用
        _qs = q_list if q_list is not None else questions.get("questions", [])
        needs_detail = any(
            q.get(f'answerSource{q["id"]}') == "cover" for q in _qs
        )
        if not needs_detail:
            return None

        course_id = str(_course.get("courseId", ""))
        class_id = str(_course.get("clazzId", ""))
        cpi = str(_course.get("cpi", "") or questions.get("cpi", ""))

        records = None
        for attempt in range(3):
            try:
                resp = _session.get(
                    "https://mooc1.chaoxing.com/mooc-ans/work/record-list",
                    params={
                        "courseId": course_id, "classId": class_id, "workId": work_id,
                        "workAnswerId": work_answer_id, "cpi": cpi,
                        "api": "1", "mooc2": "1", "ut": "s",
                    },
                    timeout=20,
                )
                records = _parse_work_record_list(resp.text)
                if records:
                    break
            except Exception as e:
                logger.debug(f"提交后自检：获取作答记录失败 (第{attempt + 1}次): {e}")
            if attempt < 2:
                time.sleep(1.5)

        if not records:
            logger.debug("提交后自检：未取到作答记录，跳过自检")
            return None

        latest_times = max(r[0] for r in records)
        try:
            resp = _session.get(
                "https://mooc1.chaoxing.com/mooc-ans/work/record-detail",
                params={
                    "courseId": course_id, "classId": class_id, "workId": work_id,
                    "workAnswerId": work_answer_id, "times": str(latest_times), "cpi": cpi,
                    "ut": "s", "isdisplaytable": "0", "firstHeader": "2",
                    "isWork": "false", "workSystem": "0", "api": "1",
                    "archive": "false", "mooc2": "1",
                },
                timeout=20,
            )
            detail = _parse_work_record_detail(resp.text)
        except Exception as e:
            logger.debug(f"提交后自检：获取作答详情失败: {e}")
            return None

        if not detail:
            logger.debug("提交后自检：作答详情解析为空，跳过自检")
            return None

        expected = {
            str(q["id"]) for q in _qs
            if q.get(f'answerSource{q["id"]}') == "cover"
            and str(q["answerField"].get(f'answer{q["id"]}', "")).strip()
        }
        echoed = {str(d["id"]) for d in detail if str(d.get("my_answer") or "").strip()}
        missing = expected - echoed
        if not echoed:
            # 一题都没回显 ⇒ 明确异常：提交未被平台接收。
            # （`expected` 非空已由上面的 needs_detail 保证）
            logger.error(
                f"提交后自检未通过：服务端回显为空，答案疑似未被接收（题目ID {sorted(missing)}）。"
                f"本题组按失败处理，请人工确认后再重做"
            )
            return False
        if missing:
            # 🔴 2026-10-08：**部分**未回显不计失败，只告警。
            #   原因：平台对部分题型（听力等）不回显"我的答案"，本项目注释已记录该特性；
            #   若按"缺一题即失败"处理，会把成功提交误判成失败 → 触发整章重试（浪费+风控）。
            #   而"回显数为 0"才是可与"平台不回显"区分开的明确异常。
            #   ⚠️ 待观测：若长期出现大量 missing，需重新评估平台回显策略。
            logger.warning(
                f"提交后自检：{len(missing)}/{len(expected)} 题未回显"
                f"（平台可能不回显该题型），已回显 {len(echoed)} 题，判定提交有效"
            )
        else:
            logger.info(f"提交后自检通过：服务端已回显 {len(expected)} 题作答内容")
        return True

    def _fetch_work_page_html(self, _session, _course, _job, _job_info, cpi):
        """访问章节检测题目页，返回 HTML（已提交=详情页 / 未通过=可编辑页）。

        抽成独立方法的原因（2026-09-11）：record-list 有数据时走 record-detail
        主路线，但平台对多数测验不返回"正确答案"字段 → 解析为空 → 成绩检查被
        静默跳过，学习库/题库沉淀从未发生。修复方式是让主路线也能回退到题目页解析，
        两处共用本方法。
        """
        resp = _session.get(
            "https://mooc1.chaoxing.com/mooc-ans/api/work",
            params={
                "api": "1",
                "workId": _job["jobid"].replace("work-", ""),
                "jobid": _job["jobid"],
                "originJobId": _job["jobid"],
                "needRedirect": "true",
                "skipHeader": "true",
                "knowledgeid": str(_job_info.get("knowledgeid", "") or _job.get("knowledgeid", "")),
                "ktoken": str(_job_info.get("ktoken", "") or _job.get("ktoken", "")),
                "cpi": str(_job_info.get("cpi", "") or _job.get("cpi", "") or cpi),
                "ut": "s",
                "clazzId": str(_course.get("clazzId", "")),
                "type": "",
                "enc": str(_job.get("enc", "")),
                "mooc2": "1",
                "courseid": str(_course.get("courseId", "")),
            },
            timeout=20,
        )
        return resp.text

    def _harvest_work_page_result(self, html: str, q_list=None) -> Optional[dict]:
        """从"已提交详情页"解析作答结果，并沉淀学习库 / 题库。返回 result dict 或 None。

        用 newAnswerBx 作答块解析（_parse_answer_blocks + _group_blocks_by_question），
        不依赖平台未必返回的"正确答案"字段：
          - 平台判对（marking_dui）→ 写学习库 verified + **沉淀进题库 cache.json**
          - 平台判错 → 写负反馈（wrong），供下次遇同题时排除该答案
        """
        blocks = _parse_answer_blocks(html)
        if not blocks:
            return None
        grouped = _group_blocks_by_question(q_list or [], blocks)
        if not grouped:
            return None
        # 「待批阅」状态（2026-09-14 实地发现）：超星对简答/填空类测验**不自动
        # 判分**，详情页顶部显示"待批阅"，作答块里没有任何 mark/score 字段。
        # 此前这种情况被当成"全部判错 + 0 分"：除了产出误导性负反馈（会让 AI
        # 把原本正确的答案改掉），更危险的是下面"判定错误 → 从题库撤下"的分支
        # 会把题库里已确认正确的答案误删。
        if "待批阅" in html:
            logger.info("章节检测状态：待批阅（平台对简答/填空类不自动判分，需教师批阅）"
                        "→ 跳过对错判定、题库沉淀与重做")
            return {
                "all_correct": True,   # 没有"已被判定错误"的题，无重做必要
                "pending_review": True,
                "feedback": [],
                "score": None,
                "times": 0,
                "redoable": False,
            }
        learner = LearnedAnswers()
        feedback = []
        all_correct = True
        to_tiku = 0                    # 本轮沉淀进题库（cache.json）的题数
        sub_saved = 0                  # 本轮新增的"已确认小问答案"数
        for g in grouped:
            title = normalize_title(
                re.sub(r'（\d+\.\d+分）$', '',
                       re.sub(r'^\d+', '', str(g.get("title") or ""))))
            learner.record(title, g.get("my_answer") or "",
                           g.get("mark") or "unknown", g.get("score"),
                           options=g.get("options"))
            # 小问级沉淀（2026-09-11 用户需求）：题组没全对时，把其中"单独判对"
            # 的小问答案也记下来。听力题一个小问蒙对也是有效信息，下次查询会用
            # _apply_verified_subs 把它填进对应位置。
            _subs = g.get("subs") or []
            if _subs:
                sub_saved += learner.record_subs(title, _subs)
            # 平台确认正确的答案 → 沉淀进本地题库（cache.json）。
            # 只有这里（平台判据）和回收脚本才写题库；AI 的作答不再直接入库，
            # 避免"猜的答案被当成标准答案"（2026-09-11 用户反馈 3.x 听力大量判错）。
            _save_full = ""
            if g.get("mark") == "dui" and g.get("my_answer"):
                _save_full = str(g["my_answer"]).strip()
            else:
                # 整组这次没全对，但可能"多轮积累后每个小问都已被平台确认"——
                # 这在听力题上很常见（每轮蒙对一两个），等价于全对，
                # 组装成完整答案后同样沉淀进题库（2026-09-11 用户要求）。
                _n_q = len(_subs)
                if _n_q > 1:
                    _sv_all = learner.get_sub_verified(title)
                    if _sv_all and len(_sv_all) >= _n_q:
                        _save_full = _assemble_subs(_sv_all, _n_q)
                        if _save_full:
                            logger.info(
                                f"小问答案已集齐（{_n_q}/{_n_q}），组装完整答案沉淀进题库："
                                f"{title[:40]} -> {_save_full}")
                            # 小问全齐 == 等价于"整组全对"：同步登记为已验证答案，
                            # 顺带把该值从错题列表移除。否则下次会因"缓存值在错题里"
                            # 被否决、白跑一轮 AI（2026-09-11 审查发现）。
                            learner.record(title, _save_full, "dui", None,
                                           options=g.get("options"))
            if _save_full:
                try:
                    if CacheDAO().get_cache(title) != _save_full:
                        CacheDAO().add_cache(title, _save_full)
                        to_tiku += 1
                except Exception as e:
                    logger.debug(f"沉淀答案进题库失败: {e}")
            else:
                # 本次作答判错：若题库里存的正是这个被判错的值，必须撤下
                # （2026-09-11 审查发现）。否则错误答案会一直当"标准答案"复用，
                # 而听力测验只能做一次，本账号再也没有纠正机会。
                _my = str(g.get("my_answer") or "").strip()
                # 🔴 2026-10-01 修 bug：原判据是 `mark != "dui"`，于是 mark 解析失败
                #    得到的 `"unknown"` 也被当成"平台判错" → **把题库里本来正确的答案
                #    撤掉**（3 天实测 69 次，日志里表现为"沉淀进题库 -26 题"这种负数）。
                #    只有**明确的**判错/部分对才撤；unknown = 平台没给判定，不清不撤。
                if g.get("mark") in ("cuo", "bandui") and _my:
                    try:
                        if CacheDAO().get_cache(title) == _my:
                            CacheDAO().remove_cache(title)
                            to_tiku -= 1
                            logger.warning(
                                f"平台判定答案错误，已从题库撤下：{title[:40]} -> {_my}")
                    except Exception as e:
                        logger.debug(f"撤下题库答案失败: {e}")
            if g.get("mark") != "dui":
                all_correct = False
                # 2026-09-29（用户要求）：多选题的「部分正确」线索。
                # 判 bandui 说明这次选的组合里有对的、但不全；把"至少这些选项里
                # 有正确的"告诉模型，让它补全而不是换个方向重新猜。
                _hint = ""
                if g.get("mark") == "bandui":
                    try:
                        _ps = learner.get_partials(title)
                    except Exception:
                        _ps = []
                    _cand = set()
                    for _p in _ps:
                        if isinstance(_p.get("score"), (int, float)) and _p["score"] > 0:
                            _cand |= set(re.findall(r"[A-Za-z]", str(_p.get("ans") or "")))
                    if _cand:
                        _hint = ("\n  历史线索：你答过的组合里 " + "、".join(sorted(_cand)) +
                                 " 之中**至少有一个是正确的**（那次得了部分分），"
                                 "请在它们基础上**补全**答案，而不是完全另起。")
                # 🔴 2026-10-01：`unknown`（平台没给判定）**不能**对外宣称
                #    "平台已判该答案不通过" —— 那是假信息，会逼 AI 把一个可能
                #    正确的答案改掉（实测那批 unknown 题就是这么被反复重答的）。
                _mk = g.get("mark") or "unknown"
                if _mk == "unknown":
                    feedback.append(
                        f"- 题目：{title}\n"
                        f"  你的上次答案：{g.get('my_answer') or '(空)'}\n"
                        f"  本次未能取到平台判定，请重新核对本题、"
                        f"给出你最有把握的答案{_hint}"
                    )
                else:
                    feedback.append(
                        f"- 题目：{title}\n"
                        f"  你的上次答案：{g.get('my_answer') or '(空)'}"
                        f"（{'部分正确' if _mk == 'bandui' else '判定错误'}）\n"
                        f"  平台已判该答案不通过，请重新分析并给出不同答案{_hint}"
                    )
        total_score = sum(b.get("score") or 0.0 for b in blocks)
        st = learner.stats()
        logger.info(
            f"章节检测成绩解析：全对={all_correct} 总得分={total_score} | "
            f"沉淀进题库 {to_tiku} 题、已确认小问 {sub_saved} 个 | "
            f"学习库累计: 已验证 {st['verified']} 题 / 错题 {st['wrong']} 题 / "
            f"小问答案 {st.get('subs', 0)} 个")
        return {
            "all_correct": all_correct,
            "feedback": feedback,
            "score": total_score,
            "times": 0,
            # 已提交完成、不可重做：保护作答次数（本课程测验只能做一次，
            # 盲目重做会被平台拒绝并触发无意义的章节重试）
            "redoable": False,
        }

    def _check_work_result(self, _session, _course, _job, _job_info, questions,
                           q_list=None) -> Optional[dict]:
        """
        章节检测提交后，查询最新一次作答的成绩与对错详情，供判断是否需要重做。

        Args:
            _session: 当前会话
            _course: 课程信息
            _job: 任务点信息
            questions: 提交时使用的表单数据（含 workId / workAnswerId 等）
            q_list: 提交时的题目列表（含 answerField 小问字段，用于作答块分组与学习记录）

        Returns:
            {"all_correct": bool, "feedback": list[str], "score": float, "times": int,
             "redoable": bool}
            或 None（无法获取成绩详情时返回 None）
        """
        work_id = str(
            questions.get("workId", "")
            or questions.get("workRelationId", "")
            or _job["jobid"].replace("work-", "")
        )
        work_answer_id = str(questions.get("workAnswerId", "") or "")
        course_id = str(_course.get("courseId", ""))
        class_id = str(_course.get("clazzId", ""))
        cpi = str(_course.get("cpi", "") or questions.get("cpi", ""))

        # 1. 获取作答记录列表（提交后服务端异步生成记录，需稍作等待并多次重试）
        records = None
        for attempt in range(5):
            try:
                resp = _session.get(
                    "https://mooc1.chaoxing.com/mooc-ans/work/record-list",
                    params={
                        "courseId": course_id,
                        "classId": class_id,
                        "workId": work_id,
                        "workAnswerId": work_answer_id,
                        "cpi": cpi,
                        "api": "1",
                        "mooc2": "1",
                        "ut": "s",
                    },
                    timeout=20,
                )
                records = _parse_work_record_list(resp.text)
                if records:
                    break
            except Exception as e:
                logger.warning(f"获取章节检测作答记录失败 (第{attempt + 1}次): {e}")
            if attempt < 4:
                time.sleep(1.5)

        if not records:
            # 兜底：重新访问题目页判断是否已通过（详情页=已提交有成绩；可编辑页=未通过可重做）
            logger.warning("无法获取章节检测作答记录，尝试通过题目页状态判断")
            try:
                html = self._fetch_work_page_html(_session, _course, _job, _job_info, cpi)
                if 'answerwqbid' in html:
                    # 可编辑页面：说明未全部正确，可重新作答
                    logger.warning("题目页仍可编辑，判定章节检测未全部正确")
                    return {
                        "all_correct": False,
                        "feedback": [],
                        "score": 0.0,
                        "times": 0,
                        "redoable": True,
                    }
                # 已提交详情页：用作答块解析（不依赖"正确答案"字段）并沉淀
                res = self._harvest_work_page_result(html, q_list)
                if res:
                    return res
                if '正确答案' in html and '我的答案' in html:
                    # 兼容：老结构详情页（含正确答案字段）仍走记录详情解析
                    detail = _parse_work_record_detail(html)
                    if detail:
                        feedback = []
                        all_correct = True
                        unknown = 0          # 答案字段没解析出来的题数
                        for q in detail:
                            # 🔴 2026-10-08 移植上游 PR #626：解析失败**不得**当"对"
                            #   （两侧同为 '' 时旧代码判 `'' == ''` ⇒ 误判全对 ⇒
                            #    假成功）。改为：计入 unknown，且不认为全对。
                            if not q.get("parse_ok", True):
                                unknown += 1
                                all_correct = False
                                continue
                            my_ans = (q.get("my_answer") or "").strip()
                            correct_ans = (q.get("correct_answer") or "").strip()
                            # 🔴 归一化后再比：消除 对/true、全半角标点、选项前缀等
                            #   **纯格式差异**，避免"实际全对却判错"引发无谓重做。
                            if (normalize_answer_text(my_ans)
                                    != normalize_answer_text(correct_ans)):
                                all_correct = False
                                feedback.append(
                                    f"- 题目：{q.get('title', '')}\n"
                                    f"  题型：{q.get('type_label', '')}\n"
                                    f"  你的上次答案：{my_ans or '(空)'}\n"
                                    f"  正确答案：{correct_ans or '(空)'}"
                                )
                        if unknown:
                            logger.warning(f"作答详情有 {unknown} 题答案字段未能解析，"
                                           f"已跳过这些题的对错判定")
                        m = re.search(r'本次成绩<i>([\d.]+)</i>分', html)
                        score = float(m.group(1)) if m else 0.0
                        return {
                            "all_correct": all_correct,
                            "feedback": feedback,
                            "score": score,
                            "times": 0,
                            "redoable": False,
                            "unknown": unknown,
                        }
                return None
            except Exception as e:
                logger.warning(f"兜底判断章节检测状态失败: {e}")
                return None

        latest_times = max(r[0] for r in records)
        latest_score = dict(records).get(latest_times, 0.0)

        # 2. 获取最新一次作答详情（含每道题对错与正确答案）
        try:
            resp = _session.get(
                "https://mooc1.chaoxing.com/mooc-ans/work/record-detail",
                params={
                    "courseId": course_id,
                    "classId": class_id,
                    "workId": work_id,
                    "workAnswerId": work_answer_id,
                    "times": str(latest_times),
                    "cpi": cpi,
                    "ut": "s",
                    "isdisplaytable": "0",
                    "firstHeader": "2",
                    "isWork": "false",
                    "workSystem": "0",
                    "api": "1",
                    "archive": "false",
                    "mooc2": "1",
                },
                timeout=20,
            )
            detail = _parse_work_record_detail(resp.text)
        except Exception as e:
            logger.warning(f"获取章节检测作答详情失败: {e}")
            return None

        if not detail:
            # 主路线取不到数据（平台对多数测验不返回"正确答案"字段）：
            # 回退到题目页作答块解析。否则成绩检查被静默跳过，
            # 学习库 verified / wrong 与题库沉淀永远不会发生
            # （2026-09-11 实测：全时段日志"沉淀"0 次，3.x 听力大量判错却无记录）。
            logger.info("作答详情解析为空，回退到题目页作答块解析")
            try:
                html = self._fetch_work_page_html(_session, _course, _job, _job_info, cpi)
                if 'answerwqbid' in html:
                    logger.warning("题目页仍可编辑，判定章节检测未全部正确")
                    return {
                        "all_correct": False,
                        "feedback": [],
                        "score": 0.0,
                        "times": 0,
                        "redoable": True,
                    }
                res = self._harvest_work_page_result(html, q_list)
                if res:
                    return res
            except Exception as e:
                logger.warning(f"回退题目页解析失败: {e}")
            logger.warning("章节检测作答详情解析为空，跳过成绩检查")
            return None

        # 3. 逐题判断对错，收集错误反馈
        feedback = []
        all_correct = True
        compared = 0          # 真正比对过的题数：全是空答案时不能判"全对"
        for q in detail:
            my_ans = (q.get("my_answer") or "").strip()
            correct_ans = (q.get("correct_answer") or "").strip()
            if not correct_ans:
                continue      # 题目未返回正确答案，无从判断，不计入比对
            compared += 1
            # 🔴 2026-10-08 移植上游 PR #626：**归一化后**再比。
            #   原为直接 `my_ans != correct_ans` —— 页面"对"vs 答案"true"、
            #   "A."vs "A"、全半角标点等**纯格式差异**会被判成错 ⇒ 无谓重做
            #   （增加请求量与风控暴露；对"仅允许作答一次"的课程可能直接失败）。
            #   归一化是确定性的，不改变"真错"的判定，只消除格式噪声。
            if (normalize_answer_text(my_ans)
                    != normalize_answer_text(correct_ans)):
                all_correct = False
                feedback.append(
                    f"- 题目：{q.get('title', '')}\n"
                    f"  题型：{q.get('type_label', '')}\n"
                    f"  你的上次答案：{my_ans or '(空)'}\n"
                    f"  正确答案：{correct_ans or '(空)'}"
                )

        # 比对不到任何有效答案（platform 未回填"正确答案"字段）时，不能认作全对。
        # 典型是听力题：字幕/答案不返回，0 分却会被这里误判成"通过"。
        #
        # 🔴 2026-10-08 修（**D1 修复引入的副作用**）：
        #   本函数返回的 `detail` 曾因 `_parse_work_record_detail` 缺 `return` 恒为 None
        #   ⇒ 调用方一直走"回退到题目页作答块解析"分支（该分支会沉淀学习库/题库）。
        #   把 `return questions` 补回后主路线开始接管，但平台**通常不返回"正确答案"字段**
        #   ⇒ 每题的 `correct_ans` 都为空 ⇒ `compared == 0` ⇒ 原本直接 `return None`
        #   ⇒ **回退分支不再执行 ⇒ 学习库/题库停止沉淀**（实测：acc7 的"沉淀进题库"
        #      由修复前的 60 次/天 降为 0 次）。
        #   修法：`compared == 0` 时**同样回退**到题目页作答块解析（该方法不依赖
        #   "正确答案"字段，用平台判对/判错标记沉淀）。代价是每轮多一次页面请求。
        if compared == 0:
            logger.warning("章节检测：未取到可比对的有效答案（平台未返回『正确答案』字段），"
                           "回退到题目页作答块解析")
            try:
                _html = self._fetch_work_page_html(_session, _course, _job, _job_info, cpi)
                _res = self._harvest_work_page_result(_html, q_list)
                if _res:
                    return _res
            except Exception as _e:                                 # noqa: BLE001
                logger.warning(f"回退题目页解析失败: {_e}")
            logger.warning("章节检测：未取到可比对的有效答案，无法确认是否通过（不按全对处理）")
            return None

        logger.debug(f"章节检测成绩: {latest_score} 分, 全部正确: {all_correct}, "
                     f"错题数: {len(feedback)}, 已比对: {compared}/{len(detail)}")
        return {
            "all_correct": all_correct,
            "feedback": feedback,
            "score": latest_score,
            "times": latest_times,
        }

    def study_read(self, _course, _job, _job_info) -> StudyResult:
        """
        阅读任务学习, 仅完成任务点, 并不增长时长
        """
        _session = SessionManager.get_session()
        _resp = _session.get(
            url="https://mooc1.chaoxing.com/ananas/job/readv2",
            params={
                "jobid": _job["jobid"],
                "knowledgeid": _job_info["knowledgeid"],
                "jtoken": _job["jtoken"],
                "courseid": _course["courseId"],
                "clazzid": _course["clazzId"],
            },
        )
        if _resp.status_code != 200:
            logger.error(f"阅读任务学习失败 -> [{_resp.status_code}]{_resp.text[:500]}")
            return StudyResult.ERROR

        # 🔴 2026-10-04（何同学："不能没做完就报成功"）：
        #   原实现**只要 HTTP 200 就返回 SUCCESS**，完全不看平台回的 `msg`。
        #   真机实测 acc4 全部阅读任务都返回 `{"msg":"无效的请求参数","status":false}`
        #   —— 即**一个阅读都没做成**，但日志写的是 `Task success`，
        #   界面据此显示"已完成"，平台侧仍是黄点 ⇒ 又一例"假完成"。
        #   现改为**以 status/msg 为准**：不成功就如实返回 ERROR。
        #   （注：`readv2` 端点在本平台已不可用，真正原因见 §19 报告 ——
        #     平台改成了"阅读时长"机制，需另行逆向；此处先保证**如实反映**。）
        try:
            _resp_json = _resp.json()
        except Exception as e:                                         # noqa: BLE001
            logger.warning(f"阅读任务学习 -> 响应非 JSON（{e}），按未完成处理")
            return StudyResult.ERROR

        _msg = str(_resp_json.get("msg") or "")
        _ok = _resp_json.get("status") is True
        logger.info(f"阅读任务学习 -> {_msg}")
        if not _ok:
            logger.warning(f"阅读任务**未完成**（平台回：{_msg}）-> 如实上报失败，不伪装成功")
            return StudyResult.ERROR
        return StudyResult.SUCCESS

    # ======================================================================
    # 作业模块（2026-09-13 新增，独立于章节测验通道）
    # ======================================================================
    def list_homeworks(self, course: dict, include_all: bool = False) -> list[dict]:
        """扫描某门课程的作业列表（只读，不产生任何作答行为）。

        Args:
            include_all: True 时连「已完成/已批阅」的也返回（供界面让用户
                自行勾选"重做"）；默认 False 只返回待完成的。
        """
        _session = SessionManager.get_session()
        self.rate_limiter.limit_rate(random_time=True, random_max=0.5)
        try:
            works = scan_works(_session, course)
        except (PauseInterrupt, RiskControlError):
            raise
        except Exception as e:
            logger.warning(f"作业列表扫描异常（{course.get('title')}）：{e}")
            return []
        if include_all:
            logger.info(f"课程《{course.get('title')}》：{len(works)} 份作业（含已完成）")
            return works
        # 已完成 / 已批阅的不再处理
        todo = []
        for w in works:
            st = (w.get("status") or "")
            if st in _WORK_STATUS_DONE:
                logger.info(f"作业《{w.get('name')}》状态为「{st}」，跳过")
                continue
            todo.append(w)
        logger.info(f"课程《{course.get('title')}》：{len(works)} 份作业，"
                    f"其中 {len(todo)} 份待完成")
        return todo

    # ────────────────────────────────────────────────────────────────
    # 🔴 2026-09-26 新增：提交前的**平台侧回读校验**
    #
    # 用户明确要求（原话）：
    #   「提交时需要测它的完成率这些，都是要从平台那边获得正确的反馈。
    #     比如已经把答案写上了才能点提交。」
    #
    # 背景（R语言医学数据分析两份 0 分事故）：
    #   程序"以为"自己把答案填进去了，就直接提交 —— 但答案其实根本没写进去
    #   （`Tiku.query()` 丢掉答案那个 bug）。平台侧没有任何反馈被检查，
    #   于是两份作业都在"程序以为写对了"的情况下被提交成了 0 分。
    #
    # 解法：把「写答案」和「点提交」拆开，中间插一道**平台侧回读**：
    #   ① tempSave（暂存）—— 只写不交，不消耗作答次数、不产生成绩
    #   ② 重新拉答题页 —— 读平台上"每题到底存了什么"
    #   ③ 逐题比对：程序认为写了 X，平台读回来是不是 X
    #        · 全部对上 → 允许提交（这才叫"已经写上去了"）
    #        · 有对不上 → 拒绝提交，明确报出是哪几题
    #
    # ⚠️ 关键前提：必须**真的能回读到答案**。若平台不把已存答案回显到答题页
    #    （某些题型/改版后的页面），就退化为"校验不可用"——此时**不阻塞提交**，
    #    但必须留下明确日志，绝不能假装校验通过了。
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_platform_answers(html_text: str) -> dict:
        """从答题页 HTML 里抠出**平台当前已存**的答案。

        🔴🔴 2026-09-26 二次修正（**这是"填空题判定不回显"误判的真因**）：

        真机核对（`_dump_第2章-...-填空题.html`，72720 字节）发现
        **填空题的答案根本不是放在 `input` 里，而是 `textarea`**：

            <div id="ananas-editor-answer1" aria-label="已作答-1"></div>
            <textarea name="answer2152924721" id="answer2152924721"
                      style="display:none;">-1</textarea>

        注意字段名的构成：`answer` + **qid(215292472)** + **空序号(1)**。
        ⇒ 旧解析器只 select `input[name^=answer]` 且把 `answer2152924721`
          整串当 qid（得到 `2152924721`，对不上任何题）→ 判定"平台不回显"
          ⇒ 于是设了 `ECHO_TYPES` 把填空题排除在比对之外 —— **方向错了**。

        正确的形态清单：
          · 选择/多选/判断：<input type="hidden" name="answer{qid}" value="B">
          · **填空题：<textarea name="answer{qid}{空序号}">xx</textarea>**
          · 单选/多选勾选态：<input type="radio|checkbox" name="answer{qid}" checked>
          · 少数改版页：<div data-answer="xx" data-qid="...">

        `value` / textarea 文本里装的就是平台当前存的作答（未作答时为空）。
        这就是"从平台获得正确反馈"的落点：**读它就知道答案有没有写上去**。

        Returns:
            {qid(str): 平台答案(str)} —— **只包含非空值**。
            qid 一律是**纯数字题号**（去掉空序号后缀）。
            返回空 dict 表示"这道页面里没有任何已存答案"（区别于"解析失败"）。
        """
        try:
            from bs4 import BeautifulSoup
            import html as _h
        except Exception:
            return {}
        body = _h.unescape(html_text or "")
        soup = BeautifulSoup(body, "html.parser")
        out: dict = {}

        # ── qid 归一：answer2152924721 → 215292472（去掉尾部空序号）
        # 题号是 9 位纯数字（实测 215292472 / 215292590 / 215260741）。
        # 规则：取开头连续数字，若长度 > 9 则截断到 9 位（空序号一般是 1~2 位）。
        _qid_cache: dict = {}

        def _norm_qid(raw: str) -> str:
            if raw in _qid_cache:
                return _qid_cache[raw]
            digits = ""
            for ch in raw:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if not digits:
                _qid_cache[raw] = ""
                return ""
            qid = digits if len(digits) <= 9 else digits[:9]
            # 清理掉误留的尾随序号（个别页面多空连写 answer{qid}12）
            _qid_cache[raw] = qid
            return qid

        def _put(qid: str, val: str, multi=False):
            """写入结果。multi=True（多选/多空）用 '|' 连接。"""
            val = (val or "").strip()
            if not qid or not val:
                return
            if multi and out.get(qid):
                if val not in str(out[qid]).split("|"):
                    out[qid] = str(out[qid]) + "|" + val
            else:
                out[qid] = val

        # 🔴🔴 2026-09-26 22:50 **四题型真实字段形态（真机 56216179 逐页核对）**
        # ─────────────────────────────────────────────────────────────
        #  单选  <input type="hidden" name="answer405811899" value="《齐民要术》">
        #  多选  <input type="hidden" name="answers405811919" value="AB">  ← **复数 answers！**
        #  判断  <input type="hidden" name="answer405811939" value="false">
        #  填空  <textarea name="answer4058119591" style="display:none;">北京</textarea>
        #        <textarea name="answer4058119592" style="display:none;">上海</textarea>
        #        ⚠️ 多空 = answer{qid}1 / answer{qid}2 … **必须按尾号排序拼接**，
        #           否则出现 "上海|北京" 这种颠倒（比错更致命：看起来写了其实错位）
        #        （兄弟节点 <div id="ananas-editor-answer1" aria-label="已作答北京">
        #          只是 UI 冗余，textarea 里就有真值 —— 仅当 textarea 空时才用它）
        #  简答  <input type="hidden" name="answer405811979" value='长文本'>  ← **单引号！**
        # ─────────────────────────────────────────────────────────────
        # ⇒ 旧解析器三大漏洞：
        #   ① 只 select `input[name^=answer]`，**漏了 `answers{qid}`（多选）**
        #   ② 只认双引号 value，**漏了单引号 value（简答）**
        #   ③ 多空没有按序号排序 → 拼接顺序错乱

        # ① 通用：input / textarea，name 以 answer / answers 开头
        #    多空/多选用 (qid, 序号) 聚合后再排序拼接
        _mult: dict = {}        # qid -> [(seq, val)]
        _sing: dict = {}        # qid -> val
        for node in soup.select("input[name], textarea[name]"):
            name = (node.get("name") or "").strip()
            if not (name.startswith("answer") or name.startswith("answers")):
                continue
            is_multi = name.startswith("answers")   # 多选专用（复数）
            rest = name[7:] if is_multi else name[6:]
            qid = _norm_qid(rest)                    # 取前 9 位
            if not qid:
                continue
            # 尾部剩下的数字 = 小问/空序号（没有则 0）
            tail = rest[len(qid):] if len(rest) > len(qid) else ""
            seq = int(tail) if tail.isdigit() else 0
            typ = (node.get("type") or "").lower()
            if typ in ("radio", "checkbox"):
                if node.has_attr("checked"):
                    _mult.setdefault(qid, []).append((seq, (node.get("value") or "").strip()))
                continue
            if node.name == "textarea":
                val = (node.get_text() or "").strip()
            else:
                val = (node.get("value") or "").strip()
                if not val:
                    # 🔴 单引号 / 双引号 两种都要认（简答是单引号）
                    raw_tag = str(node)
                    m = re.search(r"""\bvalue\s*=\s*(['"])(.*?)\1""",
                                  raw_tag, re.S)
                    if m:
                        val = (m.group(2) or "").strip()
            if not val or val in ("-1",):     # -1 是平台"未作答"占位
                continue
            if is_multi or seq:
                _mult.setdefault(qid, []).append((seq, val))
            else:
                _sing[qid] = val

        for qid, v in _sing.items():
            out[qid] = v
        for qid, items in _mult.items():
            items.sort(key=lambda x: x[0])          # 🔴 按序号排序，防错位
            vals = []
            for _, v in items:
                if v and v not in vals:
                    vals.append(v)
            if vals:
                out[qid] = "|".join(vals)

        # ② 🔴 aria-label="已作答XX" —— **填空/简答的唯一可靠来源**
        #    形态：<div id="ananas-editor-answer1" aria-label="已作答北京">
        #          <div id="ananas-editor-answer"  aria-label="已作答长文本">
        #    ⚠️ 多空必须**按 id 尾号排序**（answer1/answer2…），
        #       否则 aria 出现顺序不定 → 测出 "上海|北京" 这种颠倒结果。
        _aria_items = []          # (序号, 值)
        for node in soup.select("[aria-label]"):
            lab = (node.get("aria-label") or "").strip()
            if not lab.startswith("已作答"):
                continue
            v = lab[len("已作答"):].strip()
            if not v:
                continue
            nid = (node.get("id") or "")
            m_seq = re.search(r"answer(\d+)$", nid)   # ananas-editor-answer1
            seq = int(m_seq.group(1)) if m_seq else 999
            _aria_items.append((seq, v))
        _aria_items.sort(key=lambda x: x[0])
        _aria_vals = [v for _, v in _aria_items]

        if _aria_vals:
            # 只在 ① 号没读到该题时补充（textarea 才是有值的权威来源）
            txt_qids: list = []
            for node in soup.select("textarea[name], input[name]"):
                name = (node.get("name") or "").strip()
                if not name.startswith("answer"):
                    continue
                qid = _norm_qid(name[6:])
                if qid and qid not in txt_qids:
                    txt_qids.append(qid)
            if len(txt_qids) == 1:
                qid = txt_qids[0]
                if not out.get(qid):
                    _put(qid, "|".join(_aria_vals), multi=True)
            else:
                for qid, v in zip(txt_qids, _aria_vals):
                    if not out.get(qid):
                        _put(qid, v)

        # ③ 兜底：data-answer 属性回显
        if not out:
            for node in soup.select("[data-answer]"):
                qid = (node.get("data-questionid")
                       or node.get("data-qid") or "").strip()
                val = (node.get("data-answer") or "").strip()
                if qid and val:
                    out[qid] = val
        return out

    def _verify_answers_on_platform(self, session, course, work, parsed,
                                    questions, answers,
                                    use_new=True, detail=None) -> dict:
        """回读平台，确认答案真的写进去了（提交前的最后一道 gate）。

        Returns:
            {"ok": bool, "reason": str, "detail": [str], "confirmed": int,
             "checked": bool}   # checked=False = 回读不可用，不做判定
        """
        def _result(ok, reason="", detail=None, confirmed=0, checked=True):
            return {"ok": ok, "reason": reason, "detail": detail or [],
                    "confirmed": confirmed, "checked": checked}

        if not use_new:
            # 旧版通道没有 tempSave/回读能力，明确标注"未校验"，不假装通过
            logger.info("作业使用旧版提交通道，无平台回读能力（跳过回读校验）")
            return _result(True, "旧版通道不支持回读", [], 0, checked=False)

        total = len(questions)
        if total == 0:
            return _result(True, "无题目", [], 0, checked=False)

        # ① 先暂存 —— 只写不交，不消耗作答次数
        try:
            save_res = self._submit_homework_once(
                session, course, work, parsed, questions, answers, True, use_new)
        except (PauseInterrupt, RiskControlError):
            raise
        except Exception as e:
            return _result(False, f"暂存失败（无法确认答案已写入）：{e}")

        if not (save_res or {}).get("status"):
            msg = str((save_res or {}).get("msg") or "平台未返回原因")
            return _result(False, f"暂存被平台拒绝：{msg}")

        logger.info("作业已在平台暂存，开始回读校验…")

        # ② 🔴🔴 逐页回读（2026-09-26 22:40 修）
        #    旧写法只请求一次答题页 → 永远第 1 题（且带 workAnswerId 更糟）
        #    → 只看到第 1 题的答案 → 其余全判"未写入" → 误拦 86 分作业。
        #    现在按题目数逐页读，合并全部页的答案。
        try:
            plat = self._readback_all_pages(
                session, course, work,
                detail=detail or work.get("_detail") or None,
                questions=questions)
        except (PauseInterrupt, RiskControlError):
            raise
        except Exception as e:
            logger.warning(f"回读答题页失败（不阻塞提交，但无法确认）：{e}")
            return _result(True, f"回读页面失败：{e}", [], 0, checked=False)

        if not plat:
            logger.warning("回读未拿到任何已存答案，无法确认答案是否写入")
            return _result(True, "回读拿不到页面内容", [], 0, checked=False)

        # 🔴🔴 2026-09-26 真实回读实测结论（5003 / 张旭 / R语言医学数据分析 6 份作业）
        # ─────────────────────────────────────────────────────────────
        # 题型            平台是否回显   实测证据
        # single(选择)      ✅ 回显       <input name="answer{qid}" value="B">
        # multiple(多选)    ✅ 回显       （同上机制）
        # judgement(判断)   ✅ 回显       <input name="answer{qid}" value="true">
        # completion(填空)  ✅ **回显**   <textarea name="answer{qid}{空序号}">-1</textarea>
        #
        # ⚠️⚠️ 重大更正（2026-09-26 19:10）：
        #   此前曾判定"填空题不回显"，并据此把填空题排除在比对之外。
        #   那是**解析器的 bug**，不是平台特性 —— 旧解析器只 select `input`，
        #   而填空题的答案放在 **`textarea`** 里，于是永远读不到 → 误判为不回显。
        #   真机证据（`_dump_第2章-...-填空题.html`）：
        #       <textarea name="answer2152924721" style="display:none;">-1</textarea>
        #   答案 `-1` 明明就在页面上。已修 `_parse_platform_answers`（补 textarea）。
        #
        # ⇒ 结论：**四种题型全部参与回读比对**，一律"以平台为准"。
        ECHO_TYPES = ("single", "multiple", "judgement", "completion")

        def _norm(x):
            """把答案归一化到"语义相同就算相同"的可比形态。

            🔴 2026-09-26 实测踩坑：多选题的选项顺序**不代表答案不同**
            （程序写 "CAB"、平台回显 "ABC" 是同一份作答），
            原先直接 `upper()` 比较 → 被判成"不符" → **错误地拒绝提交**。
            这里对"纯字母且像多选"的值做排序归一化。
            """
            s = str(x if x is not None else "").strip()
            s = s.replace("|", "").replace("｜", "")
            low = s.lower()
            if low in ("true", "t", "1", "对", "正确", "是", "√", "yes", "y"):
                return "true"
            if low in ("false", "f", "0", "错", "错误", "否", "×", "no", "n"):
                return "false"
            s = re.sub(r"\s+", "", s).upper()
            # 多选：全是字母（A-Z，长度≥2）→ 排序后比较，消除顺序差异
            if len(s) >= 2 and re.fullmatch(r"[A-Z]+", s):
                return "".join(sorted(set(s)))
            return s

        # 先看每种题型到底有没有回显能力（只要有 1 题读到值就算能回显）
        echo_capable = set()
        for q in questions:
            qid = str(q["id"])
            if plat.get(qid):
                echo_capable.add(q.get("type"))
        if not echo_capable and not plat:
            # 一道都没读到 → 平台整体不回显（可能未作答，也可能不支持）
            logger.warning("平台未回显任何已存答案（可能全部未作答，或该页不支持回读）"
                           " → 本次提交**未经平台内容校验**")
            return _result(True, "平台未回显任何答案", [], 0, checked=False)

        def _get_ans(qid_raw):
            """🔴🔴 2026-09-26 22:55 **answers 的 key 可能是 int 也可能是 str**。

            `iter_dohw_questions` 写的是 `answers[q["id"]]`，而
            `_parse_dohw_question` 返回的 id 是 **int**；但回读比对时
            `qid = str(q["id"])` 是 **str** → `answers.get(qid)` 恒为 None
            → 「应校验 59 题、确认 0 题」（真机实测踩到）。
            这里 int / str 两种 key 都试。
            """
            for k in (qid_raw, str(qid_raw)):
                try:
                    if k in answers:
                        return answers[k]
                except TypeError:
                    continue
            try:
                return answers.get(int(qid_raw))
            except (TypeError, ValueError):
                return None

        confirmed, mismatch, missing, skipped = 0, [], [], []
        for q in questions:
            qid, qtype = str(q["id"]), q.get("type")
            want = _get_ans(q["id"])
            if want is None or not str(want).strip():
                continue                     # 本来就没答案的题不参与比对
            if qtype not in ECHO_TYPES:
                skipped.append(f"[{qtype}] id={qid}（该题型平台不回显，跳过比对）")
                continue
            got = plat.get(qid)
            if got is None:
                # 该题型其它题能回显、但这题没有 → 说明这题确实没写进去
                if qtype in echo_capable:
                    missing.append(f"[{qtype}] id={qid} 平台未回显该题作答")
                else:
                    skipped.append(f"[{qtype}] id={qid}（该题型整体不回显）")
                continue
            if _norm(want) == _norm(got):
                confirmed += 1
            else:
                mismatch.append(f"[{qtype}] id={qid} 程序写入「{want}」"
                                f"→ 平台读回「{got}」")

        # 应参与比对（= 可选型且有答案的题）
        expect = sum(1 for q in questions
                     if q.get("type") in ECHO_TYPES
                     and _get_ans(q["id"]) is not None
                     and str(_get_ans(q["id"])).strip())

        logger.info(f"平台回读：应校验 {expect} 题（可选型），"
                    f"确认 {confirmed} 题，不符 {len(mismatch)} 题，"
                    f"未回显 {len(missing)} 题，跳过 {len(skipped)} 题（不可回显题型）")
        if skipped:
            for s in skipped[:5]:
                logger.info(f"    跳过：{s}")

        if mismatch or missing:
            detail = mismatch[:15] + missing[:5]
            reason = (f"平台回读不符：{len(mismatch)} 题内容不对、"
                      f"{len(missing)} 题未回显（应校验 {expect} 题，确认 {confirmed} 题）")
            return _result(False, reason, detail, confirmed)

        if expect and confirmed < expect:
            return _result(False,
                           f"平台仅确认 {confirmed}/{expect} 题（答案未全部写入）",
                           [], confirmed)

        if expect == 0:
            # 全是不可回显题型（例如整份都是填空题）→ 校验不可用，如实说明
            return _result(True, "本作业题型均不可回读", [], 0, checked=False)

        return _result(True, "", [], confirmed, checked=True)

    def _fetch_raw_answer_page(self, session, course, work, detail=None,
                               index=0) -> str:
        """直接取答题页原始 HTML（供回读校验用）。失败返回空串。

        🔴🔴🔴 2026-09-26 22:40 **回读必须走和翻页完全相同的姿势**：
          ① **绝不能带 `workAnswerId`** —— 带了平台进"续答模式"，
             永远返回第 1 题（回读只会看到第 1 题，其余全判"未写入"）。
          ② **必须先清粘性 cookie（route/k8s/jrose）** —— 否则 index 失效，
             同样永远第 1 题。
          ③ 要带 `index=N` 才能取到第 N 题。

        真机实测（56216179 / 100 题）：
            带 workAnswerId + 不清 cookie → 每次都是第 1 题（回读 0/66）
            用本函数姿势 → i=0/1/2/30/66/99 → 页面题号 1/2/3/31/67/100 ✅
        """
        try:
            from api.homework import _drop_sticky_cookies
        except Exception:
            _drop_sticky_cookies = None
        try:
            detail = detail or {}
            if _drop_sticky_cookies:
                _drop_sticky_cookies(session)
            params = {
                "courseId": detail.get("courseid") or work.get("courseid")
                            or course.get("courseId"),
                "workId": detail.get("workid") or work.get("workid"),
                "classId": detail.get("classid") or work.get("clazzid")
                           or course.get("clazzId"),
                "cpi": work.get("cpi") or course.get("cpi") or "",
                "oldWorkId": "", "status": "1", "mooc": "1", "source": "0",
                "checkIntegrity": "true",
                "enc": detail.get("enc") or work.get("enc") or "",
                # 🔴 唯一翻页手段；**不能**和 workAnswerId 并存
                "index": str(index),
            }
            resp = session.get(PAGE_WORK_DOHW, params=params, timeout=30)
            resp.raise_for_status()
            return resp.text or ""
        except (PauseInterrupt, RiskControlError):
            raise
        except Exception as e:
            logger.warning(f"取答题页原始 HTML 失败：{e}")
            return ""

    def _readback_all_pages(self, session, course, work, detail, questions,
                            max_pages=300) -> dict:
        """**逐页回读**整份作业，合并成 {qid: 平台当前答案}。

        Args:
            questions: 题目清单（用来知道总页数；一个 question 一页）
        Returns:
            {qid(str): answer(str)}，只含非空值（= 平台真的存了的）
        """
        merged: dict = {}
        n = len(questions) or 1
        n = min(n, max_pages)
        ok_pages = 0
        for i in range(n):
            try:
                # 🔴 2026-10-02 P1：批量循环里的固定间隔同样加随机（见 get_job_list 注释）
                self.rate_limiter.limit_rate(random_time=True, random_max=0.5)
            except Exception:
                pass
            html_text = self._fetch_raw_answer_page(
                session, course, work, detail=detail, index=i)
            if not html_text:
                continue
            ok_pages += 1
            try:
                part = self._parse_platform_answers(html_text)
            except Exception as e:
                logger.warning(f"回读第 {i + 1} 页解析失败：{e}")
                continue
            for k, v in (part or {}).items():
                if v and not merged.get(k):
                    merged[k] = v
        logger.info(f"逐页回读完成：共取 {ok_pages}/{n} 页，"
                    f"合并到 {len(merged)} 条平台答案")
        return merged

    def _answer_one_question(self, q: dict):
        """坐**单道题**的答案（供翻页回调 `iter_dohw_questions` 使用）。

        🔴 2026-09-26 20:15 新增。翻页协议要求"翻到哪题就答哪题"，所以
        必须把"查答案"抽成单题回调，而不是等所有题抓齐再统一查。

        与主流程里的查询逻辑保持一致：题库 → None 则返回 None
        （翻页场景下**不随机蒙**，让上层按真实答案率决定要不要提交，
         避免又把"蒙的答案"混进去当已作答）。
        异常时返回 None，不打断翻页。
        """
        try:
            ans = self.tiku.query(dict(q))
        except (PauseInterrupt, RiskControlError):
            raise
        except Exception as e:
            logger.warning(f"题目 {q.get('id')} 查询失败：{e}")
            return None
        if ans is None or not str(ans).strip():
            logger.warning(f"题目 {q.get('id')} 未获答案 -> "
                           f"{(q.get('title') or '')[:50]}")
            return None
        return ans

    def _submit_homework_once(self, session, course, work, parsed, questions,
                              answers, save_only, use_new=True,
                              work_answer_id=None) -> dict:
        """单次提交/暂存（供回读校验复用，避免重复内联）。

        🔴 2026-09-26 20:15：新增 `work_answer_id` —— 翻页场景下必须把最新
        的作答记录 ID 透传给 `submit_dohw`，否则正式交卷会带着旧 ID 提交，
        平台可能认成"另一份记录"→ 答案对不上。
        """
        if use_new:
            return submit_dohw(session, course, parsed, questions,
                               answers, save_only=save_only,
                               work_answer_id=work_answer_id)
        return submit_work(session, course, work, parsed, questions,
                           answers, save_only=save_only)

    def study_work_homework(self, course: dict, work: dict,
                            job_info: dict = None) -> StudyResult:
        """完成单份作业：抓题 → AI/题库作答 → 提交 → 回收成绩。

        与 study_work（章节测验、PC 协议）完全独立，互不影响。
        遇到需要人工介入的情况（未知题型 / 权限被拒 / 已批阅）会抛出
        HomeworkNeedsAttention，由上层暂停并汇报。
        """
        if not self.tiku or self.tiku.DISABLE:
            logger.warning("未配置题库，跳过作业（无法作答）")
            return StudyResult.SUCCESS

        _session = SessionManager.get_session()
        self.rate_limiter.limit_rate(random_time=True, random_max=0.5)
        work_name = work.get("name") or work.get("workid")

        # 0. 新通道（2026-09-26 新增）：列表页给的是 taskrefId，
        #    需先取详情页拿 workId/workAnswerId/enc，必要时先「重做」重置作答。
        detail = None
        if work.get("taskrefid") and not work.get("_legacy"):
            try:
                detail = fetch_work_detail(_session, course, work)
                logger.info(
                    f"作业《{work_name}》详情：workId={detail.get('workid')} "
                    f"得分={detail.get('score') or '未作答'} "
                    f"可重做={detail.get('redotimes')} 次")
            except (PauseInterrupt, RiskControlError):
                raise
            except HomeworkUnavailable as e:
                logger.info(f"作业《{work_name}》当前不可做：{e}")
                return StudyResult.SUCCESS
            except HomeworkNeedsAttention:
                raise
            except Exception as e:
                logger.error(f"作业《{work_name}》详情页获取失败：{e}")
                return StudyResult.ERROR

            # 已完成/已批阅的作业：只有用户明确勾了"重做"才动，否则跳过
            if work.get("redo"):
                self.rate_limiter.limit_rate(random_time=True, random_max=0.5)
                try:
                    rr = redo_work(_session, course, detail)
                except (PauseInterrupt, RiskControlError):
                    raise
                except Exception as e:
                    logger.error(f"作业《{work_name}》重做请求失败：{e}")
                    return StudyResult.ERROR
                if not rr.get("status"):
                    raise HomeworkNeedsAttention(
                        f"作业《{work_name}》重做被拒：{rr.get('msg')}")
                logger.info(f"作业《{work_name}》已重置作答，开始重做")
            elif (work.get("status") or "") in _WORK_STATUS_DONE:
                logger.info(f"作业《{work_name}》已完成且未勾选重做，跳过")
                return StudyResult.SUCCESS

        # 1. 抓题（新通道优先，旧通道兜底）
        #
        # 🔴🔴🔴 2026-09-26 20:15 **架构级修正：逐题翻页**
        #
        # 真机定论：doHomeWork 答题页**一题一页**，form 里只有 1 个
        # questionId / 1 个 index。旧代码 `fetch_work_page` 不传 index
        # → 永远第 0 题 → 「50 题作业只做了 1 题」→ 只拿 2 分。
        #
        # 翻页必须**边翻边答边暂存**（真机 getTheNextQuestion 协议）：
        #     GET ...&index=i → 答题 → POST tempSave=true → 拿新 answerId → i+1
        # 所以这里把"抓题"和"作答"合并成一个流程。
        _paging = None
        if detail is not None:
            _t_paging = time.time()
            try:
                _paging = iter_dohw_questions(
                    _session, course, detail,
                    answer_fn=lambda q: self._answer_one_question(q),
                    cpi=detail.get("cpi") or course.get("cpi", ""),
                    save_each=True,
                )
            except HomeworkUnavailable as e:
                logger.info(f"作业《{work_name}》当前不可做：{e}")
                return StudyResult.SUCCESS
            except (PauseInterrupt, RiskControlError):
                raise
            except HomeworkNeedsAttention:
                raise
            except Exception as e:
                logger.error(f"作业《{work_name}》翻页抓题失败：{e}")
                return StudyResult.ERROR
            logger.info(f"作业《{work_name}》翻页抓题完成，"
                        f"耗时 {time.time() - _t_paging:.1f}s，"
                        f"共 {len(_paging.get('questions') or [])} 题")

        try:
            if _paging is not None:
                # 翻页结果已经是完整题目列表，直接用
                parsed = {
                    "title": work_name,
                    "questions": _paging["questions"],
                    "form": {},
                    "total": _paging["total"],
                    "answer_id": _paging["answer_id"],
                    "error": None,
                }
                # 用第一题的原始页补回公共表单字段（提交时需要）
                if _paging["questions"]:
                    try:
                        # 🔴 不传 work_answer_id：传了会进"续答模式"→ index 失义
                        _p0 = fetch_work_page(
                            _session, course, detail, cpi=detail.get("cpi", ""),
                            index=0)
                        parsed["form"] = _p0.get("form") or {}
                        parsed["index"] = _p0.get("index", 0)
                        parsed["total"] = _p0.get("total") or _paging["total"]
                    except Exception as e:
                        logger.warning(f"补公共表单字段失败（不致命）：{e}")
            else:
                parsed = fetch_work(_session, course, work, job_info or {})
        except HomeworkUnavailable as e:
            logger.info(f"作业《{work_name}》当前不可做：{e}")
            return StudyResult.SUCCESS
        except (PauseInterrupt, RiskControlError):
            # 暂停信号 / 风控熔断必须透传！它们都继承 Exception，
            # 被下面的 except Exception 吞掉会导致"点了暂停停不下来"。
            raise
        except HomeworkNeedsAttention:
            # 用户要求：解决不了的先暂停再汇报 —— 直接上抛给 process_homeworks
            raise
        except Exception as e:
            logger.error(f"作业《{work_name}》抓题失败：{e}")
            return StudyResult.ERROR

        questions = parsed.get("questions") or []
        if not questions:
            logger.warning(f"作业《{work_name}》没有解析到题目，跳过")
            return StudyResult.SUCCESS

        total = len(questions)
        logger.info(f"作业《{work_name}》共 {total} 道题，开始作答")

        # 2. 搜答案（复用题库，含学习库/缓存/AI 全链路）
        answers: dict = {}
        usable = 0
        # 🔴 2026-09-26：「蒙的答案」与「真答案」必须分开计数。
        # 原先 `ans = random_answer(...)` 之后就统一 `usable += 1`，
        # 于是随机蒙出来的答案也算作"已解出" → 覆盖率永远显示 100%
        # → 覆盖率闸门（COVER_RATE）形同虚设，用户看到"覆盖率 100%"
        # 以为全做对了，实际上一份作业里可能有题是瞎填的。
        # 线上实例（2026-09-26 18:19，R语言医学数据分析）：
        #   填空题无答案 → 随机选"无"（空串）→ 覆盖率仍报 100% → 提交出去
        guessed = []
        # 🔴🔴 2026-09-26 用户质疑后新增：**"到底有没有真做题"的硬检查**。
        #
        # 用户原话：「我不是质疑你到底有没有真的去做这个题，
        #            你得真正的去做这个题才行啊」
        #
        # 判据：真实做题**必须**经过题库查询/AI 请求，这是**网络耗时**，
        # 不可能瞬间完成。实测（2026-09-26 18:58，真实题目）：
        #     填空题（题库未命中→真实调 AI）：1.78 秒
        #     判断题（题库未命中→真实调 AI）：2.35 秒
        # 而 18:19 出事那次，从"未能获取答案"到"提交成功"只用了 **66 毫秒**
        # —— 这个速度物理上不可能完成任何网络请求，等于**空手交卷**。
        #
        # 所以：答一题耗时 < 0.05 秒，且该题被判定"无答案"→ 视为异常，
        # 记入 no_work_evidence，由下面的闸门拦住，绝不再允许"秒交"。
        ANSWER_MIN_SECONDS = 0.05
        no_work_evidence = []      # "看起来根本没做题"的证据
        t_work_start = time.time()

        # 🔴 2026-09-26 20:15：若已走翻页流程，答案**已经在翻页时逐题查过并暂存**
        #    （见 iter_dohw_questions 的 answer_fn），这里直接复用，绝不重复查询
        #    —— 重复查询不但浪费 token，还可能因缓存不一致导致"查两次答案不同"。
        if _paging is not None and _paging.get("answers"):
            for q in questions:
                qid = q["id"]
                ans = _paging["answers"].get(qid)
                answers[qid] = ans
                if ans is not None and str(ans).strip():
                    usable += 1
                else:
                    guessed.append((qid, q.get("type"), str(ans)[:40]))
            total_cost = time.time() - t_work_start
            logger.info(f"作业《{work_name}》答案复用翻页结果（{len(answers)} 题）")
        else:
            for q in questions:
                qid = q["id"]
                t_q0 = time.time()
                # 常规题型走题库；判断题/填空/简答的答案形态校验已在 answer_check 覆盖
                try:
                    ans = self.tiku.query(dict(q))
                except (PauseInterrupt, RiskControlError):
                    raise
                except Exception as e:
                    logger.warning(f"题目 {qid} 查询失败：{e}")
                    ans = None
                q_cost = time.time() - t_q0
                if ans is None or not str(ans).strip():
                    logger.warning(f"题目 {qid} 未能获取答案 -> {q['title'][:50]}")
                    # 🔴 随机前最后挣扎：让 AI 换个模型再答一次（2026-09-29）。
                    # 用户要求"随机只能是最下下下策"——这里给它一次机会。
                    _rescue = _ai_last_resort(self.tiku, dict(q))
                    if _rescue:
                        ans = _rescue
                    else:
                        ans = random_answer(q.get("options", ""), q.get("type"))
                        guessed.append((qid, q.get("type"), str(ans)[:40]))
                    if q_cost < ANSWER_MIN_SECONDS:
                        no_work_evidence.append(
                            f"[{q.get('type')}] id={qid} 查询仅耗时 {q_cost*1000:.0f}ms 就"
                            f"判定无答案（真实查题库/AI 至少要数百毫秒）")
                answers[qid] = ans
                if str(ans).strip():
                    usable += 1

            total_cost = time.time() - t_work_start
            logger.info(f"作业《{work_name}》作答阶段总耗时 {total_cost:.2f}s，"
                        f"平均每题 {total_cost / total:.2f}s")

        cover_rate = usable / total if total else 0
        real_rate = (total - len(guessed)) / total if total else 0
        # 🔴🔴 2026-09-26 修（第二个漏洞）：
        # 原先只打印 cover_rate（含"蒙的答案"），闸门也用它 —— 而 random_answer
        # 对填空题返回的是字符串「无」、对判断题返回 "false"，**都是非空**，
        # 于是 usable 把它们全算进去 → cover_rate 永远 100% → 闸门形同虚设。
        # 线上铁证（2026-09-26 18:19:41.800）：
        #   题目 215292472 未能获取答案
        #   随机选择（completion 占位）-> 无
        #   答案覆盖率 100%              ← 一道题都没答对却报 100%
        #   提交成功 -> success！        ← 66 毫秒后空答案交出去
        # ⇒ 日志与闸门一律改用 **real_rate（真实答案率）**，
        #   cover_rate 只作为"答案字段非空率"参考值附注。
        logger.info(f"作业《{work_name}》真实答案率 {real_rate * 100:.0f}%"
                    f"（{total - len(guessed)}/{total} 题有可靠答案；"
                    f"另有 {len(guessed)} 题是占位值，不计入）")
        logger.info(f"  （参考）答案字段非空率 {cover_rate * 100:.0f}% "
                    f"—— 这个数字**不代表答对**，请以上面的真实答案率为准")
        if guessed:
            logger.warning(f"  以下 {len(guessed)} 道题没有可靠答案（已用占位值填充）：")
            for qid, qtype, val in guessed[:10]:
                logger.warning(f"    - [{qtype}] id={qid} -> {val or '(空)'}")

        # 🔴🔴 2026-09-26 用户明确要求（原话）：
        #   「我只是说凭程序反馈回来的正确率这些，测试这些，然后提交时需要
        #     测它的完成率这些，都是要从平台那边获得正确的反馈。
        #     比如已经把答案写上了才能点提交。」
        #
        # ⇒ 要求不是"不提交"，而是：**提交前必须回读平台，确认答案真的落上去了**。
        #   把"写答案"和"点提交"分成两步，中间加一道平台侧回读校验：
        #     ① 先 tempSave（暂存）—— 这一步只写不交，不消耗作答次数
        #     ② 回读答题页 —— 看平台上每题到底存了什么
        #     ③ 逐题比对：程序认为写了 X，平台读回来是不是 X
        #        · 全对上 → 才允许提交（这是"完成率"的真实依据）
        #        · 有对不上 → 拒绝提交，报告"哪几题没写进去"
        #
        # 这样"提交"这个动作就有了**平台侧证据**支撑，
        # 而不是靠程序自己推断"应该写进去了"（R语言两份 0 分就是这么来的）。
        cover_threshold = getattr(self.tiku, "COVER_RATE", 0.8)
        use_new = detail is not None
        # 🔴 2026-09-26 20:15：翻页流程下，平台每暂存一次就换一个新
        #    workRelationAnswerId，**正式提交必须带最新的那个**
        #    （真机 submitFormNew 里 answerId = $("#workRelationAnswerId").val()）。
        _final_answer_id = (_paging or {}).get("answer_id") if _paging else None

        def _do_submit(save_only: bool):
            return self._submit_homework_once(
                _session, course, work, parsed, questions,
                answers, save_only, use_new,
                work_answer_id=_final_answer_id)

        # ══════════════════════════════════════════════════════════
        # 🛑 第一道闸：**"根本没做题"的证据**（最快的一道，先拦）
        #
        # 用户质疑原话：「我不是质疑你到底有没有真的去做这个题，
        #                你得真正的去做这个题才行啊」
        #
        # 触发条件：有题在 <50ms 内就被判定"无答案"。
        # 真实查题库要 HTTP 往返、调 AI 要 1~3 秒（实测 1.78s / 2.35s），
        # 50ms 内出结果 = 根本没发出请求 = 空手交卷。
        # 线上铁证：18:19 那次从"无答案"到"提交成功"仅 66ms。
        # ══════════════════════════════════════════════════════════
        if no_work_evidence:
            msg = (f"作业《{work_name}》未提交：检测到**疑似未真正作答**"
                   f"（{len(no_work_evidence)}/{total} 题查询耗时异常短）")
            logger.error("=" * 66)
            logger.error(f"🛑 {msg}")
            for line in no_work_evidence[:10]:
                logger.error(f"    {line}")
            logger.error(f"    作答阶段总耗时仅 {total_cost:.2f}s"
                         f"（真实做题通常每题需 0.5~3 秒）")
            logger.error("  已保存草稿，未提交 —— 避免又一次空答案 0 分。")
            logger.error("=" * 66)
            try:
                _do_submit(save_only=True)
            except (PauseInterrupt, RiskControlError):
                raise
            except Exception as e:
                logger.warning(f"作业《{work_name}》保存草稿异常：{e}")
            raise HomeworkNeedsAttention(msg)

        # ── 提交前的平台侧回读校验 ────────────────────────────────
        # 只有真拿到"平台确认答案已写入"才允许交卷。
        # 校验不通过 = 绝不可提交（否则就是第二份 0 分作业）。
        verify = self._verify_answers_on_platform(
            _session, course, work, parsed, questions, answers,
            use_new=use_new, detail=detail)

        if not verify.get("ok"):
            reason = verify.get("reason") or "未能从平台确认答案已写入"
            msg = f"作业《{work_name}》未提交：{reason}"
            logger.warning("=" * 66)
            logger.warning(f"⚠ {msg}")
            for line in (verify.get("detail") or [])[:20]:
                logger.warning(f"    {line}")
            logger.warning("  已把能确认的答案保存为草稿，请到学习通核对后自行交卷。")
            logger.warning("=" * 66)
            try:
                _do_submit(save_only=True)
            except (PauseInterrupt, RiskControlError):
                raise
            except Exception as e:
                logger.warning(f"作业《{work_name}》保存草稿异常：{e}")
            raise HomeworkNeedsAttention(msg)

        n_ok = verify.get("confirmed", 0)
        logger.info(f"作业《{work_name}》平台回读校验通过："
                    f"{n_ok}/{total} 道答案已在平台上确认写入 → 允许提交")

        # 🔴 2026-09-26 修：闸门必须用 **real_rate**（真实答案率）。
        # 用 cover_rate（含"蒙的占位值"）会让闸门永远失效 —— 见上面 18:19 的
        # 线上铁证：一道题都没答上来，却报"覆盖率 100%"直接交卷。
        # 2026-09-30 用户要求（与章节测验路径同口径）：随机兜底占比在容忍范围内时，
        # **允许带随机答案提交** ——「交比不交强，不交的话看着就像没做」。
        # 只有随机占比过高（整卷靠蒙）才继续拦下、只存草稿。
        _guess_ratio = (len(guessed) / total) if total else 0
        _guess_max = getattr(self.tiku, "GUESS_MAX_RATIO", 0.5)
        # 放行条件 = 「填充率达标」**且**「随机占比不超限」——与章节测验路径同口径。
        # ⚠ 必须同时卡 cover_rate：否则"一题都没答出来"时 _guess_ratio = 0
        #   反而满足"随机占比低"的条件 → 空卷被提交（09-26 的 0 分正是此类）。
        if ((not self.tiku.SUBMIT)
                and (cover_rate < cover_threshold or _guess_ratio > _guess_max)):
            msg = (f"作业《{work_name}》真实答案率仅 {real_rate * 100:.0f}%、"
                   f"答案填充率 {cover_rate * 100:.0f}%"
                   f"（阈值 {cover_threshold * 100:.0f}%；"
                   f"{len(guessed)} 题无可靠答案，随机占比 {_guess_ratio * 100:.0f}% "
                   f"上限 {_guess_max * 100:.0f}%），已保存草稿、未提交")
            logger.warning(msg)
            try:
                save_res = _do_submit(save_only=True)
                if save_res.get("status"):
                    logger.info(f"作业《{work_name}》草稿已保存（未提交）")
                else:
                    logger.warning(
                        f"作业《{work_name}》草稿保存被拒：{save_res.get('msg')}")
            except (PauseInterrupt, RiskControlError):
                raise
            except Exception as e:
                logger.warning(f"作业《{work_name}》保存草稿异常：{e}")
            raise HomeworkNeedsAttention(msg)
        elif real_rate < cover_threshold:
            logger.info(
                f"作业《{work_name}》真实答案率 {real_rate * 100:.0f}% 未达阈值 "
                f"{cover_threshold * 100:.0f}%，但随机兜底只占 {_guess_ratio * 100:.0f}%"
                f"（≤ {_guess_max * 100:.0f}%）-> 按用户要求照常提交")

        # 3. 提交
        try:
            res = _do_submit(save_only=False)
        except (PauseInterrupt, RiskControlError):
            raise
        except Exception as e:
            # 网络/超时类异常可重试，不上升为"需人工"
            logger.error(f"作业《{work_name}》提交异常（可重试）：{e}")
            return StudyResult.ERROR

        if not res.get("status"):
            msg = str(res.get("msg") or "(平台未返回原因)")
            # 明确"需要人判断"的拒绝：次数用尽 / 权限 / 截止 / 已提交过
            fatal_kw = ("次数", "权限", "截止", "已提交", "已批阅", "过期",
                        "禁止", "无效的参数", "未创建完成")
            if any(k in msg for k in fatal_kw):
                raise HomeworkNeedsAttention(f"作业《{work_name}》平台拒绝：{msg}")
            logger.error(f"作业《{work_name}》提交失败（可重试）：{msg}")
            return StudyResult.ERROR

        logger.info(f"作业《{work_name}》提交成功 -> {res.get('msg')}")

        # 4. 回收成绩 → 沉淀进学习库（判对的答案下次直接命中）
        self._harvest_homework_scores(_session, course, work, parsed, questions)

        return StudyResult.SUCCESS

    def _harvest_homework_scores(self, session, course, work, parsed, questions):
        """提交后回收逐题判分，把答对的答案沉淀进学习库（learned_answers）。

        平台提交响应里可能带逐题标记；不一定都带，这里做**尽力而为**：
        带就沉淀，不带就静默跳过，绝不因回收失败影响主流程。
        """
        try:
            from api.learned import LearnedAnswers
            learner = LearnedAnswers()
            correct = 0
            # 新版页面：提交后重开答题页，页面上的 answer{qid} 保留上次作答，
            # 配合每题的对错标记即可判断。为避免多打一次请求，这里只在
            # 响应里明确给出逐题结果时才沉淀。
            marks = (parsed.get("_marks") or {})
            for q in questions:
                qid = q["id"]
                if qid not in marks:
                    continue
                ans, mark = marks[qid]
                if mark == "dui" and ans:
                    learner.record(q["title"], str(ans), "dui",
                                   options=q.get("options"))
                    correct += 1
            if correct:
                logger.info(f"作业《{work.get('name')}》回收 {correct} 道已判对的答案")
        except Exception as e:
            logger.debug(f"作业成绩回收跳过（不影响主流程）：{e}")

    def scan_homeworks_all(self, courses: list, include_done: bool = True,
                           with_redo: bool = True) -> dict:
        """扫描**所有课程**的作业清单（只读，绝不提交）。

        Args:
            with_redo: 是否额外逐份拉「重做次数 / 取分规则」。
                这两项只存在于作业详情页，列表页拿不到，所以要**逐份请求**。
                只对**已完成**的作业拉（未作答的没有重做一说），
                并发 6 条，实测 51 份约 8~12 秒。

        Returns:
            {"courses": [{
                "courseid", "title",
                "works": [{"name","status","taskrefid","enc_task","redo","score",
                           "redotimes","redo_total","redo_used","score_rule"}]
             }...],
             "total": n, "todo": n}
        """
        out = []
        total = todo = 0

        # 快路径：stu-work 列表页一次就返回全部课程的作业 → 只请求 1 次。
        # 慢路径（兜底）：逐课程请求，兼容"全量页拿不到该课程"的情况。
        #
        # 🔴 2026-09-26 关键修正：全量页的 courseId（班次 ID）与 get_course_list 的
        # courseId（模板 ID）**是两套体系，交集为 0**。实测 58 门课 vs 8 个作业
        # courseId，匹配数 0 —— 这正是用户反复反馈"作业怎么还是没有"的根因。
        # 可靠桥梁是**课程名**：作业条目自带的 course_full（取自 aria-label 的
        # 「所属课程XXX」，是完整名）。
        grouped, id2name = {}, {}
        try:
            grouped, id2name = fetch_all_works_detail(SessionManager.get_session())
        except Exception as e:
            logger.warning(f"作业全量扫描失败，回退逐课程模式：{e}")
            grouped, id2name = {}, {}

        # 🔴🔴 2026-09-26 二次修正（推翻上一条结论）：
        # 实测确认 **作业页的 courseId 与 get_course_list 的 courseId 是同一套**
        # （R语言 265817798、Python 265817797、离散数学 260672158 … 9/9 全中）。
        # 之前"交集为 0"的结论是**在 courseId 字段还没被正确解析时测的**，属误判。
        #
        # 既然 ID 同源 → **直接用 courseId 匹配（O(1)、零歧义）**。
        # 课程名只作极端兜底（名字会被平台截断成「R语言医学数...」，
        # 单靠名字匹配反而会漏课 —— 曾因此丢掉 6 门课的作业）。
        by_id: Dict[str, list] = {str(k): v for k, v in grouped.items() if k}

        # 课程名索引（兜底用）
        by_name: Dict[str, list] = {}
        for gcid, ws in grouped.items():
            nm = (id2name.get(gcid) or "").strip()
            if nm:
                by_name.setdefault(nm, []).extend(ws)
            else:
                by_name.setdefault("__unknown__", []).extend(ws)

        def _name_key(t: str) -> str:
            """课程名归一化：去掉空白与全角/半角差异，便于跨页匹配。"""
            t = (t or "").strip()
            t = t.split(" - ")[0].strip() if " - " in t else t
            return re.sub(r"\s+", "", t)

        name_idx = {_name_key(k): k for k in by_name if k != "__unknown__"}

        # 记录「已被某门课认领」的全量页 courseId → 剩下的就是孤儿作业
        # （2026-09-26：班级型课程 HHHH 的作业属于此类，见主循环后的补齐段）
        claimed_cids: set = set()

        def _lookup_works(course: dict):
            """找课程对应的作业列表。

            优先级：① courseId 精确匹配（主路径，ID 同源必然命中）
                    ② 课程名精确/前缀匹配（兜底，应对个别课程 ID 不一致）
            """
            cid = str(course.get("courseId") or "")
            if cid and cid in by_id:
                claimed_cids.add(cid)
                return by_id[cid]
            # ---- 兜底：按名字 ----
            k = _name_key(course.get("title") or "")
            if not k:
                return None
            if k in name_idx:
                gcid = name_idx[k]
                claimed_cids.add(gcid)
                return by_name[gcid]
            for nk, orig in name_idx.items():
                if not nk:
                    continue
                if k.startswith(nk) or nk.startswith(k):
                    claimed_cids.add(orig)
                    return by_name[orig]
            return None

        def _mk_items(works):
            items = []
            for w in works:
                st = w.get("status") or ""
                is_todo = st not in _WORK_STATUS_DONE
                items.append({
                    "name": w.get("name"),
                    "status": st or "未完成",
                    "taskrefid": w.get("taskrefid"),
                    "workrelationid": w.get("workrelationid"),
                    "enc_task": w.get("enc_task"),
                    "courseid": w.get("courseid"),
                    "clazzid": w.get("clazzid"),
                    "userid": w.get("userid"),
                    "cpi": w.get("cpi"),
                    # 🔴 必须带上 href：详情页只有走这个完整 URL（含 enc_task 签名）
                    # 才拿得到 redoTimes / 取分规则，手工拼参数会被打回 3991B 壳页。
                    "href": w.get("href"),
                    "todo": is_todo,
                    # 🔴 2026-09-26 语义修正：redo 原来写成 `not is_todo`
                    # （=已完成的默认勾重做），但实测这会让"已完成"的作业
                    # 被当成要重做 → 用户看到 6 份已完成全被勾上。
                    # 现在**重做必须由用户显式勾**，默认一律 False。
                    "redo": False,
                    # 下面几项由 with_redo 阶段填充
                    "redotimes": None,
                    "redo_total": None,
                    "redo_used": None,
                    "score": "",
                    "score_rule": "",
                })
            return items

        for c in courses:
            cid = str(c.get("courseId") or "")
            # 全量页拿到过数据 → 该列表就是"有作业的课程"全集，
            # 匹配不上 = 这门课确实没作业，**不必再逐门请求**（省 28 秒）。
            # 只有全量页整体失败（grouped 为空）时才逐课程兜底。
            #
            # 🔴 2026-09-28：**额外课程**（超星班等）不在 stu-work 全量页里，
            #    走快路径必然 `_lookup_works` 返回 None → 被 `continue` 静默
            #    跳过。必须对它们强制走逐课程请求。
            if grouped and not c.get("extra"):
                works = _lookup_works(c)
                if works is None:
                    continue
            else:
                if c.get("extra") and not c.get("cpi"):
                    # 额外课程只有 courseId+clazzId、没有 cpi，而作业列表接口
                    # 需要它 → 复用考试链路那套「课程中间页 302 解析」拿 cpi。
                    try:
                        from api.exam import fetch_course_tokens
                        tk = fetch_course_tokens(SessionManager.get_session(), c)
                        if tk.get("cpi"):
                            c = {**c, "cpi": tk["cpi"]}
                            logger.info(f"额外课程《{c.get('title')}》"
                                        f"解析到 cpi={tk['cpi']}")
                        else:
                            logger.warning(f"额外课程《{c.get('title')}》"
                                           f"未能解析出 cpi，作业列表可能为空")
                    except Exception as e:
                        logger.warning(f"额外课程 cpi 解析失败：{e}")
                try:
                    works = self.list_homeworks(c, include_all=include_done)
                except (PauseInterrupt, RiskControlError):
                    raise
                except Exception as e:
                    logger.warning(f"课程《{c.get('title')}》作业扫描失败：{e}")
                    works = []
            items = _mk_items(works)
            for it in items:
                total += 1
                if it["todo"]:
                    todo += 1
            if items:
                # 🔴 2026-09-28 修重复卡片：无论走**快路径还是慢路径**，只要产出了
                #    条目就算"这门课已认领"。此前 `claimed_cids` 只在 `_lookup_works`
                #    （快路径）里更新，而**额外课程走慢路径**（`list_homeworks`）
                #    → 不被记为已认领 → 下面的「孤儿作业补齐」段又补一份同 courseId
                #    的卡片。实测 acc2 的 241139787（办公软件高级…）出现两次：
                #      其一（extra）：无 clazzId / 无 cpi
                #      其二（orphan）：有 clazzId=141547852
                #    更糟的是 `taskByCourse()` 用 find() 取**第一个**匹配
                #    → 拿到缺 clazzId 的那份，提交作业时课程信息不全。
                if cid:
                    claimed_cids.add(cid)
                out.append({"courseid": cid,
                            "title": c.get("title"), "works": items})

        # ---- 补齐「孤儿作业」：全量页有、但没有任何课程认领的作业 ----
        #
        # 🔴 2026-09-26 新增：学生号可通过「建班级」得到一类**班级型课程**
        # （实测 HHHH，courseId=267569467）。它**不在 get_course_list() 里**
        # （实测 createcourse=0 / teacherEndCourse=0，39 门课全是 roletype=3），
        # 所以上面按课程遍历的主循环永远碰不到它 → 用户能看到作业消息，
        # 程序里却「怎么都扫不到」，无法作答。
        #
        # 修法：全量页 `stu-work` 本来一次就返回**所有**课程的作业，
        # 主循环漏掉的（= 未被任何课程认领的 courseId）在这里原样补成
        # 一个「伪课程卡片」，标题取作业自带的完整课程名（HHHH）。
        # 这样这类测试课无需任何额外配置即可扫描/勾选/作答。
        if grouped:
            # 从作业条目里抽 clazzId / cpi（同课程的所有作业一致，取第一个非空）
            clazzid2: Dict[str, str] = {}
            cpi2: Dict[str, str] = {}
            for _gcid, _ws in grouped.items():
                for _w in _ws:
                    if _w.get("clazzid") and _gcid not in clazzid2:
                        clazzid2[_gcid] = str(_w["clazzid"])
                    if _w.get("cpi") and _gcid not in cpi2:
                        cpi2[_gcid] = str(_w["cpi"])
            for gcid, ws in grouped.items():
                if not gcid or gcid in claimed_cids:
                    continue
                orphan_items = _mk_items(ws)
                if not orphan_items:
                    continue
                title = (id2name.get(gcid) or "").strip() or gcid
                logger.info(f"发现未归属课程的作业：{title}（{gcid}），"
                            f"{len(orphan_items)} 份")
                total += len(orphan_items)
                todo += sum(1 for it in orphan_items if it["todo"])
                # 伪课程卡片：补齐 courseId/clazzId/cpi，让下游（详情兜底、
                # 作答、界面渲染）与原课程卡片行为一致
                _clz = clazzid2.get(gcid, "")
                _cpi = cpi2.get(gcid, "")
                # 🔴 2026-09-28：**自动登记**孤儿课程，免去用户手填
                #    courseId + clazzId。孤儿作业取自 stu-work 全量页，其
                #    courseId 与 get_course_list 同源、clazzId 由条目自带，
                #    两个值都是平台给的（不用猜）。登记后**考试**扫描也能
                #    覆盖这门课 —— 考试没有全局接口，必须先知道
                #    courseId+clazzId 才能进课程页（用户问的"为什么不直接
                #    把课程加到列表"就是这个）。`add()` 自带去重，重复扫描
                #    不会写重复项。
                if _clz:
                    try:
                        from api import extra_courses
                        if all(str(_it.get("courseid")) != gcid
                               for _it in extra_courses.load()):
                            extra_courses.add(gcid, _clz, title)
                            logger.info(f"已自动登记额外课程：{title}"
                                        f"（courseId={gcid} clazzId={_clz}）")
                    except Exception as e:
                        logger.warning(f"自动登记额外课程失败：{e}")
                out.append({"courseId": gcid, "courseid": gcid,
                            "title": title, "clazzId": _clz, "cpi": _cpi,
                            "works": orphan_items, "orphan": True})

        # ---- 补「重做次数 / 取分规则」（只读，逐份详情页）----
        if with_redo and out:
            cbyid = {str(c.get("courseId") or ""): c for c in courses}
            # 只查已完成的：没做过的作业谈不重做
            probe = []
            for c in out:
                for it in c["works"]:
                    if not it["todo"]:
                        w = {
                            "taskrefid": it.get("taskrefid"),
                            "workid": it.get("taskrefid"),
                            "courseid": it.get("courseid") or c["courseid"],
                            "clazzid": it.get("clazzid"),
                            "userid": it.get("userid"),
                            "cpi": it.get("cpi"),
                            "enc_task": it.get("enc_task"),
                            # 🔴 带上 href —— 详情页只有走这个完整 URL 才拿得到
                            # redoTimes / 取分规则（实测手工拼参数会被打回壳页）
                            "href": it.get("href"),
                        }
                        probe.append(w)
            if probe:
                try:
                    info = fetch_redo_info_batch(
                        SessionManager.get_session(), cbyid, probe)
                except Exception as e:
                    logger.warning(f"重做信息批量拉取失败：{e}")
                    info = {}
                for c in out:
                    for it in c["works"]:
                        k = str(it.get("taskrefid") or "")
                        d = info.get(k)
                        if not d or not d.get("ok"):
                            continue
                        it["redotimes"] = d.get("redotimes", 0)
                        it["redo_total"] = d.get("redo_total", 0)
                        it["redo_used"] = d.get("redo_used", 0)
                        it["score"] = d.get("score", "")
                        it["score_rule"] = d.get("score_rule", "")
                n_ok = sum(1 for v in info.values() if v.get("ok"))
                logger.info(f"重做信息：{n_ok}/{len(probe)} 份拉取成功")

        # 🔴 2026-09-28 兜底去重：同一 courseId 只留一份（保留**信息更全**的，
        #    即带 clazzId 的那份）。上面两条路径理论上已互斥，这里防历史缓存
        #    与将来回归 —— 重复卡片会让同一份作业被做两遍，且前端
        #    `taskByCourse()` 用 find() 取第一个匹配，可能拿到缺 clazzId 的那份。
        if out:
            _by: dict = {}
            _order: list = []
            for _c in out:
                _k = str(_c.get("courseid") or "") or f"__{id(_c)}"
                if _k not in _by:
                    _by[_k] = _c
                    _order.append(_k)
                elif not _by[_k].get("clazzId") and _c.get("clazzId"):
                    _by[_k] = _c
            if len(_by) != len(out):
                logger.warning(f"作业扫描去重：{len(out)} → {len(_by)} 门"
                               f"（同 courseId 重复卡片）")
            out = [_by[_k] for _k in _order]

        return {"courses": out, "total": total, "todo": todo}

    # ------------------------------------------------------------------
    # 考试模块（2026-09-26 新增）：独立 exam，与作业/章节测验完全分开
    # ------------------------------------------------------------------
    def scan_exams_all(self, courses: list, progress=None) -> dict:
        """扫描**所有课程**的考试清单（只读，绝不提交）。

        ⚠️ 考试由老师发布后才出现；未发布时平台返回「暂无考试」，
        此时该课程不会出现在结果里（属正常，不是失败）。

        Returns:
            {"courses": [{"courseid","title","exams":[{title,status,...}]}],
             "total": n, "todo": n, "scanned": n, "failed": n}
        """
        from api.exam import scan_exams_all as _scan
        from api import extra_courses
        _session = SessionManager.get_session()

        # 🔴 2026-09-27：并入「额外课程」（超星班等不在课程列表接口里的课程）。
        #    这类课程没有 cpi，扫描时由课程中间页 302 的 Location 自动解析。
        # 🔴 2026-09-28：调用方（`scan_all_tasks` / 作业扫描）现在也会先并一次，
        #    这里按 courseId 去重，避免同一门课被扫两遍。
        try:
            _have = {str(c.get("courseId")) for c in (courses or [])}
            _extra = [c for c in extra_courses.as_courses()
                      if str(c.get("courseId")) not in _have]
        except Exception as e:
            logger.warning(f"读取额外课程失败（不影响常规扫描）：{e}")
            _extra = []
        if _extra:
            logger.info(f"考试扫描：常规课程 {len(courses or [])} 门"
                        f" + 额外课程 {len(_extra)} 门")
        return _scan(_session, list(courses or []) + _extra, progress=progress)

    def scan_all_tasks(self, courses: list, with_exam: bool = True,
                       exam_progress=None) -> dict:
        """一次扫完「作业 + 考试」，供选课界面登录后自动调用。

        作业走快路径（stu-work 全量页 1 次请求，约 0.5 秒）；
        考试必须逐课程拿令牌（约 0.3 秒/门，58 门约 17 秒）。

        Returns:
            {"courses": [{courseid,title,works:[...],exams:[...]}],
             "work_total","work_todo","exam_total","exam_todo","exam_failed"}
        """
        # 🔴 2026-09-28：并入「额外课程」，让**作业与考试共用同一份课程源**。
        #    此前只有考试扫描自己并（`scan_exams_all` 内部），作业扫描只认
        #    传进来的 courses → 登录后自动扫描会出现"某门课考试扫得到、
        #    作业却扫不到"（用户报："既然能识别出来考试和作业，为什么不把
        #    那个课程直接加到列表"）。这里统一并一次，两条链路都覆盖。
        courses = list(courses or [])
        try:
            from api import extra_courses
            _have = {str(c.get("courseId")) for c in courses}
            _extra = [c for c in extra_courses.as_courses()
                      if str(c.get("courseId")) not in _have]
            if _extra:
                logger.info(f"作业+考试扫描：常规课程 {len(courses)} 门"
                            f" + 额外课程 {len(_extra)} 门")
            courses += _extra
        except Exception as e:
            logger.warning(f"并入额外课程失败（不影响常规扫描）：{e}")

        works_res = self.scan_homeworks_all(courses, include_done=True)
        exams_res = {"courses": [], "total": 0, "todo": 0, "failed": 0}
        if with_exam:
            try:
                exams_res = self.scan_exams_all(courses, progress=exam_progress)
            except (PauseInterrupt, RiskControlError):
                raise
            except Exception as e:
                logger.warning(f"考试扫描失败（不影响作业）：{e}")

        # 合并：以作业结果为主键，考试挂到同 courseid 下
        merged: dict = {}
        order: list = []
        for c in works_res.get("courses") or []:
            cid = str(c.get("courseid") or "")
            if cid and cid not in merged:
                merged[cid] = c
                order.append(cid)
            merged.setdefault(cid, c)["exams"] = []
        for c in exams_res.get("courses") or []:
            cid = str(c.get("courseid") or "")
            if not cid:
                continue
            if cid not in merged:
                merged[cid] = {"courseid": cid, "title": c.get("title"),
                               "works": []}
                order.append(cid)
            merged[cid]["exams"] = c.get("exams") or []
        # 补齐字段，避免前端 undefined
        for cid in order:
            merged[cid].setdefault("works", [])
            merged[cid].setdefault("exams", [])

        return {
            "courses": [merged[cid] for cid in order],
            "work_total": works_res.get("total", 0),
            "work_todo": works_res.get("todo", 0),
            "exam_total": exams_res.get("total", 0),
            "exam_todo": exams_res.get("todo", 0),
            "exam_failed": exams_res.get("failed", 0),
        }

    # ------------------------------------------------------------------
    # 考试作答（2026-09-26 新增，手机端协议，链路见 api/exam.py）
    # ------------------------------------------------------------------

    def _exam_answer_fn(self, q: dict):
        """考试单题作答回调（复用题库 + AI 兜底链路，与作业同源）。"""
        try:
            # 🔴 2026-09-27 修 bug：考试题目的 `type` 是**数字**
            #    （0 单选 / 1 多选 / 2 填空 / 3 判断 / 4 简答），
            #    而题库+AI 链路全程按**字符串**（'single'/'multiple'/…）判断。
            #    结果：`check_answer(answer, 0, tiku)` 落到 else 分支
            #    → `check_submittable` 返回 False → **AI 给出的答案被一律判为
            #    "类型不符"丢弃**（真机日志：「AI大模型答题 返回答案类型不符，
            #    回退到下一个题库」，10 道题里 9 道明明答了却全被扔）。
            #    这里只做一次映射给查询用，**不改动原始题目 dict**
            #    （后续 build_answer_form 仍按数字 type 走）。
            _q = dict(q)
            if isinstance(_q.get("type"), int):
                _q["type"] = {0: "single", 1: "multiple", 2: "completion",
                              3: "judgement", 4: "shortanswer"}.get(
                                  _q["type"], str(_q["type"]))
            ans = self.tiku.query(_q)
        except (PauseInterrupt, RiskControlError):
            raise
        except Exception as e:
            logger.warning(f"[考试] 题目 {q.get('id')} 查询失败：{e}")
            return None
        if ans is None or not str(ans).strip():
            # 🔴 2026-10-03（何同学要求，补全考试链路）：
            #    顺序必须是「先让 AI 换模型再答 → 仍没有才随机」。
            #    ⚠️ 作业链路（`:3551`）与章节测验（`:1820` / `:1936`）**都**接了
            #    `_ai_last_resort`，**唯独考试没接** —— 于是"一个 AI 答不出"
            #    就直接丢题（2026-10-03 真机：50 题里 1 题因此空着）。
            #    ① 换模型重答：最多换 3 个槽位，附强制提示词（用户原话
            #      「一个 AI 返回识别不了，就调第二个 AI」「随机只能是最下下下策」）。
            _rescue = _ai_last_resort(self.tiku, q)
            if _rescue:
                logger.info(f"[考试] 题目 {q.get('id')} 经 AI 换模型重答成功：{_rescue}")
                return _rescue
            #    ② 仍没有答案 → 随机兜底：考试只有 1 次机会，空着 = 0 分；
            #      单选 N 选 1 → 期望 1/N，判断题 → 期望 50% ⇒ 严格优于空着。
            #      ⚠️ 考试题的 options 是 list[dict]（`{"letter","value","text"}`），
            #      不能直接喂 `random_answer()`（它按字符串 split 取首字母），
            #      见 `_random_exam_answer` 的说明。
            ans = _random_exam_answer(q, _q.get("type") or "")
            if ans:
                logger.info(f"[考试] 题目 {q.get('id')} 未获答案 → 随机兜底：{ans}")
                return ans
            logger.warning(f"[考试] 题目 {q.get('id')} 未获答案且无选项可随机 -> "
                           f"{(q.get('title') or '')[:50]}")
            # 🔴 2026-11-06：简答题（题型4）**没有选项**，`_random_exam_answer` 必然
            #   返回空 ⇒ 该题提交空内容 ⇒ 平台拒绝 ⇒ 连续 3 题触发
            #   "提交通道异常，提前终止"，**整卷未提交**（实测免疫学作业1：
            #   答到 203/222 卡在简答题，submitted=false，分数不更新）。
            #   这里给个占位文本，保证提交通道不被判异常、整卷能收尾；
            #   主观题本来就要人工/老师判分，占位不影响客观题得分。
            if _q.get("type") == "shortanswer":
                logger.info(f"[考试] 题目 {q.get('id')} 为简答题，用占位内容提交以保住整卷")
                return "略（要点见课堂笔记）"
            return None

        # 🔴🔴 2026-10-12（何同学真机实测反馈）：**判断题一律再过一遍 AI**。
        #
        # 实测 acc2《大学生劳动教育》85 题：30 道判断题的答案**100% 来自「全能搜题」**，
        # 人工核对发现 **11 题答反**：
        #   · 「恩格斯曾说过"劳动创造了人类本身"」→ 库答"错误"（实为正确）
        #   · 「端午节与春节、清明节、中秋节并称中国四大传统节日」→ 库答"错误"
        #   · 「胜任力的冰山模型，水面上的部分是关键因素」→ 库答"正确"
        #   · 同卷还自相矛盾：「董事长不属于专业技术人员」=正确 与
        #     「董事长、总经理属于第二大类(=专业技术人员)」=正确 —— 二者不可能同真
        #
        # 对照实验（`_diag_judge_ai.py`）：让 AI 独立重答同一批 30 题，
        # 与库答案**12 题不一致**，其中可人工判定的 10 题 **全部支持 AI、零反例**。
        # ⇒ 判断题以 AI 为准；单选/多选**不动**（那两类由 TikuAdapter 供给，质量明显更好）。
        #
        # ⚠️ 只对 judgement 生效，避免影响其余题型的既有准确率。
        if str(_q.get("type") or "") == "judgement":
            # 🔴🔴 2026-10-12 23:2x **回退**：判断题改回「**题库优先**」。
            #
            # 教训链（务必读完再改）：
            #   22:3x 我把「单次 AI 复核」升级成**双模型投票**，理由是代码里
            #   一条旧结论：「与库答案 12 题不一致，可人工判定的 10 题
            #   **全部支持 AI、零反例** ⇒ 判断题以 AI 为准」。
            #   ⇒ 实测 acc1《舞台人生》：**81 分，比修复前的 82 还低 1 分**。
            #   现场数据：判断题 30 道**全部**被投票覆盖，其中
            #   **7 道与 tiku.db 的库答案不同** —— 投票把最可靠的信号丢掉了。
            #
            #   错在哪：
            #     ① 那条「AI 全胜」的实验是在**中国历史人文地理**上做的，
            #        **跨到戏剧艺术就不成立** —— 拿旧实验当普适结论用。
            #     ② 投票「不一致取首个」= 无条件站 AI，等于**弃掉题库**。
            #        而 TikuAdapter 的一致率是全部来源里**最高档（89.5%）**。
            #
            # ⇒ 现策略（保守、可预期）：
            #     题库有答案 → **直接用题库**，不再让 AI 覆盖；
            #     题库没答案 → 才叫 AI 兜底（这是唯一必要的兜底）。
            #
            # ⚠️ 保留的两处**真 bug** 修复（与投票无关，它们是干净的修复）：
            #     · 判断题跳过「选项集合不符」校验（answer.py）
            #     · 判断题答案同义写法归一（answer.py）
            _lib = "" if ans is None else str(ans).strip()
            if _lib:
                return ans                      # 题库有答案 → 用题库
            # 题库没答案 → AI 兜底（走原来的单次复核，不投票）
            try:
                _ai = _ai_last_resort(self.tiku, q)
            except Exception as _e:                                  # noqa: BLE001
                _ai = ""
            if _ai and str(_ai).strip():
                logger.info(
                    f"[考试] 判断题 {q.get('id')} 题库无答案 → AI 兜底 = "
                    f"{_ai}：{(q.get('title') or '')[:36]}")
                return _ai
            return ans              # 两条路都没答案 → 保持题库返回（原样）
        # 🔴 2026-10-13 **半自动多选题复核**（方案 1）。
        #
        #   背景（两天实考数据）：
        #     · acc2 10-12「AI 覆盖题库」→ **82 分**
        #     · acc1 10-13「题库优先」   → **89 分**
        #     ⇒ 无条件让 AI 覆盖题库是**有害**的。
        #   但多选权重最高（15 题 × 2 分 = 30 分），且题库**确有错**：
        #     acc1 第 43 题「初唐四杰」库=BC（漏两人），AI 修正为 ABCD ✅
        #
        #   ⇒ 折中：**有条件的单次复核**（这才是"半自动"的本意）
        #       · AI 能映射成本题合法字母 且 ≠ 库 → 采纳 AI
        #       · AI 映射不出 / 格式非法        → 保留库（不猜）
        #       · 单选、判断完全不介入
        #
        #   🔴🔴 2026-10-13 12:5x **重要修正：只取 1 票，不做两票投票。**
        #     我先前把它做成 `_vote_raw(n=2)`，何同学指出关键事实：
        #     **腾讯系列模型共用一个 API Key，连续/并发调用会加剧限流。**
        #     实测印证：本场 13 道多选**全部**落到"票不足"，日志满是
        #     `APITimeoutError` + "额度耗尽→切换槽位" ⇒ 第二票几乎取不到，
        #     复核形同虚设，还**白白多烧一倍配额**（反而拖慢整场）。
        #     ⇒ 降为单票：调用量减半，行为与 acc1（89 分）实况一致。
        #     （`_vote_raw` 保留，将来若 AI 配额充裕可再启用两票）
        if str(_q.get("type") or "") == "multiple" and \
                getattr(self, "_exam_multi_review", True):
            try:
                _cur = "".join(sorted(set(c for c in str(ans or "").upper()
                                          if c in "ABCD")))
                _v = _ai_last_resort(self.tiku, q)
                _m = _map_rescue_answer(str(_v).strip(), q) if _v else ""
                if _m and all(c in "ABCD" for c in str(_m)):
                    _m = "".join(sorted(set(str(_m).upper())))
                    if _m and _m != _cur:
                        logger.warning(
                            f"[考试] 多选 {q.get('id')} 库={_cur or '(空)'}"
                            f" → AI={_m}（采用 AI）："
                            f"{(q.get('title') or '')[:34]}")
                        return _m
                    logger.info(
                        f"[考试] 多选 {q.get('id')} 库={_cur} 与 AI={_m} "
                        f"相同 → 保持：{(q.get('title') or '')[:34]}")
                else:
                    logger.debug(
                        f"[考试] 多选 {q.get('id')} AI 未给出合法字母 "
                        f"→ 保留库={_cur or '(空)'}")
            except Exception as _e:                                # noqa: BLE001
                logger.debug("多选题 AI 复核失败（保留库答案）：%s", _e)
            except Exception as _e:                                # noqa: BLE001
                logger.debug("多选题 AI 复核失败（保留库答案）：%s", _e)
        # 🔴🔴 2026-10-12 真机事故（acc2《机器的征途》exam_id=10929608）：
        #   85 题里**2 题的答案格式非法，平台必然判 0 分**：
        #     第 50 题（多选）程序答 `NMD` —— 这是**选项内容**不是字母；
        #                          正确应为 `C`（NMD 对应选项 C）
        #     第 71 题（判断）程序答 `这是人类航天史上的里程碑` —— AI 返回了
        #                          **解释性文本**，不是判断值
        #   根因：题库/AI 返回的答案**没有按题型做格式校验**就直接 return。
        #   `_map_rescue_answer`（内容→字母映射）只在 `_ai_last_resort` 分支里，
        #   主流程 `tiku.query` 的返回值从不过这一关。
        #   ⇒ 提交前按题型加一道**格式闸门**：不合法就修，修不了才回退。
        _t = str(_q.get("type") or "")
        _a = "" if ans is None else str(ans).strip()
        if not _a:
            return ans
        if _t == "judgement":
            if _a in ("正确", "错误", "对", "错", "A", "B"):
                return _a
            # 不合法 → 尝试从文本里识别；识别不出则按库答案兜底
            _fixed = _judge_from_text(_a)
            if _fixed:
                logger.warning("[考试] 判断题 %s 答案格式非法 %r → 修正为 %s",
                               q.get("id"), _a[:30], _fixed)
                return _fixed
            logger.warning("[考试] 判断题 %s 答案无法归一 %r → 保持原值",
                           q.get("id"), _a[:30])
        elif _t in ("single", "multiple"):
            # ⚠️🔴 2026-10-12 修正（自测抓到的严重错误）：
            #   **不能先"提取 A/B/C/D"**。像 `NMD`（第 50 题的正确答案内容）
            #   整个字符串**全是 A-D 字母** —— 先提字母会得到 `D`，
            #   而正确答案是 `C`。这比原 bug 更糟（原 bug 只是原样提交）。
            #   ⇒ 顺序必须是：**先按"纯字母串"判定 → 不是再走内容映射**；
            #     且"纯字母"要求**每个字母都是本题的合法选项**，
            #     否则 `NMD` 这种内容串会被误当答案。
            _opts = q.get("options") or ""
            try:
                _legal = {o.get("letter") for o in _opts
                          if isinstance(o, dict) and o.get("letter")}
            except TypeError:
                _legal = set()
            _up = _a.upper()
            _is_plain = (bool(_up) and all(c in "ABCD" for c in _up)
                         and (_legal <= set(_up) if _legal else True))
            if _is_plain:
                _letters = "".join(sorted(set(_up)))
                if _letters != _a:
                    logger.warning("[考试] %s 题 %s 答案 %r 顺序/重复异常 → 归一为 %s",
                                   _t, q.get("id"), _a[:30], _letters)
                return _letters
            # 不是纯字母（可能是选项内容，如 `NMD`）→ 走内容映射
            try:
                from api.base import _map_rescue_answer as _mra
                _mapped = _mra(_a, q)
            except Exception:                                          # noqa: BLE001
                _mapped = ""
            if _mapped and all(c in "ABCD" for c in str(_mapped)):
                logger.warning("[考试] %s 题 %s 答案为内容 %r → 映射为 %s",
                               _t, q.get("id"), _a[:30], _mapped)
                return _mapped
            logger.warning("[考试] %s 题 %s 答案 %r 无法映射字母 → 保持原值",
                           _t, q.get("id"), _a[:30])
        return ans

    def study_exam_one(self, course: dict, exam: dict, user_id: str = "",
                       code: str = "", auto_submit: bool = None) -> dict:
        """跑完一场考试（进考场→逐题作答→交卷）。

        ⚠️ **会真实消耗一次作答机会**，前端须由用户逐场确认后再调用。

        Args:
            exam: 考试条目，须含 exam_id（或 test_paper_id）+ enc_task
            auto_submit: True=答完直接交；False=只暂存（审核提交）；
                         None 时取配置 `exam_auto_submit`（**缺省 False**）
        """
        from api.exam import run_exam, fetch_exam_list_phone

        if auto_submit is None:
            # 🔴 2026-10-12：考试用**专用配置键** `exam_auto_submit`，缺省 False。
            #    此前读的是作业那个 `auto_submit`（缺省 True）—— 导致"作业自动提交"
            #    会顺带把只有一次机会的考试也交掉。两者从此互不牵连。
            # ⚠️ 同日修正：`Chaoxing` **没有 `self.config` 属性**（实测
            #   `AttributeError`）—— 原写法在此分支必崩。之所以从未暴露：
            #   所有真实调用点（`web_ui` / `process_exams`）都显式传了
            #   `auto_submit`，`None` 分支从未被走到。属潜伏 bug。
            #   改为从 kwargs 里安全取，取不到即按"不交卷"处理（保守）。
            _cfg = getattr(self, "config", None) or self.kwargs or {}
            auto_submit = bool(_cfg.get("exam_auto_submit", False))
        # 🔴 2026-09-27 修根因：**考试提交通道必须带真实 uid**。
        # `get_exam_signature(uid, ...)` 把 uid 编进风控签名（`enc_val = f"{uid}"`），
        # `data.userId` 也是它。而整条考试链路此前 user_id 一路是空串 →
        # 签名按 uid=0 计算 → 平台一律回 `提交失败：无效操作`：
        # 逐题暂存和交卷**全部被拒**（实测 100% 复现；日志里 50 题"暂存成功"
        # 只是我们的代码没检查返回体，实际平台全拒了）。
        # 对齐 CxKitty 的 `uid=self.acc.puid`：这里从 cookie 取（`get_uid()`）。
        if not user_id:
            try:
                user_id = str(self.get_uid() or "")
                logger.info(f"[考试] 已自动补齐 uid={user_id}")
            except Exception as _e:
                logger.warning(f"[考试] 取 uid 失败，提交可能被拒：{_e}")
        # 🔴 2026-09-27：`Chaoxing` 上根本没有 pause_event 属性（作业链路靠
        # PauseInterrupt 异常暂停，从不持有该事件），直接取会 AttributeError ——
        # 这段是 2026-09-26 随考试链路一起写的，但直到今天首次「开考」才被执行。
        # 用 getattr 兜住：没有该属性时等同"未暂停"。
        _pe = getattr(self, "pause_event", None)
        if _pe is not None and _pe.is_set():
            raise PauseInterrupt()

        # 缺 enc_task 时，现场用手机端列表补
        if not exam.get("enc_task"):
            try:
                lst = fetch_exam_list_phone(SessionManager.get_session(), course, user_id)
            except Exception as e:
                logger.warning(f"手机端考试列表拉取失败：{e}")
                lst = []
            want = str(exam.get("exam_id") or exam.get("test_paper_id") or "")
            for it in lst:
                if str(it.get("exam_id")) == want:
                    exam = {**exam, **it}
                    break

        title = exam.get("title") or exam.get("name") or "考试"
        logger.info(f"▶ 开始考试《{title}》"
                    f"（{'自动交卷' if auto_submit else '仅暂存'}）")
        t0 = time.time()
        try:
            # 🔴 2026-10-12：走 `run_exam_with_dump`，把「题干+完整选项+
            # 程序最终答案」逐题落盘。这是**唯一**能在不二次进考场的前提下
            # 拿到选项全文的时机（反复 start 未交卷会被平台限制，已实证）。
            # 落盘路径取自 kwargs `exam_dump_dir`（`Chaoxing` 无 config 属性）。
            # 为空则不落盘（行为与从前一致）。
            _cfg = getattr(self, "config", None) or self.kwargs or {}
            _dump_dir = str(_cfg.get("exam_dump_dir") or "").strip()
            # 🔴 2026-11-06：import 必须在**分支外**。原先把 `import api.exam as
            #   _exam_mod` 放在 `if _dump_dir:` 里面，但下面 `res = ...` 无条件
            #   使用 `_exam_mod` ⇒ 只要没配落盘目录就
            #   `UnboundLocalError: cannot access local variable '_exam_mod'`，
            #   整场考试直接失败（新账号登录时未带 exam_dump_dir 就会踩到）。
            import api.exam as _exam_mod
            _dump = ""
            if _dump_dir:
                # 直接从 exam 取标识（`want` 只在 enc_task 缺失分支里定义，
                # 这里复用它会 NameError）
                _eid = str(exam.get("exam_id") or exam.get("test_paper_id")
                           or title)
                _safe = re.sub(r'[\\/:*?"<>|\s]+', "_", _eid)
                # 🔴 2026-10-13：文件名加**实例前缀**。
                #   同一门考试会被**多个账号**考（如 acc1 与 acc4 都考了
                #   大学生劳动教育 exam_id=10984799），此前文件名只有 exam_id
                #   ⇒ 后跑的那场直接覆盖先跑的，acc1 的逐题落盘已被实测冲掉。
                #   实例名取自 `CK_DATA_DIR` 的末段（acc1..acc15），为空则不加前缀
                #   （单实例场景行为不变）。
                _inst = ""
                try:
                    _dd = os.environ.get("CK_DATA_DIR", "").rstrip("\\/")
                    _tail = _dd.split("\\")[-1].split("/")[-1] if _dd else ""
                    _inst = _tail if _tail.startswith("acc") else ""
                except Exception:                                # noqa: BLE001
                    _inst = ""
                _dump = os.path.join(
                    _dump_dir,
                    f"exam_{_inst + '_' if _inst else ''}{_safe}.jsonl")
            res = (_exam_mod.run_exam_with_dump(
                       SessionManager.get_session(), course, exam,
                       answer_fn=lambda q: self._exam_answer_fn(q),
                       user_id=user_id, code=code,
                       auto_submit=auto_submit, dump_path=_dump)
                   if _dump else
                   _exam_mod.run_exam(SessionManager.get_session(), course, exam,
                                      answer_fn=lambda q: self._exam_answer_fn(q),
                                      user_id=user_id, code=code,
                                      auto_submit=auto_submit))
        except (PauseInterrupt, RiskControlError):
            raise
        except Exception as e:
            logger.error(f"考试《{title}》异常：{type(e).__name__}: {e}")
            return {"ok": False, "title": title,
                    "msg": f"{type(e).__name__}: {e}"}
        logger.info(f"■ 考试《{title}》结束：{res.get('msg')}"
                    f"（耗时 {time.time() - t0:.1f}s）")
        res["title"] = title
        res["exam_id"] = exam.get("exam_id") or exam.get("test_paper_id") or ""
        return res

    def process_exams(self, course: dict, stop_flag=None,
                      exam_list: list = None, user_id: str = "",
                      code: str = "", auto_submit: bool = None) -> dict:
        """做完一门课程的全部（或指定的）考试。

        Args:
            exam_list: 指定要做的考试清单；None 时 = 该课全部可作答考试
        默认**逐场确认**由前端控制：前端只把用户勾选的传进来。
        """
        from api.exam import fetch_exam_list_phone, can_take_exam

        cid = str(course.get("courseId") or "")
        if exam_list is None:
            try:
                lst = fetch_exam_list_phone(SessionManager.get_session(), course, user_id)
            except Exception as e:
                logger.warning(f"考试列表拉取失败（{course.get('title')}）：{e}")
                lst = []
            # 只挑真正能进考场的（手机端列表 status 字段）
            todo = []
            for it in lst:
                st = it.get("status") or ""
                if any(w in st for w in ("已完成", "已批阅", "已过期", "已取消")):
                    continue
                todo.append(it)
        else:
            todo = list(exam_list)

        done = skipped = 0
        results = []
        for ex in todo:
            if stop_flag is not None and getattr(stop_flag, "is_set", None) \
                    and stop_flag.is_set():
                break
            _pe2 = getattr(self, "pause_event", None)
            if _pe2 is not None and _pe2.is_set():
                raise PauseInterrupt()
            r = self.study_exam_one(course, ex, user_id=user_id, code=code,
                                    auto_submit=auto_submit)
            results.append(r)
            if r.get("ok"):
                done += 1
            else:
                skipped += 1
        return {"done": done, "skipped": skipped, "results": results,
                "courseid": cid}

    def process_homeworks(self, course: dict, stop_flag=None,
                          work_list: list = None) -> dict:
        """做完一门课程的全部（或指定的）作业。

        Args:
            work_list: 指定要做的作业清单（含 taskrefid/redo）。
                       None 时 = 该课全部待完成的作业。

        Returns:
            {"done": n, "skipped": n, "need_attention": [原因...]}
        """
        if work_list is not None:
            todo = list(work_list)
            logger.info(f"课程《{course.get('title')}》按指定清单处理 {len(todo)} 份作业")
        else:
            todo = self.list_homeworks(course)
        result = {"done": 0, "skipped": 0, "need_attention": []}
        for w in todo:
            if stop_flag is not None and stop_flag():
                logger.info("收到停止信号，中断作业处理")
                break
            try:
                r = self.study_work_homework(course, w)
                if r == StudyResult.SUCCESS:
                    result["done"] += 1
                else:
                    result["skipped"] += 1
            except HomeworkNeedsAttention as e:
                logger.warning(f"作业需要人工处理，已暂停：《{w.get('name')}》-> {e}")
                result["need_attention"].append(f"{w.get('name')}：{e}")
                # 用户要求：遇到解决不了的先暂停再汇报 —— 不再继续后续作业
                break
            except (PauseInterrupt, RiskControlError):
                # 暂停 / 风控必须透传，不能被当成"作业处理异常"吞掉
                raise
            except Exception as e:
                logger.error(f"作业《{w.get('name')}》处理异常：{e}")
                result["skipped"] += 1
        return result

    def _send_monitor_heartbeat(self, course, point):
        """
        发送章节监控心跳包到 detect.chaoxing.com。

        模拟真实浏览器的 JSONP 打点请求，佐证访问行为的真人属性。

        Args:
            course: 课程信息字典
            point: 当前章节信息字典
        """
        version = get_timestamp()
        callback = f"jsonp{secrets.randbelow(10**21 - 10**20) + 10**20}"
        params = {
            "version": version,
            "refer": "http://i.mooc.chaoxing.com",
            "from": "",
            "fid": self.get_fid(),
            "jsoncallback": callback,
            "t": get_timestamp(),
        }
        referer_url = (
            f"https://mooc1.chaoxing.com/mycourse/studentstudy?"
            f"chapterId={point['id']}&courseId={course['courseId']}"
            f"&clazzid={course['clazzId']}&cpi={course['cpi']}&mooc2=1"
        )
        try:
            session = SessionManager.get_session()
            resp = session.get(
                "https://detect.chaoxing.com/api/monitor",
                params=params,
                headers={"Referer": referer_url},
                timeout=5,
            )
            logger.trace(f"Monitor heartbeat sent -> {resp.status_code}")
        except Exception as e:
            logger.trace(f"Monitor heartbeat failed (non-critical): {e}")

    def study_emptypage(self, _course, point):
        _session = SessionManager.get_session()
        # &cpi=0&verificationcode=&mooc2=1&microTopicId=0&editorPreview=0
        _resp = _session.get(
            url="https://mooc1.chaoxing.com/mooc-ans/mycourse/studentstudyAjax",
            params={
                "courseId": _course["courseId"],
                "clazzid": _course["clazzId"],
                "chapterId": point["id"],
                "cpi": _course["cpi"],
                "verificationcode": "",
                "mooc2": 1,
                "microTopicId": 0,
                "editorPreview": 0,
            },
            timeout=8,
        )
        if _resp.status_code != 200:
            logger.error(f"空页面任务失败 -> [{_resp.status_code}]{point['title']}")
            return StudyResult.ERROR
        else:
            logger.info(f"空页面任务完成 -> {point['title']}")
            return StudyResult.SUCCESS

    def _access_chapter_for_count(self, _course, point):
        _session = SessionManager.get_session()
        # &cpi=0&verificationcode=&mooc2=1&microTopicId=0&editorPreview=0
        _resp = _session.get(
            url="https://mooc1.chaoxing.com/mooc-ans/mycourse/studentstudyAjax",
            params={
                "courseId": _course["courseId"],
                "clazzid": _course["clazzId"],
                "chapterId": point["id"],
                "cpi": _course["cpi"],
                "verificationcode": "",
                "mooc2": 1,
                "microTopicId": 0,
                "editorPreview": 0,
            },
            timeout=8,
        )
        if _resp.status_code != 200:
            logger.error(f"章节访问失败 -> [{_resp.status_code}]{point['title']}")
            return None
        else:
            logger.info(f"章节访问成功 -> {point['title']}")
            return _resp.text

    def _extract_and_send_setlog(self, html_text):
        """
        从 studentstudyAjax 返回的 HTML 中提取 setlog URL 并执行。

        该 URL 包含服务端生成的 encode 参数，是记录章节学习次数的关键 API。

        Args:
            html_text: studentstudyAjax 返回的 HTML 内容
        """
        match = re.search(
            r'<script[^>]+src="(https://fystat-ans\.chaoxing\.com/log/setlog[^"]+)"',
            html_text
        )
        if not match:
            logger.trace("未在响应中找到 setlog URL")
            return

        setlog_url = match.group(1)
        try:
            session = SessionManager.get_session()
            resp = session.get(setlog_url, timeout=5)
            logger.trace(f"Setlog sent -> {resp.status_code}")
        except Exception as e:
            logger.trace(f"Setlog failed (non-critical): {e}")

    def increase_chapter_learning_count(self, course, points, target_count):
        """
        增加课程章节学习次数。

        循环遍历课程的所有章节，每访问一个章节页面：
        1. 调 studentstudyAjax 获取页面 HTML（含服务端生成的 setlog URL）
        2. 提取并执行 setlog URL（记录学习次数）
        3. 立即发送 monitor 心跳包（模拟 fn() 首次心跳）
        4. 停留 30 秒（模拟前端 setInterval(fn, 30000) 的间隔）
        5. 再次发送 monitor 心跳包（模拟 30s 后的第二次心跳）
        6. 计数器 +1，继续下一个章节

        Args:
            course: 课程信息字典
            points: 课程所有章节列表
            target_count: 目标总次数

        Returns:
            StudyResult: 操作结果
        """
        total = 0
        consecutive_failures = 0
        max_consecutive_failures = 10
        logger.info(f"开始增加章节学习次数, 目标总次数: {target_count}, 章节数: {len(points)}")
        if not points:
            logger.warning("章节列表为空, 跳过章节学习次数增加")
            return StudyResult.SUCCESS
        while total < target_count:
            for point in points:
                if total >= target_count:
                    break
                self.rate_limiter.limit_rate(random_time=True, random_min=0, random_max=0.2)
                html_text = self._access_chapter_for_count(course, point)
                if not html_text:
                    logger.error(f"章节学习次数增加失败, 当前章节: {point['title']}")
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        logger.error(
                            f"章节学习次数增加连续失败 {consecutive_failures} 次, 终止任务"
                        )
                        return StudyResult.ERROR
                    continue

                consecutive_failures = 0

                # 第 1 步：从 HTML 中提取 setlog URL 并执行（真正的计次 API）
                self._extract_and_send_setlog(html_text)

                # 第 2 步：立即发送 monitor 心跳包（模拟 fn()）
                self._send_monitor_heartbeat(course, point)

                # 第 3 步：停留 30 秒（模拟前端 setInterval 间隔）
                time.sleep(30)

                # 第 4 步：再次发送 monitor 心跳包（模拟 setInterval 触发的第二次心跳）
                self._send_monitor_heartbeat(course, point)

                total += 1
                logger.info(f"章节学习次数进度: {total}/{target_count}")
        logger.info(f"章节学习次数增加完成, 共完成: {total} 次")
        return StudyResult.SUCCESS
