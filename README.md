# mpnano-sender

本文 192.0.2.0/24 地址仅用于文档示例；请替换为现场 HM30 有线网卡地址后执行。

Jetson 裁剪目录 → SIYI HM30 TCP → MissionPlanner 四图窗口。

本仓库维护天空端模块。读取原管线提供的图片目录，后台按序发送预览，不改写或删除原图。地面 UI 由独立 MissionPlanner 仓库维护。

```python
from sender import Sender, SenderConfig
worker = Sender('/data/flight/crops', '/data/mpnano-state', '192.0.2.30',
                config=SenderConfig(rate_kib=128))
worker.start()                    # 本地初始化成功，不代表地面在线
print(worker.state_dir)          # 保存实际任务路径，恢复时需要
print(worker.status())          # 最近队列快照
finished = worker.stop(15)       # False 表示仍在退出，保留句柄继续等待
```

发行名 `mpnano-sender`，导入名 `sender`，Python ≥3.8，Pillow 10.4.0。安装 `python -m pip install .`，或安装构建的 wheel。模块内部管理独立发送进程，继续使用已验收的 CLI/队列/协议实现，不在识别回调中等待网络。

## 文档入口

- [项目与联合方案](docs/项目与联合方案.md)
- [sender 接入与使用教程](docs/sender接入与使用教程.md)
- [sender 参数配置说明](docs/sender参数配置说明.md)
- [地面站使用教程](docs/地面站使用教程.md)
- [测试总结与验收边界](docs/测试总结与验收边界.md)
- [潜在优化方案](docs/潜在优化方案.md)
- [debug 日志说明](docs/debug日志说明.md)

## 仓库范围

`sender/` 为正式模块，`examples/` 为配置示例，`docs/` 为正式文档。根目录 `build_sender.py` 构建 wheel 和文档包；wheel 只包含 sender，不含模拟器、测试、MP、receiver 或现场数据。

```bash
python -m pip install "setuptools>=61,<76" wheel
python build_sender.py
```

输出位于 `dist-mis/sender/`。也可直接 `python -m pip install .` 安装源码。

本地开发工具、回归测试、备份及历史资料分别保存在 `scripts-mis/`、`tests-mis/`、`backups-mis/` 以及其他 `*-mis/` 目录，均不提交。旧测试脚本保留原始路径引用，归档后不能直接照搬历史命令运行；公开仓库不包含历史测试工具或原始证据，测试结论与边界见文档。

`receiver/` 暂保留原路径且不提交，因为另一个 MP 工作区仍引用此接收核心。`runtime/` 与新产生的 `artifacts/` 正被另一轮 MP 联调使用，暂保留原路径且忽略，待联调停止后再改为 `-mis`。它们都不是天空端安装依赖。从此仓库克隆不能单独构建完整 MP，地面端需由其独立仓库提供。

0.4.0 是测试候选模块，完整现场验收尚未结束；本次模块接口回归与历史 HM30 结果分开说明。
