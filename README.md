# Codex Rewind Desktop

为 **Windows Codex 桌面端**添加回复操作栏回退图标，支持聊天回退、文件快照恢复和撤销回退。

**当前为实验性本地扩展。图标通过增强启动窗口加载，仅安装 MCP 插件不会改变普通窗口。**

## 快速开始

前提：已安装官方 Windows Codex 桌面端、Python 3.10+、Node.js 22+，并且 `python` 与 `node` 已加入 PATH。

1. 下载本仓库或 `git clone`，保留整个目录。
2. 双击 `NATIVE_START.cmd`，打开增强窗口；它与普通窗口共享本地聊天和账号，使用独立的界面配置目录。
3. 打开一个已完成回复的本地聊天，把鼠标放到助手回复附近。
4. **复制 → 回退箭头 → 更多**：点击目标回复的箭头，预览范围并确认。
5. 目标回复与此前聊天保留，后续轮次从新分支移除。原聊天完整保留。
6. 回退分支的箭头会切换为“撤销回退”，确认后恢复原来的聊天与文件。

普通窗口没有图标时，请使用 `NATIVE_START.cmd` 打开的窗口。重复运行入口会打开增强窗口并复用服务。正在生成的回复需要先完成；图标位于已完成回复的操作栏。

## 文件快照

- 启用后，为当前聊天的完整回复自动记录文件快照，每个回复只记录一次；发送下一条消息前也会补录。
- 启用前的历史回复没有对应文件快照时，提供“仅回退聊天”。快照记录后发生的手动改动会作为冲突处理。
- 普通文件和二进制文件按 SHA-256 内容存储。恢复前检查全部受影响路径，发现额外修改就停止。
- 默认只处理聊天工作目录，跳过 `.git`、`node_modules`、`.venv`、符号链接和 junction。
- 空目录、ACL、ADS、外部服务和正在写入的数据库不属于文件内容快照。共享工作目录中的其他修改可能被纳入快照；请避免并发写入。
- 快照与恢复日志保存在 `%LOCALAPPDATA%\CodexRewindDesktop`；没有自动清理界面。

## 停止与卸载

双击 `NATIVE_STOP.cmd` 移除增强窗口的图标并停止桥接；快照保留。关闭增强窗口会同时关闭它的本地调试端口。

可选 MCP 插件：双击 `INSTALL.cmd`。安装器通过桌面端原生插件管理器添加本地市场，并生成适合本机的 `.mcp.json`。安装后保留源码目录，移动后重新安装。仅使用原生图标无需安装 MCP。

双击 `UNINSTALL.cmd` 停止图标并移除 MCP 插件与本地市场。Git Bash 用户可运行 `./ROLLBACK.sh`。

`START.cmd` 为原有手动检查点面板，可独立使用。

## 实现与兼容性

使用官方桌面端自带的 `codex.exe app-server`，通过原生 `thread/fork` 创建保留目标回复的分支，通过原聊天实现 redo；使用持久化恢复日志处理文件写入中断。

原生图标通过只监听 **127.0.0.1** 的 Electron 调试端口和受限桥接注入。默认端口 `54732`。本地调试接口允许本机程序访问窗口内容，因此只在使用增强窗口期间保持开启。程序不修改 `app.asar` 或 CSP。

已验证桌面包 **26.1111.9664.0**、自带 app-server **0.169.1**。回复操作栏依赖内部界面结构，其他版本或未来更新可能需要适配。本版只支持本地、paginated 格式的空闲 Codex 聊天。

## 开发与验证

Python 标准库实现，无 pip 依赖；Node 使用内置 WebSocket。

```powershell
# 全部测试：需要 Windows 与已安装的 Codex 桌面端
python -m unittest discover -v
node --check native.js
node --check native_cdp.mjs

# 独立文件/聊天引擎测试，不连接真实聊天
python -m unittest -v test_snapshot test_rewind test_native_flow
```

真实 app-server 集成测试会创建专用测试聊天并使用少量模型额度：

```powershell
python verify_native.py --live --run-dir C:\temp\codex-rewind-test
```

验证结果：21 项自动化测试通过；真实桌面界面验证了图标位置、回退后仅保留目标回复、文件恢复和撤销回退。文件快照从启用时开始记录，不重建未保存的历史文件。

## 可复用 Codex 指令

```text
使用 codex-rewind-desktop 管理我指定的本地聊天。
先读取真实聊天 ID 与空闲状态；手动模式任务开始前 checkpoint，完成后 seal。
回退或 redo 前展示 preview 的目标、文件清单与冲突，经确认后使用一次性 token apply。
出现中断记录时先检查状态并恢复操作前文件。保留原聊天，使用真实文件快照恢复。
```

## 致谢

原生回复栏定位研究参考 [CodexPlusPlus](https://github.com/search?q=CodexPlusPlus&type=repositories)，研究版本 commit `458893b5e697d280bb5e096d96939f9210122c0c`。本仓库界面与桥接独立实现。非 OpenAI 官方项目。

## 许可证

MIT，见 [LICENSE](LICENSE)。
