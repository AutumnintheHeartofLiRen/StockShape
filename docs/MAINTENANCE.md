# 使用与维护

## 本地数据与桌面使用

依赖和启动命令见根目录 README，所有命令在项目目录执行。默认路径跟随项目目录；桌面也支持用 `STOCKSHAPE_ROOT` 明确指定根目录。

首次建立行情库，先查看参数再下载：

```powershell
.\.venv\Scripts\python.exe build_full_a_history_v2.py --help
.\.venv\Scripts\python.exe build_full_a_history_v2.py --root .\data\full_a
```

数据来自 AKShare，保持非复权。构建器默认跳过已有有效文件；`--force` 会重新下载，`--merge` 才生成总池。行情不随源码分发，使用者自行建立和维护自己的库。

建库后点击桌面的“更新本地数据”，或运行：

```powershell
.\.venv\Scripts\python.exe update_full_a_daily_v2.py
```

更新只补已有股票文件，新股仍用建库脚本补入。面板显示准备阶段、逐股数量、百分比、当前股票与预计剩余时间；100% 表示处理完毕，仍需查看失败数量。更新中不要关闭程序。

搜索、自选和 K 线不依赖私有策略。图表先按完整历史计算均线再裁剪显示范围，不足相应周期时显示缺失。

## AI 研究

V1.4 在桌面“AI 研究助手”中提供云端/本地模型设置、对话、工具过程和停止操作，详细用法见 [AI_AGENT.md](AI_AGENT.md)。本地可选择 Ollama、LM Studio 或自定义回环服务，检测连接并读取模型列表，通常无需密钥；先在对应运行器下载/加载支持工具调用的模型。模型列表可读不代表推理或工具调用已验证。设置保存到 `.env.local`；未配置服务不影响普通功能。远端 HTTPS、本机回环可 HTTP。

AI 仅读取有限本地行情及任务交易日匹配的已保存判定，不更新数据、不执行私有模型。数据过期时用普通桌面按钮更新或分析，再重新研究；研究和更新/分析不能同时启动。对话只在窗口内存中，未接入笔记、持久记忆、新闻或财报库。

云端收到问题、有限上下文和查询摘要；本地模式请求发送给本机服务，是否离线取决于运行器和所选模型。私有源码、参数与完整指标不自动发送。密钥在本机文件中明文保存，不要提交或附在问题中。非空进程 `AI_*` 变量会覆盖本地配置，配置修改未生效时先检查环境变量。

## 私有策略与结果

私有入口为 `private/strategy_engine.py`，由 `Store` 加载 `create_engine(store)`。缺少入口文件或有效 `strategy_config.json` 时分析按钮禁用，不会猜测参数或自动生成原策略。预检不执行插件；接口或模型依赖损坏时在启动分析后显示错误。

获得策略包后按维护者提供的清单安装，重新启动。切换本地参数后重新分析，结果保存本次参数快照；旧参数结果在界面标明。自选保存在 `desktop_data/watchlist.json`，分析结果保存在 `desktop_data/analysis_watchlist.*`、`analysis_all.*`。取消分析不保存部分结果。

私有模型及原脚本依赖仍可能含原机器路径；迁移时按包内说明检查。公开框架的启动和行情更新不要求固定磁盘路径。

## 测试与构建

```powershell
# 公开测试使用临时目录、合成参数和模拟数据源，不请求行情接口
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v

# 只有安装了维护者的私有回归资料时才执行
.\.venv\Scripts\python.exe -B -m unittest discover -s private/tests -v

# 重新构建本机桌面壳，先关闭旧 EXE
.\.venv\Scripts\python.exe -m pip install pyinstaller==6.22.3
.\.venv\Scripts\python.exe tools\build_desktop.py
```

EXE 位于 `dist/StockShape/StockShape.exe`，后台任务仍使用项目的 `.venv/Scripts/python.exe`。保留同目录 `_internal`、项目脚本和数据；仅复制 EXE 不能构成独立安装包。公开二进制应在公开导出目录重新构建，不能把本机目录整体打包上传。

测试使用合成数据和模拟模型服务，不消耗模型额度，也不能代替真实行情和云端模型的兼容性验收。修改数据读取、日期切分、工具权限或策略适配后，增加对应边界回归；普通文档修改不需要重跑全市场。

## 更新与备份

界面、图表、数据服务、后台协议分别位于 `desktop/main.py`、`chart.py`、`services.py`、`worker.py`。AI 界面和独立任务位于 `desktop/agent_panel.py`、`agent_worker.py`，配置、接口、循环和只读工具位于 `research_agent/`。私有选股规则只在策略包中修改，公开服务和提示词不复制规则。

备份公开源码以及本机 `private/`、真实配置、完整行情库、自选和研究记录。原模型脚本暂在根目录，也要作为私有包备份。`.venv`、`dist` 和 `build` 可重建。`.env.local` 含模型配置和密钥，应单独安全备份；当前未接入系统凭据库或密钥加密。

每次发布重新执行[公开导出检查](PUBLISHING.md)，新增文件不会自动进入白名单。
