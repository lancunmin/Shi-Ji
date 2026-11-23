# 拾迹 · shiji

**中文互联网公开信息查人工具。** 给它一个昵称、手机号、邮箱、姓名+单位，它去把这个人散落在微信、B站、微博、知乎、小红书、贴吧、企业/司法/学术公开库里的痕迹捡起来，判断是不是同一个人，最后给一份**每条结论都能点开验证**的分级报告。

零依赖（只用 Python 标准库），可以直接当 Agent Skill 用，也可以当命令行工具用。

```bash
git clone https://github.com/lancunmin/shiji.git
cd shiji
python scripts/shiji.py doctor
```

---

## 为什么又做一个

市面上的 ID 检索工具（sherlock、maigret 这一系）解决的是**「这个用户名在哪些站存在」**。真正查人的时候你会发现这不够用：

| 常见失败 | 拾迹怎么处理 |
| --- | --- |
| 只会告诉你「某站有这个 ID」 | 要求进入**内容层**：签名、投稿标题、公众号文章、公示名单——「主页存在」不算证据 |
| 查到手机号/群号就停了 | 内置**扩展算子表**：每个新线索对应一组下一步查询（`references/pivot.md`） |
| 中文站全都搜不到 | 先用 `doctor` 查**网络出口**：域名解析到 `198.18.x` 说明流量走了代理，平台按异常网络拦（这是最常见的原因，见 `references/access.md`） |
| 结论全是 0.62 猜测 | 换成**五档判据**（已核实/高度可能/推测/线索/已排除），档位由证据类型决定，不写伪精确的概率 |
| 跑很久还被风控 | 引擎轮换 + 熔断冷却 + 请求预算 + 缓存；被封就换源，不反复重试 |

## 能拿到什么，拿不到什么

**能（实测可用）**

- **B站**：昵称 → mid → 签名/粉丝/投稿数/视频标题（签名里常有邮箱、微博、粉丝群号）
- **微信公众号**：文章标题 + 摘要 + 公众号名 + 日期
- **网页检索**：Bing 中国 / 360 / DuckDuckGo 多引擎合并去重，支持按域名硬过滤
- **官方公开库**：政府网搜索、百度学术、天眼查移动版（交叉验证用）
- **报告**：证据台账 → Markdown 报告，分五档、带出处

**拿不到（站点硬墙，不硬闯）**

知乎、微博、小红书、抖音、贴吧的**正文**；企查查/爱企查/知网的检索结果。

这些站要么登录墙，要么验证码，要么 `robots.txt` 直接写 `Disallow: /`。拾迹的做法是生成一份**人工检索清单**（`links` 命令，17 个平台的深链），你点开、把内容贴回来，它接着推理。

> 我们不做打码、签名逆向、账号池、代理池。理由写在 [`references/access.md`](references/access.md)：法律风险、维护寿命、以及「破了签名拿到的仍是公开内容」——**破解只买到速度，买不到内容**。

## 命令

```bash
python scripts/shiji.py doctor                        # 网络出口 + 信源自检（先跑这个）
python scripts/shiji.py dossier "昵称"                 # 一条命令出画像卡片（推荐入口）
python scripts/shiji.py search "关键词" --limit 8      # 多引擎检索
python scripts/shiji.py search "关键词" --host zhihu.com   # 只要某站结果
python scripts/shiji.py wx "关键词" --fetch 1          # 公众号：元数据 + 尝试取正文
python scripts/shiji.py bili-user "昵称"               # B站：昵称 → mid/签名/粉丝
python scripts/shiji.py bili-video "关键词"            # B站：视频（UP 主/播放/时间）
python scripts/shiji.py bili-space 517327498          # B站：mid → 简介/投稿数
python scripts/shiji.py fetch URL --max-chars 4000    # 抓正文
python scripts/shiji.py variants "昵称"                # 昵称/ID 变体扩展
python scripts/shiji.py links "关键词" --out handoff.md     # 人工检索清单
python scripts/shiji.py ledger add --claim "..." --verdict confirmed --url ... --quote "..."
python scripts/shiji.py ledger render --out report.md --subject "目标"
```

`dossier` 做的事：搜昵称 → 列出账号矩阵 → **从签名里自动抽「大号/QQ群/邮箱/手机/公众号」** → 顺着「大号」再查一层 → 查公众号 → 网页结果做**整词去噪**（中文被拆成单字的结果直接丢掉）→ 合成一段「一眼结论」。一条命令出卡片，不用人拼。

全部支持 `--json`，方便接到别的流程里。

## 当 Agent Skill 用

仓库根目录的 `SKILL.md` 就是技能描述。放进你的 skills 目录（Claude Code、DSH 或任何支持 Agent Skills 的 harness），说「查一下这个人 / 这个 ID 是谁 / 核查一下网传身份」就会触发。

技能里写死了几条纪律：

1. 先 `doctor` 再搜（一半的「搜不到」是网络问题）。
2. 只有「主页存在」不能写进结论。
3. 每条事实都要能点开验证，落不到链接的写「未证实」。
4. 两轮没有新线索就停手写报告，不硬凑。
5. 不查未成年人，不做骚扰/跟踪/开盒，报告里手机号邮箱默认打码。

## 目录

```
shiji/
├── SKILL.md                 # 技能主流程（立案 → 检索 → 扩展 → 验证 → 报告）
├── scripts/shiji.py         # 零依赖 CLI：检索/微信/B站/正文/变体/台账/自检
├── references/
│   ├── sources.md           # 信源分级表 + 实测可达性
│   ├── query-syntax.md      # 中文检索的四个杠杆与分词陷阱
│   ├── pivot.md             # 线索扩展算子表（20+ 条）
│   ├── evidence.md          # 五档判据 + 同名消歧四步
│   ├── report.md            # 报告模板
│   ├── access.md            # 反爬实测、网络出口诊断、不做清单
│   └── casebook.md          # 两个完整走法（信息充足 / 线索稀疏）
└── evals/evals.json         # 6 条质量评测用例
```

## 常见问题

**Q：搜什么都返回验证码？**
先看是不是代理接管了国内流量。`doctor` 会直接告诉你：域名解析到 `198.18.x` 就是 fake-ip。断开代理或用分流，比换工具有效。

**Q：公众号正文抓不到？**
微信文章现在由 JS 渲染，脚本只能拿到标题、摘要、公众号名、日期。正文请在浏览器打开（`wx` 输出的链接可以直接点），或让用户贴回来。

**Q：B站接口 412？**
第一次请求前要先访问一次 B 站首页拿 `buvid3`，脚本已内置。仍失败就是被限流了，等一会儿。

**Q：能查手机号归属地、身份证吗？**
归属地可以用公开号段规则做粗判；身份证、住址、行踪这类**不做**，也不建议任何人做。

## 合规

仅用于检索公开可访问的信息，服务于自查数字足迹、身份核实、尽调取证等正当用途。
不用于人肉、跟踪、骚扰、开盒；不查未成年人；不采集身份证号、精确住址、行踪等敏感信息。
使用者需自行确保用途符合当地法律（在中国大陆，注意《个人信息保护法》《网络安全法》的相关要求）。作者不对使用者的行为负责。

## License

MIT
