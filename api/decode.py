# -*- coding: utf-8 -*-
"""
超星学习通数据解析模块

该模块负责解析超星学习通平台的课程、章节、任务点等各种数据，
并转换为程序内部使用的结构化数据格式。
"""
import json
import re
from typing import List, Dict, Tuple, Any, Optional

from bs4 import BeautifulSoup, NavigableString

from api.font_decoder import FontDecoder
from api.logger import logger


# ---------------------------------------------------------------------------
# 🔴 2026-10-07 移植上游 PR #630（ieduer, 2026-09-27）的正则常量
#    仅为「新增」用途，不改变任何既有解析分支的行为。
# ---------------------------------------------------------------------------

# 课程页顶部的任务点总进度，例如：
#   已完成任务点: <span style="color:#00B368">74</span>/74
# 纯文本形式「已完成任务点：3 / 10」也能匹配。
_JOB_PROGRESS_RE = re.compile(
    r"已完成任务点\s*[:：]\s*(?:<[^>]*>\s*)*(\d+)\s*(?:<[^>]*>\s*)*/\s*(\d+)")

# 任务点页面里 mArg 被真正填充（后面跟 `{`）—— 该 num 存在对应标签页。
_MARG_RE = re.compile(r"mArg\s*=\s*\{")

# num 超出标签页数量时，页面模板里的 mArg **未被填充**（`mArg = $mArg`）。
# 这是比「连续 N 次取不到卡」更精确的「到头」判据（上游 PR #630 方案）。
_NO_CARD_RE = re.compile(r"mArg\s*=\s*\$mArg\b")


def decode_course_list(html_text: str) -> List[Dict[str, str]]:
    """
    解析课程列表页面，提取课程信息
    
    Args:
        html_text: 课程列表页面的HTML内容
        
    Returns:
        课程信息列表，每个课程包含id、title、teacher等信息
    """
    logger.trace("开始解码课程列表...")
    soup = BeautifulSoup(html_text, "lxml")
    raw_courses = soup.select("div.course")
    course_list = []

    for course in raw_courses:
        # 跳过未开放课程
        if course.select_one("a.not-open-tip") or course.select_one("div.not-open-tip"):
            continue

        course_detail = {
            "id": course.attrs["id"],
            "info": course.attrs["info"],
            "roleid": course.attrs["roleid"],
            "clazzId": course.select_one("input.clazzId").attrs["value"],
            "courseId": course.select_one("input.courseId").attrs["value"],
            "cpi": re.findall(r"cpi=(.*?)&", course.select_one("a").attrs["href"])[0],
            "title": course.select_one("span.course-name").attrs["title"],
            "desc": course.select_one("p.margint10").attrs["title"] if course.select_one("p.margint10") else "",
            "teacher": course.select_one("p.color3").attrs["title"]
        }
        course_list.append(course_detail)

    return course_list


def decode_course_folder(html_text: str) -> List[Dict[str, str]]:
    """
    解析二级课程列表页面，提取文件夹信息
    
    Args:
        html_text: 二级课程列表页面的HTML内容
        
    Returns:
        课程文件夹信息列表
    """
    logger.trace("开始解码二级课程列表...")
    soup = BeautifulSoup(html_text, "lxml")
    raw_courses = soup.select("ul.file-list>li")
    course_folder_list = []

    for course in raw_courses:
        if not course.attrs.get("fileid"):
            continue

        course_folder_detail = {
            "id": course.attrs["fileid"],
            "rename": course.select_one("input.rename-input").attrs["value"]
        }
        course_folder_list.append(course_folder_detail)

    return course_folder_list


def decode_course_point(html_text: str) -> Dict[str, Any]:
    """
    解析章节列表页面，提取章节点信息

    Args:
        html_text: 章节列表页面的HTML内容

    Returns:
        章节信息字典，包含是否锁定状态、章节点列表，
        以及页面顶部「已完成任务点: 完成数/总数」（jobProgress，页面无此信息时为 None）

    🔴 2026-10-07 移植上游 PR #630（ieduer）：新增 `jobProgress`。
       这是**平台自己算的权威口径**，直接从课程页 HTML 正则提取，
       不需要 playwright 打开浏览器 —— 比 `has_finished` 逐节点计数可信，
       也比 playwright 方案快且稳。

       实测（acc10 生态文明建设）：`jobProgress = {done:74, total:74}`，
       与节点级黄点口径（0 黄点）完全吻合；而 API 口径曾误报过。
    """
    logger.trace("开始解码章节列表...")
    soup = BeautifulSoup(html_text, "lxml")
    course_point = {
        "hasLocked": False,  # 用于判断该课程任务是否是需要解锁
        "points": [],
        "jobProgress": None,  # 👈 新增：页面顶部「已完成任务点 X/Y」
    }

    # 👇 移植 PR #630：课程页顶部的任务点总进度（权威口径）
    m_prog = _JOB_PROGRESS_RE.search(html_text)
    if m_prog:
        try:
            _d, _t = int(m_prog.group(1)), int(m_prog.group(2))
            if 0 <= _d <= _t:
                course_point["jobProgress"] = {"done": _d, "total": _t}
        except (TypeError, ValueError):
            pass

    # 🔴 2026-10-03（第三处漏刷根因）：原实现只扫
    #   `div.chapter_unit` → `li` → `li.div`，
    #   但平台对**深层嵌套（第 7 级）**的节点会把 `<div class="chapter_item">`
    #   直接放进 `<ul>`（**不在任何 `<li>` 内**）⇒ 这些节点被**整个漏解析**，
    #   程序压根不知道它们存在 ⇒ 永远不刷 ⇒ 平台一直显示黄点。
    #
    #   实测（acc2 安全人机工程 266313880）：
    #     · 真实 HTML 里 `div.chapter_item` 共 **124** 个，老逻辑只解析出 **123**
    #     · 漏的正是「能力与安全生产的关系」(id=1258439571)，
    #       平台给它标的是「**2个待完成任务点**」
    #     · 该课程平台口径「已完成任务点 150/168」，差的 18 个 = 15 个未完成节点
    #       （12 个 yi=1 + 3 个 yi=2），其中就含这个被漏掉的节点
    #
    #   改为直接遍历所有 `div.chapter_item[id^=cur]`（保持文档顺序），不再依赖 li。
    seen = set()
    for div in soup.find_all("div", class_="chapter_item"):
        m = re.findall(r"^cur(\d{1,20})$", div.attrs.get("id") or "")
        if not m or m[0] in seen:
            continue
        seen.add(m[0])
        point = _parse_point_div(div)
        if point is None:
            continue
        if point.get("need_unlock", False):
            course_point["hasLocked"] = True
        course_point["points"].append(point)

    return course_point


def _parse_point_div(point) -> Optional[Dict[str, Any]]:
    """从单个 `div.chapter_item` 解析出节点信息。"""
    try:
        point_id = re.findall(r"^cur(\d{1,20})$", point.attrs["id"])[0]
    except (KeyError, IndexError, TypeError):
        return None

    title_el = point.select_one("a.clicktitle")
    point_title = title_el.text.replace("\n", "").strip() if title_el else ""

    # 提取任务数量
    job_count = 1  # 默认为1
    need_unlock = False
    jc_el = point.select_one("input.knowledgeJobCount")
    tips_el = point.select_one("span.bntHoverTips")
    if jc_el:
        job_count = jc_el.attrs["value"]
    elif tips_el and "解锁" in tips_el.text:
        need_unlock = True

    # 判断是否已完成
    is_finished = bool(tips_el and "已完成" in tips_el.text)

    return {
        "id": point_id,
        "title": point_title,
        "jobCount": job_count,
        "has_finished": is_finished,
        "need_unlock": need_unlock,
    }


def _extract_points_from_chapter(chapter_unit) -> List[Dict[str, Any]]:
    """
    从章节单元中提取章节点信息

    ⚠️ 2026-10-03：本函数**已不是主路径**（`decode_course_point` 改为直接遍历
    所有 `div.chapter_item`，因为平台会把深层节点裸放在 `<ul>` 里，不在 `li` 内）。
    这里保留只是兼容旧调用，逻辑统一委托给 `_parse_point_div`。
    """
    point_list = []
    for raw_point in chapter_unit.find_all("li"):
        point = raw_point.div
        if point is None or "id" not in point.attrs:
            continue
        detail = _parse_point_div(point)
        if detail:
            point_list.append(detail)
    return point_list


def decode_course_card(html_text: str,
                       keep_passed_task: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    解析任务点列表页面，提取任务点信息

    Args:
        html_text: 任务点列表页面的HTML内容
        keep_passed_task: 放行「isPassed=true 且 job=True」的真任务点（见 get_job_list）

    Returns:
        任务点列表和任务信息的元组；
        该 num **没有对应标签页**时，任务信息为 `{"noCard": True}`（移植上游 PR #630）
    """
    logger.trace("开始解码任务点列表...")

    # 检查章节是否未开放
    if "章节未开放" in html_text:
        return [], {"notOpen": True}

    # 提取mArg参数
    temp = re.findall(r"mArg=\{(.*?)\};", html_text.replace(" ", ""))
    if not temp:
        # 🔴 2026-10-07 移植上游 PR #630（ieduer）：
        #   请求的 num **超出该章节的标签页数量**时，页面模板里的 mArg 不会被填充
        #   （HTML 里留着 `mArg = $mArg`）。据此可**单次**判定"页签到头"，
        #   比"连续 N 次取不到原始卡"更精确、更早停。
        #   ⚠️ 纯新增字段：原有调用方不看 noCard 时行为完全不变。
        if _NO_CARD_RE.search(html_text):
            return [], {"noCard": True}
        return [], {}

    # 解析JSON数据
    cards_data = json.loads("{" + temp[0] + "}")

    if not cards_data:
        if _NO_CARD_RE.search(html_text):
            return [], {"noCard": True}
        return [], {}

    # 提取任务信息
    job_info = _extract_job_info(cards_data)

    # 处理所有附件任务
    cards = cards_data.get("attachments", [])
    job_list = _process_attachment_cards(cards, keep_passed_task=keep_passed_task)

    # 🔴 2026-10-03：暴露**原始卡片数**给调用方。`get_job_list` 要靠它判断
    #   "这一页是不是真的没有内容" —— 不能看 job_list：已完成的卡
    #   (isPassed=True) 会被 `_process_attachment_cards` 正常过滤掉，
    #   "过滤后为空" ≠ "页面无卡"，两者混用会导致分页提前退出（见 base.py 说明）。
    job_info["raw_cards"] = len(cards)

    return job_list, job_info


def _extract_job_info(cards_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    从卡片数据中提取任务基本信息
    
    Args:
        cards_data: 卡片数据字典
        
    Returns:
        任务基本信息字典
    """
    defaults = cards_data.get("defaults", {})
    if not defaults:
        return {}

    return {
        "ktoken": defaults.get("ktoken", ""),
        "mtEnc": defaults.get("mtEnc", ""),
        "reportTimeInterval": defaults.get("reportTimeInterval", 60),
        "defenc": defaults.get("defenc", ""),
        "cardid": defaults.get("cardid", ""),
        "cpi": defaults.get("cpi", ""),
        "qnenc": defaults.get("qnenc", ""),
        "knowledgeid": defaults.get("knowledgeid", "")
    }


def _job_id(card: Dict[str, Any]) -> str:
    """任务 ID：**新版卡片把它放在 `property._jobid`**，老版才在顶层 `jobid`。

    🔴 2026-10-04 实测（acc4 复核报告"存在任务点但无可执行任务"的 18 个节点）：
      · `video` 卡：顶层无 `jobid`，`property._jobid = 1643085307957198`
      · `read`  卡：顶层无 `jobid`，`property._jobid = read-218073146`
      · `vote`  卡：顶层无 `jobid`，`property._jobid = 1643103724147499`
    只取顶层会让这些任务的 jobid 变成空串 → 后续上报没有 ID 可用。
    """
    return str(card.get("jobid") or (card.get("property") or {}).get("_jobid") or "")


def _process_attachment_cards(cards: List[Dict[str, Any]],
                              keep_passed_task: bool = False) -> List[Dict[str, Any]]:
    """
    处理所有附件任务卡片，强化直播任务识别逻辑

    Args:
        cards: 附件卡片列表
        keep_passed_task: 为 True 时，**放行「isPassed=true 且 job=True」的真任务点**。
            见 `get_job_list` 的说明 —— 节点被平台标为未完成时，卡上的 isPassed 不可信。
    """
    job_list = []
    # 📌 2026-10-04 19:5x 试验结论（保留备查）：
    #   曾在此计算「本节点卡片是否全部 isPassed=true」，并对这类 job 打
    #   `forced_redo` 标记，让 `base.study_video` 跳过「瞬间完成」、改为
    #   重置进度真实重播 —— 目的是修 acc4 劳动教育那 11 个「平台标未完成、
    #   卡内却全部 isPassed=true」的黄点。
    #   **实测无效**：11 个节点全部真实重播且"任务完成"，平台 `has_finished`
    #   一个都没变。连同 isdrag=4×20 次、isdrag=3 满进度在內，
    #   共 3 种上报姿势全部无效 ⇒ 属**平台侧数据不一致**
    #   （页面汇总 41/41 已满，平台自己也不算它们缺）。
    #   ⇒ 已回退，不再对这类节点做特殊处理。

    for index, card in enumerate(cards):
        # 跳过已通过的任务
        if card.get("isPassed", False):
            # 🔴 2026-10-03（何同学："不要没完成全部就结束了"）：
            #   节点已被平台标为「未完成」时，卡上的 `isPassed=true` **不可信** ——
            #   实测 acc2 安全人机工程 15 个节点：节点内的卡全部 `isPassed=true`、
            #   播放进度 90~100%，但平台仍标「N个待完成任务点」（页面口径 151/168），
            #   程序据此整章跳过 ⇒ 这些节点**永远刷不掉**。
            #
            # 🔴 2026-10-04 补（同一坑的第二层）：原来的放行条件写成
            #   `keep_passed_task and card.get("job")` —— 要求卡上有 `job`。
            #   但实测 acc1「舞台人生 / 人文地理(上)」那 51 个未完成节点全是
            #     `type=video, job=None, isPassed=True, property._jobid=1556...`
            #   （顶层 job / jobid 都没有）⇒ 只按 `job` 判会把它们**继续挡在外面**，
            #   节点永远刷不掉。改为：**卡上有真实任务 ID 就放行**
            #   （`jobid` 或 `property._jobid` 任一存在）。
            _real_id = card.get("jobid") or (card.get("property") or {}).get("_jobid")
            if not (keep_passed_task and _real_id):
                continue

        # 处理无 job 字段的卡片
        # 🔴 2026-10-04 真机取证后**改回**"识别不出 read 就继续走 type 分支"：
        #   10-03 曾以"影响面为 0"为由回退过一次，但那次抽样的 60 个节点**只覆盖了
        #   `video/workid` 两类**，恰好漏掉了下面这些 —— 它们全是 `job=None`：
        #     · `大学生劳动教育 7.1.1 [大国工匠]顾秋亮`  type=video  job=None（正常视频！）
        #     · `大学生劳动教育 9.1 调查问卷`           type=vote   job=None
        #     · `舞台人生 9.1 阅读` / `植物导读 11.1 阅读` / `英语四级 5.1 拓展学习`
        #                                              type=read   job=None
        #   这些节点的 `jobCount=1`、平台标「未完成」，卡也确有内容 ——
        #   ⇒ **`job` 字段不是可靠的任务点判据**，可靠的是 `property._jobid`。
        #   继续丢弃它们的后果：节点永远刷不掉，且日志只剩"存在任务点但无可执行任务"。
        if card.get("job") is None:
            read_job = _process_read_task(card)
            if read_job:
                job_list.append(read_job)
                continue
            # 识别不出 read：**继续往下走 type 分支**，不再丢弃

        # 一开始就把超星api的屎山处理掉，不要用一个屎山行为掩盖另一个屎山 (指根据otherInfo中是否有courseId决定url拼接方式😂)
        # 清理otherInfo字段中的无效参数，这里优化了一下(保留了作者原来的注释TAT）
        if "otherInfo" in card:
            logger.trace("Fixing other info...")
            card["otherInfo"] = card["otherInfo"].split("&")[0]
            logger.trace(f"New info: {card['otherInfo']}")

        # 多维度判断是否为直播任务
        card_type = card.get("type", "").lower()
        property_data = card.get("property", {})
        prop_type = property_data.get("type", "").lower()
        resource_type = property_data.get("resourceType", "").lower()

        # 直播任务特征：包含liveId、streamName等字段，
        # 或类型标识包含live（因为live和video有点类似，怕超星又搞出什么幺蛾子就加了一些关键字识别）
        is_live = (
                "live" in card_type
                or "live" in prop_type
                or "live" in resource_type
                or "livestream" in card_type
                or property_data.get("liveId") is not None
                or property_data.get("streamName") is not None
                or property_data.get("vdoid") is not None
        )

        # 根据任务类型处理
        if is_live:
            live_job = _process_live_task(card)
            if live_job:
                job_list.append(live_job)
        elif card_type == "video":
            video_job = _process_video_task(card)
            if video_job:
                job_list.append(video_job)
        elif card_type == "document":
            doc_job = _process_document_task(card)
            if doc_job:
                job_list.append(doc_job)
        elif card_type == "workid":
            work_job = _process_work_task(card)
            if work_job:
                job_list.append(work_job)
        elif card_type == "vote":
            # 🔴 2026-10-04：调查问卷（`type=vote` / `module=insertvote`）。
            #   实测它**确实是任务点**（有 `property._jobid=1643103724147499`，
            #   节点 `jobCount=1`），但项目**没有提交问卷的实现**。
            #   这里只识别并如实记日志 —— **不假装完成**（与"没做完不许报成功"一致）。
            logger.info("识别到调查问卷任务 jobid=%s title=%s"
                        " —— 暂无自动作答实现，跳过（节点会保持未完成）"
                        % (_job_id(card), (card.get("property") or {}).get("title", "")))
        else:
            logger.warning(f"Unknown card type: {card_type}")
            logger.warning(card)

    return job_list


def _process_live_task(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理直播类型任务，提取所有必要参数"""
    try:
        property_data = card.get("property", {})
        return {
            "type": "live",
            "jobid": _job_id(card) or str(card.get("id", "")),  # 兼容不同格式的任务ID
            "name": property_data.get("title", property_data.get("name", "未知直播")),
            "otherinfo": card.get("otherInfo", ""),
            "property": property_data,  # 保留完整属性用于后续处理
            "mid": card.get("mid", ""),
            "objectid": card.get("objectId", ""),
            "aid": card.get("aid", ""),
            # 补充直播特有标识
            "liveId": property_data.get("liveId"),
            "streamName": property_data.get("streamName")
        }
    except Exception as e:
        logger.error(f"解析直播任务失败: {str(e)}, 任务数据: {str(card)[:200]}")
        return None


def _process_read_task(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理阅读类型任务。

    🔴 2026-10-04：原条件要求 `not property.read` —— 但真机取证的阅读任务卡
    **恰恰是 `property.read = true`**（`module=insertreadV2`，`_jobid=read-218073146`），
    于是全被 `return None` 丢掉。现只按 `type == "read"` 判定。
    """
    if card.get("type") != "read":
        return None

    return {
        "title": card.get("property", {}).get("title", ""),
        "type": "read",
        "id": card.get("property", {}).get("id", ""),
        "jobid": _job_id(card),
        "jtoken": card.get("jtoken", ""),
        "mid": card.get("mid", ""),
        "otherinfo": card.get("otherInfo", ""),
        "enc": card.get("enc", ""),
        "aid": card.get("aid", "")
    }


def _process_video_task(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理视频类型任务"""
    try:
        return {
            "type": "video",
            "jobid": _job_id(card),
            "name": card.get("property", {}).get("name", ""),
            "otherinfo": card.get("otherInfo", ""),
            "mid": card["mid"],  # 必须字段，如果不存在会抛出异常
            "objectid": card.get("objectId", ""),
            "aid": card.get("aid", ""),
            "playTime": card.get("playTime", 0),
            "rt": card.get("property", {}).get("rt", ""),
            "attDuration": card.get("attDuration", ""),
            "attDurationEnc": card.get("attDurationEnc", ""),
            "videoFaceCaptureEnc": card.get("videoFaceCaptureEnc", ""),
        }
    except KeyError:
        logger.warning("出现转码失败视频，已跳过...")
        return None


def _process_document_task(card: Dict[str, Any]) -> Dict[str, Any]:
    """处理文档类型任务"""
    return {
        "type": "document",
        "jobid": _job_id(card),
        "otherinfo": card.get("otherInfo", ""),
        "jtoken": card.get("jtoken", ""),
        "mid": card.get("mid", ""),
        "enc": card.get("enc", ""),
        "aid": card.get("aid", ""),
        "objectid": card.get("property", {}).get("objectid", "")
    }


def _process_work_task(card: Dict[str, Any]) -> Dict[str, Any]:
    """处理作业类型任务"""
    return {
        "type": "workid",
        "jobid": _job_id(card),
        "otherinfo": card.get("otherInfo", ""),
        "mid": card.get("mid", ""),
        "enc": card.get("enc", ""),
        "aid": card.get("aid", "")
    }


def decode_questions_info(html_content: str) -> Dict[str, Any]:
    """
    解析题目信息，提取表单数据和问题列表
    
    Args:
        html_content: 题目页面HTML内容
        
    Returns:
        包含表单数据和问题列表的字典
    """
    soup = BeautifulSoup(html_content, "lxml")
    form_data = _extract_form_data(soup)

    # 检查是否存在字体加密
    has_font_encryption = bool(soup.find("style", id="cxSecretStyle"))
    font_decoder = None

    if has_font_encryption:
        font_decoder = FontDecoder(html_content)
    else:
        logger.warning("未找到字体文件，可能是未加密的题目不进行解密")

    # 处理所有问题
    questions = []
    for div_tag in soup.find("form").find_all("div", class_="singleQuesId"):
        question = _process_question(div_tag, font_decoder)
        if question:
            questions.append(question)

    # 更新表单数据
    form_data["questions"] = questions
    form_data["answerwqbid"] = ",".join([q["id"] for q in questions]) + ","

    return form_data


def _extract_form_data(soup: BeautifulSoup) -> Dict[str, Any]:
    """从BeautifulSoup对象中提取表单数据"""
    form_data = {}
    form_tag = soup.find("form")

    if not form_tag:
        return form_data

    # 提取所有非答案字段的input
    for input_tag in form_tag.find_all("input"):
        name_attr = input_tag.attrs.get("name")
        if name_attr is None:
            continue

        if isinstance(name_attr, list):
            name_str = str(name_attr[0]) if name_attr else ""
        else:
            name_str = str(name_attr)

        if not name_str or "answer" in name_str:
            continue

        val_attr = input_tag.attrs.get("value", "")
        if isinstance(val_attr, list):
            val_str = "".join(str(v) for v in val_attr)
        else:
            val_str = str(val_attr)

        form_data[name_str] = val_str

    return form_data


def _process_question(div_tag, font_decoder=None) -> Dict[str, Any]:
    """处理单个问题"""
    # 提取问题ID和题目类型
    question_id = div_tag.attrs.get("data", "")
    # TiMu 缺失保护（2026-09-13 加固）：此前直接 `div_tag.find(...).attrs`
    # 取值，页面结构稍有差异（如手机端作业页）就 AttributeError，
    # **整道题的解析直接崩掉**，表现为"抓不到题目"且原因难查。
    # 缺失时按未知题型走兜底，至少不会全盘失败。
    _timu = div_tag.find("div", class_="TiMu")
    q_type_code = (_timu.attrs.get("data", "") if _timu is not None else "")
    q_type = _get_question_type(q_type_code)

    # 提取题目内容和选项。
    # 听力题的一个 div 里含多组小问，每组各有一个 <ul>；只取第一个 ul 会丢掉
    # 后续小问的选项（实测 3 小问只解析出 4 个选项，导致 AI 无法按小问作答）。
    title_div = div_tag.find("div", class_="Zy_TItle")
    q_title = _extract_title(title_div, font_decoder)
    q_options = []
    ul_tags = div_tag.find_all("ul")
    for ul in ul_tags:
        for li in ul.find_all("li"):
            q_options.append(_extract_choices(li, font_decoder))
    # 听力题一个 div 含多个小问（每个小问各有一个 ul），必须保留页面原始顺序：
    # sort 会把各小问的 A/B/C/D 交错成 19A,20A,21A,19B,20B,21B...，
    # AI 看到的不再是"每小问一组选项"，无法按小问作答。
    # 只有一个 ul 的普通题目仍排序，维持原有行为。
    if len(ul_tags) <= 1:
        q_options.sort()
    q_options = '\n'.join(q_options)

    # 听力题组：每个小问有独立的隐藏提交字段 answer{qid}{小问GUID}——这是页面上
    # 真实存在的表单项；基础名 answer{qid} 在表单里不存在，只提交基础名会被
    # 平台静默丢弃（实测"我的答案"为空、0 分，2026-09-11）。
    # 按文档顺序记录这些真实字段名（= 小问顺序），填写阶段按序拆分连写答案。
    # sub_types = 每个小问的题型码（ul 的 qtype），提交时用于构建基础字段的
    # JSON 聚合（浏览器 setReadComprehensionAnswer() 的格式，抓包实证）。
    answer_field = {
        f"answer{question_id}": "",
        f"answertype{question_id}": q_type_code,
    }
    sub_types = []
    if len(ul_tags) > 1:
        for ul in ul_tags:
            try:
                sub_types.append(int(ul.get("qtype") or 0))
            except (TypeError, ValueError):
                sub_types.append(0)
        for inp in div_tag.find_all("input", attrs={"type": "hidden"}):
            name = (inp.get("id") or "").strip()
            if name.startswith(f"answer{question_id}") and name != f"answer{question_id}":
                answer_field[name] = ""

    # 填空题（2026-09-13 实测抓包校正）
    # ---------------------------------------------------------------
    # PC 版章节测验的真实结构（第一章测验实测 dump）：
    #   <ul class="Zy_ulTk">
    #     <div class="blankItemDiv">
    #       <span class="font14 tiankong fl">第1空：</span>
    #       <div class="XztiHover1 fl blankItemInp">
    #         <div class="InpDIV" id="inpDiv{qid}1"></div>
    #         <div class="textDIV" style="display:none">
    #           <textarea name="answerEditor{qid}1"></textarea>   ← 真实提交字段
    #     页面还自带 hidden 字段 tiankongsize{qid}=空数。
    #
    # ⚠ 此前按 CxKitty 手机端作业页的结构（ul.blankList2 > li）解析，实测
    #   PC 版**一个空都匹配不到**：blankCount 恒为 0、字段为空、提交时等于
    #   整题未作答。这个 bug 只有实地跑才暴露得出来。
    #
    # 两种结构都兼容：优先 PC 版（answerEditor 前缀），退回手机端（answer 前缀）。
    blank_count = 0
    if q_type == "completion":
        pc_items = div_tag.select("ul.Zy_ulTk > div.blankItemDiv")
        if pc_items:
            blank_count = len(pc_items)
            for i in range(blank_count):
                answer_field[f"answerEditor{question_id}{i + 1}"] = ""
        else:
            mobile_items = div_tag.select("ul.blankList2 > li")
            if mobile_items:
                blank_count = len(mobile_items)
                for i in range(blank_count):
                    answer_field[f"answer{question_id}{i + 1}"] = ""

    if q_type == "unknown":
        # 记录题干摘要：实地跑课时据此判断这是什么题型，再补映射
        logger.warning(
            f"未识别题型（代码 {q_type_code}）题干：{q_title[:100]!r}")

    # 题组标记（2026-09-13）：阅读理解(15) / 听力(19) 这类题，一个题块里含多个
    # 小问，每个小问有独立的隐藏提交字段（answer{qid}{GUID}）。填写阶段必须
    # 把连写答案按小问拆开逐个填 —— 只填基础字段会被平台当作"未作答"（0 分）。
    # 只在**已知题组类型码**上启用：不能仅凭"有多个 ul"判断，题干里出现普通
    # 列表（如"下列说法正确的是：1)... 2)..."）也会有多个 ul，会误判成题组。
    is_group = q_type_code in ("15", "19") and len(sub_types) > 1

    return {
        "id": question_id,
        "title": q_title,
        "options": q_options,
        "type": q_type,
        "sub_types": sub_types,
        "answerField": answer_field,
        "blankCount": blank_count,
        "is_group": is_group,
    }


def _get_question_type(type_code: str) -> str:
    """根据题型代码返回题型名称。

    代码表来源：CxKitty 的 QuestionType 枚举（权威，见 refs/cxkitty_schema.py），
    与超星官方"系统支持 18 种题型"的说法一致。

    返回值只有 5 类 + unknown，按**答案形态**归类（下游只关心"答案长什么样、
    怎么提交"，不关心题型叫什么）：
        single      选项字母（含连写）
        multiple    多选字母
        judgement   对错
        completion  填空（含多空）
        shortanswer 自由文字
        unknown     形态不确定（走兜底：不拦答案 + 通用提示词）

    注意：15 阅读理解 / 19 听力 是**题组**（一个题块含多个小问），
    其小问答案需按序连写并拆分到各自的隐藏字段 —— 见 is_group 标记。
    """
    type_map = {
        # ---- 选项字母类（单一答案形态，check_answer 严格校验安全）----
        "0": "single",        # 单选题
        "1": "multiple",      # 多选题
        # 19 听力：题组，答案连写多小问。必须返回 single —— answer.py 的听力
        # 提示词条件是 q_info['type'] in ('single','multiple')，改成 unknown
        # 会让听力提示词失效（退回普通提示词 → 极易全部选同一个字母）。
        # 多小问的拆分另由 is_listening_question / is_group 负责。
        "19": "single",
        # ---- 判断题 ----
        "3": "judgement",     # 判断题
        # ---- 填空类（多空，DOM 与填空题同构）----
        "2": "completion",    # 填空题
        "14": "completion",   # 完型填空
        # ---- 文字作答类（自由文字，校验最宽松）----
        "4": "shortanswer",   # 简答题
        "5": "shortanswer",   # 名词解释
        "6": "shortanswer",   # 论述题
        "7": "shortanswer",   # 计算题
        "8": "shortanswer",   # 其它
        "9": "shortanswer",   # 分录题
        "10": "shortanswer",  # 资料题
        "18": "shortanswer",  # 口语题
        # ---- 保持 unknown ----
        # 15 阅读理解 / 20 共用选项：题组，一个题块含多个小问，答案可能分行。
        #   若映射成 single，check_answer 的 check_single 会把含换行的多小问
        #   答案整个丢掉（比原来 unknown 更差）。多小问拆分由 is_group 标记负责，
        #   与 type 名无关，故 type 保持 unknown（校验最宽松，不会误杀）。
        # 11 连线 / 13 排序 / 21 测评：答案形态不确定（可能是 "A-B,C-D" 配对、
        #   顺序标记等），既非纯字母也非纯文字，一律 unknown 兜底放行。
    }

    if type_code in type_map:
        return type_map[type_code]

    # 仍未映射的代码：按 unknown 兜底（不拦答案、走通用提示词）。
    # 这里记 warning 是为了**收集证据** —— 实地跑课时把这些代码捞出来再补。
    logger.warning(
        f"未知题型代码 -> {type_code}（按 unknown 兜底；"
        f"如需精确支持请把该代码补进 _get_question_type 的 type_map）")
    return "unknown"


def _extract_title(element, font_decoder=None) -> str:
    """提取标题内容，支持解码加密字体"""
    if not element:
        return ""

    # 收集元素中的所有文本和图片
    content = []
    for item in element.descendants:
        if isinstance(item, NavigableString):
            content.append(item.string or "")
        elif item.name == "img":
            img_url = item.get("src", "")
            content.append(f'<img src="{img_url}">')

    raw_content = "".join(content)
    cleaned_content = raw_content.replace("\r", "").replace("\t", "").replace("\n", "")

    # 如果有字体解码器，进行解码
    if font_decoder:
        return font_decoder.decode(cleaned_content)

    return cleaned_content


def _extract_choices(element, font_decoder=None) -> str:
    """提取选项内容，支持解码加密字体"""
    if not element:
        return ""

    # 提取aria-label属性值作为选项，解决#474
    choice = element.get("aria-label") or element.get_text()
    if not choice:
        return ""

    cleaned_content = re.sub(r"[\r\t\n]", "", choice)

    if font_decoder:
        cleaned_content = font_decoder.decode(cleaned_content)

    cleaned_content = cleaned_content.strip()
    if cleaned_content.endswith("选择"):
        cleaned_content = cleaned_content[:-2].rstrip()

    return cleaned_content
