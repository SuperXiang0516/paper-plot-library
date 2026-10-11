# 论文绘图案例库 · paper-plot-library

独立于衰老文献日报的本地第一版 `0.1.0`。已有50篇人工核查种子；自动更新
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

文献发现 → 正式版本/DOI/撤稿筛查 → 作者代码声明 → 固定提交静态检查 →
许可允许的官方整图与补充材料 → 输入路径/真实表头 → 入选排序 → 中文学习注释 →
IF/JCR/引用注释 → 持久化目录 → 独立网站。

每个 UTC 自然日最多新增5篇不同论文（不是必须当天发表5篇）。不足不凑数。
重复运行不会重复新增。新入选案例优先获得 AI 预算，旧种子另行逐批修补。
每次最多5次AI HTTP尝试，含失败重试；不是5篇×无限次重试。配置上限可以降低。
单篇失败不拖垮其它候选，失败保留状态；临时中断保留既有可用证据，并标明旧状态。

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
python3 -m paper_plot_library update --no-discovery --repair-limit 1 --max-ai-calls 1 --max-requests 40
```

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
- `.github/workflows/site.yml`：以后上传GitHub后的每日更新/发布工作流，当前未启用。
- `docs/acceptance.md`、`docs/iterations.md`：验收范围、真实限制与迭代记录。

## 状态与人工核对边界

“人工种子”“候选/待核”“部分完成”“静态核查就绪”分开显示。“未运行”始终保留。
只有审批绑定到当前图像、脚本提交、脚本哈希、真实输入哈希、审核人和输入结构，
并通过本轮正式文献/来源检查，才允许核查就绪。仅文件名带Fig1不算图—代码对应已确认。

自动建议分类不直接进入已核筛选目录，避免遇到新论文时乱造图型。现有图形/功能
目录支持多对多、分别筛选、动态计数。可以新增审核后的类型，但初版没有无人值守
视觉模型分面定位；复杂新图、动态路径和非公开数据需要人工确认。

## 以后上传 GitHub（本次未执行）

1. GitHub Desktop → File → New Repository：Name填 `paper-plot-library`，Local path选父目录 `/Users/xiangzhoufu/Documents/Codex`。最终路径应为 `/Users/xiangzhoufu/Documents/Codex/paper-plot-library`，不要多套一层同名目录。已有README、LICENSE、.gitignore，不重新生成或覆盖它们。
2. 确认 `.env`、`.ppl`、`site` 未出现在待提交列表，提交并 Publish repository。
3. 网页进入新仓库 Settings → Secrets and variables → Actions，单独添加上述Secrets。
4. Settings → Pages → Source选择 GitHub Actions；Actions运行“论文绘图案例库 · 自动更新与发布”。
5. 手动先取消 collect，检查纯构建发布，再勾选 collect做第一次完整云端联调。

工作流日程为 UTC 01:47；洛杉矶夏令时前一日18:47、冬令时前一日17:47（北京时间09:47）。
GitHub定时任务可能延迟，公开仓库长期不活动可能停用定时任务，需查看Actions实际记录。
放到GitHub后云端运行，不要求你的电脑全天在线；目前仅本地代码，尚不会自动每天运行。

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
