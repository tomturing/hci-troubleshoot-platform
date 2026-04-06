### 更新网卡设备信息失败：服务不可用

#### 现象描述
1、执行开机任务失败，报错描述：创建虚拟机网卡连接失败
- acli task get -k '创建虚拟机网卡连接失败' -t ${YYYY-MM-DD} -s -1
acli task get -k '创建虚拟机网卡连接失败' -t ${YYYY-MM-DD} -s -1
acli task get -k '创建虚拟机网卡连接失败' -t ${YYYY-MM-DD} -s -1


#### 版本问题
业务方代码逻辑导致的死锁(参考KB：超融合HCI-深信服技术支持）

##### 判断方法
- 1、查看/sf/log/${date}/vn/vn-manager-service-api.log，搜索：DB Deadlock sql exception。
- acli log get -k 'DB Deadlock sql exception' -t ${YYYY-MM-DD} -p /sf/log/${date}/vn/vn-manager-service-api.log
```bash
acli log get -k 'DB Deadlock sql exception' -t ${YYYY-MM-DD} -p /sf/log/${date}/vn/vn-manager-service-api.log
```


##### 解决方案
- 1、重启数据库服务可以恢复
- acli service asv mysql-managerd restartacli service asv mysql restart
```bash
acli service asv mysql-managerd restart
acli service asv mysql restart
acli service asv mysql-managerd restart
acli service asv mysql restart
```

#### 网卡异常
- 在对应时间网卡出现短暂的异常，导致数据库服务Mysql的IO超时，造成虚拟网络服务连接数据库异常，进而导致虚拟机开机更新网卡异常导致虚拟机无法正常启动（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、kernel.log中堆栈显示pfifo_fast_reset 或 p_eth0(igb):transmit queue 0 timed out
```bash
acli log get -E -k 'pfifo_fast_reset|p_eth0(igb):transmit queue 0 timed out' -t ${YYYY-MM-DD} -f /sf/log/${date}/kernel.log
```


##### 解决方案
- 快速恢复方案
- 待数据库服务恢复后，在主控上执行重启虚拟网络管理面服务，重启该服务不影响业务
- acli service anet vn-manager-service-api restart
```bash
acli service anet vn-manager-service-api restart
```
