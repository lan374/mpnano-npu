# debug 日志说明

## 输出与范围

天空模块自动接入SkyDebugLog；API默认在实际任务state_dir/logs/sky.debug.jsonl。子进程控制台由模块读入最近状态和有界diagnostic_tail，不要求另外开日志进程。CLI会直接输出初始化信息与周期JSON。

地面MP通常在%LOCALAPPDATA%/MpNano/P3/logs/ground.debug.jsonl；具体接收目录以receiver_start事件为准。两端各自写本地文件，日志不额外经HM30回传。没有记录SSH密码或图片二进制。

日志为UTF-8 JSONL，每行一个对象，utc为UTC、side区分sky/ground、run_id区分进程。按session＋seq＋sha256关联图片；run_id不要求两端相同。人工查看北京时间时加8小时，不把跨机UTC差视作精确链路时延。

## 主要事件

|端|事件|含义|
|---|---|---|
|天空|sender_started/stopped/failed|生命周期、路径及启动配置|
|天空|queue_status|队列和tcp最近概况，counts省略零项|
|天空|connect_attempt/connected/disconnected/pong|连接、失败及心跳，不等于射频指标|
|天空|image_send/image_ack/image_nack|逐图发送与匹配回应|
|天空|image_timing|限速等待、sendall、ACK等待、本地确认持久化|
|天空|loop_timing|扫描、准备、传输、等待累计；预览处理是prepare子项|
|地面|received/duplicate/rejected|持久接收、去重或拒绝|
|地面|save_timing|metadata/hash/jpeg/dedup/persist分段和save_total|
|地面|ack_written|ACK写入本机流，ack_write_ms不代表天空已收到|
|地面|connection_eof/connection_closed|帧边界关闭/异常关闭，不能据EOF推断用户意图|
|MP|display_session/displayed/display_skipped|选择/交给控件显示/显示数据异常跳过|
|MP|receiver_start/stop、window_visible、display_pause|监听与窗口行为|

保存总项包含子项，不重复相加。地面分段计时只记录成功完成样本，失败Save/ACK不能从成功统计推导。天空ACK等待含网络、地面处理和调度，不是纯RTT。receive_to_display_ms是地面等待，不是源图产生到显示的端到端延迟。

## 容量与可靠性

每侧默认4MiB轮转、当前加3份备份约16MiB；异步有界队列拥塞允许丢诊断，查看diagnostic_dropped_total。磁盘写失败不会以日志阻塞传输，但可能失去诊断。DEBUG最详细，INFO/WARNING更少；不改变已存在持久队列。

可靠送达以天空SQLite确认和地面持久record为准，不以日志条数代替。displayed表示交给控件，不证明人看清；回放会增加显示条数，不增加唯一接收数。日志不包含HM30 RSSI、真实射频丢包、MAVLink质量或DeepStream推理内部信息。

## 排查顺序

先记版本、配置、session与实际目录。无received先看天空连接和路由；有received无image_ack看断连/duplicate；有ACK无displayed看任务选择/隐藏/暂停/游标。保留两端日志和持久状态，再按明确任务归档；不要直接删状态重试。

普通进程退出尽力刷新日志，强杀/断电可能丢尾部。API初始化发生在日志建立前的错误看status的diagnostic_tail；CLI看终端stderr。本地归档的只读容量工具scripts-mis/check_storage.py不进入部署包；它不证明目录可写、配额可用或一轮飞行空间足够。
