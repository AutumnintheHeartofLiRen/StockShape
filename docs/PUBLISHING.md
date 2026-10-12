# GitHub 发布与私有策略

## 发布方式

公开桌面、行情、图表、自选和通用服务；原策略实现、真实参数和研究资料保留私有。仅隐藏参数不够：源码中的默认值、规则判断、测试快照和文档同样会暴露策略。本项目已提取本地策略插件，并移走真实回归和模型说明。

这种拆分保留的是原策略，不是防复制授权系统。获得策略包的人仍可复制，公开框架也允许接入其他策略。如果以后需要账号授权、到期或撤销权限，让核心计算留在你控制的服务端，客户端只调用鉴权接口。

## 生成公开源码

```powershell
# 只读检查白名单文件与忽略规则
.\.venv\Scripts\python.exe tools\prepare_public_release.py --check

# 导出到新的空目录；默认 build/public-release
.\.venv\Scripts\python.exe tools\prepare_public_release.py
```

如果上次目录存在且非空，工具会拒绝覆盖；可选择新的 `build` 子目录：

```powershell
.\.venv\Scripts\python.exe tools\prepare_public_release.py --output build/public-release-next
```

工具只复制显式白名单，不创建 Git 仓库、不上传，不删除本机文件。**新建 GitHub 仓库时使用导出的目录，不直接把当前本机工作目录上传。** 导出后可在该目录创建新的 Git 历史，查看待提交清单，再连接你选择的远程仓库。

白名单不包含 `private/`、真实 `strategy_config.json`、`.env.local`、行情、自选、分析结果、研究参考、旧模型脚本、构建产物和虚拟环境。参数示例为 `null`，没有原策略缺省值；公开测试只用合成参数。

`.gitignore` 是第二层防误提交。已经被 Git 跟踪的文件不受它影响；误提交后仅添加忽略规则或删除最新文件不能抹去历史。[Git 官方说明](https://git-scm.com/docs/gitignore)

当前本机目录没有 Git 历史，本次没有初始化仓库或上传。检查针对显式发布边界，不是自动识别所有秘密的扫描器；新增公开文档、截图、数据样例时仍要查看实际内容。

## 首次上传

在 [GitHub 新建仓库](https://github.com/new)，填写仓库名称与说明，按公开发布目标选择 Public。首次创建保持空仓库，不勾选 README、`.gitignore` 和许可证初始化项，避免与本地首次提交产生不同历史。操作依据见 [GitHub 导入说明](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)。

在 PowerShell 中进入已检查的公开导出目录，然后执行：

```powershell
git init -b main
git add .
git diff --cached --name-only
```

查看暂存文件清单，确认只有公开框架、示例与测试，再提交。将下面远程地址替换成刚创建仓库的 HTTPS 地址：

```powershell
git commit -m "Initial public release: StockShape V1.4"
git remote add origin "https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git"
git remote -v
git push -u origin main
```

首次提交需要配置 Git 的 `user.name` 和 `user.email`。已配置时可直接使用；提交邮箱会写入 Git 历史，按自己的公开身份选择。首次推送可按 Git Credential Manager 提示在浏览器登录；命令行不能使用 GitHub 账户密码代替令牌，见 [GitHub 认证说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github)。不要把令牌放入远程地址、脚本或聊天。

后续更新保留首次发布目录的 `.git`，先把更新的公开源码同步到这个仓库，再提交和推送。当前导出工具拒绝覆盖非空目录，应先导出到新的空目录，再按公开白名单同步；不要将整个私有工作目录复制进发布仓库。

## 私有策略包约定

为兼容本机原流程，下列相对路径组成当前私有包，全部排除公开导出：

| 路径 | 内容 |
| --- | --- |
| `private/strategy_engine.py` | 插件入口与完整分析适配 |
| 根目录四个旧模型源码、`run_frozen_model_daily.py` | 原模型依赖与脚本流程 |
| `strategy_config.json` | 真实参数、原值与变更记录 |
| `private/docs/`、`private/tests/` | 维护者的说明与真实回归 |

使用原策略需要匹配版本的入口、依赖和参数，而非单个数值。私有测试与个人数据不必随使用包分发，不要把完整本机目录当作策略包。安装后重新启动桌面程序。

程序不会自动下载私有策略。AI 研究助手从本机 `.env.local` 读取模型设置，仅在用户点击“开始研究”时调用所配置的模型服务；密钥和真实配置不进入公开导出。使用方法见 [AI 助手说明](AI_AGENT.md)，服务边界见[架构说明](ARCHITECTURE.md)。

## 许可证

公开框架与私有策略分开决定授权。公开仓库按开源方式供别人使用、修改和分发，需要明确的开源许可证；本次尚未替维护者选择或添加 `LICENSE`。[GitHub 许可证说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)

选定后把 `LICENSE` 加入导出白名单。公开框架的开源许可证不再限制“必须向我索要参数才能运行框架”；原策略通过不公开、不随包分发来保留，私有策略包的许可单独说明。
