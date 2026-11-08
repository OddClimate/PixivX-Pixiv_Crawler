# PixivX
<img width="1242" height="852" alt="image" src="https://github.com/user-attachments/assets/af5038a0-3190-48fc-8b04-75c37dba41d5" />

Pixiv 日榜 / 周榜 / 月榜 / 新人榜 桌面客户端，支持勾选下载、原图预览、代理与免代理镜像源
> !部分AI代码注意 仅用于个人学习与备份，请遵守 Pixiv 的服务条款，并积极支持原插画画师

## 功能

- 日榜 / 周榜 / 月榜 / 新人榜 图片下载
- 图片预览
- 可配置 HTTP / SOCKS5 代理

## 直接下载使用（无需 Python）

Windows 10/11 x64 用户直接运行：
**release/PixivX.exe**
程序已内置 Python 运行时和全部依赖，双击即可运行：

> 首次启动可能需要几秒等待

## 安装并运行

源码保留在仓库中，方便后续修改和二次开发

- Python 3.10+
- Windows

```bash
pip install -r requirements.txt
python pixiv_client.py
```

## 使用说明

点击标题栏右侧的设置：

| 项目 | 说明 |
| --- | --- |
| 代理 | 例如 `http://127.0.0.1:7890` 或 `socks5://127.0.0.1:7891`，可点"测试"验证 |
| 镜像榜单接口 | 默认 `https://pix.ipv4.host` |
| 镜像图片域名 | 默认 `https://i.pixiv.re`，用于替换 `i.pximg.net` 下载原图 |

切换软件主题：点击右上角的开关

> 由于镜像源本身的原因，镜像源不支持新人榜

## 项目结构

```
pixiv_client.py   # GUI 主程序
pixiv_api.py      # 榜单抓取、原图地址解析、图片下载
pixiv.ico         # 应用图标
build_exe.bat     # 打包脚本
```

## 已知限制

- 仅支持 Windows
- 只下载原创作品的原图，不做登录态、收藏、关注等操作

## 喜欢的话也请点一下Star，这对我非常重要！

## License

本项目采用 [MIT License](LICENSE)。

Copyright (c) 2026 OddClimate

你可以自由使用、修改和分发本软件，但需保留上述版权声明和许可声明。
