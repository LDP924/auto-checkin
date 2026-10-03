# Auto Checkin · 五合一每日签到

森空岛 · 夸克网盘 · AppShare · QQ音乐 · 音贝任务 —— 零依赖纯 Python 标准库，专为阿里云效流水线设计的每日自动签到。

| 模块 | 功能 | 凭证 | 自动续期 |
|---|---|---|---|
| `skland_sign.py` | 森空岛：明日方舟 + 终末地（角色签到 + 登岛检票） | token 或 手机号+密码 | ✅ token 失效自动密码换新 |
| `quark_sign.py` | 夸克网盘：每日签到领空间 + 连签进度 | 抓包 kps/sign/vcode | ❌（过期需重新抓包） |
| `appshare_sign.py` | AppShare：每日签到（经验 + 分享值，连续签到升级奖励档） | 账号+密码（推荐）或 token | ✅ 每次自动登录换新 token |
| `qqmusic_sign.py` | QQ音乐：会员成长值五任务全自动（签到15 + 挂件5 + 装扮5 + 逛听书5 + 收听节目10） | qm_keyst（抓包一次，失效自动救活） | ✅ musickey 每次自动换新（无限续） |
| `yinbei_sign.py` | 音贝：开放平台任务中心（签到+邀请+互动/作品任务+周阶梯奖励，先探额度只补差量防风控） | 同 QM_KEY/QM_UIN | ✅ 同上 |
| `qmcommon.py` | QQ音乐系公共底座（u6 协议 / zzc 签名 / 令牌续期） | — | — |
| `main.py` | 统一调度 + Server酱/WeCom 聚合推送 | — | — |

五个业务模块各自独立完整，可单独运行，也可 `python main.py` 一键全跑。
所有接口的原始响应以 `[RAW][模块名]` 前缀打印（云效构建日志可见，便于排查；不进推送正文）。

## 快速开始

```bash
python main.py        # 五合一
python qqmusic_sign.py  # 单跑 QQ音乐
```

## 环境变量

缺哪个就自动跳过哪个模块，互不影响。

| 变量 | 说明 |
|---|---|
| `SKLAND_TOKENS` | 森空岛鹰角通行证 token（多账号逗号分隔；有效期约 30 天） |
| `SKLAND_PHONE` / `SKLAND_PASSWORD` | 鹰角通行证手机号+密码（配了即 token 失效自动续，推荐） |
| `SKLAND_DID` | 数美设备指纹（预注册值，防 CI 机房 IP 风控；失效时本地生成新值替换） |
| `COOKIE_QUARK` | 夸克网盘凭证，多账号换行或 `&&` 分隔：`user=张三; url=抓包的整段growth/reward链接`（或旧格式 `kps=xxx; sign=xxx; vcode=xxx`） |
| `APPSHARE_TOKEN` | AppShare 抓包 token（优先使用，直签） |
| `APPSHARE_ACCOUNT` / `APPSHARE_PASSWORD` | AppShare 账号+密码（token 失效或未配 token 时自动登录换新）。**需先在 App「账号安全」设置密码** |
| `APPSHARE_ANDROID_ID` | AppShare 设备 ID（可选，默认独立值避免与手机互踢） |
| `QM_KEY` | QQ音乐 Cookie 里的 qm_keyst 值（Q_H_L_ 开头；最小认证集：三键同值+QQ号，配一次永久续） |
| `QM_UIN` | QQ音乐 QQ 号（纯数字，音贝模块共用） |
| `QM_UA` | QQ音乐系业务接口 UA（抓包你的完整 UA；不配则用通用精简 UA） |
| `QM_PLAY_TPL_B64` | 可选。自定义「收听节目」播放上报模板（base64），覆盖内置的 cmd=2000049 真实样本模板；运行时自动刷新 qq/authst/tid/traceid/optime/sid/str3 |
| `QM_CACHE_DIR` | key 缓存目录（默认 /root/.cache，云效缓存） |
| `SERVERCHAN_SENDKEY` | Server酱 SendKey（免费 5 条/天，留空不用） |
| `WECOM_SECRET` | WeCom 家校通道（学校通知）推送：育人应用 secret。**配了即启用**，额度账号×200人次/天≈无限 |
| `WECOM_TOUSER` | 家长 userid（定向推送）；留空则广播给全部家长 |
| `WECOM_CORP_ID` / `WECOM_AGENT_ID` | WeCom 企业 ID / 应用 ID（配了 WECOM_SECRET 即必填） |

森空岛也支持 `creds.txt`（每行一个 token，已被 .gitignore 忽略）与 `config.json`（游戏过滤等高级配置），详见 [skland 模块文档](https://github.com/LDP924/skland-auto-sign)。

## QQ音乐五任务原理（实测）

- 签到 / 听书 / 挂件 / 装扮：`music.lvz.LevelConfigSvr` 直接领奖与行为上报；挂件需 Cancel→Use 成对制造当日动作，装扮用 UseCosmetic 并动态跟随当前装扮
- 收听节目：StartTask 挂开始标记 → 重放播放上报（内置 cmd=2000049 模板，songid+album 按日轮换防统计通道去重）→ W 态领取前先重挂 StartTask（拉开间隔避冷却）再 IssueAward，全程无人工
- 三态统一：N=执行动作 / W=进行中直接领取（能否领取决于行为已计次，StartTask 返回的 count 即播放量探针） / Y=零请求
- 领取定律（10-03 终案）：20020=领取冷却（撞到 3s 后原地重领）；30005=行为未计次（仅任务 20 触发 60s 兜底重领——池同步窗口 10-60s，每天仅两轮不等=+10 丢到明天；上报被去重则量永远不足，重领一次即弃）；10006=写操作锁（当日领取失败累计过多，次日恢复，勿连撞）；30009=已领（含跨天前的昨日延续——服务端 20 重置在 0 点后滚动执行，昨日未完成单被作废关单转 Y、奖励蒸发）
- 10-02 实测：冷却修复后挂件/装扮/听书 00:01 首次运行当场全到账
- 跨天陷阱：当日未签时 20:Y 是昨日残留（服务端不重置），强制重做

## 阿里云效部署

1. 新建流水线 → 每日定时（如 06:00）→ 构建命令：`python main.py`
2. 环境变量里配置上表凭证（云效「变量与缓存」→ 添加环境变量）
3. 源代码源选本仓库（直接 HTTPS 拉取即可，无需凭据）

## token 持久化（云效缓存目录）

森空岛 / AppShare / QQ音乐系的模块会把登录换来的 token 写进 `/root/.cache`（云效默认缓存目录，
跨次运行持久）：下次运行直接用缓存 token 签到，token 失效才自动登录换新。
**云效「变量和缓存 → 缓存」里把 `/root/.cache` 开启即可**，无需添加新目录。
QQ音乐系缓存形如 `<模块前缀>_<QQ号>.txt`；森空岛/AppShare 本地回落 `creds.txt` / `appshare_token.cache`（均已被 .gitignore 忽略）。

## 通知

每日推一条消息（Server酱 或 WeCom 家校通道），标题直接显示完成度「**自动签到完成 5/5**」，
聊天列表卡片一眼看完不用点开。有失败时标题点名失败模块（如「自动签到完成 4/5 · 失败:夸克网盘」），
正文为五个模块的签到明细（获得物品 / 容量 / 奖励明细 / 连签进度）。

判定规则：森空岛需「签到成功 且 检票成功」才算成功；其余模块成功输出即成功。
