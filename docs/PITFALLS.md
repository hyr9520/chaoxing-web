# 踩坑记录（Pitfalls）

> 本文只记**会重复发生的结构性缺陷**，不记一次性事故。
> 每条格式：**现象 → 根因 → 修法**。换机器部署时先读这里，能省几小时。
> 全部内容已脱敏（无账号、姓名、学校、密钥）。

---

## 一、考试链路（最贵的坑都在这）

### 1. 单题失败会毁掉整卷

**现象**：考试答到第 N 题后突然终止，日志写「连续 3 题提交被平台拒绝（暂不支持的题型：X）→ 判定提交通道异常，提前终止」，`submitted=false`，平台状态回到「待做」，分数不更新。**前面 220 题全部白答。**

**根因**：
- 超星是**逐题暂存**（`tempSave=true`），最后才交卷（`tempSave=false`）
- 某题的表单构造抛异常 → 该题暂存被拒
- `submit_exam_page` 有「连续 3 次被拒即判定通道异常」的保护 → 主动终止，不交卷

**修法**（`api/exam.py::build_answer_form`）：
```python
# 任何题型都不许 raise
elif qtype in (QT_SHORT, QT_NOUN, QT_ESSAY, QT_CALC, QT_OTHER, QT_ENTRY):
    form[f"answer{qid}"] = str(answer or "").strip() or "略"
elif qtype in (20, 21, 22):        # B1/B2/C 共用备选答案
    form[f"answer{qid}"] = _as_choice(answer, question) or 第一个选项
else:                               # 未知题型
    form[f"answer{qid}"] = str(answer or "").strip() or "略"
```

**原则**：**宁可这一题 0 分，也不许拖垮整卷。**

---

### 2. 题型码远不止 0-9

实测遇到过的：

| 码 | 题型 | 提交字段 | 备注 |
|---|---|---|---|
| 0 | 单选 | `answer{qid}` | |
| 1 | 多选 | **`answers{qid}`** | **复数**！写成单数是经典坑 |
| 2 | 填空 | `answer{qid}1/2/...` + `blankNum{qid}` | 单空不许按逗号拆 |
| 3 | 判断 | `answer{qid}` = `true`/`false` | **小写**，大写会被判错 |
| 4 | 简答 | `answer{qid}` 文本 | 主观题，老师判分 |
| 5-9 | 名词/论述/计算/其它/分录 | `answer{qid}` 文本 | 同上 |
| 20-22 | B1/B2/C 型（共用备选答案）| 按单选提交 | **答案页里平台不给标准答案** |

**还有 10 连线、11 排序、15 完形、18 阅读等未实现** —— 靠兜底分支接住。

---

### 3. `import` 写在 `if` 分支里 → UnboundLocalError

**现象**：没配 `exam_dump_dir` 时，整场考试报
`UnboundLocalError: cannot access local variable '_exam_mod'`，一场都跑不了。

**根因**：
```python
if _dump_dir:
    import api.exam as _exam_mod     # ← 只在这个分支里导入
res = (_exam_mod.run_exam_with_dump(...) if _dump else _exam_mod.run_exam(...))
#        ^^^^^^^^ 没配目录时这里必然 NameError
```

**修法**：`import` 提到分支外。**凡是「分支内 import + 分支外使用」都是雷。**

---

### 4. 答案页要经过 `mycourse/transfer` 才能拿到

**现象**：`reVersionPaperMarkContentNew`（试卷详情）直连只返回 **803 字节壳页**，什么都解析不到。

**修法**：三步链，缺一不可。
```
1) GET /exam-ans/exam/test/look?courseId=&classId=&examId=&examAnswerId=&cpi=
   → 交卷成功页，里面有「查看试卷详情」的 refer 参数
2) 从 refer 里取出 reVersionPaperMarkContentNew 的完整地址
3) GET /exam-ans/mycourse/transfer?moocId=&clazzid=&ut=s&refer=<urlencoded>
   → 777KB 完整页（含每题「正确答案」）
```

**另一个前置条件**：考试列表页 `exam-list` 需要 **`enc` / `openc` 课程令牌**，
缺了只有 1160 字节壳页。令牌从 `fetch_exam_list()` 拿。

---

### 5. 平台可能不给标准答案

`B1/B2/C 型题`（共用备选答案）在试卷详情里 `rightAnswerContent` **本来就是空的**（老师没设）。
不要试图"推理"出答案 —— **如实标记为无答案**。

---

### 6. 重考 = `reVersionReTest`，`id` 用 relationId 不是 paperId

```js
// 来自 stu-exam-list.js::toSetReTest
GET /exam-ans/exam/test/reVersionReTest?courseId=&classId=&tId={paperId}&id={relationId}&p=1&tag=1
→ {"msg":"重考成功","status":true}
```

`id` 传成 `examId` 会返回 `status:false`。
注意：重考后状态从「待批阅」变「**待做**」，**覆盖**上次成绩。

---

## 二、答题与题库

### 7. `DummyTiku` 是**静默**降级

**现象**：答题全是随机，日志只有「随机兜底」，看不出根因。实测一次重做 201 题错 165 题。

**根因**：登录请求没带题库配置（`tiku_type` / `adapter_url` / `ai_*`），
`build_tiku()` 返回 `DummyTiku(DISABLE=True)`。

**修法**：登录必须带完整配置；并加显式告警（`state["tiku_warn"]`）。

---

### 8. 学习库 key 的形态必须与查询侧一致

**现象**：学习库明明有答案，程序却一条都查不到，整卷随机。

**根因**：
- 写入时 key 用了 `【单选题】题干`
- 查询走 `normalize_title(q_info['title'])`，**平台给的题干不带【】**
- ⇒ 永远匹配不上

**修法**：写入时**不要**加题型前缀。存量数据要批量剥前缀：
```python
new_key = re.sub(r'^【[^】]*】', '', old_key).strip()
```

**注意**：`normalize_title` 只做「去 img 标签 / &nbsp; 归一 / 空白压缩」，
**不剥** `【题型】` 前缀。

---

### 9. `tiku.db` 的 hash 不是 `md5(题干)`

试过 `md5(原文)` / `md5(去空白)` / `md5(去【】+小写)` **全都不对**。

**后果**：手工 INSERT 进 `tiku.db` 的题，`TikuAdapter` 能查到题（`plat=0`）但 `answerKey=[]` —— **拿不到答案**。

**正解**：写**学习库**（`learned_answers.json`），让程序自己算 hash；
网关会定期把学习库**回灌**进 `tiku.db`（`sync_learned_to_tiku`）。

**学习库记录格式**：
```json
{"题干": {"verified": "D", "verified_at": "...", "last_mark": "dui",
         "options": "选项1\n选项2\n...", "wrong": [], "sub_verified": {"0": "D"}}}
```

---

### 10. 学习库是**按 mtime 热加载**的

改文件**不用重启**（`_load()` 比较 `st_mtime_ns`）。
但如果**在另一个进程里写**，正在跑的实例要等下一次读才会看到 —— 不要以为"没生效"就反复重启。

---

## 三、视频 / 章节

### 11. 判断题的 `true`/`false` 绝不能被大写

`TRUE` / `False` 提交上去平台判错。必须是全小写。

### 12. 填空题单空不许按逗号拆

`"9,596,961"` 是**一个空**的内容（千分位），拆成 3 个空会判错。
判据是 `blankCount`（PC 版）或 `blankNum`（手机版），**不是**看有没有逗号。

### 13. 已完成的任务点会被"秒跳"

现象是"前 18 个任务点几秒就过了"，误以为速度异常。
实际是平台已通过（`has_finished=true`）→ 程序直接计入并跳过。
**判断真假完成看平台的 `has_finished`，不是看进度条。**

### 14. 防睡眠是**线程级**的

`SetThreadExecutionState` 只对**调用它的线程**有效。
- 刷课线程持有 → 刷完 `finally` 释放
- **考试跑在新线程** → 必须**自己**再持有一遍，否则刷课一结束系统就休眠，考试跑到一半被冻结

---

## 四、仓库 / 部署

### 15. 根目录不是 git 仓库

代码仓库在**子目录**（如 `chaoxing/`），根目录只有数据和脚本。
在根目录跑 `git status` 会得到 `fatal: not a git repository`，别以为仓库丢了。

### 16. 本地分支名可能与远端不一致

本地 `clean_main` 在远端**不存在**（远端只有 `main`）。
直接 `git push` 会报错，必须：
```bash
git push origin clean_main:main
```

### 17. 本机探测必须禁代理

访问 `127.0.0.1` 时如果系统开了代理（加速器/VPN），请求会被劫持到代理服务器，
表现为"服务明明在跑却连不上"。

```bash
curl --noproxy '*' http://127.0.0.1:5000/api/status
# Python
urllib.request.build_opener(urllib.request.ProxyHandler({}))
```

### 18. public 仓库不能有隐私

`.gitignore` 排除 `HANDOFF.md` / `TEST_REPORT_*.md` / `DEPLOY_REPORT_*.md`
是因为它们含**账号、姓名、学校、网关口令**。
想沉淀经验就**脱敏后**放进 `docs/`，不要直接解禁原文件。

---

## 五、自检清单（上线前跑一遍）

```bash
# 1. 单题失败不会毁整卷（任何题型都不 raise）
grep -c "暂不支持" api/exam.py          # 期望 0

# 2. 没有分支内 import + 分支外使用
grep -nE "^\s{8,}import " api/base.py | head

# 3. 工作区干净且与远端一致
git status --porcelain                  # 空
git ls-tree -r --name-only origin/main | wc -l   # == git ls-files | wc -l

# 4. 关键函数确实在远端
git show origin/main:api/exam.py | grep -c "def set_retest"
```
