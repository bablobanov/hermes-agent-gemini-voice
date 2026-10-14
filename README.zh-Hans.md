# Hermes Agent Gemini Voice

![Hermes Agent Gemini Voice：通过 Gemini Live 在 Android 手机上与你的 Hermes Agent 对话](assets/hermes-agent-gemini-live-voice-android.svg)

[English](README.md) · [Русский](README.ru.md) · [安全](SECURITY.md) · [更新日志](CHANGELOG.md)

通过 **Android 手机上的 Gemini 应用**与你的 Hermes Agent 对话：走路时、开车时，无需打开聊天窗口。对 Gemini 说
*“新建文档：Hermes，比较一下周五飞柏林最便宜的三个航班”*。Hermes 会发现这个文档，用它平常的工具和技能完成任务，
并把答案写回同一个文档。之后你对 Gemini 说 *“读一下文档 Hermes 航班”*，就能听到结果。

不需要新应用、不需要 Gemini API 密钥、不需要公网端点：通道就是你自己的 Google 日历、文档和表格。Hermes 通过插件
自己的 Google OAuth 令牌读取它们，Gemini 应用负责朗读。

```text
Gemini 应用 ──新建──► 标题含 "Hermes" 的日历事件 / 文档 / 表格（你的账号）
                              │ 插件每分钟轮询一次
                              ▼
        gemini-voice ──任务──► Hermes 网关（同一个会话，沿用你的工具和规则）
                              │ 最终回复
                              ▼
        回复写回同一个对象 ──► 在 Gemini 中说“读一下文档 …”
```

## 功能

- **任务输入。** 标题中包含标签词（默认 `Hermes`）的新 Google 日历事件、文档或表格会成为发给 Hermes 的消息。插件
  先写入 `[Hermes] got it, working on it`，完成后替换为 `[Hermes] reply:` 和答案
- **摘要输出。** 任何 Hermes 定时任务（cron）都可以投递到新文档（`gemini_voice:doc`）或指定时间的日历事件
  （`gemini_voice:event-0800`），于是早间简报就变成一句 *“我早上 8 点有什么安排？”*
- **同一个对话。** 所有语音任务进入同一个会话，就像私聊。Hermes 需要你做决定时会在回复里提问，你再口述一个新任务来回答
- **只认你自己的对象。** 邀请、别人共享给你的文档、别人加到你日历上的事件一律忽略，即使标题里有标签
  （见[安全与披露](#安全与披露)）

### Gemini 应用的行为（作者实测，2026 年 10 月）

- 一个任务 = 一个新对象，一句话说完：*“新建文档 Hermes …，内容是 …”*、*“明天 10 点新建事件：Hermes …”*、
  *“新建表格 Hermes 购物：牛奶、面包、奶酪”*
- Gemini 不能编辑已有文档，也不能往已有表格里加行，所以对话通过新对象继续
- Gemini 不会主动通知回复已就绪，需要时你自己去问。日历事件可以用自然的问法：*“我明天 10 点有什么安排？”*

## 前提条件

- Hermes Agent **0.21.6 或更高版本**，并运行消息网关（本插件是一个网关平台）。已在 0.21.6 上实机测试；
  引擎契约测试（52 项检查）在 0.21.6 和 0.22.0 上均通过
- 本插件需要 Gemini 应用、Google 账号及 Google 服务，这些在中国大陆不可用
- 手机上 Gemini 应用所使用的 Google 账号
- 该账号自己的 Google Cloud 项目（免费），启用 **Google Calendar、Google Drive、Google Docs 和 Google Sheets API**，
  并创建一个类型为 **“桌面应用”（Desktop app）的 OAuth 客户端**
- 在 OAuth 同意屏幕上点击 **Publish app**（状态为 “In production”）。若保持 “Testing” 状态，Google 会在 7 天后使
  刷新令牌失效，通道会在一周后静默停止工作。仅供自己账号使用时，未经验证的应用也可以，授权时会看到一次 Google 的
  “未验证应用”提示

## 安装

1. 安装并启用插件（从插件目录安装，或克隆本仓库并把 `plugins/gemini-voice` 复制到 Hermes 插件目录）：

   ```sh
   hermes plugins install gemini-voice
   hermes plugins enable gemini-voice
   ```

2. 在 Hermes 主机的终端里为插件授权一次 Google 访问。准备好 OAuth 客户端 JSON 文件，或在提示时粘贴客户端 ID 和密钥：

   ```sh
   hermes gemini-voice auth --client-secret-file <PATH_TO_CLIENT_JSON>
   ```

   命令会输出一个 Google 授权链接。用登录了手机上那个 Google 账号的浏览器打开它，允许全部四项权限，然后复制无法打开的
   页面地址（`http://localhost:1/…`）并粘贴回来。命令会把 `GEMINI_VOICE_CLIENT_ID`、`GEMINI_VOICE_CLIENT_SECRET` 和
   `GEMINI_VOICE_REFRESH_TOKEN` 写入 Hermes 的 `.env`，不会打印任何密钥。

3. 检查并用你平常的方式重启网关：

   ```sh
   hermes gemini-voice check
   ```

   预期输出：`TOKEN_OK scopes: calendar.events.owned drive.metadata.readonly documents spreadsheets`，且 Calendar、
   Drive、Docs、Sheets 四个 API 都显示 `OK`。

4. `config.yaml` 中的可选设置（以下为默认值）：

   ```yaml
   plugins:
     entries:
       gemini-voice:
         settings:
           labels: [Hermes]          # "Herm*" 匹配任何以 "Herm" 开头的词
           language: en              # en | ru，插件自身状态行的语言
           timezone: UTC             # IANA 时区名，用于状态时间和 event-HHMM 投递
           poll_seconds: 60
           max_age_hours: 24         # 更早创建的对象永远不会当作任务
           answer_timeout_minutes: 40
           max_tasks_per_day: 30
           sources: [calendar, docs, sheets]
   ```

   语音任务使用该平台的工具集，配置方式与其他平台相同（`platform_toolsets.gemini_voice`，默认
   `hermes-gemini_voice`）。如果希望语音拥有与主聊天相同的能力，就给它配置相同的工具。

## 使用

| 对 Gemini 说 | 结果 |
|---|---|
| “新建文档：Hermes，帮我起草一封给房东的暖气问题回复” | Hermes 把草稿写进该文档 |
| “明天 9 点新建事件：Hermes，去护照办公室要带什么” | 答案写进事件描述 |
| “新建表格 Hermes 开销：油费 3000” | Hermes 处理后在下面几行回复 |
| “读一下文档 Hermes 房东” / “我明天 9 点有什么安排？” | Gemini 朗读回复 |
| “新建文档：Hermes，可以，发出去吧” | 回答 Hermes 在上一条回复中提出的问题 |

把定时任务的输出投递到适合语音收听的目标：

```sh
hermes cron create "30 7 * * *" "Morning briefing: mail, calendar, tasks" \
  --name "Briefing" --deliver gemini_voice:event-0800 --failure-deliver local
```

`gemini_voice:doc` 会新建一个标题为 `[Hermes digest] <输出的第一行>` 的文档，正文也以这一行开头。`gemini_voice:event-HHMM`
会在该本地时间新建一个 15 分钟的事件（同一天再次投递时更新它）。只写 `gemini_voice` 时投递到 `GEMINI_VOICE_HOME_CHANNEL`
（`doc` 或 `event-HHMM`；其他值会改为 `doc` 并在日志中警告），未设置该变量时不会投递。该变量会把目标设为平台的主频道，而
Hermes 也会向主频道发送自己的通知：计划重启后的“重新上线”消息会变成一个新文档（关机通知会被插件拒绝）。不设置它，网关的服务通知
就不会进入你的 Google 账号。带附件的输出会被拒绝，不会只投递一半。语音会话本身不是投递目标。当你用语音要求以后或定期做某事
（*“每天 8 点给我发 …”*）时，Hermes 会被告知用这两个目标之一创建定时任务。

## 安全与披露

- **网络：** 只访问 Google API（Calendar、Drive、Docs、Sheets）和 `oauth2.googleapis.com`，每次轮询和每个任务各一次。
  没有遥测，不访问其他主机
- **凭据：** 插件只使用三个 `GEMINI_VOICE_*` 变量中它自己的 OAuth 客户端和刷新令牌，从不读取其他工具的令牌文件。访问
  令牌只保存在内存中；唯一写入凭据的地方是由你运行的 `hermes gemini-voice auth`，并通过 Hermes 自带的 `.env` 写入函数完成
- **Google 权限范围：** `calendar.events.owned`、`drive.metadata.readonly`、`documents`、`spreadsheets`。没有 Gmail，
  没有完整的 Drive 访问权。插件读取文件元数据以发现新对象，读取带标签对象的文本，只写入任务对象以及它的定时投递所创建的对象
- **谁能下达任务：** 能在这个 Google 账号里创建对象的人，通常就是拿着装有 Gemini 应用的你的手机的人。只有账号所有者本人
  创建的对象才算数：日历事件必须是所有者创建并组织的单次事件、没有其他宾客且之后未被任何人修改；文档和表格必须归你所有、
  未共享且最后由你修改。任务开始前会再次检查归属。正因如此，该平台注册为 `trusted_inbound`：事件来自你授权的 Google 账号，
  而不是来自其他人
- **日历编辑权限：** Google 不会告诉你是谁修改了事件。事件创建后两分钟内的修改视为 Gemini 应用在补全事件；此后事件的标题、时间
  和描述必须保持插件第一次看到时的样子（颜色等其他字段可以变），否则任务不会执行，并在事件中写明原因。你授予了主日历“更改活动”
  权限的人，仍可能在事件创建后的前两分钟内修改它：不要把这项权限给别人，或者在 `sources` 中去掉 `calendar`。任务开始前的归属检查、
  任务文本的读取和确认的写入都基于事件或文档的同一个版本（其间有改动就重新检查）
- **你的文字绝不会被删除。** 插件只在它写入的位置替换它自己写入的那一段（文档中按记录的位置，事件中只在描述的最末尾，表格中
  按记录的行），从不靠搜索 `[Hermes]` 查找；如果那里有变化，回复会改为追加。在表格中，回复行通过一次原子更新插入到确认所在的
  位置，绝不会覆盖已有的行；插件自己的确认单元格只在 Google 服务端精确匹配时才会被清空：如果你改过它，你的修改保留，紧挨在回复下方
- **与其他聊天相同的工具和规则。** 插件不增加自己的审批，也不绕过任何审批。需要人工批准的命令无法通过语音批准：Hermes 会
  等待 `approvals.timeout`（默认 300 秒）后拒绝它；请在你的消息应用中重新发出这类请求。`clarify` 在此平台被禁用，因为回合
  进行中没人能回答
- **引擎内部接口：** 除了文档化的插件 API，插件还导入 `gateway.session_context.get_session_env`（`pre_tool_call`
  钩子不会说明工具调用发生在哪个平台，而 `clarify` 只在本平台被禁用），并读取适配器基类、网关运行器和网关事件上的几个
  下划线属性：语音会话的回合是否仍在进行、网关是否在重启后恢复回合、流式输出的最终文本、状态写入。0.22.0 的
  `hermes plugins validate` 会把这个导入列为目录规则 9 的警告。引擎契约测试覆盖了以上每一项：升级 Hermes 之前，请先用新版本
  运行它（见[测试](#测试)）
- **后台工作：** 平台连接期间，网关进程内有一个轮询任务
- **不会有消息自行晚到。** 语音会话只接收任务的最终回复。后台进程不会唤醒它；从其他聊天发给它的消息、附件（文件、图片、
  音频）和审批请求都会被拒绝，绝不会报告为已送达。以后要看的结果通过定时投递写入新文档或事件（见[使用](#使用)）
- **至多一次：** 每个对象在交给网关之前都会先记录，并且绝不会再次交出。任务逐个执行：只有网关完成上一个回合后，下一个才开始。
  重启后，新任务会等网关先恢复被中断的回合；Hermes 会在该对象中说明任务被中断并询问是否继续（用一个新任务回答）。没有锚点的
  回复只会写入这个被中断的任务，绝不会写进别的对象。最终回复一到达就会存入插件的日志，因此在写入 Google 之前重启也不会丢失；
  网关在崩溃后重新发送的回复从不决定写入哪个对象（插件已持有的会被确认，
  其他的一律拒绝）。若在 `answer_timeout_minutes` 内没有回复，对象里会写明任务未完成
- **摘要永远不是任务：** 定时投递创建的文档在标题和第一行都带有 `[Hermes digest]`，日历事件带有私有标记，因此输出永远不会被
  当作你的指令读回，即使其标题含有标签
- **无人值守：** 令牌被撤销或过期时绝不会打开浏览器授权流程。平台会在网关状态中报告 `retrying / auth_failed`
  （`gateway_state.json`、`hermes gateway status`），记录一行日志，每 15 分钟重新检查一次，网关照常运行

## 状态与排障

```sh
hermes gemini-voice status   # 平台状态、上次成功轮询时间、任务计数（不含标题和正文）
hermes gemini-voice check    # 令牌、权限范围和四个 API
```

| 状态码 | 含义与处理 |
|---|---|
| `auth_failed` | 令牌被撤销、过期或缺少权限：运行 `hermes gemini-voice auth`，然后重启网关 |
| `google_unreachable` | 网络或 Google 故障：轮询以最长 15 分钟的退避间隔重试；只有一个 API 故障时，其他照常工作。Google 不响应的任务会以逐渐加长的间隔重试约 45 分钟，然后以一条说明关闭，说明会在 Google 恢复响应后立即写入 |
| `api_disabled` | 在 OAuth 客户端所属的 Cloud 项目中启用这四个 API |

限制：每个 Hermes 配置档对应一个 Google 账号，每个账号一个轮询器；任务逐个执行；重复事件永远不是任务；没有附件，只有文字；
表格最多读取到第 500 行和 Z 列（任务中总会注明），回复写入第一个工作表的 A 列，最多 300 行；日历事件描述中的回复最多
7,000 个字符；轮到执行时已超过 `max_age_hours` 的任务会被跳过并写明原因；默认每天最多 30 个任务。

## 测试

```sh
python -B -m unittest discover -s plugins/gemini-voice/tests -t plugins/gemini-voice/tests -v
```

单元测试不需要网络，也不需要 Hermes，使用内存中的 Google 模拟。可选的引擎契约测试会通过真实的 Hermes 检出加载插件，
没有检出时跳过（跳过不等于通过）：

```sh
GEMINI_VOICE_TEST_ENGINE_ROOT=<HERMES_CHECKOUT> GEMINI_VOICE_TEST_ENGINE_PYTHON=<HERMES_PYTHON> \
  python -B -m unittest discover -s plugins/gemini-voice/tests -t plugins/gemini-voice/tests -p test_engine_contract.py -v
```

在由 Hermes 环境管理器管理的安装（0.21.6 及更高版本）中，引擎的依赖只能通过其启动器看到：不要指定 Python，
通过启动器运行测试。

```sh
GEMINI_VOICE_TEST_ENGINE_ROOT=<HERMES_CHECKOUT> \
  <HERMES_CHECKOUT>/.hermes/bin/hermes --run-module unittest discover -s plugins/gemini-voice/tests -t plugins/gemini-voice/tests -v
```

## 项目

- `plugins/gemini-voice/`：插件本体（仅用标准库，无 Python 依赖）及其测试
- [更新日志](CHANGELOG.md) · [贡献指南](CONTRIBUTING.md) · [安全](SECURITY.md) · [MIT 许可证](LICENSE)

MIT © 2026 Ilya Balobanov。独立集成，并非 Google 或 Nous Research 的官方产品。Android、Gemini 和 Google 是 Google LLC 的商标。

The Android robot is reproduced or modified from work created and shared by Google and used according to terms described in the Creative Commons 3.0 Attribution License.
