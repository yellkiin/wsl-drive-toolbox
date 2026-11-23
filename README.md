# WSL 与移动硬盘工具箱

一个 Windows 桌面工具。它可以把 WSL 中的命令行程序接到当前 Windows 代理，也可以查找移动硬盘上的占用，请求 Windows 安全弹出。

**[下载 v0.2.0 · Windows x64](https://github.com/yellkiin/wsl-drive-toolbox/releases/download/v0.2.0/WSL-Drive-Toolbox-v0.2.0-windows-x64.zip)** · [发布记录与 SHA-256](https://github.com/yellkiin/wsl-drive-toolbox/releases/tag/v0.2.0)

解压到内置磁盘，双击 **`一键连接WSL.exe`**。同一个 EXE 包含代理和硬盘两个页面。Windows 端支持 Windows 10/11 x64，运行发布包无需另装 Python。代理功能需要已安装 WSL，目标发行版内有 Bash 或 Zsh 和 `python3` 3.8 或以上版本。

## 第一次连接 WSL

1. 在 Windows 上打开要使用的代理软件。
2. 在工具的「WSL 代理」页选择**发行版**。如果你有两套 Ubuntu，它们各自独立。在平时使用的 WSL 终端运行 `echo "$WSL_DISTRO_NAME"` 可以查看当前发行版名。
3. 点击「读取用户」，选择**平时运行命令的 Linux 用户**。界面会标出这个发行版的默认登录用户。在平时工作的终端运行 `whoami` 就能核对。每个账号有自己的主目录和应用配置，选择 `root` 只会配置管理员自己的环境。
4. 如果要同步 WSL 中的 Codex CLI，勾选「同时更新 WSL 内的 Codex .env」。点击「一键连接 WSL」，看到外网验证通过后，打开新终端再启动应用。

下次双击 EXE 会沿用上次的发行版、普通用户和 Codex 选项，并重新检测代理。切换到另一个 Windows 代理软件后也可这样操作。检测优先采用经验证的当前 Windows 系统代理。如果有多个可用端口且无法自动确定，可在「可用代理」中选中目标行再连接。

![WSL 代理页示意](docs/images/universal-proxy-demo.png)

截图里的 `developer` 是演示账号。实际使用时，列表显示你所选发行版里的账号。

## 端口与应用

WSL 应用使用所选用户固定的本地 HTTP 入口。工具把新连接转发到 Windows 代理当前监听的端口，因此应用显示的本地端口与 Windows 软件显示的端口可以不同。关闭窗口后入口可以继续工作，WSL 重启后由新的受管理 shell 按需启动。「关闭代理」可停用入口，「回滚配置」可恢复上次工具管理的配置。首次配置、关闭或回滚后，需要重开使用这些设置的终端和应用。

| 场景 | 工具如何处理 |
| --- | --- |
| 代理发现 | 验证实际监听和转发，支持无认证的 HTTP CONNECT、HTTPS 代理、SOCKS5。可使用「完整检测」检查更多本机监听，或手动指定地址。 |
| Git 固定使用旧端口 | 连接时检查当前用户的全局 Git `http.proxy`。能确认属于本次或旧代理的本地设置会迁移到统一入口。带认证、按站点设置、来源不明的覆盖需单独核对。 |
| 应用是否真的经过代理 | 点「工具联网检查」分别检查 curl、Git、pip、npm、Node。工具会同时看请求结果和本地入口是否观察到该目标的新连接。项目配置、`no_proxy`、软件自己的代理均可能改变路径。 |
| WSL 中的 Codex CLI | 可选同步其代理环境，并检查配置是否一致。模型登录和 API 调用需要在 Codex 中验证。 |
| NAT、PAC、TUN 或认证代理 | 在 NAT 模式下，WSL 需要能访问 Windows 代理的监听地址。使用 PAC、认证代理或纯 TUN 的环境，需要代理程序提供可达的无认证 HTTP/SOCKS5 入口，或者使用其原生路由。 |

工具配置 Bash/Zsh 用户会话。APT、systemd 服务、Docker 容器以及 Windows 桌面应用有各自的网络配置。

## 弹出移动硬盘

在「移动硬盘」页核对型号、容量和盘符，点击「查看占用」。如果部分进程无法读取，可以点击「管理员扫描」扩大范围。保存工作后点击「正常关闭占用并重试」，核对将关闭的程序、WSL 和需要停止的服务。收到 Windows 允许安全移除的结果后再拔盘。服务选择、资源管理器标签页和系统拒绝原因详见[硬盘占用说明](docs/disk-occupancy.md)。

![硬盘占用页示意](docs/images/disk-occupancy-demo.png)

## 从源码运行与打包

在 Windows 上安装带 Tk 的 Python 3.10 或更新版本，然后在仓库根目录运行。源码使用 Python 标准库。

```powershell
python start.py                  # 打开工具箱
python start.py --quick-connect  # 使用上次选择重新检测并连接
python start.py --demo           # 使用演示数据
python -m toolbox --help         # 查看扫描、连接、诊断等命令行参数
python -m unittest discover -s tests -v
```

使用 Windows x64 Python 和 PowerShell 7 构建。第一组命令在仓库根目录生成与发布包同名的 EXE。

```powershell
python -m pip install ".[build]"
./scripts/build.ps1
```

发布时使用 `./scripts/build.ps1 -DistPath dist -ReleaseZip`。输出包括 EXE、含 EXE 和许可证的 ZIP，以及 ZIP 的 SHA-256 文件。GitHub Actions 在 Windows 和 Ubuntu 上运行测试，然后用同一脚本生成 Windows x64 发布包。

项目使用 [MIT 许可证](LICENSE)。界面截图使用演示数据。
