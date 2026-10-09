# sender 参数配置说明

SenderConfig支持Python关键字和from_json。配置在进程启动时生效，不支持热更新；未知键、非法类型和非有限数字报错。部署调试不混入SSH或模拟器参数。

## 接入参数

|参数|说明|
|---|---|
|watch_dir|原管线传入的本轮图片目录，必须存在|
|state_dir|新任务为状态父目录；resume=True为已有任务实际目录|
|ground_host|地面有线IP/可解析主机名，必填|
|config|SenderConfig；省略使用默认值|
|resume|默认False新建任务；True要求已有queue.sqlite3|
|python_executable|默认sys.executable；另一个解释器也须安装模块和依赖|

API日志位于实际state_dir/logs；CLI另支持--log-dir，必须与源目录分离且一个实例独占。

## 运行参数

|字段|默认|范围/单位|意义及调试注意|
|---|---:|---|---|
|port|8765|整数1～65535|与地面监听一致|
|mode|stable|stable/atomic|atomic仅用于明确完整后原子发布|
|scan_interval|0.2|秒≥0.02|扫描节流及无进展等待，不是逐图固定等待|
|stable_seconds|0.2|秒≥0|稳定观察窗口，太短易读到未完成文件|
|longest_edge|640|整数1～4096|预览最长边，不改变原图|
|jpeg_quality|75|整数1～95|预览JPEG质量，低值节省流量但可能丢细节|
|max_ready|16|正整数|就绪预览缓存张数，不是全部任务图片数|
|max_cache_mib|128|正数，至少1字节|MiB=1024²字节；单图超预算会阻塞准备|
|max_retries|3|非负整数|初次之外的坏图重试，默认最多4次尝试|
|retry_seconds|1|秒>0|内容失败间隔；网络重连另有退避|
|full_scan_seconds|30|秒>0|周期完整发现，过大会延迟特殊到达图的发现|
|gap_seconds|30|秒>0|已有更高序号时等待缺号；不用于自动结束任务|
|network_timeout|10|秒>0|网络相关总期限，弱网过短易反复重连|
|heartbeat_seconds|5|秒>0|空闲活性检查周期，不是射频丢包指标|
|rate_kib|128|KiB/s>0|应用帧限速，含元数据，不含TCP/射频开销|
|log_level|DEBUG|DEBUG/INFO/WARNING|DEBUG含逐图诊断和计时|

布尔值不能作为数值。CLI参数把下划线换成连字符，如--rate-kib。上表描述模块API，历史CLI部分错误会在底层校验。

```json
{
  "port": 8765,
  "mode": "stable",
  "scan_interval": 0.2,
  "stable_seconds": 0.2,
  "longest_edge": 640,
  "jpeg_quality": 75,
  "rate_kib": 128,
  "log_level": "DEBUG"
}
```

先使用默认值，一次只改一个因素。历史台架256KiB/s是候选值，不意味着当前无线环境、飞控共存或其他尺寸图片也适合，默认仍128。画质和尺寸改变只影响预览；恢复时尽量保持本轮原配置，既有缓存身份不会任意改写。

稳定等待、内容重试和断网恢复是不同机制。不要为了“快”把超时设得很小，也不要把断线当坏图耗尽重试。MP观看间隔独立设置。

底层默认源图上限32MiB、像素上限16×1024²，协议图片payload上限2MiB，地面尺寸上限4096×4096。这些不是本版JSON可配置字段。超过限制应检查图像规格，不直接移除保护。

CLI还保留--session-id、--new-session、--cycles等诊断入口；前两者互斥，cycles是调度轮数，不是秒数或图片数。省略--ground-host只缓存不传输。它们不属于部署配置JSON。
