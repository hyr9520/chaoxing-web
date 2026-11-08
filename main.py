# -*- coding: utf-8 -*-
import argparse
import configparser
import enum
import os
import random
import socket
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any
from tqdm import tqdm
import requests
from api.answer import Tiku
from api.base import Chaoxing, Account, StudyResult
from api.exceptions import LoginError, InputFormatError, PauseInterrupt, RiskControlError
from api.logger import logger
from api.notification import Notification
from api.live import Live
from api.live_process import LiveProcessor
from api.process import increase_learning_count_for_course

try:
    from queue import PriorityQueue, ShutDown
except ImportError:
    from queue import PriorityQueue


    class ShutDown(Exception):
        pass


class ChapterResult(enum.Enum):
    SUCCESS = 0,
    ERROR = 1,
    NOT_OPEN = 2,
    PENDING = 3


def wait_network(timeout: float = 300) -> bool:
    """轮询等待网络可用：TCP 能连上学习通即视为就绪。

    典型场景：开机后服务自动恢复时 Wi-Fi 还没连上、睡眠唤醒后旧连接全废。
    此时每个请求都会秒抛 ConnectionError，若按普通失败计次，几秒内就会
    把全部重试机会烧光、把任务误标为失败（2026-09-11 早晨的真实事故）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("mooc1.chaoxing.com", 443), timeout=4)
            s.close()
            return True
        except OSError:
            time.sleep(5)
    return False


def log_error(func):
    def wrapper(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except PauseInterrupt:
            # 暂停属正常控制流，不是错误：直接放行，别打堆栈刷屏
            raise
        except BaseException as e:
            logger.error(f"Error in thread {threading.current_thread().name}: {e}")
            traceback.print_exception(type(e), e, e.__traceback__)
            raise

    return wrapper


def str_to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Samueli924/chaoxing",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("--use-cookies", action="store_true", help="使用cookies登录")

    parser.add_argument(
        "-c", "--config", type=str, default=None, help="使用配置文件运行程序"
    )
    parser.add_argument("-u", "--username", type=str, default=None, help="手机号账号")
    parser.add_argument("-p", "--password", type=str, default=None, help="登录密码")
    parser.add_argument(
        "-l", "--list", type=str, default=None, help="要学习的课程ID列表, 以 , 分隔"
    )
    parser.add_argument(
        "-s", "--speed", type=float, default=1.0, help="视频播放倍速 (默认1, 最大2)"
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=4, help="同时进行的章节数 (默认4, 如果一个章节有多个任务点，不会限制同时处理任务点的数量)"
    )

    parser.add_argument(
        "-v",
        "--verbose",
        "--debug",
        action="store_true",
        help="启用调试模式, 输出DEBUG级别日志",
    )
    parser.add_argument(
        "-a", "--notopen-action", type=str, default="retry",
        choices=["retry", "ask", "continue"],
        help="遇到关闭任务点时的行为: retry-重试, ask-询问, continue-继续"
    )

    parser.add_argument("--auto-sign", action="store_true", help="自动签到")
    parser.add_argument(
        "--retry-interval", type=float, default=1.0, help="重试等待时间, 单位秒 (默认1.0)"
    )

    parser.add_argument(
        "-lc",
        "--add-learning-count",
        action="store_true",
        help="开启章节学习次数增加模式",
    )
    parser.add_argument(
        "-tc",
        "--target-count",
        type=int,
        default=100,
        help="章节学习次数目标总次数 (默认100)",
    )

    # 在解析之前捕获 -h 的行为
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
        parser.print_help()
        sys.exit(0)

    return parser.parse_args()


def load_config_from_file(config_path):
    """从配置文件加载设置"""
    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf8")

    common_config: dict[str, Any] = {}
    tiku_config: dict[str, Any] = {}
    notification_config: dict[str, Any] = {}

    # 检查并读取common节
    if config.has_section("common"):
        common_config = dict(config.items("common"))
        # 处理course_list，将字符串转换为列表
        if "course_list" in common_config and common_config["course_list"]:
            common_config["course_list"] = [item.strip() for item in common_config["course_list"].split(",") if
                                            item.strip()]
        # 处理speed，将字符串转换为浮点数
        if "speed" in common_config:
            common_config["speed"] = float(common_config["speed"])
        if "jobs" in common_config:
            common_config["jobs"] = int(common_config["jobs"])
        # 处理notopen_action，设置默认值为retry
        if "notopen_action" not in common_config:
            common_config["notopen_action"] = "retry"
        if "retry_interval" in common_config:
            common_config["retry_interval"] = float(common_config["retry_interval"])
        else:
            common_config["retry_interval"] = 1.0
        if "work_max_retries" in common_config:
            try:
                common_config["work_max_retries"] = int(common_config["work_max_retries"])
            except ValueError:
                common_config["work_max_retries"] = 3
        else:
            common_config["work_max_retries"] = 3
        if "use_cookies" in common_config:
            common_config["use_cookies"] = str_to_bool(common_config["use_cookies"])
        if "add_learning_count" in common_config:
            common_config["add_learning_count"] = str_to_bool(common_config["add_learning_count"])
        if "target_count" in common_config:
            common_config["target_count"] = int(common_config["target_count"])
        if "username" in common_config and common_config["username"] is not None:
            common_config["username"] = common_config["username"].strip()
        if "password" in common_config and common_config["password"] is not None:
            common_config["password"] = common_config["password"].strip()

    # 检查并读取tiku节
    if config.has_section("tiku"):
        tiku_config = dict(config.items("tiku"))
        # 处理数值类型转换
        for key in ["delay", "cover_rate"]:
            if key in tiku_config:
                tiku_config[key] = float(tiku_config[key])

    # 检查并读取notification节
    if config.has_section("notification"):
        notification_config = dict(config.items("notification"))

    return common_config, tiku_config, notification_config


def build_config_from_args(args):
    """从命令行参数构建配置"""
    common_config = {
        "use_cookies": args.use_cookies,
        "username": args.username,
        "password": args.password,
        "course_list": [item.strip() for item in args.list.split(",") if item.strip()] if args.list else None,
        "speed": args.speed or 1.0,
        "jobs": args.jobs,
        "notopen_action": args.notopen_action or "retry",
        "retry_interval": args.retry_interval or 1.0,
        "add_learning_count": args.add_learning_count,
        "target_count": args.target_count,
    }
    return common_config, {}, {}


def init_config():
    """初始化配置"""
    args = parse_args()

    if args.config:
        common_config, tiku_config, notification_config = load_config_from_file(args.config)
    else:
        common_config, tiku_config, notification_config = build_config_from_args(args)
    return common_config, tiku_config, notification_config, args.config


def init_chaoxing(common_config, tiku_config, config_path=None):
    """初始化超星实例"""
    username = common_config.get("username", "")
    password = common_config.get("password", "")
    use_cookies = common_config.get("use_cookies", False)

    # 如果没有提供用户名密码，从命令行获取
    if (not username or not password) and not use_cookies:
        username = input("请输入你的手机号, 按回车确认\n手机号:")
        password = input("请输入你的密码, 按回车确认\n密码:")

    account = Account(username, password)

    # 设置题库
    tiku = Tiku.get_tiku_from_config(tiku_config, config_path=config_path)  # 载入题库
    tiku.init_tiku()  # 初始化题库

    # 获取查询延迟设置

    # 检查大模型连接（如果使用的是大模型题库）
    # 根据配置文件中的 provider 判断是否为大模型题库
    provider = tiku_config.get('provider', '')
    provider_list = [name.strip() for name in provider.split(',') if name.strip()]
    if any(name in ['AI', 'SiliconFlow'] for name in provider_list):
        check_connection = tiku_config.get('check_llm_connection', 'true').lower() == 'true'
        if check_connection:
            logger.info(f'正在验证大模型配置 (provider={provider})...')
            if not tiku.check_llm_connection():
                logger.error('大模型连接检查失败')
                choice = input('大模型连接检查失败，无法准确答题，是否继续运行？(Y/n): ').strip().lower()
                # 直接回车默认继续运行
                if choice not in ('', 'y', 'yes'):
                    raise RuntimeError('用户取消运行')
                logger.info('用户选择继续运行...')

    query_delay = tiku_config.get("delay", 0)

    # 章节检测答错后允许的最大重做次数（答错时反馈给AI并重新提交，直到全部正确）
    work_max_retries = common_config.get("work_max_retries", 3)

    # 实例化超星API
    chaoxing = Chaoxing(
        account=account,
        tiku=tiku,
        query_delay=query_delay,
        work_max_retries=work_max_retries,
    )

    return chaoxing


# ══════════════════════════════════════════════════════════════════════
# 🔴 2026-10-02 P2：任务之间加「拟人静置」
#
# 背景（当日外部调研结论，多方印证）：
#   平台对"行为节奏"建模，「视频播完立刻进下一节」「提交后立即跳转下一节」
#   是自动化脚本的典型特征。社区规避指南普遍给出的节奏建议是
#   「视频播完后等 10 秒再进入下一节」「点提交后静置 8 秒以上再跳转」，
#   并且强调**间隔必须随机** —— 固定间隔本身也是可识别特征
#   （TrueSight 分析报告：「过于均匀或过于规律的间隔会被识别为自动化脚本」）。
#
# 为什么不改 `speed` / `jobs` 就够了：那两个管的是"单次上报的时长"与"并发章节数"，
# 而节奏特征藏在**任务与任务之间的空隙**里 —— 我们的代码里原本是 0 空隙。
#
# 安全性设计（确保不影响原程序）：
#   1. 任何异常都吞掉 —— 绝不影响刷课主流程与返回值；
#   2. 开关：环境变量 `CX_HUMAN_PAUSE=0` 可一键关闭（默认开启）；
#   3. 上限 ≤8s，远低于看门狗的僵死阈值(900s)，不会误触发"任务僵死"；
#   4. 暂停信号最多延迟一个静置周期（≤8s）后照常抛 PauseInterrupt —— 与
#      原本"点暂停后当前请求结束即生效"的量级一致；
#   5. 已完成章节直接 return，不进入静置 —— 不浪费任何时间。
# ══════════════════════════════════════════════════════════════════════
_HUMAN_PAUSE_ON = os.environ.get("CX_HUMAN_PAUSE", "1").strip().lower() \
    not in ("0", "false", "no", "off")


def _human_pause(lo: float, hi: float, why: str = "") -> None:
    """章节/任务点之间的随机静置（拟人化）。失败时静默跳过。"""
    if not _HUMAN_PAUSE_ON:
        return
    try:
        delay = random.uniform(lo, hi)
    except Exception:                                     # noqa: BLE001
        delay = lo
    if not delay or delay <= 0:
        return
    try:
        logger.debug(f"拟人静置 {delay:.1f}s {why}")
        time.sleep(delay)
    except Exception as e:                                # noqa: BLE001
        logger.debug(f"拟人静置异常（忽略）: {e}")


# 章节之间的静置区间（秒）。单门课几十个章节，总增量约 2~4 分钟，
# 相对视频总时长可忽略。
HUMAN_PAUSE_CHAPTER = (2.0, 6.0)
# 同一章节内相邻任务点之间的静置区间（秒）
HUMAN_PAUSE_JOB = (3.0, 8.0)


def process_job(chaoxing: Chaoxing, course: dict, job: dict, job_info: dict, speed: float,
                only_kinds: set | None = None) -> StudyResult:
    """处理单个任务点。

    only_kinds (2026-09-15 新增)：内容类型白名单，取值 {"video", "quiz"} 的任意组合。
    只处理白名单里的类型，其余**直接跳过并按成功计**（不是失败 —— 用户没勾就是不想要，
    标成失败会让章节反复重试）。

    为什么需要它：章节测验（workid）此前只能跟着"刷课"一起做，
    没法"只做测验不刷视频"。用户明确要求拆成三个独立勾选框
    （视频 / 测验 / 作业），这里就是后端的执行点。

    None = 不过滤（旧行为，全做），保证既有调用方零影响。
    """
    # 任务点归类：video/audio → 视频；workid → 测验；document/read/live → 其它。
    #
    # 白名单模式下**只做被勾选的类型**，"其它"（文档/阅读/直播）一律跳过 ——
    # 语义是"用户勾了什么就只做什么"。历史上文档是跟着"刷课"顺带做的，
    # 但那是因为没有独立开关；现在拆开了就不该再顺手做用户没勾的内容。
    #
    # 具体归属：
    #   勾"视频" → 做 video/audio，顺带做 document/read（同属"看内容"一类，
    #              且它们不是独立评分项，跳过它们会导致章节被判定未完成、
    #              反而卡住后续测验，故保留）
    #   勾"测验" → 只做 workid
    kind = "video" if job["type"] in ("video", "audio", "document", "read") else (
        "quiz" if job["type"] == "workid" else None)
    if only_kinds is not None and kind is not None and kind not in only_kinds:
        logger.debug(f"按勾选跳过任务点（{kind} 未勾选）: {course['title']} 任务ID: {job['jobid']}")
        return StudyResult.SUCCESS
    # 既不属于"视频类"也不属于"测验类"（如 live 直播）：白名单模式下跳过
    if only_kinds is not None and kind is None:
        logger.debug(f"按勾选跳过任务点（非勾选类型 {job['type']}）: {course['title']} 任务ID: {job['jobid']}")
        return StudyResult.SUCCESS

    # 视频任务
    if job["type"] == "video":
        logger.trace(f"识别到视频任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        # 超星的接口没有返回当前任务是否为Audio音频任务
        video_result = chaoxing.study_video(
            course, job, job_info, _speed=speed, _type="Video"
        )
        if video_result == StudyResult.PENDING:
            # 播放已完成、等平台判定：不是失败，绝不能走音频回退（那会重播一遍）
            return video_result
        if video_result.is_failure():
            logger.warning("当前任务非视频任务, 正在尝试音频任务解码")
            video_result = chaoxing.study_video(
                course, job, job_info, _speed=speed, _type="Audio")
        if video_result.is_failure():
            logger.warning(
                f"出现异常任务 -> 任务章节: {course['title']} 任务ID: {job['jobid']}, 已跳过"
            )
        return video_result
    # 文档任务
    elif job["type"] == "document":
        logger.trace(f"识别到文档任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        return chaoxing.study_document(course, job)
    # 测验任务
    elif job["type"] == "workid":
        logger.trace(f"识别到章节检测任务, 任务章节: {course['title']}")
        try:
            return chaoxing.study_work(course, job, job_info)
        finally:
            # 🔴 2026-10-01：`work_feedback`（上一轮错题反馈）只应作用于**本次**章节检测。
            #    它是有效功能（把错题+错答案喂给 AI，提高重做正确率，多选题尤其明显），
            #    但**用完必须清**，否则会残留到后续任务 —— 尤其是紧接着的作业链路，
            #    那份旧反馈会被塞进无关题目的 AI 提示词里。
            #    `study_work` 内部有多个 return 点（全对 / 不可重做 / 重试耗尽 …），
            #    逐个加清理容易漏；在**唯一的生产调用点**收口最稳（`main.py` 这处，
            #    其余调用都在测试脚本里）。
            try:
                chaoxing.tiku.set_work_feedback(None)
            except Exception as e:                                 # noqa: BLE001
                logger.debug(f"清理章节检测错误反馈失败（忽略）：{e}")
    # 阅读任务
    elif job["type"] == "read":
        logger.trace(f"识别到阅读任务, 任务章节: {course['title']}")
        return chaoxing.study_read(course, job, job_info)
    # 直播任务
    elif job["type"] == "live":
        logger.trace(f"识别到直播任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        try:
            # 准备直播所需参数
            defaults = {
                "userid": chaoxing.get_uid(),
                "clazzId": course.get("clazzId"),
                "knowledgeid": job_info.get("knowledgeid")
            }

            # 创建直播对象
            live = Live(
                attachment=job,
                defaults=defaults,
                course_id=course.get("courseId")
            )

            # 启动直播处理线程
            thread = threading.Thread(
                target=LiveProcessor.run_live,
                args=(live, speed),
                daemon=True
            )
            thread.start()
            thread.join()  # 等待直播处理完成
            return StudyResult.SUCCESS
        except Exception as e:
            logger.error(f"处理直播任务时出错: {str(e)}")
            return StudyResult.ERROR

    logger.error(f"未知任务类型: {job['type']}")
    return StudyResult.ERROR


@dataclass
class ChapterTask:
    index: int
    point: dict[str, Any]
    course: dict[str, Any]
    result: ChapterResult = ChapterResult.PENDING
    tries: int = 0

    def __lt__(self, other):
        """比较两个任务的索引大小，用于优先级队列排序."""
        if not isinstance(other, ChapterTask):
            return NotImplemented
        return self.index < other.index


class JobProcessor:
    def __init__(self, chaoxing: Chaoxing, tasks: list[ChapterTask], config: dict[str, Any]):
        """初始化任务处理器."""
        if "jobs" not in config or not config["jobs"]:
            config["jobs"] = 4

        self.chaoxing = chaoxing
        self.speed = config["speed"]
        self.max_tries = 5
        self.tasks = tasks
        self.failed_tasks: list[ChapterTask] = []
        self.task_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.retry_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.wait_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        # 收敛判定用的独立计数器：unfinished_tasks 被 retry 往返刻意弄成非零平衡
        # （重投时不调 task_done 来抵消 put 的 +1），不能用来判断"是否还有活干"。
        # _pending = 尚未有最终结论（成功 / 放弃）的任务数
        self._lock = threading.Lock()
        self._pending = 0
        # 正在后台等平台判定的视频数（判定等待线程不占 worker，但 run() 要等它们收尾）
        self._verdict_pending = 0
        # 风控熔断标志：置位后所有 worker 停止领取新任务，run() 提前收尾
        self._tripped = False
        self.threads: list[threading.Thread] = []
        self.worker_num = config["jobs"]
        self.config = config
        self.retry_interval = config.get("retry_interval", 1.0)
        # 内容类型白名单（2026-09-15）：{"video","quiz"} 的任意子集，None = 全做。
        # 由前端"视频 / 测验 / 作业"三个勾选框决定，见 _do_start。
        only = config.get("only_kinds")
        self.only_kinds = set(only) if only else None

    def _finish_task(self):
        with self._lock:
            self._pending -= 1

    def run(self):
        for task in self.tasks:
            self.task_queue.put(task)
        with self._lock:
            self._pending = len(self.tasks)

        for i in range(self.worker_num):
            thread = threading.Thread(target=self.worker_thread, daemon=True)
            self.threads.append(thread)
            thread.start()

        self._retry_thread = threading.Thread(target=self.retry_thread, daemon=True)
        self._retry_thread.start()

        # 收敛判定：不用 task_queue.join()。join() 在 worker 全体异常死亡时永不返回
        # （任务计数被重试往返刻意保持非零），表现为"进度停住、网页还显示在跑"。
        # 改为：所有任务都有了最终结论就结束；worker 全死但仍有待办 -> 判定僵死退出，
        # 让上层感知失败，看门狗才能接管重启。
        dead_rounds = 0
        try:
            while True:
                with self._lock:
                    pending = self._pending
                    vpending = self._verdict_pending
                if self._tripped:
                    # 风控熔断：丢弃剩余任务，立刻收尾。
                    # 继续刷下一个章节会累加平台风控计数，风险远大于收益。
                    logger.error("任务因风控熔断中止，剩余任务点未处理（请等待冷却后重试）")
                    while not self.task_queue.empty():
                        try:
                            self.task_queue.get_nowait()
                            self.task_queue.task_done()
                        except Exception:
                            break
                    # ⚠️ retry_queue 也必须清（2026-09-24 修，风控失效的真凶之三）：
                    # 只清 task_queue 不够 —— retry_thread 会把 retry_queue 里的
                    # 任务搬回主队列，等于熔断后队列又被灌满，worker 继续领活撞 403。
                    self._retry_queue_drain()
                    break
                if pending <= 0 and vpending <= 0:
                    break
                if not any(t.is_alive() for t in self.threads):
                    dead_rounds += 1
                    if dead_rounds >= 3:   # 连续 3 秒无存活 worker，确认僵死
                        logger.error(
                            f"所有工作线程均已退出，仍有 {pending} 个任务未完成，判定为僵死，中止本轮任务")
                        break
                else:
                    dead_rounds = 0
                time.sleep(1)
        finally:
            # 用 finally：暂停（PauseInterrupt）会从这里直接穿出去，
            # 不清理的话 retry_thread 会永远阻塞在 retry_queue.get() 上，
            # 每暂停/继续一次就积一个僵尸线程。
            for q in (self.task_queue, self.retry_queue):
                if hasattr(q, "shutdown"):
                    try:
                        q.shutdown()
                    except Exception:
                        pass

    @log_error
    def worker_thread(self):
        while True:
            if self._tripped:   # 风控熔断后不再领取新任务
                return
            try:
                task = self.task_queue.get()
            except ShutDown:
                logger.info("Queue shut down")
                return

            # 领到任务后再确认一次熔断标志（2026-09-24 补）：
            # 从 get() 返回到这里存在时间窗口 —— 别的 worker 可能刚刚触发熔断，
            # 此时手上这个任务不该再执行（执行 = 又一轮 403）。
            if self._tripped:
                self.task_queue.task_done()
                self._finish_task()
                return

            # 任务点处理必须整体兜底：任何未被捕获的异常（含 requests 超时、
            # 解析 None 崩、题库异常）若直接抛出，会让本 worker 线程死亡。
            # 一旦所有 worker 都死，task_queue.join() 永远不返回，而主线程
            # 还活着 -> 看门狗看到"线程存活"不重启 -> 永久僵死。
            # 更糟的是该任务点没有 task_done()，队列计数永久挂着。
            try:
                task.result = process_chapter(self.chaoxing, task.course, task.point, self.speed,
                                              self.only_kinds)
            except PauseInterrupt:
                # 暂停信号：当前任务还没做完，放回队列后立刻让 worker 退出。
                # 必须先于 BaseException 捕获——否则会被当成"任务失败"，
                # 任务转入重试队列继续跑，表现为"点了暂停其实还在刷课"。
                # 用 return 而非 raise：线程正常收尾，不往 stderr 打未捕获异常堆栈。
                logger.info("收到暂停信号，工作线程退出")
                self.retry_queue.put(task)
                return
            except RiskControlError as e:
                # 风控熔断：这是"账号被平台盯上"的信号，绝不能当成当前这一个
                # 任务失败然后继续刷下一个（那正是把风控次数放大十几倍的路径）。
                # 置位标志让所有 worker 停止领活、run() 提前收尾。
                logger.error(f"风控熔断：{e}")
                logger.error("已停止本轮所有任务，避免继续触发平台风控。")
                self._tripped = True
                self.task_queue.task_done()
                self._finish_task()
                return
            except requests.exceptions.RequestException as e:
                # 网络异常（开机竞态/断网/睡眠唤醒）：先等网络恢复再计次。
                # 否则开机后几秒内 ConnectionError 连发，5 次重试全部烧光，
                # 任务被误标失败并放弃（2026-09-11 早晨 4.4/4.5 的真实事故）。
                # 等到网络恢复后，下一轮尝试即可成功，通常只消耗 1 次重试。
                logger.warning(
                    f"网络异常: {task.course.get('title', '')} - "
                    f"{task.point.get('title', '')} -> {type(e).__name__}，等待网络恢复(最多5分钟)...")
                if wait_network(300):
                    logger.info("网络已恢复，该任务将重新尝试")
                else:
                    logger.error("等待网络恢复超时(5分钟)，按失败计次")
                task.result = ChapterResult.ERROR
            except BaseException as e:
                logger.error(
                    f"处理章节任务点异常，按失败计次: {task.course.get('title', '')} - "
                    f"{task.point.get('title', '')} -> {type(e).__name__}: {e}")
                traceback.print_exception(type(e), e, e.__traceback__)
                task.result = ChapterResult.ERROR

            match task.result:
                case ChapterResult.SUCCESS:
                    logger.debug("Task success: {} - {}", task.course["title"], task.point["title"])
                    self.task_queue.task_done()
                    self._finish_task()
                    logger.debug(f"unfinished task: {self.task_queue.unfinished_tasks}")

                case ChapterResult.NOT_OPEN:
                    if self.config["notopen_action"] == "continue":
                        logger.warning("章节未开启: {} - {}, 正在跳过", task.course["title"], task.point["title"])
                        self.task_queue.task_done()
                        self._finish_task()
                        continue

                    task.tries += 1
                    if task.tries >= self.max_tries:
                        logger.error(
                            "章节未开启: {} - {} 可能由于上一章节的章节检测未完成, 也可能由于该章节因为时效已关闭，"
                            "请手动检查完成并提交再重试。或者在配置中配置(自动跳过关闭章节/开启题库并启用提交)"
                            , task.course["title"], task.point["title"])
                        self.task_queue.task_done()
                        self._finish_task()
                        continue

                    # 重投不调 task_done：retry_thread 重新 put 时计数会 +1，
                    # 不调 task_done 正好抵消，保持 unfinished_tasks 平衡。
                    self.retry_queue.put(task)

                case ChapterResult.PENDING:
                    # 视频播完但平台未判通过：不阻塞 worker，委托后台线程沿用
                    # 30 秒上报节奏等判定（判定与周期上报相关，不能纯被动查询）。
                    # worker 立即去处理后续章节 —— 等待与后续播放完全重叠。
                    # 记账：不 task_done、不 _finish_task，由后台线程结论时收尾
                    # （等价于把 worker 的结论分支原样搬进 _verdict_wait_conclude）。
                    ctx = getattr(self.chaoxing._verdict_ctx, "data", None)
                    if ctx is None:
                        logger.error("判定上下文缺失，按失败处理: {}", task.point.get("title"))
                        self.failed_tasks.append(task)
                        self.task_queue.task_done()
                        self._finish_task()
                        continue
                    with self._lock:
                        self._verdict_pending += 1
                    threading.Thread(
                        target=self._verdict_wait_conclude, args=(task, ctx),
                        daemon=True, name=f"verdict-{task.index}").start()

                case ChapterResult.ERROR:
                    task.tries += 1
                    logger.warning("重试任务 {} - {} ({}/{} 次尝试)", task.course["title"], task.point["title"],
                                   task.tries,
                                   self.max_tries)
                    if task.tries >= self.max_tries:
                        logger.error("任务重试次数达到上限: {} - {}", task.course["title"], task.point["title"])
                        self.failed_tasks.append(task)
                        self.task_queue.task_done()
                        self._finish_task()
                        continue
                    # 同上：重投不调 task_done，由 retry_thread 的 put 抵消
                    self.retry_queue.put(task)

                case _:
                    logger.error("任务 {} 的状态无效 {}", task.result, task.point["title"])
                    self.failed_tasks.append(task)
                    self.task_queue.task_done()
                    self._finish_task()

    def _verdict_wait_conclude(self, task: ChapterTask, ctx: dict):
        """后台判定等待线程：沿用 study_video 原等待循环（含周期上报），
        结论时代 worker 执行收尾记账（task_done / _finish_task / 重试投递）。"""
        title = task.point.get("title", "")
        try:
            result = self.chaoxing.wait_video_passed(
                ctx["session"], ctx["course"], ctx["job"], ctx["job_info"],
                ctx["dtoken"], ctx["duration"], ctx["type"], ctx["headers"])
        except RiskControlError as e:
            logger.error(f"判定等待期触发风控熔断：{e}")
            self._tripped = True
            result = StudyResult.FORBIDDEN
        except BaseException as e:
            logger.error("判定等待线程异常: {} -> {}: {}", title, type(e).__name__, e)
            result = StudyResult.ERROR

        try:
            if result == StudyResult.SUCCESS:
                # 日志格式必须与 RE_DONE 匹配（"任务完成: <任务名>"），进度统计靠它
                logger.info("任务完成: {}", ctx["job"].get("name", title))
                self.task_queue.task_done()
                self._finish_task()
            elif result == StudyResult.FORBIDDEN:
                # 403：交回主流程按普通失败重试（触发会话恢复/重播），不在此处收尾。
                #
                # ⚠️ 但熔断后绝不能塞回（2026-09-24 修，风控失效的真凶之二）：
                # 403 本身就是熔断信号，塞回 retry_queue 等于"熔断了还接着撞"。
                # 实测这条路径与 retry_thread 的不检查叠加，让熔断后仍多打 4 轮 403。
                if self._tripped:
                    logger.warning("已熔断，丢弃该任务不再重试: {}", title)
                    self.task_queue.task_done()
                    self._finish_task()
                else:
                    self.retry_queue.put(task)
            else:  # ERROR / 其他
                task.tries += 1
                if task.tries >= self.max_tries:
                    logger.error("任务重试次数达到上限(判定等待): {} - {}", task.course.get("title", ""), title)
                    self.failed_tasks.append(task)
                    self.task_queue.task_done()
                    self._finish_task()
                else:
                    logger.warning("判定等待未通过, 重试任务 {} - {} ({}/{} 次尝试)",
                                   task.course.get("title", ""), title, task.tries, self.max_tries)
                    self.retry_queue.put(task)
        except ShutDown:
            pass
        finally:
            with self._lock:
                self._verdict_pending -= 1

    @log_error
    def retry_thread(self):
        """重试搬运线程：把 retry_queue 里的任务搬回 task_queue。

        ⚠️ 必须检查 self._tripped（2026-09-24 修，风控失效的真凶之一）：
        原来这里无条件搬运，熔断置位后仍把 403 失败的任务搬回主队列，
        导致"已熔断"之后 worker 又从队列里领到活、继续去撞同一个 403。
        实测 acc2 熔断 #1（11:28:42）后，11:29:34 / 11:30:34 又刷了同一个视频两遍，
        2 分 20 秒内凑满熔断 #2 —— 熔断机制形同虚设，这本是可以避免的。
        """
        try:
            while True:
                task = self.retry_queue.get()
                if self._tripped:
                    # 已熔断：任务直接作废，不再搬回队列（也不再触发任何请求）
                    self._finish_task()
                    self._retry_queue_drain()
                    continue
                self.task_queue.put(task)
                # task_done is not called when a task failed and needs to be retried so if is reinserted into the queue,
                # the task num will increase by one and become more than the real task number
                self.task_queue.task_done()
                time.sleep(self.retry_interval)
        except ShutDown:
            pass

    def _retry_queue_drain(self):
        """清空重试队列（熔断时调用），防止残留任务在熔断后被搬运回主队列。"""
        while not self.retry_queue.empty():
            try:
                self.retry_queue.get_nowait()
                self._finish_task()
            except Exception:
                break


def process_chapter(chaoxing: Chaoxing, course: dict[str, Any], point: dict[str, Any], speed: float,
                    only_kinds: set | None = None) -> ChapterResult:
    """处理单个章节。only_kinds 见 process_job 的说明（None = 全做）。"""
    logger.info(f'当前章节: {point["title"]}')
    if point["has_finished"]:
        logger.info(f'章节：{point["title"]} 已完成所有任务点')
        return ChapterResult.SUCCESS

    # 随机等待，避免请求过快
    chaoxing.rate_limiter.limit_rate(random_time=True, random_min=0, random_max=0.2)

    # 🔴 2026-10-02 P2：进入未完成章节前先随机静置（拟人化，见 _human_pause 说明）。
    #    放在 `has_finished` 早退之后 —— 已完成章节直接跳过，不浪费时间。
    _human_pause(*HUMAN_PAUSE_CHAPTER, why="(章节间)")

    # 🔴🔴 2026-10-04：**章节访问上报（setlog）** —— 「黄点」的根治手段。
    #   背景（何同学发现"打开课程能看到好几个没刷的"）：
    #     节点被平台标未完成（目录黄点、`has_finished=false`），但节点内的卡
    #     却**全部 `isPassed=true`**。acc4 劳动教育有 11 个这样的节点。
    #   试过、全部无效的 3 种姿势：
    #     · `isdrag=4` 拖拽瞬间完成 ×20 次
    #     · `isdrag=3` 满进度上报
    #     · 重置进度**真实重播**（11 个节点全播完并"任务完成"）→ `has_finished` 一个不变
    #   **有效的那一个**：用浏览器打开该节点页面 → 节点立刻变绿
    #     （7.1.1、7.1.2 实测；再用 API 复现 7.1.3「视频」与 8.1「阅读」各一次即变绿）
    #   抓包定位到页面 JS 发的两个请求：
    #     ① GET `mooc-ans/mycourse/studentstudyAjax?...chapterId={节点id}`
    #        → 返回的 HTML 里含一段 `<script src=".../log/setlog?...">`
    #     ② GET `fystat-ans.chaoxing.com/log/setlog?personid=&courseId=&classId=&encode=&chapterId=`
    #        （`encode` 由服务端生成，**与章节无关**，同一课程内通用）
    #   ⇒ 「这类节点要的是**章节访问**，不是任务点上报」。纯 API 刷课从来不打开
    #     章节页，所以这些节点永远刷不掉 —— 这就是"界面/API 说做了、平台仍黄点"
    #     的真正成因。项目的 `_access_chapter_for_count` + `_extract_and_send_setlog`
    #     本来就实现了这两个请求，只是没接进刷课流程。
    #   ⚠️ 位置：`has_finished` 早退之后 —— 已完成章节不再浪费这 2 个请求。
    #   ⚠️ 失败不阻塞刷课（它只是"补标记"，主流程照走）。
    try:
        chaoxing.rate_limiter.limit_rate(random_time=True, random_min=0, random_max=0.2)
        _html = chaoxing._access_chapter_for_count(course, point)
        if _html:
            chaoxing._extract_and_send_setlog(_html)
            logger.info(f"章节访问上报(setlog)完成 -> {point['title']}")
        else:
            logger.warning(f"章节访问上报无响应 -> {point['title']}")
    except Exception as _e:                                          # noqa: BLE001
        logger.warning(f"章节访问上报失败（不阻塞刷课）: {_e}")

    # 获取当前章节的所有任务点
    job_info = None
    jobs, job_info = chaoxing.get_job_list(course, point)

    # 发现未开放章节, 根据配置处理
    if job_info.get("notOpen", False):
        return ChapterResult.NOT_OPEN

    # 🔴 2026-10-03（何同学："不要每次没读取到全部的、没完成全部的就结束了"）
    #   读不到任务点 ≠ 该章节没有任务点。
    #   `get_job_list` 在「页面上确实有卡、却解析不出任何可执行任务」时会打
    #   `blocked` 标记（并且**不再**上报"空页面完成"这个假成功）。
    #   此处必须判 ERROR 交重试 —— 绝不能默认 SUCCESS，否则界面会显示"已完成"，
    #   而平台侧的任务点一个都没达成（这就是"界面与平台不一致"的根源）。
    #   重试到 `max_tries`(5) 后进 failed_tasks，会被**如实**记为失败。
    if not jobs:
        if job_info.get("blocked"):
            logger.error(f"章节「{point['title']}」存在任务点但无可执行任务 —— 判定为未完成")
            return ChapterResult.ERROR
        return ChapterResult.SUCCESS

    job_results: list[StudyResult] = []
    for idx, job in enumerate(jobs):
        result = process_job(chaoxing, course, job, job_info, speed, only_kinds)
        job_results.append(result)
        # 🔴 2026-10-02 P2：相邻任务点之间也静置（最后一个之后不白等）
        if idx < len(jobs) - 1:
            _human_pause(*HUMAN_PAUSE_JOB, why="(任务点间)")

    # 有视频在等平台判定：章节未完结但也不算失败，交后台等待线程收尾
    if any(r == StudyResult.PENDING for r in job_results):
        return ChapterResult.PENDING

    for result in job_results:
        if result.is_failure():
            return ChapterResult.ERROR

    return ChapterResult.SUCCESS


def process_course(chaoxing: Chaoxing, course: dict[str, Any], config: dict):
    """处理单个课程"""
    logger.info(f"开始学习课程: {course['title']}")

    # 获取当前课程的所有章节
    point_list = chaoxing.get_course_point(
        course["courseId"], course["clazzId"], course["cpi"]
    )

    # 为了支持课程任务回滚, 采用下标方式遍历任务点

    _old_format_sizeof = tqdm.format_sizeof
    tqdm.format_sizeof = format_time

    tasks = []

    for i, point in enumerate(point_list["points"]):
        task = ChapterTask(point=point, index=i, course=course)
        tasks.append(task)
    p = JobProcessor(chaoxing, tasks, config)
    p.run()

    tqdm.format_sizeof = _old_format_sizeof


def filter_courses(all_course, course_list):
    """过滤要学习的课程"""
    if not course_list:
        # 手动输入要学习的课程ID列表
        print("*" * 10 + "课程列表" + "*" * 10)
        for course in all_course:
            print(f"ID: {course['courseId']} 班级ID: {course['clazzId']} 课程名: {course['title']}")
        print("*" * 28)
        print("提示: 同一 courseId 下若存在多个班级, 将分别完成。")
        try:
            course_list = input(
                "请输入想要学习的课程列表,以逗号分隔,例: 2151141,189191,198198\n"
            ).split(",")
        except Exception as e:
            raise InputFormatError("输入格式错误") from e

    # 筛选需要学习的课程
    course_task = []
    seen_keys = set()
    for course in all_course:
        key = (course["courseId"], course["clazzId"])
        if course["courseId"] in course_list and key not in seen_keys:
            course_task.append(course)
            seen_keys.add(key)
    
    # 如果没有指定课程，则学习所有课程
    if not course_task:
        course_task = all_course

    return course_task


def format_time(num, suffix='', divisor=''):
    total_time = round(num)
    sec = total_time % 60
    mins = (total_time % 3600) // 60
    hrs = total_time // 3600

    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{sec:02d}"

    return f"{mins:02d}:{sec:02d}"


def main():
    """主程序入口"""
    try:
        # 初始化配置
        common_config, tiku_config, notification_config, config_path = init_config()

        # 强制播放按照配置文件调节
        common_config["speed"] = min(2.0, max(1.0, common_config.get("speed", 1.0)))
        common_config["notopen_action"] = common_config.get("notopen_action", "retry")
        
        # 初始化增加章节学习次数配置
        add_learning_count = str_to_bool(common_config.get("add_learning_count", False))
        target_count = int(common_config.get("target_count", 100))
        
        # 初始化超星实例
        chaoxing = init_chaoxing(common_config, tiku_config, config_path=config_path)

        # 设置外部通知
        notification = Notification()
        notification.config_set(notification_config)
        notification = notification.get_notification_from_config()
        notification.init_notification()

        # 检查当前登录状态
        _login_state = chaoxing.login(login_with_cookies=common_config.get("use_cookies", False))
        if not _login_state["status"]:
            raise LoginError(_login_state["msg"])

        # 获取所有的课程列表
        all_course = chaoxing.get_course_list()

        # 过滤要学习的课程
        course_task = filter_courses(all_course, common_config.get("course_list"))

        # 开始学习
        logger.info(f"课程列表过滤完毕, 当前课程任务数量: {len(course_task)}")

        _old_format_sizeof = tqdm.format_sizeof
        tqdm.format_sizeof = format_time

        tasks = []
        for course in course_task:
            logger.info(f"正在读取课程章节: {course['title']}")
            point_list = chaoxing.get_course_point(
                course["courseId"], course["clazzId"], course["cpi"]
            )
            for i, point in enumerate(point_list["points"]):
                task = ChapterTask(point=point, index=i, course=course)
                tasks.append(task)

        # 全局并发执行所有课程的任务点
        p = JobProcessor(chaoxing, tasks, common_config)
        p.run()

        tqdm.format_sizeof = _old_format_sizeof

        logger.info("所有课程学习任务已完成")
        notification.send("chaoxing : 所有课程学习任务已完成")

        # 刷课完成后，如果开启了增加章节学习次数，则执行
        if add_learning_count:
            logger.info("刷课完成，开始增加章节学习次数...")
            common_config["target_count"] = target_count
            for course in course_task:
                increase_learning_count_for_course(chaoxing, course, common_config)
            logger.info("所有课程章节学习次数增加完成")
            notification.send("chaoxing : 所有课程章节学习次数增加完成")
        
    except SystemExit as e:
        if e.code != 0:
            logger.error(f"错误: 程序异常退出, 返回码: {e.code}")
        sys.exit(e.code)
    except KeyboardInterrupt as e:
        logger.error(f"错误: 程序被用户手动中断, {e}")
    except BaseException as e:
        logger.error(f"错误: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        try:
            notification.send(f"chaoxing : 出现错误 {type(e).__name__}: {e}\n{traceback.format_exc()}")
        except Exception:
            pass  # 如果通知发送失败，忽略异常
        raise e


if __name__ == "__main__":
    main()
