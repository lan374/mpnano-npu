# sender 接入与使用教程

本文 192.0.2.0/24 地址仅用于文档示例；请替换为现场 HM30 有线网卡地址后执行。

## 1. 安装

要求 Python≥3.8、Pillow==10.4.0。不要搬运其他系统的虚拟环境，不修改系统 Python 或原 DeepStream 依赖。Ubuntu20.04/Python3.8/ARM64 是历史 CLI 测试环境；本次新增模块接口及 Ubuntu22.04 仍需目标机验证。

```bash
python3 -m venv .venv-mis
.venv-mis/bin/python -m pip install /实际路径/mpnano_sender-0.4.0-py3-none-any.whl
.venv-mis/bin/python -m sender --help
```

在源码目录可用 `python -m pip install .`。首次安装可能需要联网获取依赖；离线时要额外准备适配目标 Python/ARM64 的 Pillow wheel。sender 的通用 wheel 不包含平台依赖，py3-none-any 不代表所有 Jetson 环境已经测试。

主管线导入 sender 的解释器也需安装本模块及依赖。可通过 python_executable 指向专用解释器，发送子进程使用那个环境；那个环境同样必须装好模块。若完全不想给 DeepStream 环境增加导入依赖，使用后文 CLI，由管线管理独立进程。

## 2. 目录与写入约定

图片目录先创建，再传给 sender；新图可持续到达。任务开始为空、从0命名，如0.jpg、1.jpg，不要求固定帧率。源目录、状态目录、日志目录不能相等或互为上下级；不要在裁剪目录里创建 sender 状态。

stable 默认观察文件稳定并验证解码。若生产端写入中途长时间停顿，仍可能被当作坏图，不能视作绝对完成信号。atomic 只用于明确先写临时文件、完整后同文件系统重命名的生产端。实际插件写法未知时维持 stable。

## 3. 原管线接入

在创建本轮目录之后、进入识别循环之前启动一次，不要逐帧调用。start 等待初始化，适合任务初始化或管理线程，不适合视频逐帧回调。

```python
from sender import Sender, SenderConfig
config = SenderConfig.from_json('/opt/mpnano/sender.json')
worker = Sender(watch_dir='/data/current-flight/crops',
                state_dir='/data/mpnano-state',
                ground_host='192.0.2.30', config=config)
try:
    worker.start(timeout=10)
    actual_state = worker.state_dir
    # 将 actual_state 和 worker.status()['session_id'] 写入原管线任务信息。
    # 此处继续原识别流程；管理线程可定期查询 worker.status()。
finally:
    if not worker.stop(timeout=15):
        print('仍在停止，请保留句柄并再次调用 stop:', worker.state_dir)
```

start 成功只表示本地初始化成功，地面未连接时依然运行并重连。初始化错误抛异常；初始化超时会请求停止，但应保留 worker 并再次 stop 等待，不能当成已退出。

stop 协作退出，等正在执行的一步结束，超时返回 False，不自动强杀。网络默认总期限10秒，图片处理和磁盘操作也耗时，15秒不是最坏情况上限。先停止产图，再决定等图片全部确认或留队列稍后恢复；stop 不等于排空全部图片。

status 返回独立拷贝：lifecycle（created/starting/running/stopping/stopped/failed）、实际 state_dir；初始化后有 session_id、log_dir、run_id；queue 是最近约一秒的队列/tcp快照。counts 仅含非零状态，缺字段可能尚未上报。退出后看 exit_code 和 diagnostic_tail；旧 tcp.connected 只是最后采样，生命周期优先。

一个 Sender 对象只能 start 一次。地面当前为单活动连接，不建议多个发送器竞争同一接收器。无需 SSH 参数，模块不管理 SSH。

## 4. 新任务和恢复

默认 resume=False，在状态父目录下分配 flight-随机ID，不复用上一轮状态。短暂断网无需重建对象，sender自行重连。

进程重启恢复同一轮：传保存的实际状态目录，不能传父目录。

```python
worker = Sender('/data/current-flight/crops',
                '/data/mpnano-state/flight-实际ID',
                '192.0.2.30', config=config, resume=True)
worker.start()
```

resume 需要已有 queue.sqlite3，原图和路径仍属于同一轮。不要删除状态、手改数据库或给新的0号图片复用旧任务。同状态目录有独占锁，不允许两个发送进程同时使用。

## 5. CLI 入口

```bash
# 新任务：记下启动JSON中的实际state_dir
python -m sender --watch-dir /data/current-flight/crops --state-dir /data/mpnano-state --new-session --ground-host 192.0.2.30 --rate-kib 128
# 恢复：不要再次带--new-session
python -m sender --watch-dir /data/current-flight/crops --state-dir /data/mpnano-state/flight-实际ID --ground-host 192.0.2.30 --rate-kib 128
```

Ctrl+C 正常停止。API 的 stdin 停止通道是内部实现，调用方不应直接写入。CLI不自动读取JSON配置；JSON由SenderConfig.from_json加载。API默认创建新任务，而CLI只有显式--new-session才分配新子目录，勿混淆。

## 6. 现场运行和升级

检查 `ip route get 192.0.2.30` 确实走HM30网口；MP开始监听，防火墙放行限定来源/网口/程序。先少量图片检查回传及原分割读取，再增加负载；不能拿SSH连通替代链路验收。

更新代码前停止sender，保留运行状态和原图。安装位置与数据位置分开，记录版本、配置及任务ID。当前不自动创建系统服务、自启或删除历史任务。接入真实插件后需确认数据写入约定和资源占用。
