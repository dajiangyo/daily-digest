# 每日排行 + AI 速览 · 无人值守流水线

不开电脑、不用 VPS，每天定时抓取全站项目排行，交给 AI 总结成中文晨报，
自动提交进仓库、部署成网页、推送到你手机上。

## 它每天做什么

```
cron 触发（北京时间 08:33 主 / 09:07 备份，均避开整点）
        ↓
多源抓取（GitHub Trending ×3 + Hacker News + 恋活插件 ×3）
        ↓
跨分栏去重 → 按分栏排名 → 各自出榜
        ↓
AI 总结（DeepSeek / OpenAI / Moonshot / 智谱 / 本地 Ollama）
        ↓
渲染 Markdown + HTML（综合排行 + 恋活插件排行）
        ↓
写文件 → commit 回仓库 → Pages 自动部署 → 推送通知
        ↓
记录 state.json（当天已成功则后续时间点自动跳过）
```

产物里有两份独立榜单：

| 分栏 | 内容 | 排序方式 |
|---|---|---|
| 综合排行 | GitHub Trending（全语言/Python/TypeScript）+ Hacker News | 轮转交错，防止单一源刷屏 |
| 恋活插件排行 | Koikatsu 插件生态（topic + 关键词 + 官方组织三路检索） | 直接按总星标降序 |

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
| `GENERIC_WEBHOOK_URL` | 自建服务 / n8n / Node-RED | 渠道任选 |

推送渠道**配一个就够**，没配的会自动跳过，不会让任务失败。

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
```

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

Trending 只反映「当天新增 star」，成熟但冷门的生态（比如恋活插件）永远上不了榜。
想追话题就用搜索接口，按总星标排序：

```yaml
- type: github_search
  name: 恋活插件 · 标签
  query: "topic:koikatsu"        # 任意 GitHub 搜索语法
  sort: stars                    # stars / updated / forks
  limit: 40                      # 最大 100
  min_stars: 0                   # 客户端过滤，低于该星数丢弃
  pushed_within_days: 365        # 可选：只留最近 N 天有推送的
  section: koikatsu
```

想换成别的话题只改 `query`：`topic:stable-diffusion`、`org:xxx`、
`"vrm in:name,description"` 都行。

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

恋活栏的三个源都是 GitHub 总星标，量纲一样。这时轮转是**错的**——
实测踩到过：108★ 的 `IllusionMods/HSPlugins` 排到了 958★ 的
`ManlyMarco/KK-HF_Patch` 前面，只因为它来自另一个源。

所以 `rank_by_score()` 直接按星标降序。这里有个坑值得记下来：
排序键读的是 `extra["raw_score"]` 而**不是** `it.score`——因为
`dedupe_rank()` 会把 `it.score` 原地改写成归一化值，两个函数若先后作用于
同一批对象，直接读 `it.score` 拿到的会是 `[0, 1]` 而不是真实星标。

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
- **搜索接口限速**：匿名 10 次/分钟、带 token 30 次/分钟。本项目的恋活栏
  用了 3 个搜索源，一次运行只发 3 个请求，远不触及。但如果把
  `pushed_within_days` 设得很小导致某次搜索返回 0 条，不会重试到触发限速。
- **单次最多返回 1000 条**：GitHub 搜索接口的硬限制，`per_page` 上限 100。
  恋活生态总共 200 多个仓库，够用。
