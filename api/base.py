# -*- coding: utf-8 -*-
import functools
import json
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
    HomeworkNeedsAttention,
    HomeworkUnavailable,
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


def map_single_answer(res, options_str: str, origin_html_content="") -> str:
    """普通单选：把题库/AI 返回的「选项内容（词）」映射回「选项字母」。

    平台对单选题只认选项字母（如 "C"）。题库/AI 常返回选项内容本身
    （如 "review"），直接提交会被平台拒绝（"作业提交失败！"，2026-09-11 实测）。

    返回映射出的字母；无法映射时返回空串（调用方走随机作答），
    避免把非法格式提交给平台。
    """
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
        每题信息列表：{id, title, type_label, my_answer, correct_answer}
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

        # 我的答案
        mam = re.search(r'我的答案：</span>\s*<div class="fl answerCon">\s*(.*?)\s*</div>', qb, re.S)
        my_answer = re.sub(r'<[^>]+>', '', mam.group(1)).strip() if mam else ''

        # 正确答案
        cam = re.search(r'正确答案：</span>\s*<div class="fl answerCon">\s*(.*?)\s*</div>', qb, re.S)
        correct_answer = re.sub(r'<[^>]+>', '', cam.group(1)).strip() if cam else ''

        questions.append({
            "id": qid,
            "title": title,
            "type_label": type_label,
            "my_answer": my_answer,
            "correct_answer": correct_answer,
        })


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

    def login(self, login_with_cookies=False):
        if login_with_cookies:
            logger.info("Logging in with cookies")
            SessionManager.update_cookies()
            logger.debug(f"Logged in with cookies: {SessionManager.get_instance()._session.cookies}")
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
        resp = _session.post(_url, headers=gc.HEADERS, data=_data)
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

        logger.trace(f"原始章节列表内容:\n{_resp.text}")
        logger.info("课程章节读取成功...")
        return decode_course_point(_resp.text)

    def get_job_list(self, course: dict, point: dict) -> tuple[list[dict], dict]:
        _session = SessionManager.get_session()
        self.rate_limiter.limit_rate()
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

        # 学习界面任务卡片数（2026-09-13 性能优化）：
        # 超星用 num 参数分页返回章节内的任务点卡片，原实现**固定试 0~6 共 7 次
        # 请求/章节** —— 实测 27 章的课程就是 189 次请求，这是"读得慢"的主因。
        # 绝大多数章节只有 1 个卡片，因此改为**连续 2 次返回空就停**：
        #   · 单卡片章节：num=0 命中 → num=1 空 → num=2 空 → 停（3 次，省 57%）
        #   · 多卡片章节：逐次累加，直到连续 2 次为空（语义与原来一致，不丢任务点）
        # 保留"累加所有非空结果"，所以行为等价，只是不再无谓地试到最后。
        _empty_streak = 0
        for _possible_num in "0123456":

            logger.trace("开始读取章节所有任务点...")

            cards_params.update({"num": _possible_num})
            _resp = _session.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params=cards_params)
            if _resp.status_code != 200:
                logger.error(f"未知错误: {_resp.status_code} 正在跳过")
                logger.error(_resp.text)
                return [], {}

            _job_list, _job_info = decode_course_card(_resp.text)
            if _job_info.get("notOpen", False):
                # 直接返回, 节省一次请求
                logger.info("该章节未开放")
                return [], _job_info

            job_list += _job_list
            job_info.update(_job_info)

            if _job_list:
                _empty_streak = 0
            else:
                _empty_streak += 1
                if _empty_streak >= 2:
                    logger.trace(f"连续 2 次无任务点(num={_possible_num})，停止探测")
                    break

        if not job_list:
            self.study_emptypage(course, point)

        logger.trace(f"原始任务点列表内容:\n{_resp.text}")
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
                    captcha_solver = CxCaptcha(user_agent=ua, cookies=cookies_str, ocr=ocr_inst)
                    solved = False
                    for attempt in range(3):
                        logger.info(f"第 {attempt + 1} 次尝试通关验证码...")
                        if captcha_solver.try_pass():
                            logger.success("验证码通关成功！")
                            solved = True
                            break
                        else:
                            logger.warning("验证码验证失败，正在重试...")
                            time.sleep(2)
                    if solved:
                        _session.cookies.update(captcha_solver.s.cookies)
                        res = _session.get(_url, params=params, headers=headers)
                    else:
                        logger.error("多次验证码通关失败，可能需要手动干预。")
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
                    logger.trace(resp.text)
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
            logger.trace(resp.text)
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
            logger.debug(resp.text)
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

        forbidden_retry = 0
        max_forbidden_retry = 2

        # 仅当本地已有进度时才尝试"瞬间完成"。从 0 开始的新视频若一上来就把
        # playingTime 推到 duration，服务器比对真实耗时与进度增量会发现严重不匹配，
        # 判定为拖拽作弊，isPassed 迟迟不给 true（实测 4.1/4.2 要干等 5-10 分钟）。
        passed = False
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
                        logger.warning(
                            "出现403报错, 正在尝试刷新会话状态 (第{}次)",
                            forbidden_retry,
                        )
                        time.sleep(random.uniform(2, 4))
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
        if self.tiku.DISABLE or not self.tiku:
            return StudyResult.SUCCESS

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

            # 2. 设置上一轮错误反馈（供AI重新作答时参考）
            if feedback_history and hasattr(self.tiku, 'set_work_feedback'):
                try:
                    self.tiku.set_work_feedback(feedback_history)
                    logger.debug("已将上一轮错误反馈设置到题库")
                except Exception as e:
                    logger.warning(f"设置题库错误反馈失败: {e}")

            # 3. 搜题
            total_questions = len(questions["questions"])
            found_answers = 0
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
                        if _n_blank > 1:
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
            usable = 0
            for q in questions["questions"]:
                filled = q["answerField"][f'answer{q["id"]}']
                if (q.get(f'answerSource{q["id"]}') == "cover"
                        and check_submittable(filled, q.get("type"))):
                    usable += 1
                else:
                    logger.warning(
                        f"答案不可提交，本应放弃该题 -> {q['title'][:60]} 填写值={filled!r}")
            cover_rate = (usable / total_questions) * 100
            logger.info(f"章节检测题库覆盖率： {cover_rate:.0f}%（可用答案 {usable}/{total_questions}）")
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
            else:
                questions["pyFlag"] = "1"
                logger.info(f"章节检测题库覆盖率低于{self.tiku.COVER_RATE * 100:.0f}%，不予提交")
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

                    def _set_field(name, val):
                        for _i2, (_nm, _v) in enumerate(pairs):
                            if _nm == name:
                                pairs[_i2][1] = val

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
                    logger.error(f'{"提交" if questions["pyFlag"] == "" else "保存"}答题失败 -> {res.text}')
                    return StudyResult.ERROR

            # 4.1 提交后自检：接口返回成功不等于答案被接收，回查服务端回显
            _work_id = str(questions.get("workId", "") or _job["jobid"].replace("work-", ""))
            _work_answer_id = str(questions.get("workAnswerId", "") or "")
            if self._verify_submit_accepted(
                    _session, _course, _job, _job_info, questions,
                    _work_id, _work_answer_id) is False:
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
                                questions, work_id, work_answer_id) -> Optional[bool]:
        """提交后自检：平台这次到底收没收到答案。

        之前只看接口返回的 status，接口成功 ≠ 答案被接收（听力题提交带题号的
        原始文本时接口照样返回成功，但成绩是 0）。这里回查最新一次作答详情，
        用服务端回显的"我的答案"来确认。

        Returns:
            True  - 服务端回显的作答内容非空（答案已生效）
            False - 服务端回显为空（答案未被接收）
            None  - 查不到详情，无法判断（按旧行为放行，避免误判）
        """
        needs_detail = any(
            q.get(f'answerSource{q["id"]}') == "cover" for q in questions.get("questions", [])
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
            str(q["id"]) for q in questions.get("questions", [])
            if q.get(f'answerSource{q["id"]}') == "cover"
            and str(q["answerField"].get(f'answer{q["id"]}', "")).strip()
        }
        echoed = {str(d["id"]) for d in detail if str(d.get("my_answer") or "").strip()}
        missing = expected - echoed
        if missing:
            logger.error(
                f"提交后自检未通过：服务端回显为空，答案疑似未被接收（题目ID {sorted(missing)}）。"
                f"本题组按失败处理，请人工确认后再重做"
            )
            return False
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
                if g.get("mark") != "dui" and _my:
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
                feedback.append(
                    f"- 题目：{title}\n"
                    f"  你的上次答案：{g.get('my_answer') or '(空)'}"
                    f"（{'部分正确' if g.get('mark') == 'bandui' else '判定错误'}）\n"
                    f"  平台已判该答案不通过，请重新分析并给出不同答案"
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
                        for q in detail:
                            my_ans = (q.get("my_answer") or "").strip()
                            correct_ans = (q.get("correct_answer") or "").strip()
                            if my_ans != correct_ans:
                                all_correct = False
                                feedback.append(
                                    f"- 题目：{q.get('title', '')}\n"
                                    f"  题型：{q.get('type_label', '')}\n"
                                    f"  你的上次答案：{my_ans or '(空)'}\n"
                                    f"  正确答案：{correct_ans or '(空)'}"
                                )
                        m = re.search(r'本次成绩<i>([\d.]+)</i>分', html)
                        score = float(m.group(1)) if m else 0.0
                        return {
                            "all_correct": all_correct,
                            "feedback": feedback,
                            "score": score,
                            "times": 0,
                            "redoable": False,
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
            if my_ans != correct_ans:
                all_correct = False
                feedback.append(
                    f"- 题目：{q.get('title', '')}\n"
                    f"  题型：{q.get('type_label', '')}\n"
                    f"  你的上次答案：{my_ans or '(空)'}\n"
                    f"  正确答案：{correct_ans or '(空)'}"
                )

        # 比对不到任何有效答案（platform 未回填详情）时，不能认作全对。
        # 典型是听力题：字幕/答案不返回，0 分却会被这里误判成"通过"。
        if compared == 0:
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
            logger.error(f"阅读任务学习失败 -> [{_resp.status_code}]{_resp.text}")
            return StudyResult.ERROR
        else:
            _resp_json = _resp.json()
            logger.info(f"阅读任务学习 -> {_resp_json['msg']}")
            return StudyResult.SUCCESS

    # ======================================================================
    # 作业模块（2026-09-13 新增，独立于章节测验通道）
    # ======================================================================
    def list_homeworks(self, course: dict) -> list[dict]:
        """扫描某门课程的作业列表（只读，不产生任何作答行为）。"""
        _session = SessionManager.get_session()
        self.rate_limiter.limit_rate()
        try:
            works = scan_works(_session, course)
        except (PauseInterrupt, RiskControlError):
            raise
        except Exception as e:
            logger.warning(f"作业列表扫描异常（{course.get('title')}）：{e}")
            return []
        # 已完成 / 已批阅的不再处理
        todo = []
        for w in works:
            st = (w.get("status") or "")
            if st in ("已完成", "已批阅", "待批阅"):
                logger.info(f"作业《{w.get('name')}》状态为「{st}」，跳过")
                continue
            todo.append(w)
        logger.info(f"课程《{course.get('title')}》：{len(works)} 份作业，"
                    f"其中 {len(todo)} 份待完成")
        return todo

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
        self.rate_limiter.limit_rate()
        work_name = work.get("name") or work.get("workid")

        # 1. 抓题
        try:
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
        for q in questions:
            qid = q["id"]
            # 常规题型走题库；判断题/填空/简答的答案形态校验已在 answer_check 覆盖
            try:
                ans = self.tiku.query(dict(q))
            except (PauseInterrupt, RiskControlError):
                raise
            except Exception as e:
                logger.warning(f"题目 {qid} 查询失败：{e}")
                ans = None
            if ans is None or not str(ans).strip():
                logger.warning(f"题目 {qid} 未能获取答案 -> {q['title'][:50]}")
                ans = random_answer(q.get("options", ""), q.get("type"))
            answers[qid] = ans
            if str(ans).strip():
                usable += 1

        cover_rate = usable / total if total else 0
        logger.info(f"作业《{work_name}》答案覆盖率 {cover_rate * 100:.0f}%")

        # 覆盖率过低时不盲交 —— 像听力题那样整份硬交会直接 0 分。
        # 处理策略对齐章节测验：先"保存"草稿（tempsave 不消耗作答次数），
        # 把已解出的答案留住，再暂停汇报，让用户看到哪些题没答上、可手动补。
        cover_threshold = getattr(self.tiku, "COVER_RATE", 0.8)
        if (not self.tiku.SUBMIT) and cover_rate < cover_threshold:
            msg = (f"作业《{work_name}》答案覆盖率仅 {cover_rate * 100:.0f}%"
                   f"（阈值 {cover_threshold * 100:.0f}%），已保存草稿、未提交")
            logger.warning(msg)
            try:
                save_res = submit_work(_session, course, work, parsed, questions,
                                       answers, save_only=True)
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

        # 3. 提交
        try:
            res = submit_work(_session, course, work, parsed, questions, answers,
                              save_only=False)
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
        return StudyResult.SUCCESS

    def process_homeworks(self, course: dict, stop_flag=None) -> dict:
        """做完一门课程的全部作业。

        Returns:
            {"done": n, "skipped": n, "need_attention": [原因...]}
        """
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
