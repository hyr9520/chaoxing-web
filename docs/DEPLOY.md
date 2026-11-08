# 新机器部署手册

> 目标：换一台电脑，**从零把环境跑起来**，且不遗失历史踩过的坑。
> 本文分三段：**① 给 AI 的提示词 → ② 部署步骤 → ③ 部署后必须补什么**。

---

## ① 给 AI 的提示词（直接复制粘贴）

在新电脑上对 AI 说：

```
帮我在这台机器上部署 GitHub 仓库 hyr9520/chaoxing-web。

要求：
1. 先读仓库里的 docs/PITFALLS.md 和 docs/ARCHITECTURE.md，
   了解这个项目踩过的坑和代码分层，再动手。
2. 环境：Python 3.13+。装 requirements.txt 的依赖。
3. 这个项目有两种入口：
   - python web_ui.py        单账号（端口 5000）
   - python start_multi.py 3 多账号（5000/5001/5002 + 网关 8888）
   先按单账号起，验证能登录再扩。
4. 部署完把「必须补的配置」列给我，我自己填密钥和账号，
   你不要把任何密钥写进代码或提交到仓库。
5. 不要改 .gitignore 里那些被排除的文件（含隐私）。
6. 起服务后必须真实打开 http://127.0.0.1:5000 确认能登录、能列课程，
   不要只看到进程起来就说成功。
```

**如果你只想要最小可用（先跑视频，不答题）**，把第 3 条改成：
```
先只跑视频，题库和 AI 都不配，我要先验证登录和课程列表正常。
```

---

## ② 部署步骤

### 1. 环境

| 项 | 要求 |
|---|---|
| Python | **3.13+** |
| 系统 | Windows 最优（多实例启动器针对 Windows 优化）；Linux/macOS 单账号可用 |
| 网络 | 能访问学习通；**本机请求要禁代理**（见 PITFALLS #17）|

### 2. 拉代码 + 装依赖

```bash
git clone https://github.com/hyr9520/chaoxing-web.git
cd chaoxing-web
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 启动

**单账号**
```bash
python web_ui.py
# 浏览器打开 http://127.0.0.1:5000
```

**多账号**
```bash
python start_multi.py 3          # 3 个实例 + 网关
# 网关 http://127.0.0.1:8888
```

数据落在 `chaoxing_data/acc1~accN/`（`CK_DATA_DIR` 可改），
各账号的 cookies / 学习库 / 界面配置**完全隔离**。

### 4. Docker（可选）

```bash
docker build -t chaoxing-web .
docker run -d -p 5000:5000 -v /你的配置目录:/config chaoxing-web
```
> Dockerfile 用 `config.ini` 走命令行模式；**Web 模式的配置在界面里填**，
> 两种模式的配置文件不是同一套，别混。

---

## ③ 部署后必须补的东西

### 必填（不填就跑不起来或跑得没意义）

| # | 项 | 在哪填 | 不填的后果 |
|---|---|---|---|
| 1 | **学习通账号 + 密码** | 界面登录框 | 无法登录 |
| 2 | **题库 / AI 配置** | 界面「设置」 | 见下方说明 |

**关于题库**：不配也能刷视频和章节任务点，但**需要答题的章节检测会被跳过或全随机**。
实测代价：一次重做 201 题错 165 题（PITFALLS #7）。

### AI Key（最常忘的一项）

两种方式，**任选其一**：

**方式 A：本地文件**（推荐，本地开发）
```bash
cp ai_keys.local.txt.example ai_keys.local.txt
# 编辑 ai_keys.local.txt，填自己的 key
```

格式（JSON，按平台分组）：
```json
{
  "tencent": ["sk-xxx1", "sk-xxx2"],
  "agnes":   ["sk-yyy1"]
}
```

**方式 B：环境变量**（容器/服务器）
```bash
export CHAOXING_AI_KEYS='{"tencent":["sk-xxx1"]}'
```

> 优先级：**环境变量 > ai_keys.local.txt**。
> `ai_keys.local.txt` 已在 `.gitignore`，**永远不会被提交**。

### 题库适配器 tikuAdapter（可选，但强烈建议）

项目默认连 `http://127.0.0.1:8060/adapter-service/search`。

| 项 | 说明 |
|---|---|
| 是什么 | 独立的聚合题库服务（不是本仓库的代码）|
| 从哪来 | `https://github.com/DokiDoki1103/tikuAdapter` 下载可执行文件 |
| 怎么起 | 在 tikuAdapter 目录跑 `tikuAdapter.exe`（监听 8060）|
| 不配的后果 | 回退到「学习库 → AI → 随机」，命中率下降 |

**重要（PITFALLS #9）**：`tiku.db` 的 hash 不是 `md5(题干)`，**不要手工 INSERT**。
要让程序写**学习库**（`learned_answers.json`），网关会定期回灌进 `tiku.db`。

### 其他可能需要补的

| 项 | 什么时候需要 |
|---|---|
| `exam_dump_dir` | 想让考试逐题落盘（事后核对答案）——填目录路径 |
| `exam_auto_submit` | 考试是否自动交卷。**默认 false = 只暂存**，确认无误再开 |
| 网关口令 | 远程访问时需要（`gateway.py` 的鉴权）|
| 内网穿透 | 要在外网看进度/操作时（只需 1 条隧道打到网关 8888）|

---

## ④ 部署完成自检

按顺序跑，**每一步都要有明确输出**，别跳过：

```bash
# 1. 依赖完整
python -c "import flask, requests, bs4, loguru, openai; print('deps ok')"

# 2. 进程起来
curl --noproxy '*' -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/
# 期望 200

# 3. 未登录状态
curl --noproxy '*' -s http://127.0.0.1:5000/api/status
# 期望 logged_in: false

# 4. 浏览器实测登录 → 能列出课程 → 能看到章节
#    这一步必须人工确认，接口返回 200 不代表登录成功

# 5. 题库连通（若配了 adapter）
curl --noproxy '*' -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8060/
# 期望 200；不通就先起 tikuAdapter.exe
```

**常见"看起来成功其实失败"**：
- 进程起来了但登录失败 → 看 `chaoxing_data/acc1/chaoxing.log`
- 登录成功但答题全随机 → **题库没配好**（PITFALLS #7），不是程序坏了
- 接口连不上但服务在跑 → **代理劫持**（PITFALLS #17），加 `--noproxy '*'`

---

## ⑤ 和本仓库历史无关的坑（换机器特有）

| 现象 | 原因 | 处理 |
|---|---|---|
| `fatal: not a git repository` | 在**根目录**跑 git，代码在子目录 | `cd` 进代码目录 |
| `git push` 报错 | 本地分支 `clean_main` 远端没有 | `git push origin clean_main:main` |
| 中文文件名乱码 | Windows 终端编码 | `chcp 65001` 或改用 Git Bash |
| `pythonw` 启动无日志 | 没有 stdout | 输出重定向到文件 |
| 端口被占 | 上次进程没退干净 | `netstat -ano | findstr :5000` 后 taskkill |
