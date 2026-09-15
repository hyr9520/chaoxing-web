import configparser
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
from api.config import data_dir
from api.image_support import build_image_data_urls
from api.learned import LearnedAnswers, normalize_title
from api.logger import logger

# 关闭警告
disable_warnings(exceptions.InsecureRequestWarning)

__all__ = ["CacheDAO", "Tiku", "TikuFallback", "TikuYanxi", "TikuGo", "TikuLike", "TikuAdapter", "AI", "SiliconFlow",
           "TikuManual"]


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


def _pick_alternative(ans: str, wrong: list, q_info: dict) -> str:
    """给出一个"不同于所有历史错误答案"的候选（2026-09-11 硬保证）。

    背景：用户要求"听力题 AI 答错后，下次重新来必须返回另一个答案，答对后再进题库"。
    只靠 prompt 里"请排除上述答案"是软性的 —— 模型经常原样返回已判错的答案，
    导致同一道错题被反复提交同一个错误值（听力题只能做一次，本账号无从纠正）。

    策略：
      - 单字母（单选/判断）：从选项池里挑第一个不在 wrong 中的
      - 连写字母（听力多小问，如 "DCBD"）：只改动其中一位，
        生成第一个不在 wrong 中的组合（从首位开始尝试）
    都试不出来时返回空串（调用方据此放弃写入题库）。
    """
    text = str(ans or "").strip()
    wrong_set = {str(w).strip() for w in (wrong or [])}
    if not text or not re.fullmatch(r"[A-Za-z]+", text):
        return ""          # 非纯字母形态（填空题/文本答案）无法安全构造候选
    text = text.upper()
    pool = _option_pool(q_info)

    if len(text) == 1:
        for c in pool:
            if c not in wrong_set:
                return c
        return ""

    # 多小问连写：逐位尝试替换
    for i in range(len(text)):
        for c in pool:
            if c == text[i]:
                continue
            cand = text[:i] + c + text[i + 1:]
            if cand not in wrong_set:
                return cand
    return ""


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
                _verified = LearnedAnswers().get_verified(q_info['title'])
            except Exception as e:
                logger.warning(f"读取学习库失败（不影响作答）：{e}")
                _verified = ""
            # 2026-09-15 补：与 query_all() 对齐，学习库值也要过形态校验。
            # 此前 query() 直接返回 verified，若学习库里存的是脏值
            # （旧版程序写入的拒答文本、或题型变更导致形态不符），
            # 会被原样提交上去。query_all() 走的是"缓存层校验"，
            # 单题路径缺了这道闸。
            if _verified:
                _ok = plausible_answer(_verified, q_info.get('type'))
                if _ok or getattr(self, 'is_manual', False):
                    logger.info(f"从学习库获取答案（已验证）：{q_info['title']} -> {_verified}")
                    return _verified
                logger.warning(
                    f"学习库答案形态与题型不符，视为未命中重新查询："
                    f"{q_info['title']} -> {_verified}")

        # 先过缓存（若存在章节检测错误反馈，说明处于重做模式，跳过缓存让大模型参考反馈重新作答）
        if not getattr(self, 'work_feedback', None):
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
                    answer = self._query(q_info)
                else:
                    logger.info(f"从缓存中获取答案：{q_info['title']} -> {answer}")
                    return answer.strip()
            else:
                answer = self._query(q_info)
        else:
            answer = self._query(q_info)
            if answer:
                answer = answer.strip()
                logger.info(f"从{self.name}获取答案：{q_info['title']} -> {answer}")
                if check_answer(answer, q_info['type'], self):
                    # 未经验证的答案不入题库（同 query_all）；仅手动模式保留用户输入。
                    # 用 CacheDAO() 直接构造，避免依赖另一分支里定义的局部变量。
                    if getattr(self, 'is_manual', False):
                        CacheDAO().add_cache(q_info['title'], answer)
                    return answer
                else:
                    logger.info(f"从{self.name}获取到的答案类型与题目类型不符，已舍弃")
                    return None

            logger.error(f"从{self.name}获取答案失败：{q_info['title']}")
        return None

    def query_all(self, q_list: list[dict], query_delay: float = 0.0) -> list[Optional[str]]:
        if self.DISABLE:
            return [None] * len(q_list)

        results = [None] * len(q_list)
        pending_indices = []
        wrong_map = {}          # idx -> 该题历史被判错的答案列表（供强制换答案用）
        sub_map = {}            # idx -> 该题已确认正确的小问答案 {序号: 答案}

        cache_dao = CacheDAO()
        learner = LearnedAnswers()
        skip_cache = bool(getattr(self, 'work_feedback', None))
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
            verified = None if self._is_manual_mode else learner.get_verified(q['title'])
            if verified:
                logger.info(f"从学习库获取答案（已验证）：{q['title']} -> {verified}")
                results[idx] = verified
                continue

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

            if skip_cache:
                # 重做模式：不走缓存，让大模型参考错误反馈重新作答
                pending_indices.append(idx)
                continue

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
        for provider in self.providers:
            try:
                answer = provider._query(q_info)
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

            logger.info(f'{provider.name} 返回答案类型不符，回退到下一个题库')
        return None

    def _query_all(self, q_list: list[dict], query_delay: float = 0.0) -> list[Optional[str]]:
        results = [None] * len(q_list)
        pending_indices = list(range(len(q_list)))

        for provider in self.providers:
            if not pending_indices:
                break
            if provider.DISABLE:
                continue

            sub_q_list = [q_list[idx] for idx in pending_indices]
            try:
                sub_results = provider.query_all(sub_q_list, query_delay=query_delay)
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

        return results

    def set_work_feedback(self, feedback) -> None:
        """将章节检测错误反馈转发给支持反馈的子题库（如 AI 大模型）."""
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
            verify=False
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
            logger.error(f'{self.name}查询失败:\n{res.text}')
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
            logger.error(f'{self.name}查询失败: 状态码 {res.status_code}, 响应: {res.text}')
            return None

        try:
            res_json = res.json()
        except ValueError:
            logger.error(f'{self.name}查询失败: 返回内容不是有效JSON, 响应: {res.text}')
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
        self._timeout = 300
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
            logger.error(f'{self.name}查询失败: 状态码 {res.status_code}, 响应内容: \n{res.text}')

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
        raw_options = q_info['options'] or ''
        options = [sub(r'^[A-Za-z]\.?、?\s?', '', option)
                   for option in raw_options.split('\n') if option.strip()]
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


class AI(Tiku):
    # AI大模型答题实现
    # 支持读图的模型（2026-09-13 实测）：agnes-3.0-flash 可正确读图；
    # agnes-2.5-flash / 2.0-flash 返回空，故默认只信这一档。
    # 命中规则：模型名里含 "flash" 且含 "3.0"，或含 "vl"/"vision"/"gpt-4o"/
    # "claude-3"/"gemini" 等多模态关键字。可用 set_vision_enabled() 强制开关。
    _VISION_HINTS = ("3.0", "vl", "vision", "gpt-4o", "gpt-4.1", "claude-3",
                     "claude-4", "gemini", "qwen-vl", "glm-4v")

    def __init__(self, config_path: Optional[str] = None) -> None:
        """初始化AI大模型答题实现."""
        super().__init__(config_path)
        self.name = 'AI大模型答题'
        self.last_request_time = None
        self._lock = threading.Lock()
        self.work_feedback = None  # 章节检测错误反馈（重做时参考）
        self._session = None       # 超星登录会话（用于下载题目配图）
        self._vision_override = None  # None=自动判断, True/False=强制

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
        model = (self.model or "").lower()
        return any(h in model for h in self._VISION_HINTS)

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
        if self.last_request_time:
            interval_time = time.time() - self.last_request_time
            if interval_time < self.min_interval_seconds:
                sleep_time = self.min_interval_seconds - interval_time
                logger.debug(f"API请求间隔过短, 等待 {sleep_time} 秒")
                time.sleep(sleep_time)

    def _query_locked(self, q_info: dict):
        def remove_md_json_wrapper(md_str):
            # 使用正则表达式匹配Markdown代码块并提取内容
            pattern = r'^\s*```(?:json)?\s*(.*?)\s*```\s*$'
            match = re.search(pattern, md_str, re.DOTALL)
            return match.group(1).strip() if match else md_str.strip()

        if self.http_proxy:
            proxy = self.http_proxy
            httpx_client = httpx.Client(proxy=proxy)
            client = OpenAI(http_client=httpx_client, base_url=self.endpoint, api_key=self.key)
        else:
            client = OpenAI(base_url=self.endpoint, api_key=self.key)
        # 去除选项字母，防止大模型直接输出字母而非内容
        options_list = q_info['options'].split('\n')
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

        def _call(system_content: str, user_content: str):
            """发一次问答请求（统一入口）。

            带图失败时自动去掉图片重试一次（2026-09-13 加固）：
            模型名里带视觉关键字、但实际后端不支持图片时（例如 agnes 的
            图片能力未开通），带图请求会直接 400 —— 若不放行就会让整道题
            拿不到答案。去图重试保证"至少能按纯文本作答"，与用户要求的
            "自动降级"一致。
            """
            try:
                return client.chat.completions.create(**self._completion_kwargs(
                    model=self.model,
                    messages=_make_messages(system_content, user_content,
                                            images=_images)))
            except Exception as e:
                if not _images:
                    raise
                logger.warning(
                    f"带图请求失败（{type(e).__name__}: {e}），已自动降级为纯文本重试")
                return client.chat.completions.create(**self._completion_kwargs(
                    model=self.model,
                    messages=_make_messages(system_content, user_content)))

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

    def _query(self, q_info: dict):
        with self._lock:
            try:
                return self._query_locked(q_info)
            except Exception as e:
                # 模型级故障（模型下线/无权限/余额不足）自动降级重试一次
                # （2026-09-13，用户明确要求"到时候自动降级"）。
                # 只对"该模型不可用"类错误降级；网络抖动/限流不降级 ——
                # 换模型解决不了限流，反而会白丢一个可用的档位。
                if self._is_model_fatal(e) and self._switch_to_fallback():
                    logger.warning(f"已切换到备用模型重试本题：{e}")
                    return self._query_locked(q_info)
                raise

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

    def check_llm_connection(self) -> bool:
        """
        检查大模型连接是否可用
        发送一个简单的测试请求来验证 API 配置
        """
        with self._lock:
            logger.info(f'正在检查 {self.name} 连接...')
            try:
                # 初始化客户端
                if self.http_proxy:
                    httpx_client = httpx.Client(proxy=self.http_proxy)
                    client = OpenAI(http_client=httpx_client, base_url=self.endpoint, api_key=self.key)
                else:
                    client = OpenAI(base_url=self.endpoint, api_key=self.key)

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
            system_prompt = "本题为单选题，请根据题目和选项选择唯一正确答案，输出的是选项的具体内容，而不是内容前的ABCD，并以JSON格式输出：示例回答：{\"Answer\": [\"正确选项内容\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
        elif q_info['type'] == 'multiple':
            system_prompt = "本题为多选题，请选择所有正确选项，输出的是选项的具体内容，而不是内容前的ABCD，以JSON格式输出：示例回答：{\"Answer\": [\"选项1\",\"选项2\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
        elif q_info['type'] == 'completion':
            system_prompt = "本题为填空题，请直接给出填空内容，以JSON格式输出：示例回答：{\"Answer\": [\"答案文本\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
        elif q_info['type'] == 'judgement':
            system_prompt = "本题为判断题，请回答'正确'或'错误'，以JSON格式输出：示例回答：{\"Answer\": [\"正确\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"

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
                    "content": system_prompt
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
    'AI': AI,
    'SiliconFlow': SiliconFlow,
    'TikuManual': TikuManual,
}
