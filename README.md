# BundleHelper

BundleHelper 是一个 Windows 桌面工具，用于通过 Git bundle 在本地仓库与离线 Linux 服务器之间同步全部分支和 Tag，并可将本地仓库推送到 GitHub。

## 环境

- Windows 10/11 x64
- Git for Windows 2.30+（优先使用其自带 OpenSSH 工具集）
- Windows OpenSSH Client（仅在 Git 工具集不可用时回退）
- 服务器安装 Git 和 `sha256sum`

## 开发运行

```powershell
$env:UV_CACHE_DIR="$PWD\.uv-cache"
uv sync --extra dev
uv run bundlehelper
```

如果 uv 的默认 Python 安装目录无权限，可显式设置：

```powershell
$env:UV_PYTHON_INSTALL_DIR="$PWD\.uv-python"
uv python install 3.12
uv sync --extra dev
```

## 使用流程

1. 新建项目并填写 GitHub HTTPS 地址、本地目录、SSH 信息和服务器目录。
2. 在 Windows 凭据管理器中保存 GitHub Token（私有仓库需要）。
3. 点击“检查 SSH”确认主机指纹。
4. 依次执行“首次全量部署”“拉取服务器增量”和“Push 到 GitHub”。

服务器仓库在首次部署后不再依赖 bundle 文件。服务器上的修改必须先提交；工具只接受新增分支、新增 Tag 和分支快进，不支持 LFS、submodule、分支删除或历史改写。

## 测试

```powershell
uv run pytest
```

## 打包

```powershell
uv run pyinstaller --noconfirm --clean scripts/bundlehelper.spec
```

产物位于 `dist/BundleHelper/`。

## SSH 兼容性

工具会优先使用 Git for Windows 安装目录中的 `ssh`、`scp`、`ssh-keyscan` 和 `ssh-keygen`，避免旧版 Windows 系统 OpenSSH 不支持较新密钥交换算法而出现 `unsupported KEX method`。

如果 Git 安装在非标准位置，可设置 `BUNDLEHELPER_SSH_DIR` 指向一个同时包含上述四个程序的目录，例如：

```powershell
$env:BUNDLEHELPER_SSH_DIR = 'C:\Program Files\Git\usr\bin'
```

修改环境变量后重新启动 BundleHelper。
