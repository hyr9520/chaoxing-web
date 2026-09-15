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
        章节信息字典，包含是否锁定状态和章节点列表
    """
    logger.trace("开始解码章节列表...")
    soup = BeautifulSoup(html_text, "lxml")
    course_point = {
        "hasLocked": False,  # 用于判断该课程任务是否是需要解锁
        "points": [],
    }

    for chapter_unit in soup.find_all("div", class_="chapter_unit"):
        points = _extract_points_from_chapter(chapter_unit)
        # 检查是否有锁定内容
        for point in points:
            if point.get("need_unlock", False):
                course_point["hasLocked"] = True

        course_point["points"].extend(points)

    return course_point


def _extract_points_from_chapter(chapter_unit) -> List[Dict[str, Any]]:
    """
    从章节单元中提取章节点信息
    
    Args:
        chapter_unit: BeautifulSoup对象，表示一个章节单元
        
    Returns:
        章节点信息列表
    """
    point_list = []
    raw_points = chapter_unit.find_all("li")

    for raw_point in raw_points:
        point = raw_point.div
        if "id" not in point.attrs:
            continue

        point_id = re.findall(r"^cur(\d{1,20})$", point.attrs["id"])[0]
        point_title = point.select_one("a.clicktitle").text.replace("\n", "").strip()

        # 提取任务数量
        job_count = 1  # 默认为1
        need_unlock = False
        if point.select_one("input.knowledgeJobCount"):
            job_count = point.select_one("input.knowledgeJobCount").attrs["value"]
        elif point.select_one("span.bntHoverTips") and "解锁" in point.select_one("span.bntHoverTips").text:
            need_unlock = True

        # 判断是否已完成
        is_finished = False
        if point.select_one("span.bntHoverTips") and "已完成" in point.select_one("span.bntHoverTips").text:
            is_finished = True

        point_detail = {
            "id": point_id,
            "title": point_title,
            "jobCount": job_count,
            "has_finished": is_finished,
            "need_unlock": need_unlock
        }
        point_list.append(point_detail)

    return point_list


def decode_course_card(html_text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    解析任务点列表页面，提取任务点信息
    
    Args:
        html_text: 任务点列表页面的HTML内容
        
    Returns:
        任务点列表和任务信息的元组
    """
    logger.trace("开始解码任务点列表...")

    # 检查章节是否未开放
    if "章节未开放" in html_text:
        return [], {"notOpen": True}

    # 提取mArg参数
    temp = re.findall(r"mArg=\{(.*?)\};", html_text.replace(" ", ""))
    if not temp:
        return [], {}

    # 解析JSON数据
    cards_data = json.loads("{" + temp[0] + "}")

    if not cards_data:
        return [], {}

    # 提取任务信息
    job_info = _extract_job_info(cards_data)

    # 处理所有附件任务
    cards = cards_data.get("attachments", [])
    job_list = _process_attachment_cards(cards)

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


def _process_attachment_cards(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    处理所有附件任务卡片，强化直播任务识别逻辑
    
    Args:
        cards: 附件任务卡片列表
        
    Returns:
        处理后的任务列表
    """
    job_list = []

    for index, card in enumerate(cards):
        # 跳过已通过的任务
        if card.get("isPassed", False):
            continue

        # 处理无job字段的特殊任务
        if card.get("job") is None:
            # 尝试识别阅读任务
            read_job = _process_read_task(card)
            if read_job:
                job_list.append(read_job)
            continue

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
            "jobid": card.get("jobid", str(card.get("id", ""))),  # 兼容不同格式的任务ID
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
    """处理阅读类型任务"""
    if not (card.get("type") == "read" and not card.get("property", {}).get("read", False)):
        return None

    return {
        "title": card.get("property", {}).get("title", ""),
        "type": "read",
        "id": card.get("property", {}).get("id", ""),
        "jobid": card.get("jobid", ""),
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
            "jobid": card.get("jobid", ""),
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
        "jobid": card.get("jobid", ""),
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
        "jobid": card.get("jobid", ""),
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
