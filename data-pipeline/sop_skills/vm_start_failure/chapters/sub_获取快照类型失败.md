### 获取快照类型失败

#### 现象描述
1、启动虚拟失败；描述：获取快照类型失败！错误码：0x0000000000FF1D84
- acli task get -k "获取快照类型失败" -t ${YYYY-MM-DD} -s -l 1
acli task get -k "获取快照类型失败" -t ${YYYY-MM-DD} -s -l 1
acli task get -k "获取快照类型失败" -t ${YYYY-MM-DD} -s -l 1


#### MySQL连接过多
（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、查看vtpdaemon.log日志，里面有获取虚拟机快照列表失败，请稍后重试！0x010003E6 ，上面有 mysql 连接失败的报错  Too many connections at
- acli log get -k "failed: Too many connections at" -p '/sf/log/${date}/' -f 'sfvt_vtpdaemon.log'
```bash
acli log get -k "failed: Too many connections at" -p '/sf/log/${date}/' -f 'sfvt_vtpdaemon.log'
```

2、去数据库主节点查看mysqld.log日志（在数据库主节点的/sf/log/today/mysqld.log）有连接数过多的日志：Too many connections
- ssh mysql-masteracli log get -k "Too many connections" -p '/sf/log/${date}/' -f 'mysqld.log'
```bash
ssh mysql-master
acli log get -k "Too many connections" -p '/sf/log/${date}/' -f 'mysqld.log'
ssh mysql-master
acli log get -k "Too many connections" -p '/sf/log/${date}/' -f 'mysqld.log'
```

3、cat /etc/hosts 发现数据库不在控制节点上（也就是有zk的节点上）
- acli system cat /etc/hosts |grep mysql-master|grep 'acloud.zk'
```bash
acli system cat /etc/hosts |
grep mysql-master|
grep 'acloud.zk'
acli system cat /etc/hosts |
grep mysql-master|
grep 'acloud.zk'
```


##### 解决方案
快速恢复方案：
界面切换控制节点，将数据库所在节点切换成控制节点
