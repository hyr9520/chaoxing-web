import re

# 当前题目的"选项字母集"上下文（如 "ABCD"）。
# 用于区分连写答案（"CADB"）与英文单词（"review"）—— 两者正则形态相同，
# 只能靠"字母是否都在选项集里"判定。由调用方在处理每道题前用
# set_option_letters() 设置；None 表示无上下文，退回长度兜底。
_OPTION_LETTERS = None


def set_option_letters(letters) -> None:
    """设置当前题目的合法选项字母集（传入 "ABCD" 或 ["A","B","C","D"]）。

    传 None 清空上下文。校验函数是模块级纯函数，无法感知题目上下文，
    故用这个显式入口注入。用完务必清空，避免串题。
    """
    global _OPTION_LETTERS
    if not letters:
        _OPTION_LETTERS = None
        return
    if isinstance(letters, str):
        _OPTION_LETTERS = {c.upper() for c in letters if c.isalpha()}
    else:
        _OPTION_LETTERS = {str(c).strip().upper()[:1] for c in letters if str(c).strip()}


def check_single(answer):
    if answer is None:
        return False

    text = str(answer).strip()
    if not text:
        return False

    # 单选答案文本中常见逗号（中英文）是句内标点，不应据此判定为多选。
    # 仅在出现明显“多段答案”分隔符时，才判定为非单选。
    strong_delimiters = ["\n", "|", "#", "\t", "\r", "、"]
    for sep in strong_delimiters:
        parts = [p.strip() for p in text.split(sep) if p.strip()]
        if len(parts) > 1:
            return False

    return True


def count_listening_pairs(text: str) -> int:
    """统计 "序号+字母" 形式的答案片段数量（听力题多小问的特征）。

    例如 "22.C 23.A 24.D 25.B" -> 4。
    """
    return len(re.findall(r"(?<![A-Za-z])\d{1,3}\s*[.、,，:：)\]]?\s*[A-Za-z](?![A-Za-z])",
                          str(text or "")))


def check_multiple(answer):
    _t = cut(answer)
    if _t is not None and len(_t) > 0:
        return True
    return False


def check_judgement(answer, true_list, false_list):
    val = str(answer).strip().lower()
    if val in ['true', 't', '1', '对', '正确', '√', '是', 'yes', 'y'] or val in [x.lower() for x in true_list]:
        return 1
    elif val in ['false', 'f', '0', '错', '错误', '×', '否', 'no', 'n', '不对', '不正确'] or val in [x.lower() for x in
                                                                                                     false_list]:
        return 0
    else:
        return -1


def check_completion(answer):
    if len(answer) > 0:
        return True
    else:
        return False


def _is_letter_only(text: str) -> bool:
    """是否为"干净的选项字母"形态。

    合法： "A" / "CADB" / "ABCD" / "A B"（字母间仅用空格分隔）
    非法： "The sense of being out in the world."（含英文单词）、"22.C 23.A"
    """
    text = text.strip()
    # 分隔符形态优先判定："A C D" 里每段都是单字母才算合法。
    # 必须先于连写判定：否则 "review" 这类单词会先命中连写分支（见下）。
    parts = [p for p in re.split(r"[\s.、,，|/]+", text) if p]
    if len(parts) > 1:
        return all(len(p) == 1 and p.isascii() and p.isalpha() for p in parts)
    # 连写形态："AC" / "CADB"（多选、听力题连写答案）。
    # 难点：单个英文单词（review / answer / false）形态与连写完全一样，
    # 无法靠正则区分。改用"选项字母集"判据 —— 合法答案只由出现在题干选项里的
    # 字母组成，而单词必然含选项外的字母（review 含 R/E/V/I/W）。
    # 无字母集上下文时退回长度兜底（实测最长连写为 4，8 已留足余量）。
    if re.fullmatch(r"[A-Za-z]{1,8}", text):
        if _OPTION_LETTERS is not None:
            up = text.upper()
            return bool(up) and all(ch in _OPTION_LETTERS for ch in up)
        return len(text) <= 8
    return False


# 明显是"AI 拒答 / 元话语"而不是答案的文本（2026-09-13）
# 背景：unknown 题型放宽为"不拦文字答案"后，必须另外挡住这类文本 ——
# 否则拒答句会写进 cache.json，之后每次查缓存都命中垃圾（2026-09-11 踩过）。
_REFUSAL_RE = re.compile(
    r"(无法(访问|获取|听到|播放|确定|判断|回答|识别)|"
    r"抱歉[，,]?\s*(我|作为)|作为(一个)?\s*(AI|人工智能|语言模型)|"
    r"我不能(回答|提供|判断)|我无法|没有(足够)?(信息|上下文|原文)|"
    r"as an AI|I (cannot|can't|am unable)|I'm (sorry|unable))",
    re.IGNORECASE,
)


def looks_like_refusal(text: str) -> bool:
    """是否像"AI 拒答/元话语"而不是一个真正的答案。"""
    return bool(_REFUSAL_RE.search(str(text or "")))


def check_submittable(answer, q_type: str = None) -> bool:
    """答案是否具备"可提交"的基本形态。

    选择题（single / multiple）平台 answer 字段最终要的是选项字母
    （单个如 "A"，或听力题多小问连写如 "CADB"）。带题号、空格、标点的原始文本
    （如 "22.C 23.A 24.D 25.B"）平台解析不了，提交上去等于没作答 —— 必须拦下。

    completion（填空）类题目合法答案本就是文字，只判断非空，不做形态限制；
    调用方未传 q_type 时按选择题处理。
    """
    if answer is None:
        return False
    if isinstance(answer, (list, tuple)):
        return any(str(x).strip() for x in answer)

    text = str(answer).strip()
    if not text:
        return False

    # 填空 / 简答：合法答案本就是自由文字，只判非空（2026-09-13 补简答：
    # 此前 shortanswer 落到选择题分支，中文答案会被 _is_letter_only 全部拦下，
    # 作业里的简答题因此恒被判为"答案不可提交"而白丢分）。
    if q_type in ("completion", "shortanswer"):
        return True

    # 判断题：平台提交值固定为 "true"/"false"，题库可能返回各种对错表述，
    # 由调用方（judgement_select）归一后再填，这里只拦明显不是判断答案的垃圾
    # （2026-09-13 补：此前 judgement 无分支，靠 "true"/"false" 恰好是纯字母
    # 侥幸通过；若上游传入中文"对"/"错"会被 _is_letter_only 误杀）。
    if q_type == "judgement":
        return text.lower() in (
            "true", "false", "t", "f", "1", "0",
            "对", "错", "正确", "错误", "是", "否",
            "√", "×", "yes", "no", "y", "n",
        )

    # 未知题型（2026-09-13 修复）：超星官方支持 18 种题型（排序题、完型填空、
    # 名词解释、论述、计算、分录题、资料题、读程序…），本程序只映射了 0-4 与 19，
    # 其余全部落到 "unknown"。此前 unknown 走下面的"必须纯字母"分支 →
    # 这些题的文字答案被 100% 拦下，答案根本提交不上去（与听力题事故同类）。
    # 既然格式未知，就**不拦**：宁可尝试提交（可重做，靠平台反馈学习），
    # 也不要把整题丢弃；只挡明显是 AI 拒答的元话语。
    if q_type == "unknown":
        return not looks_like_refusal(text)

    # 选择题：必须是"纯选项字母"形态，含题号结构、整句文字、中文一律拦下
    return _is_letter_only(text)


def plausible_answer(ans, q_type: str = None) -> bool:
    """缓存层专用的"像不像一个答案"校验。

    背景（2026-09-11 实测）：AI 拒答文本（"我无法访问音频文件…"）曾被当答案写进
    cache.json，此后每次查询缓存命中直接返回垃圾 —— 听力专用提示词根本没机会触发。
    该漏洞的根因是 check_answer 对 TikuFallback(skip_answer_validation) 无条件信任。

    本函数在缓存读写两端调用，按题型拦截拒答整句与格式非法：
    - single/multiple（含听力连写）：必须纯选项字母
    - judgement：只认常见对错表述
    - completion / shortanswer：合法答案本就是文字，只判非空
    - unknown（排序/完型填空/名词解释/论述/计算…）：格式未知 → 放行文字答案，
      但**仍然拦截 AI 拒答文本**（这是缓存层最重要的防线）
    """
    if ans is None:
        return False
    text = str(ans).strip()
    if not text:
        return False
    # AES 拒答文本拦截：任何题型都不允许进缓存
    if looks_like_refusal(text):
        return False
    # 填空 / 简答：自由文字，只判非空
    if q_type in ("completion", "shortanswer"):
        return True
    if q_type == "judgement":
        return text in ("对", "错", "正确", "错误", "是", "否",
                        "true", "false", "True", "False", "T", "F")
    # 未知题型：格式未知，放行（拒答已在上方统一拦下）
    if q_type == "unknown":
        return True
    # single / multiple（含听力连写字母）：纯选项字母
    return _is_letter_only(text)


def is_listening_question(q_info) -> bool:
    """按题干特征判断是否英语听力题（【听力题】标记 / 音频图标 / Questions N to M）。

    AI 提示词选择与 study_work 填写阶段的听力判定必须共用此函数保持一致。
    2026-09-11 实测教训：填写阶段若只看"答案形态是否被 normalize 改变"来判定，
    答案已是连写字母（如 BA/DCBD）时 normalize 无变化 → 漏判 → 走普通单选分支
    只取首字母，多小问只提交了第一问。
    """
    title = str((q_info or {}).get("title") or "")
    if "听力" in title:
        return True
    return bool(re.search(r"\.mp3\b|icon/video\.png|Questions\s+\d+\s+to\s+\d+",
                          title, re.IGNORECASE))


def check_answer(answer, type, tiku):  # 只会写小杯代码，这里用个tiku感觉怪怪的，但先这么写着
    # 如果是手动模式或多题库回退包装器，直接信任
    # （手动模式豁免常规校验；回退包装器因其子题库在各自环节均已单独校验过，此处无需二次校验，以防二次过滤误杀）
    if getattr(tiku, 'is_manual', False) or getattr(tiku, 'skip_answer_validation', False):
        return True

    if type == 'single':
        if check_single(answer) and check_judgement(answer, tiku.true_list, tiku.false_list) == -1:
            return True
    elif type == 'multiple':
        if check_multiple(answer) and check_judgement(answer, tiku.true_list, tiku.false_list) == -1:
            return True
    elif type == 'completion':
        if check_completion(answer):
            return True
    elif type == 'judgement':
        if check_judgement(answer, tiku.true_list, tiku.false_list) != -1:
            return True
    else:  # 未知类型（含听力题 answertype=19 未被识别时）
        # 不再无条件放行：至少要能构成可提交的答案形态
        return check_submittable(answer, type)
    return False


def cut(answer):
    cut_char = [
        "\n",
        ",",
        "，",
        "|",
        "\r",
        "\t",
        "#",
        "*",
        "-",
        "_",
        "+",
        "@",
        "~",
        "/",
        "\\",
        ".",
        "&",
        " ",
        "、",
    ]
    if answer is None:
        return None

    answer = str(answer)
    for char in cut_char:
        if char not in answer:
            continue
        res = [opt.strip() for opt in answer.split(char) if opt.strip()]
        if res:
            return res
    stripped = answer.strip()
    return [stripped] if stripped else None
