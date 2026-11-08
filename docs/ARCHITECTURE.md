# 架构说明

> 换机器接手时先读这个，能快速定位「改哪里」。

## 进程模型

```
                ┌──────────────┐
  浏览器  ────► │  gateway.py  │  统一入口（默认 8888）
                │  反向代理 +   │  · 账号路由 /api/instance/<n>/*
                │  登录态校验   │  · 三级权限（管理员 / 本人 / 只读）
                └──────┬───────┘
                       │ 按端口分发
     ┌─────────────────┼─────────────────┐
     ▼                 ▼                 ▼
  实例 5000        实例 5001         实例 50xx
  (acc1)           (acc2)            (accN)
  ├ data:           ├ data:           ├ data:
  │ chaoxing_data/acc1/              │
  │   ├ ui_config.json   界面配置 + 账号档案
  │   ├ learned_answers.json  学习库（已验证答案）
  │   ├ cookies.txt
  │   └ chaoxing.log
  └ web_ui.py 起的 Flask 进程（每个实例一份）
```

**关键**：每个实例是**独立进程 + 独立数据目录**，cookies / 学习库 / 配置**完全隔离**。
`CK_DATA_DIR` 环境变量决定用哪份数据。

## 代码分层

| 文件 | 职责 | 改动频率 |
|---|---|---|
| `web_ui.py` | Flask 路由 + 内嵌前端（HTML/JS 都在里面）| 高 |
| `api/base.py` | `Chaoxing` 主类：登录、课程、章节、视频、作业、考试调度 | 高 |
| `api/exam.py` | 考试链路：列表、封面、开考、取题、构造表单、提交、**答案页**、**重考** | 高 |
| `api/answer.py` | 答题链路：题库调度、AI 兜底、投票、本地库直查 | 中 |
| `api/learned.py` | 学习库（答案沉淀，mtime 热加载）| 低 |
| `api/decode.py` | 课程卡片 / 任务点解码 | 中 |
| `api/homework.py` | 作业链路（**考试的表单字段要对齐这里**）| 中 |
| `gateway.py` | 网关、实例编排、权限 | 中 |
| `main.py` | 命令行入口（不经过 Web 时用）| 低 |
| `start_all.pyw` | 多实例批量启动 | 低 |

## 两条答题通道（**别混淆**）

| | 章节测验 / 作业 | 考试 |
|---|---|---|
| 入口 | `study_work()` / `_run_homeworks()` | `run_exam()` / `run_exam_with_dump()` |
| 提交 | 整卷一次提交 | **逐题暂存**（`tempSave=true`）+ 最后交卷 |
| 表单构造 | `api/homework.py` | `api/exam.py::build_answer_form` |
| 答案字段 | 单选 `answer{qid}` / 多选 `answers{qid}` | 同上（**必须对齐**）|

> 历史上考试的 `build_answer_form` 漏了主观题，就是因为**没和作业链路对齐**。
> 改任一侧时，先看另一侧怎么写的。

## 答案查询的优先级

```
1. 学习库（learned_answers.json）—— 按归一化题干精确匹配
2. 本地 tiku.db 直查（local_tiku_lookup）—— 按归一化题干精确匹配，不做模糊
3. TikuAdapter（8060）—— 按 hash 精确查，选项文本逐字参与，脆弱
4. AI 兜底（可换模型重答）
5. 随机（仅考试，且只对客观题）
```

**注意**：
- 学习库命中后要过两道闸：`plausible_answer`（形态校验）+ `options_compatible`（选项一致性）
- `options_compatible` 在**任一侧选项少于 2 个**时直接放行（兼容老数据）

## 考试状态机

```
待做 ──(程序作答+交卷)──► 待批阅 ──(老师批改)──► 已完成/已批阅
  ▲                          │
  └────(重考 reVersionReTest)─┘
```

- **待做** → 可以作答
- **待批阅** → 已交卷，分数未出（连客观题也不判，等老师打开批阅）
- **已完成** → 有分数，若平台允许仍可重考

## 落盘与观测

| 产物 | 位置 | 用途 |
|---|---|---|
| 界面日志 | `chaoxing_data/<acc>/chaoxing.log` | loguru，含 TRACE 细节 |
| 前端日志区 | `/api/logs` 的 `lines` | **`print()` 走这里，不进 loguru** |
| 考试逐题落盘 | `exam_dump_dir/exam_<实例>_<examId>.jsonl` | 题干+选项+程序答案，供事后核对 |
| 学习库 | `chaoxing_data/<acc>/learned_answers.json` | 已验证答案 |

**排障要点**：`print()` 和 `logger` 写的是**两个地方**，只查一个会漏。

## 数据流：一次考试

```
扫描(/api/exam/scan) → 列表解析(fetch_exam_list, 需要 enc/openc)
  ↓ 用户勾选
启动(/api/exam/run)
  ↓
取封面(fetch_exam_cover) → 开考(start_exam) → 取题(fetch_exam_question_page)
  ↓ 每题
_answer_fn → 题库/AI → build_answer_form → submit(tempSave=true)
  ↓ 全部答完
submit(final=True) 交卷
  ↓ 之后可选
答案提取(fetch_exam_answer_page) → parse_answer_page → 写学习库
  ↓
重考(set_retest) → 再跑一遍（此时答案已命中，全对）
```
