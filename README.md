# 每日排行 + AI 速览 · 无人值守流水线

不开电脑、不用 VPS，每天定时抓取全站项目排行，交给 AI 总结成中文晨报，
自动提交进仓库、部署成网页、推送到你手机上。

## 它每天做什么

```
cron 触发（北京时间 08:33 主 / 09:07 备份，均避开整点）
        ↓
多源抓取（GitHub Trending ×3 + Hacker News）
        ↓
按分栏去重排名
        ↓
AI 总结（DeepSeek / OpenAI / Moonshot / 智谱 / 本地 Ollama）
        ↓
渲染 Markdown + HTML
        ↓
写文件 → commit 回仓库 → Pages 自动部署 → 推送通知
        ↓
记录 state.json（当天已成功则后续时间点自动跳过）
```

产出一份综合榜单：

| 分栏 | 内容 | 排序方式 |
|---|---|---|
| 综合排行 | GitHub Trending（全语言/Python/TypeScript）+ Hacker News | 轮转交错，防止单一源刷屏 |

想加更多榜单只需在 `config.yaml` 里写新的 `section`，详见「四、加数据源」。

## 一、部署（约 5 分钟）

### 1. 建仓库并放入文件

新建一个仓库（建议 **public**，公开仓库的 Actions 分钟数完全免费），
把本目录（`automation/`）的**内容**作为仓库根目录推上去：

```bash
cd automation
git init -b main
git add -A
git commit -m "init daily digest"
git remote add origin git@github.com:<你的用户名>/<仓库名>.git
git push -u origin main
```

推完确认仓库根目录能看到 `.github/`、`config.yaml`、`scripts/`。

> 注意：GitHub 只认**仓库根目录**下的 `.github/workflows/`。
> 如果你把 automation 当子目录保留，必须把 `.github/` 搬到仓库根，
> 并给 workflow 里所有路径加上 `automation/` 前缀。

### 2. 开启 Pages

`Settings → Pages → Source` 选 **GitHub Actions**（不要选 "Deploy from a branch"，
本项目用的是官方 Pages Action）。

### 3. 配置 Secrets

`Settings → Secrets and variables → Actions → Secrets`，按需添加：

| Secret 名 | 用途 | 是否必填 |
|---|---|---|
| `LLM_API_KEY` | AI 总结的密钥 | 可选（不配则用模板摘要） |
| `LLM_BASE_URL` | 接口地址，如 `https://api.deepseek.com/v1` | 配了 key 就要配 |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Telegram 推送 | 渠道任选 |
| `WECOM_WEBHOOK_KEY` | 企业微信群机器人 key | 渠道任选 |
| `FEISHU_WEBHOOK` (+`FEISHU_SECRET`) | 飞书机器人 | 渠道任选 |
| `DINGTALK_WEBHOOK` (+`DINGTALK_SECRET`) | 钉钉机器人 | 渠道任选 |
| `SERVERCHAN_KEY` | Server酱 | 渠道任选 |
| `QQ_MAIL_USER` + `QQ_MAIL_AUTH_CODE` | QQ 邮箱推送（手机 QQ 弹「QQ邮箱提醒」） | 渠道任选 |
| `QMSG_KEY` | Qmsg 酱，真正的 QQ 消息 | 渠道任选 |
| `GENERIC_WEBHOOK_URL` | 自建服务 / n8n / Node-RED | 渠道任选 |

> **别把密钥写进 `config.yaml`**——这是公开仓库，提交上去等于公开泄露。
> 密钥只放 Secrets，`config.yaml` 里只写模型名和参数。

推送渠道**配一个就够**，没配的会自动跳过，不会让任务失败。

#### 想推到手机 QQ

两条路，按「省事」到「像聊天」排：

| 方式 | 需要的 Secret | 怎么拿 | 效果 |
|---|---|---|---|
| **QQ 邮箱**（推荐） | `QQ_MAIL_USER` = 你的 QQ 邮箱<br>`QQ_MAIL_AUTH_CODE` = 16 位授权码 | QQ邮箱网页版 → 设置 → 账户 → 开启 `POP3/SMTP服务` → 按提示短信验证 → 拿到授权码 | 手机 QQ 弹「QQ邮箱提醒」，点开是完整晨报 |
| **Qmsg 酱** | `QMSG_KEY`（可选 `QMSG_QQ` 指定收件 QQ） | qmsg.zendee.cn 登录拿 key，把它的机器人加为好友 | 真正的 QQ 消息，能推到群 |

`QQ_MAIL_AUTH_CODE` **不是 QQ 登录密码**，是开启 SMTP 服务后单独生成的那串 16 位字符。
填错的话日志里会出现「服务器在登录阶段断开了连接」——QQ 在授权码错误时直接掐连接，
不返回标准的 535 认证失败，所以脚本特意把这条错误翻译成人话。

`QQ_MAIL_TO` 可以不配，默认发给自己（= `QQ_MAIL_USER`）；配了就发到别的邮箱。

### 4. 配置变量（可选）

`Settings → Secrets and variables → Actions → Variables` 加一个：

| Variable | 值 |
|---|---|
| `PAGES_URL` | `https://<你的用户名>.github.io/<仓库名>` |

配了它，通知末尾会带上网页链接。

### 5. 先手动跑一次

`Actions → Daily Digest → Run workflow`，
把 `dry_run` 勾上先试跑（只生成不推送），确认没问题再正式跑。

## 二、调度时间怎么定的

`daily.yml` 里配了**两个**时间点：

```yaml
schedule:
  - cron: "33 0 * * *"
    timezone: "Asia/Shanghai"   # 北京时间 08:33，主
  - cron: "7 1 * * *"
    timezone: "Asia/Shanghai"   # 北京时间 09:07，备份
```

**为什么不用整点。** 三个叠加的原因：

1. GitHub 官方明确说明高负载时段会延迟调度，延迟可达数十分钟，极端情况下直接丢弃且不补跑；
2. `:00 / :15 / :30 / :45` 是大量 workflow 的默认落点（`*/15`、`*/30` 这类写法太多），这四个分钟数比其它分钟挤得多；
3. UTC 00:00 是全球每日定时任务的最大洪峰——而北京时间 08:00 正好撞上它。

所以主时间取 **08:33**（= UTC 00:33），已经过了峰尖。

**为什么要两个时间点。** 08:33 被延迟或丢弃时，09:07 补上。靠 `data/state.json` 里的幂等检查保证同一天只推送一次：

```json
{ "last_date": "2026-10-15", "push_ok": true, "last_run_at": "..." }
```

判定条件收得很紧——必须**生成成功且推送成功**才跳过：

- 当天已成功 → 后续时间点秒退（实测 0.5 秒，不发起任何网络请求）；
- 生成成功但推送失败 → `push_ok: false`，下一个时间点会重试推送；
- `--dry-run` 不写 state、不碰 `.changed`，本地预览不会污染线上判定；
- 需要强制重跑时用 `--force`（workflow 里对应 `force` 输入）。

> ⚠️ 执行时间**只能**改这里。用 `.env`、环境变量或其它地方去改是无效的——
> GitHub 只读 `.github/workflows/` 里的 cron。

## 三、本地调试

```bash
pip install -r requirements.txt

# 不联网推送、不调用 AI，只看产物
python scripts/main.py --root . --dry-run --no-llm

# 带 AI 和推送正式跑
export LLM_API_KEY=sk-xxxx
export LLM_BASE_URL=https://api.deepseek.com/v1
python scripts/main.py --root .

# 当天已经跑成功过会被幂等检查跳过，加 --force 强制重跑
python scripts/main.py --root . --dry-run --force
```

### 换 AI 模型

`config.yaml` 的 `llm` 段：

```yaml
llm:
  model: claude-sonnet-5    # 换成你接口支持的任意模型名
  temperature: 0.3
  max_tokens: 8000          # 见下方「推理模型」说明，别调太低
  timeout: 300              # 推理模型响应慢，实测 25-60 秒
```

想知道接口支持哪些模型，浏览器打开 `<你的 base_url>/models`，带上
`Authorization: Bearer <key>` 头就能拿到列表。

**推理模型的两个坑**（`scripts/summarize.py` 里已处理）：

1. **思考 token 也算 `completion_tokens`**。新一代模型（Claude Opus 5 / Sonnet 5
   这类）先在内部思考再输出，`max_tokens` 给小了会出现「HTTP 200、
   `finish_reason=stop`、`content` 却是空字符串」——token 全烧在思考上，
   晨报会静默变空白。所以默认给足 8000，并在拿到空 content 时明确报错降级。
2. **Cloudflare 会挡自定义 UA**。部分中转站前置了 CF，UA 写成 `xxx/1.0`
   会被直接 403（error code 1010），脚本已改用浏览器 UA。

调用失败（含 5xx / 429 / TLS 抖动）会自动重试 3 次并退避 3、6 秒；
连续失败则降级为模板摘要，**不会让整条流水线失败**。

产物位置：

| 文件 | 用途 |
|---|---|
| `docs/index.html` | Pages 网页（自带深浅色适配） |
| `data/latest.md` | 最新一期的 Markdown |
| `data/latest.json` | 结构化数据，方便二次消费 |
| `data/archive/YYYY-MM-DD.md` | 历史归档 |

## 四、加数据源

改 `config.yaml` 即可，不用动代码。支持五种类型：

**GitHub Trending**（无需密钥）

```yaml
- type: github_trending
  name: GitHub Trending · Rust
  since: daily          # daily / weekly / monthly
  language: rust
  section: main         # 归到哪个分栏
```

**GitHub 搜索**（追某个话题的高星项目）

Trending 只反映「当天新增 star」，成熟但冷门的项目永远上不了榜。
想追某个话题就用搜索接口，按总星标排序：

```yaml
- type: github_search
  name: 话题追踪 · 示例
  query: "topic:stable-diffusion"   # 任意 GitHub 搜索语法
  sort: stars                       # stars / updated / forks
  limit: 40                         # 最大 100
  min_stars: 0                      # 客户端过滤，低于该星数丢弃
  pushed_within_days: 365           # 可选：只留最近 N 天有推送的
  section: main
```

`query` 支持 GitHub 搜索的全部语法，几种常用写法：

| 写法 | 作用 |
|---|---|
| `topic:xxx` | 作者主动打了标签的仓库，最规范 |
| `"xxx in:name,description"` | 名字或简介里提到关键词的，覆盖面更广 |
| `org:xxx` | 某个组织下的全部仓库 |
| `topic:xxx pushed:>=2025-01-01` | 叠加活跃度过滤（也可用 `pushed_within_days`） |

多个覆盖范围不重叠的检索要拆成多个源，再靠跨分栏去重合并——
GitHub 搜索 API 不支持跨限定符的 `OR`（实测直接返回 0 条）。

> 搜索接口匿名限速 10 次/分钟。Actions 里会自动用 `github.token`，
> 无需自己建 secret（workflow 已配好）。本地跑想提速的话设个环境变量：
> `$env:GITHUB_TOKEN="ghp_xxx"`。

**Hacker News**（官方 API，无需密钥）

```yaml
- type: hackernews
  name: Hacker News
  endpoint: beststories # topstories / beststories / newstories
  limit: 25
```

**任意 JSON 接口**（用点号路径描述字段）

```yaml
- type: json_api
  name: dev.to
  url: https://dev.to/api/articles
  params: { top: 7, per_page: 20 }
  list_path: ""                    # 顶层就是数组；嵌套写 "data.items"
  title_key: title
  url_key: url
  score_key: positive_reactions_count
```

**任意列表页**（CSS 选择器，站点改版只改配置）

```yaml
- type: generic_html
  name: 某站点
  url: https://example.com/list
  base_url: https://example.com
  item_selector: "div.item"
  title_selector: "a.title"
  link_selector: "a.title"
  score_selector: "span.score"
```

单个源失败只会记进 `data/latest.md` 的「抓取异常」小节，不影响其他源。

## 五、关于排名的设计

### 为什么会有两种排序

关键看**同一个分栏里的几个源，量纲是否相同**。

**量纲不同 → 轮转交错（`merge: rotate`）**

综合排行就属于这种：GitHub Trending 的分数是「当日新增 star」（动辄几万），
Hacker News 是「点赞数」（几百）。直接全局排序会让 HN 全部垫底。

`scripts/sources.py` 的 `dedupe_rank()` 因此做了两步：

1. **逐源归一化**：每个源内部把分数线性映射到 `[0, 1]`，让各源头部站在同一起跑线；
2. **轮转交错合并**：第 1 轮每个源各出 1 条，第 2 轮再各出 1 条……直到凑满 `top_n`。

> 只做第 1 步还不够：归一化后每个源的头部都是 `1.0`，条目多的源
> （比如 25 条的 HN）会在高分区间塞进更多条目，榜单被单一大源刷屏。

**量纲相同 → 直接排序（`merge: score`）**

如果一栏里的几个源量纲一样（比如都用 `github_search`，分数都是 GitHub 总星标），
轮转就是**错的**——实测踩到过：108★ 的仓库排到了 958★ 的前面，
只因为它来自另一个源。

所以 `rank_by_score()` 直接按分数降序。这里有个坑值得记下来：
排序键读的是 `extra["raw_score"]` 而**不是** `it.score`——因为
`dedupe_rank()` 会把 `it.score` 原地改写成归一化值，两个函数若先后作用于
同一批对象，直接读 `it.score` 拿到的会是 `[0, 1]` 而不是真实分数。

> 一句话选法：**同一栏里分数能直接比大小 → `score`；不能比 → `rotate`。**

### 加权

想给某个源加权，在 `config.yaml` 里写：

```yaml
rank:
  weights:
    Hacker News: 2        # 键是源的 name，值表示每轮多拿几个名额
```

## 六、已知边界

- **cron 不准点**：官方明确高负载期会延迟，所以主时间取 08:33 避开 UTC 00:00 峰尖，
  并配 09:07 作备份；幂等检查保证不会重复推送。
- **公开仓库 60 天无活动会停掉定时任务**：本流水线每天 commit 一次，天然规避。
- **Runner IP 是 Azure 网段**：抓 GitHub / HN 没问题；抓有 Cloudflare 严格防护的站点
  可能 403。那种情况需要自建 Runner。
- **Actions 单 job 上限 6 小时**：本项目单次运行通常 1-2 分钟，远不会触及。
- **GitHub ToS**：不得用于与软件项目无关的活动（挂代理、挖矿、刷量）。
  本项目属于「数据收集 + 报告生成」，在允许范围内。
- **搜索接口限速**：匿名 10 次/分钟、带 token 30 次/分钟。默认配置里没启用
  `github_search`，用到了才受此限制；一次运行每个源只发 1 个请求，通常远不触及。
- **单次最多返回 1000 条**：GitHub 搜索接口的硬限制，`per_page` 上限 100。
- **搜索接口跨限定符不支持 OR**：`topic:a OR b in:name` 这类写法实测直接返回
  0 条。覆盖范围不重叠的检索要拆成多个源，再靠跨分栏去重合并。
- **AI 接口不通时不会报错**：连续重试 3 次仍失败就降级为模板摘要，产物里会写
  「当前为无 AI 模式」。如果你看到这句话但明明配了 key，先看 Actions 日志里
  `[summarize]` 那几行的具体报错，多半是链路问题而不是配置问题。
- **本地网络抖动很常见**：实测同一台机器上，`github.com` 与中转站会同时
  出现 TLS 握手被掐（`SSLEOFError`）、过一会儿又恢复。本地调试失败不代表
  Actions 会失败——Runner 在 Azure 网段，链路完全不同。
- **AI 偶尔会自己再列一遍完整榜单**：模型有时不听话，在摘要末尾自己写一节
  「### 完整榜单」并把 24 条全复述成 Markdown 表格。渲染层做了三层兜底：
  ① 提示词里明令禁止写表格、禁止写「完整榜单」这类小节；
  ② `_strip_section_headings` 会把「完整榜单 / 完整排行 / 榜单 / 排行榜 /
  完整列表」这类小节整段丢掉（见 `render.py` 的 `AI_LIST_HEADINGS`）；
  ③ `md_to_html_fragment` 现在认得 Markdown 表格，真表格渲染成 `<table>`，
  没分隔行的伪表格老实降级成段落，不会再出现 `<p>| 排名 | ... |</p>` 的竖线乱码。
  即使模型哪天又犯，页面上也不会出现重复榜单或乱码。
