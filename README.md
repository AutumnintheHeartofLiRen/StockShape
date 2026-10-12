# StockShape

个人 A 股研究桌面框架，基于 Python、PySide6 和本地日线数据。当前 V1.4 支持股票搜索、自选管理、日 K 与 MA5/10/20/40、行情增量更新、后台选股分析和 **AI 研究助手**，模型可连接云端 API 或本地服务。

**公开仓库提供应用框架，原选股策略单独分发。** 真实参数、核心模型、个人行情库、候选名单和复盘资料不随源码公开。没有私有策略包仍可使用行情、图表、自选和 AI 的公开数据查询；生成新的选股分析需要私有策略。

## 启动

Windows，Python 3.12。在项目目录运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe start_desktop.py
```

之后可双击 `启动StockShape.vbs`。首次没有行情数据时，按[维护说明](docs/MAINTENANCE.md)建立本地股票库。

打开“AI 研究助手”的“模型设置”，选择“云端 API”或“本地模型”。云端填写 OpenAI 兼容地址、模型和密钥；本地可选择 Ollama、LM Studio 或自定义服务，启动本机服务后读取模型列表，回环地址可不填密钥。模型需支持工具调用，连接检测只检查列表接口。权重下载和加载由本地运行器管理。配置保存在本机 `.env.local`；未配置模型时，普通桌面功能仍可使用。详见 [AI 助手使用说明](docs/AI_AGENT.md)。

需要原策略时，向项目维护者获取匹配版本的私有策略包和 `strategy_config.json`，按包内说明安装后重新启动。`strategy_config.example.json` 只说明字段，不提供有效参数；只填写参数不能代替核心模型。

## 文件约定

| 位置 | 用途 | 是否公开 |
| --- | --- | --- |
| `desktop/`、`research_agent/`、行情脚本及构建工具 | 桌面、只读 Agent 和数据服务 | 是 |
| `docs/`、`tests/` | 使用说明、合成数据和模拟接口测试 | 是 |
| `private/`、`strategy_config.json` | 私有模型、真实参数与回归资料 | 否 |
| `data/`、`desktop_data/`、研究资料目录 | 行情、自选、结果和笔记 | 否 |
| `.env.local` | AI 服务设置和密钥，明文保存在本机 | 否 |

本地旧模型脚本仍保留在原位置以兼容原流程，均已排除公开导出。发布时使用白名单工具生成独立目录，不直接上传整个工作目录。

## 维护与路线

- [AI 助手使用说明](docs/AI_AGENT.md)：配置、研究范围和数据边界。
- [使用与维护](docs/MAINTENANCE.md)：数据、测试、桌面构建与备份。
- [GitHub 发布](docs/PUBLISHING.md)：公开边界、导出、私有包和许可证。
- [架构说明](docs/ARCHITECTURE.md)：当前模块和工具协议。
- [AI Agent 路线](docs/AI_ROADMAP.md)：研究记录、资料检索及后续动作。

公开部分的许可证尚未选定，发布前应补充所选 `LICENSE`。私有策略不随公开框架发布。
