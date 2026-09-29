# -*- coding: utf-8 -*-
r"""题库答案矫正：清洗 UI 污染 + 同题冲突仲裁（2026-09-28）。

## 为什么需要

回灌是「只插不更新」（`sync_learned_to_tiku.py`：`if q in exist2: return False`）
→ **先入为主**：一个错误答案一旦写进 `tiku.db`，之后哪怕学到正确答案也永远覆盖不掉。
实测库内已有 10 组同题干答案冲突，例如
    `中华人民共和国成立的时间是` → 1949年10月1日 ✓ / 1945年10月1日 ✗
    `中国最长的河流是`           → 长江 ✓ / 珠江 ✗
两者谁的 hash 先落库，谁就永久生效。

另有一类更隐蔽的历史污染：从平台页面抓答案时把 **UI 状态词**当答案抓了进来
（`地动仪已选择`、`核糖核酸已选择`、`曹雪芹已选择`…）。
源头已在 `api/homework.py:582` 修掉（`re.sub(r"\s*(已选择|已选|已作答|已答)\s*$", "", text)`），
但**已经进库的那批还在**，会作为错误答案回给平台。

## 矫正规则（从严到宽，宁可不动也不改错）

| 情况 | 处置 |
|---|---|
| 库里答案是 UI 污染，清洗后与新答案一致 | `clean` —— 只修文本，语义不变 |
| 两边清洗后一致 | `skip` |
| 库里答案曾**被判过错**（在学习库 `wrong` 里） | `update` —— 用新答案覆盖（强证据）|
| 新答案曾**被判过错** | `skip` —— 保留库里答案 |
| 其余冲突 | `conflict` —— **不自动改**，记入待核对清单 |

最后一条是刻意的：把**对的**答案改错的代价（污染全部实例）远大于留一个待核对的冲突。
"""
import re

# 平台 UI 状态词后缀（源头已修，这里是清历史遗留 + 兜底）
_POLLUTE = re.compile(r"\s*(已选择|已选|已作答|已答)\s*$")


def clean_text(s: str) -> str:
    """去掉平台 UI 状态词后缀；支持重复叠加（"XX已选择已选择"）。"""
    t = str(s or "").strip()
    prev = None
    while prev != t:
        prev = t
        t = _POLLUTE.sub("", t).strip()
    return t


def clean_answer(ans):
    """清洗答案；list / str 皆可，保持原类型。"""
    if isinstance(ans, list):
        return [x for x in (clean_text(v) for v in ans) if x]
    return clean_text(ans)


def is_polluted(ans) -> bool:
    """答案里是否含 UI 污染词。"""
    items = ans if isinstance(ans, list) else [ans]
    return any(clean_text(x) != str(x or "").strip() for x in items)


def _flat(ans) -> list:
    """展平成字符串列表。

    两层防御，都是为了消除**误报冲突**：
      · 嵌套列表（调用方误传 `[['A','B']]`）→ 展平
      · 单元素里的多行答案（`'造纸术\\n印刷术\\n指南针\\n火药'`）→ 按行拆开
        （实测 id=626：库里与学到的只是**行顺序不同**，不拆行会被判成冲突）
    """
    out = []
    for x in (ans if isinstance(ans, list) else [ans]):
        if isinstance(x, list):
            out.extend(_flat(x))
        else:
            parts = [p for p in str(x).split("\n") if p.strip()]
            out.extend(parts or [""])
    return out


def norm(ans):
    """归一化用于比对。

    list  → 清洗、去空、去重、排序后的 tuple（多选顺序无关）
    str   → 单元素 tuple
    这样 `["封装"]` 与 `"封装"` 会被判为一致。
    """
    return tuple(sorted({clean_text(x) for x in _flat(ans) if clean_text(x)}))


_LOOSE = re.compile(r"[\s，,、;；。.：:·]+")


def loose(ans) -> str:
    """宽松比对键：忽略空白与中英文标点差异。

    实测某题库里是 `MR=MC,P=AC`、新学到 `MR=MC，P=AC`（半角 vs 全角逗号），
    按严格比对会误报成"答案冲突"并丢进待核对清单。这类纯排版差异不是冲突。
    """
    return "".join(sorted(_LOOSE.sub("", clean_text(x))
                          for x in _flat(ans) if clean_text(x)))


def decide(cur, new, wrong=()) -> tuple:
    """库里已有 `cur`，新学到 `new`，该怎么做。

    Args:
        cur:   库里现存的答案（list 或 str）
        new:   本轮新学到的答案（list 或 str）
        wrong: 该题干历史上**被判错**的答案集合（元素为 list 或 str）

    Returns:
        (action, reason)
        · 'clean'    库里是 UI 污染，清洗即可（不改变语义）
        · 'skip'     一致 / 新答案不可信 → 保持原值
        · 'update'   库里答案有被判错的强证据 → 用新答案覆盖
        · 'conflict' 无强证据 → 不自动改，交人工核对
    """
    polluted = is_polluted(cur)
    c = clean_answer(cur) if polluted else cur
    if norm(c) == norm(new):
        return ('clean', '库里答案是 UI 污染（如"已选择"），清洗后与正确答案一致') \
            if polluted else ('skip', '两边一致')
    # 只有排版差异（空白 / 中英文标点）→ 不算冲突，不动
    if loose(c) == loose(new):
        return ('skip', '仅标点/空白排版差异，视为同一答案')
    wset = {norm(w) for w in (wrong or []) if norm(w)}
    wset_loose = {loose(w) for w in (wrong or []) if loose(w)}
    if norm(c) in wset or loose(c) in wset_loose:
        return ('update', '库里答案在学习库 wrong 列表里（平台判过错）')
    if norm(new) in wset or loose(new) in wset_loose:
        return ('skip', '新答案在 wrong 列表里（平台判过错），保留库里答案')
    return ('conflict', '两边都无强证据，保留原值并记入待核对清单')
