# StarRailCopilot-AccountSwitch

> 非官方、社区实现：给 [StarRailCopilot](https://github.com/LmeSzinc/StarRailCopilot) 增加**多账号自动切换 / 轮换**。
> 这是外部配套工具，**不是** StarRailCopilot（src）的一部分，不修改 src 的任何文件。

## 为什么有这个项目

StarRailCopilot 官方**明确不接受**账号切换功能：

- [Issue #212「自动账号切换」](https://github.com/LmeSzinc/StarRailCopilot/issues/212)
  （2023-12，作者 aaahai00，CONTRIBUTOR）：提议按手机号前缀/后缀 + 游戏内 UID 识别，
  启动后检测 UID 不匹配则自动切换，按调度器优先级处理多个 src 配置。
  Owner **LmeSzinc 2023-12-08 明确回复**：*「按照一贯的传统，不会添加任何与帐号切换有关的功能。」*
  该 issue 关闭时标记 `wontfix / 不做`。
- [Issue #833「增加切换账号功能」](https://github.com/LmeSzinc/StarRailCopilot/issues/833)
  （2025-05，作者 lesentropy）：按手机尾号预设账号、进游戏前切换。
  同样被标记 `wontfix / 不做`。
- 官方 [FAQ](https://aky.moe/StarRailCopilot/faq) 也写明：*「能否自动切换账号？**不能**」*。

> 说明：上游**没有**对应的 PR（用 GitHub 搜索 `type:pr` + 关键词「账号切换 / 多账号 / account switch /
> switch account / 账户 / #833」均 0 命中）——这些请求以 issue 提出并被拒绝，从未形成代码变更。
> 所以本仓库是对上游拒绝决定的**外部补全**，而不是把某个上游 PR 搬到外面。

## 设计思路（通用 repo，账号任意数量）

账号信息放在**外部 YAML**（`accounts.yaml`），与代码分离；运行时**智能补缺**：

- `uid`（游戏内 UID）由程序自己识别并回填——`init.py` 逐个 `switch` 进游戏，
  读取游戏画面 `UID:xxx` 写回 YAML。
- `phone_suffix`（手机号后 2 位）若缺失且 `phone_prefix` 在账号列表中**唯一**，
  就从登录页选中账号的脱敏号取尾 2 位智能补全；否则提示手动输入。
- 账号数量任意（当前 2 个，未来 N 个），`rotate.py` 按 YAML 列表顺序逐账号轮换。

本机相关配置（src 在哪、怎么启动、ADB 设备、日志目录等）全在 YAML 的 `global` 段，
由用户按自己机器填。

## 文件

| 文件 | 作用 |
|---|---|
| `accounts.example.yaml` | 配置模板（脱敏占位）。复制为 `accounts.yaml` 使用 |
| `init.py` | 初始化：建配置 → 逐账号智能填 `uid`/手机号后缀 → 回写 YAML → 生成 systemd service/timer → 装依赖 |
| `rotate.py` | 轮换主程序：读 YAML → 逐账号启动 src → 监控到结束 → 切下一账号 → 一轮跑完退出 |
| `hsr_account.py` | 底层：ADB/OCR 识别当前账号、按掩码切换账号（`switch`/`current`/`probe`/`verify-save`） |
| `requirements.txt` | 依赖：`pyyaml` |

## 快速开始（在目标机，如 Debian 12/13）

```bash
# 1. 复制配置模板并填写（重点：global.src_dir = 你本机 StarRailCopilot 路径）
cp accounts.example.yaml accounts.yaml
vim accounts.yaml        # 填 src_dir、adb_serial、accounts 里各账号的 name/phone_prefix

# 2. 一键初始化：
#    自动逐账号启动游戏识别 UID 并回填 accounts.yaml
#    按唯一前缀智能补手机号后缀
#    生成 ~/.config/systemd/user/starrail-rotate.{service,timer}
#    安装依赖（pip install pyyaml）
#    启用 systemd timer（默认每天 04:00）
python3 init.py

# 3. 手动跑一轮试试（不等 timer）
python3 rotate.py
```

`accounts.yaml` 包含真实手机号/UID，**已被 `.gitignore` 排除，绝不提交**；
仓库只带脱敏占位的 `accounts.example.yaml`。

## 配置字段

`global`（本机相关）：

| 字段 | 说明 |
|---|---|
| `src_dir` | StarRailCopilot 源码目录（你本机路径） |
| `venv_python` | src 的 venv python（相对 `src_dir` 或绝对路径） |
| `adb_serial` | 目标安卓设备 ADB serial（如 `192.168.1.110:16132`） |
| `adb` | adb 可执行（留空用 PATH） |
| `log_dir` | src 日志目录（相对 `src_dir`） |
| `package` | 游戏包名（默认 `com.miHoYo.hkrpg`） |
| `poll_sec` / `stale_sec` / `timeout_sec` | 轮询/无更新判定/单账号超时（秒） |

`accounts[]`（任意数量，顺序即轮换顺序）：

| 字段 | 说明 |
|---|---|
| `name` | 账号标识（`switch`/`current` 用它） |
| `phone_prefix` | 手机号前 3 位 |
| `phone_suffix` | 手机号后 2 位（`init.py` 按唯一前缀智能补） |
| `uid` | 游戏内 UID（`init.py` 自动识别填充） |
| `src_profile` | src 使用的配置档案（src 的 `config/<profile>`） |

## 轮换逻辑（rotate.py）

1. 读 `accounts.yaml`，取当前账号（游戏内 UID / 登录页选中掩码识别）。
2. 从当前账号开始，按列表顺序每个账号跑一轮：关 app → 启动 `src` 档案进程 →
   监控日志（`Request human takeover` / `Close game during wait` 标记、无更新、进程退出）→ 杀进程。
3. 每账号结束 `switch` 到下一账号；全部跑完退出（timer 次日再触发）。

systemd（user）由 `init.py` 生成：`starrail-rotate.timer` 每天 `04:00` 触发
`starrail-rotate.service`（oneshot，`TimeoutStartSec=240min`）。

## 免责

- 账号切换涉及模拟器/游戏界面自动化，**风险自负**；请先在自己设备上实测。
- 本工具只负责切换与轮换，具体刷本/养成逻辑仍由 StarRailCopilot 执行。
- 与上游无任何关系；上游拒绝该功能，本项目为独立社区实现。
