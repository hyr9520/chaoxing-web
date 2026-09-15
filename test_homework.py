# -*- coding: utf-8 -*-
"""作业模块测试：重点验证判断题/填空题的提交格式（用户 2026-09-13 重点要求）。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api.homework import build_submit_form, parse_work_page

PASS, FAIL = [], []


def check(name, got, expect):
    ok = got == expect
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f": got={got!r} expect={expect!r}"))


print("=" * 68)
print("[1] 判断题格式（最关键：必须是 true/false，不是 对/错）")
qs_j = [{"id": 1001, "type": "judgement", "type_code": 3, "blankCount": 0}]
check("中文「正确」-> true", build_submit_form(qs_j, {1001: "正确"})["answer1001"], "true")
check("中文「错误」-> false", build_submit_form(qs_j, {1001: "错误"})["answer1001"], "false")
check("中文「对」-> true", build_submit_form(qs_j, {1001: "对"})["answer1001"], "true")
check("中文「错」-> false", build_submit_form(qs_j, {1001: "错"})["answer1001"], "false")
check("√ -> true", build_submit_form(qs_j, {1001: "√"})["answer1001"], "true")
check("× -> false", build_submit_form(qs_j, {1001: "×"})["answer1001"], "false")
check("英文字面 true", build_submit_form(qs_j, {1001: "true"})["answer1001"], "true")
check("英文字面 False", build_submit_form(qs_j, {1001: "False"})["answer1001"], "false")
check("题型码 3", build_submit_form(qs_j, {1001: "对"})["answertype1001"], 3)

print("\n[2] 填空题格式（tiankongsize + 逐空 answer{id}N）")
qs_c = [{"id": 2002, "type": "completion", "type_code": 2, "blankCount": 2}]
f = build_submit_form(qs_c, {2002: ["甲", "乙"]})
check("tiankongsize=2", f["tiankongsize2002"], 2)
check("第 1 空", f["answer20021"], "甲")
check("第 2 空", f["answer20022"], "乙")

f2 = build_submit_form(qs_c, {2002: "甲|乙"})
check("管道符拆分", (f2["answer20021"], f2["answer20022"]), ("甲", "乙"))

f3 = build_submit_form(qs_c, {2002: "甲，乙"})
check("中文逗号拆分", (f3["answer20021"], f3["answer20022"]), ("甲", "乙"))

qs_c3 = [{"id": 3003, "type": "completion", "type_code": 2, "blankCount": 3}]
f4 = build_submit_form(qs_c3, {3003: "只有一个"})
check("不足补齐 3 空", f4["tiankongsize3003"], 3)
check("第 2 空为空", f4["answer30032"], "")

qs_c4 = [{"id": 4004, "type": "completion", "type_code": 2, "blankCount": 1}]
f5 = build_submit_form(qs_c4, {4004: ["唯一"]})
check("单空 tiankongsize=1", f5["tiankongsize4004"], 1)
check("单空值", f5["answer40041"], "唯一")

print("\n[3] 选择题格式不变（回归保护）")
qs_s = [{"id": 5005, "type": "single", "type_code": 0, "blankCount": 0}]
check("单选 A", build_submit_form(qs_s, {5005: "A"})["answer5005"], "A")
qs_m = [{"id": 6006, "type": "multiple", "type_code": 1, "blankCount": 0}]
check("多选 ABC", build_submit_form(qs_m, {6006: "ABC"})["answer6006"], "ABC")

print("\n[4] answerwqbid 字段")
f6 = build_submit_form(
    [{"id": 11, "type": "single", "type_code": 0}, {"id": 22, "type": "judgement", "type_code": 3}],
    {11: "A", 22: "对"})
check("逗号拼接", f6["answerwqbid"], "11,22")

print("\n[5] 简答题")
qs_sa = [{"id": 7007, "type": "shortanswer", "type_code": 4, "blankCount": 0}]
check("简答直落 answer{id}", build_submit_form(qs_sa, {7007: "我的看法"})["answer7007"], "我的看法")

print("\n[6] 手机端作业页解析（真实 DOM 片段）")
SAMPLE = """
<html><head><title>作业</title></head><body>
<h3 class="py-Title">第一单元作业</h3>
<form id="form1">
  <input id="workAnswerId" value="55501">
  <input id="totalQuestionNum" value="3">
  <input id="workRelationId" value="777">
  <input id="fullScore" value="100">
  <input id="enc_work" value="ABCencXYZ">
</form>
<div class="Py-mian1">
  <input id="answertype9001" value="0">
  <div class="Py-m1-title"><span>1.</span>下列说法正确的是（ ）</div>
  <ul>
    <li class="more-choose-item"><em class="choose-opt" id-param="A"></em>
      <div class="choose-desc">选项甲</div></li>
    <li class="more-choose-item"><em class="choose-opt" id-param="B"></em>
      <div class="choose-desc">选项乙</div></li>
  </ul>
</div>
<div class="Py-mian1">
  <input id="answertype9002" value="3">
  <div class="Py-m1-title"><span>2.</span>地球是圆的。</div>
</div>
<div class="Py-mian1">
  <input id="answertype9003" value="2">
  <div class="Py-m1-title"><span>3.</span>中国的首都是____。</div>
  <ul class="blankList2">
    <li><span>（1）</span><input class="blankInp2" value=""></li>
    <li><span>（2）</span><input class="blankInp2" value=""></li>
  </ul>
</div>
</body></html>
"""
p = parse_work_page(SAMPLE)
check("作业标题", p["title"], "第一单元作业")
check("解析题数 3", len(p["questions"]), 3)
check("表单 workAnswerId", p["form"]["workAnswerId"], "55501")
check("表单 enc_work", p["form"]["enc_work"], "ABCencXYZ")
check("第1题单选", p["questions"][0]["type"], "single")
check("第1题选项", p["questions"][0]["options"], "A.选项甲\nB.选项乙")
check("第2题判断", p["questions"][1]["type"], "judgement")
check("第2题判断题固定选项", p["questions"][1]["options"], "正确\n错误")
check("第3题填空", p["questions"][2]["type"], "completion")
check("第3题空数 2", p["questions"][2]["blankCount"], 2)
check("题干去序号", "中国的首都是" in p["questions"][2]["title"], True)

print("\n" + "=" * 68)
print(f"结果: 通过 {len(PASS)} / 失败 {len(FAIL)}")
for f in FAIL:
    print(f"  - {f}")
print("=" * 68)
sys.exit(1 if FAIL else 0)
