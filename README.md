# 论文绘图案例库 · paper-plot-library

独立于衰老文献日报的版本 `0.3.0`。已有50篇人工核查种子；自动更新
不是“50篇已复现”。没有运行作者代码，也没有安装作者的科学计算环境。

## 先看网站

当前本机预览：<http://127.0.0.1:8765/>。只有预览进程运行时这个地址才可访问。
以后重启，在终端输入：

```sh
cd /Users/xiangzhoufu/Documents/Codex/paper-plot-library
python3 -m pip install .
python3 -m paper_plot_library serve
```

如果系统没有 Python，可在本机 Codex 环境使用其已验证的运行时：

```sh
cd /Users/xiangzhoufu/Documents/Codex/paper-plot-library
/Users/xiangzhoufu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m paper_plot_library serve
```

预览与自动抓取是不同动作：`serve`只构建并预览，不抓取、不调用付费API。
按 Control+C 关闭预览。不要直接双击HTML，浏览器的本地文件权限会阻止JSON加载。

## 自动流程

默认启用 `initialize_existing_first: true`，先补齐已有候选，再开始每天发现新文章。
首次运行会固定当时全部既有记录作为本批候选，后续不会把批次重复扩成新池。
这批可能已超过最初50篇；以首次建立的批次总数为准，不删除失败文章来凑完成率。

### 第一阶段：补齐当前候选

工作流的 `mode: auto` 会优先处理这批记录；材料仍待处理或需要检查时，暂停新文献发现。
默认每轮最多安排20条来源整理，但总 HTTP 上限仍是150次，因此不保证一轮实际检查完20条。
AI 上限仍是**每轮总计5次 HTTP 尝试**，包含失败重试，不是20篇各调用5次。
图像文件及哈希、许可、正式全文、固定版本代码、作者代码关系、输入说明、AI和期刊指标查询
必须满足当前自动材料验收；已有原图或几条 AI 注释成功不等于本批全部补齐。
材料验收不要求伪造人工图—代码—输入审批，也不表示已执行作者代码或完成复现。
期刊查询完成但未匹配时保留“未匹配”，不编造 IF 或分区。

连续3次真实整理仍无法解决的记录进入“需要检查”；预算跳过不算真实失败尝试。
自动模式不会持续重试这些记录，也不会把许可受限、缺公开输入或服务错误自动标为完成。
先处理具体原因，例如改正密钥或确认可用来源，再手动用 `mode: backfill` 重新尝试补齐。
仍有缺口时，新发现继续暂停。网站会显示本批完成、待处理、需要检查的数量和受控原因。

本批自动材料真正收齐后，**下一次自动运行**才进入日更，不在刚完成补齐的同一轮额外抓取新文献。
`backfill` 始终只处理已有批次；日常保持 `auto` 即可。不要用 `daily` 绕过尚未完成的批次。

### 第二阶段：每日发现与维护

文献发现 → 正式版本/DOI/撤稿筛查 → 作者代码声明 → 固定提交静态检查 →
许可允许的官方整图与补充材料 → 输入路径/真实表头 → 入选排序 → 中文学习注释 →
IF/JCR/引用注释 → 持久化目录 → 独立网站。

进入日更后，每个 UTC 自然日最多新增5篇不同论文（不是必须当天发表5篇）。不足不凑数。
重复运行不会重复新增。以下公平修补队列只用于补齐阶段结束后的日更维护；初始化阶段使用上方的批次队列。
配置模型与密钥且调用预算非零时，已匹配正式文献、已检查全文和作者代码，
但 AI 注释缺失、过期或失败的既有案例进入修补队列。默认每轮5个既有处理名额，
分给3个尚未首次检查的种子和2个已检查案例；某队列不足时名额可转给另一队列。
既有的缺图、缺输入与 AI 失败案例按等待时间轮转，不会一直由同几篇占用。
本轮文献匹配、全文和代码检查均通过后，已检查的 AI 积压立即尝试生成，再发现新文献；
新入选案例与首次检查种子共享剩余 AI 尝试名额，种子的未完成注释下轮进入修补队列。
首次接口失败允许下一轮及时重试，连续失败之后冷却1、2、4小时，最多24小时；
资料缺口通常隔24小时再检查。本地预算跳过不作为接口失败累加。手动指定 `--doi` 可绕过冷却。

### 两个阶段共同的预算与失败边界

每次最多5次AI HTTP尝试，含失败重试；不是5篇×无限次重试。配置上限可以降低。
默认总 HTTP 上限仍为150次；配置 AI 时先为 AI 预留最多5个请求名额，初始采集 GET
上限为145次。预留只是本地计数限制，不会提前调用或扣费；失败重试计入总请求和 AI 尝试。
达到采集上限后，未用完的 AI 预留名额仍可用于注释；实际是否已调用以日志中的尝试计数为准。
因本地上限跳过 AI 时，实际尝试为0就是未发起付费请求，
不能据此判断账户余额不足。HTTP 401/402/403、429及服务方错误在日志中分别显示。
单篇失败不拖垮其它候选，失败保留状态；临时中断保留既有可用证据，并标明旧状态。
旧 AI 注释标为待更新并显示最近尝试状态；历史指标可保留已匹配状态，同时注明本轮获取失败。
本轮文献、全文或代码核查失败时，不为该案例发起付费 AI 请求；保留的旧注释标为过期。
日志阶段统计只反映本轮实际检查的状态，不把此前保留的失败状态重复算入本轮。
日志的“自动步骤完成”只说明本轮自动检查完成，不表示所有原图、输入资料或人工对应已经齐全。
每天新增额度用完显示“今日新增配额已用完”，不会误显示成“本轮未启用”。

选文首先重视脚本、输入和原图可用性，再考虑混合图、多组学/工具/生物计算、衰老主题、
期刊偏好和归一化引用偏好。IF/引用数不是研究质量判断。无许可证、第三方原图、
ND/NC等保守链接，不复制。优先完整混合图；不自动猜边界切掉图例和坐标轴。

## 本地配置 API

复制 `.env.example` 为 `.env`，在自己的编辑器中填写，不把密钥发送到聊天。
DeepSeek、EasyScholar、NCBI账户可以沿用，但新项目必须各自配置。不会自动读取原衰老网站的密钥。

- `LLM_API_KEY`、`LLM_MODEL`：DeepSeek密钥和你账户实际可用的模型名。
- `LLM_API_URL`：默认DeepSeek聊天接口；v1只允许已审核的接口主机，不接受任意URL。
- `EASYSCHOLAR_SECRET_KEY`：IF/JCR/CAS。
- `NCBI_API_KEY`、`NCBI_EMAIL`：PubMed备用检索。
- `OPENALEX_API_KEY`：可选；缺失/额度不足时保留引用指标未知。
- `GH_TOKEN`：本地可选GitHub只读令牌，提高公开仓库API额度；不要扩大访问权限。

```sh
python3 -m paper_plot_library check
python3 -m paper_plot_library update
```

`check`只显示是否配置，绝不显示密钥。`update`会访问公开接口、下载有许可的小文件，
有AI密钥时会产生账户调用费用。首次可限制为：

```sh
python3 -m paper_plot_library update --mode backfill --bootstrap-batch-limit 1 --max-ai-calls 1 --max-requests 40
```

该命令会实际访问接口，不是离线验收；此轮代码修改没有执行它。`--bootstrap-batch-limit`
控制补齐阶段批次，`--repair-limit` 仅控制后续日更维护名额，两者不是同一个选项。
完整更新参数在 `config.json`。支持补充材料（已获用户确认），默认每篇最多3个、
每文件6MB；不下载整套原始测序数据。ZIP不解包；PDF检查页数与身份标志；
无法核实身份单独标记。缺少模型、密钥或额度不会编造中文解读和指标。

## 目录与命名

- `paper_plot_library/`：Python模块，`python -m paper_plot_library`是唯一入口。
- `web/`：正式前端源码。
- `data/catalog.json`：可提交的持久化案例；不含完整摘要或抓取代码片段。
- `data/ledger.json`：每日配额和待核记录，不能随意删除。
- `data/assets/`：带源哈希名的许可允许原图。
- `site/`：可重新构建的本机预览；发布时再通过 `export` 白名单导出。
- `.ppl/`：私有缓存、补充文件、运行锁；不提交、不整目录部署。发布只上传 `export` 生成的白名单子目录。
- `.github/workflows/site.yml`：GitHub 每日更新与发布工作流；每次结果以 Actions 运行记录为准。
- `docs/acceptance.md`、`docs/iterations.md`：验收范围、真实限制与迭代记录。

## 状态与人工核对边界

网站默认显示“可浏览案例”：有当前选图对应的可发布官方整图、已静态检查的固定版本作者脚本。
“候选池”保留缺图、许可仅允许链接或脚本尚待检查的记录；“全部记录”可切换查看，排除记录另设审计筛选。
每篇的原图、代码、输入、AI、期刊指标分别显示，不再用一个“部分完成”概括所有问题。
输入文档笔记、代码读取候选、实际读取的表头分别标注；即便观察到表头，也不等于输入语义、单位或当前选图对应已经核实。
可浏览案例仍可能缺输入或 AI，真实接口/来源错误另外提示。没有许可或未取得原图的文章不会因改名变成可浏览。
“未运行”始终保留，原图—代码—输入的“精确对应已核”是另一项严格状态。
只有审批绑定到当前图像、脚本提交、脚本哈希、真实输入哈希、审核人和输入结构，
并通过本轮正式文献/来源检查，才允许核查就绪。仅文件名带Fig1不算图—代码对应已确认。
AI 响应失败区分输出截断、JSON 格式、字段、证据或未经验证的复现声明；期刊指标区分服务业务错误、结构错误和未匹配。
仅发布受控原因，不公开响应正文或密钥。不会为格式失败无限重试、额外增加付费调用。

## 上传本次修复（已有仓库）

GitHub Desktop 选择 `paper-plot-library`，确认 Changes 是本项目代码与测试。
Summary 填 `feat: backfill existing cases before daily discovery`，点击 Commit to main。
然后 Fetch origin，若提示 Pull origin 先拉取云端自动生成的数据，再 Push origin；不要丢弃云端数据或强制推送。
Push 会自动构建发布新界面，不会抓取论文。待发布成功，在 Actions 打开
“论文绘图案例库 · 自动更新与发布” → Run workflow，勾选 `collect`，`mode` 选择 `auto`。
首次云端运行建立当前批次并分批补齐；不要期待一次20条来源名额就补完所有论文。
若进入“需要检查”，先解决所列原因，再手动勾选 `collect`、选择 `backfill` 重试；日常继续用 `auto`。
DeepSeek、EasyScholar、NCBI 等已有 Secrets 保持不变，不需要重新填写或发送密钥。
遇到 Git 冲突先停止并核对，不选择覆盖。本轮只完成本地代码及离线测试，尚未实际补齐或运行云端接口。
本地测试和改界面不能一次补全50篇：实际可获得的原图、作者输入和许可仍以每轮云端结果为准。

自动建议分类不直接进入已核筛选目录，避免遇到新论文时乱造图型。现有图形/功能
目录支持多对多、分别筛选、动态计数。可以新增审核后的类型，但初版没有无人值守
视觉模型分面定位；复杂新图、动态路径和非公开数据需要人工确认。

## GitHub 云端配置（新用户）

云端更新与部署已由用户启用。以下步骤用于新用户独立配置；本地离线测试不证明云端 API 调用成功。

1. GitHub Desktop → File → New Repository：Name填 `paper-plot-library`，Local path选父目录 `/Users/xiangzhoufu/Documents/Codex`。最终路径应为 `/Users/xiangzhoufu/Documents/Codex/paper-plot-library`，不要多套一层同名目录。已有README、LICENSE、.gitignore，不重新生成或覆盖它们。
2. 确认 `.env`、`.ppl`、`site` 未出现在待提交列表，提交并 Publish repository。
3. 网页进入新仓库 Settings → Secrets and variables → Actions，单独添加上述Secrets。
4. Settings → Pages → Source选择 GitHub Actions；Actions运行“论文绘图案例库 · 自动更新与发布”。
5. 手动先取消 `collect`，检查纯构建发布；然后勾选 `collect`、`mode` 选 `auto`，开始当前批次补齐。

工作流日程为 UTC 01:47；洛杉矶夏令时前一日18:47、冬令时前一日17:47（北京时间09:47）。
GitHub定时任务可能延迟，公开仓库长期不活动可能停用定时任务，需查看Actions实际记录。
完成 GitHub 配置后由云端运行，不要求你的电脑全天在线。定时任务也使用 `auto`：先补齐当前批次，
达到材料验收后下一次自动运行转日更，最多新增5篇。查看 Actions 的实际请求计数、
批次完成/待处理/需要检查数及 Pages 发布结果，确认每次更新的完成范围。

## 回归测试

```sh
python3 -m unittest discover -s tests
python3 -m paper_plot_library build
```

全流程测试使用临时目录的合成材料，不混入真实目录；真实公开来源联调另行记录。
全部源内容按不可信数据处理，不运行其中的安装指令或代码。

接口依据：[PMC云端](https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/)、
[PMC数据结构](https://pmc-oa-opendata.s3.amazonaws.com/README.txt)、
[OpenAlex指标](https://help.openalex.org/data/works/attributes/)、
[GitHub Pages工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。
技能影响：literature-pipeline使筛选、去重和不确定性分层；downloader使补充材料须获确认、
许可与下载验证成为必经步骤；PDF技能使补充PDF页数和身份检查纳入流程。

Desktop路径说明参考[GitHub官方新建仓库指南](https://docs.github.com/en/desktop/overview/creating-your-first-repository-using-github-desktop)。
