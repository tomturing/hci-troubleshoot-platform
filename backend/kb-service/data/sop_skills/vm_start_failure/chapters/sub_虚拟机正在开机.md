### 虚拟机正在开机

#### 现象描述
启动虚拟机失败；报错：虚拟机正在开机，请稍后重试！


#### 大量任务并发导致获取锁失败

##### 判断方法
登录任务失败对应的物理主机（如上截图是10.100.62.11）；cd到/sf/log/today/目录下，查看sfvt_vtpperlproxy.log和sfvt_vtpdaemon.log日志中存在大量vtp_process_op_workingX.lock failed!日志
```bash
# 查看对应日志
acli log get -E-k 'vtp_process_op_working.*failed!' -t ${YYYY-MM-DD} -f /sf/log/${date}/sfvt_vtpdaemon.log# 查看对应日志
acli log get -E-k 'vtp_process_op_working.*failed!' -t ${YYYY-MM-DD} -f /sf/log/${date}/sfvt_vtpdaemon.log
# 查看对应日志
acli log get -E-k 'vtp_process_op_working.*failed!' -t ${YYYY-MM-DD} -f /sf/log/${date}/sfvt_vtpdaemon.log
# 查看对应日志
acli log get -E-k 'vtp_process_op_working.*failed!' -t ${YYYY-MM-DD} -f /sf/log/${date}/sfvt_vtpdaemon.log
```


##### 解决方案（上升研发）
在该主机上kill掉虚拟机启动排队中UPID，释放OP锁；后由HA拉起虚拟机或者手动拉起虚拟机；
请拨打400或上升云BG-LMT技术支持
