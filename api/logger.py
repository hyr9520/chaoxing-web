import sys

from loguru import logger
from tqdm import tqdm

tqdm_stream = sys.stderr

# 日志缓冲区，用于在手动答题时缓存后台日志，答题结束后统一输出
log_buffer = []
MAX_LOG_BUFFER_SIZE = 1000


def tqdm_sink(msg):
    manual_locked = False
    try:
        # 动态获取 api.answer 模块中的 TikuManual 锁，避免循环导入
        if 'api.answer' in sys.modules:
            TikuManual = getattr(sys.modules['api.answer'], 'TikuManual', None)
            if TikuManual and getattr(TikuManual, '_manual_lock', None):
                manual_locked = TikuManual._manual_lock.locked()
    except (AttributeError, KeyError, ImportError):
        pass

    if manual_locked:
        if len(log_buffer) < MAX_LOG_BUFFER_SIZE:
            log_buffer.append(msg)
    else:
        if log_buffer:
            for buffered_msg in log_buffer:
                tqdm.write(buffered_msg.rstrip(), file=tqdm_stream)
            log_buffer.clear()
        tqdm.write(msg.rstrip(), file=tqdm_stream)
    tqdm_stream.flush()


logger.remove()
logger.add(tqdm_sink, colorize=True, enqueue=True)


def _log_file() -> str:
    """日志文件路径：按实例数据目录隔离。

    2026-09-15 修 bug：此前所有实例都写同一个 chaoxing.log，而 loguru 的
    rotation 靠 os.rename 轮转 —— Windows 下多个进程同时持有该文件句柄时
    rename 必然失败，报 PermissionError: [WinError 32]，日志轮转永久失效
    （文件无限增长，实测已到 9.9MB 且不再切割）。

    改为每个实例写各自的日志：
      CK_DATA_DIR 已设置（多实例）-> 该目录下 chaoxing.log
      CK_DATA_DIR 未设置（单实例/CLI）-> 项目根 chaoxing.log（原行为，不变）
    """
    import os
    base = (os.environ.get("CK_DATA_DIR") or "").strip()
    if base:
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            pass
        return os.path.join(base, "chaoxing.log")
    # 未设 CK_DATA_DIR：保持原路径，向后兼容
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "chaoxing.log")


def _should_log_file() -> bool:
    """是否写文件日志。

    2026-09-15 补强：上面的按实例隔离只覆盖了多实例场景。但并发跑多个
    **不设 CK_DATA_DIR** 的 CLI 进程时（例如并行执行 6 个回归测试脚本），
    它们仍会争抢同一个项目根 chaoxing.log，Windows 下轮转照样报
    PermissionError: [WinError 32]。

    这类进程（测试脚本、一次性工具）要的只是终端输出，文件落盘对它们没有
    价值，却是冲突的来源。用 CK_NO_LOG_FILE=1 显式关掉文件 sink 即可。
    """
    import os
    return (os.environ.get("CK_NO_LOG_FILE") or "").strip() not in ("1", "true", "yes")


if _should_log_file():
    # delay=True：日志文件延迟到首条记录才创建，且不长期hold句柄，
    # 降低与其它进程/轮转发生 Windows 句柄冲突的概率。
    logger.add(_log_file(), rotation="10 MB", level="TRACE", delay=True)
