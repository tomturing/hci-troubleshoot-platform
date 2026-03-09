### 服务不可用，可能由于系统繁忙导致

#### 现象描述
启动虚拟机，状态：失败。描述：服务不可用，可能由于系统繁忙导致，请刷新页面重试。如果问题一直持续，请联系系统管理员或技术支持处理。错误码：0x0CFFFFFF。
- acli task get -k "可能由于系统繁忙导致" -t ${YYYY-MM-DD} -s -1acli task get -k "可能由于系统繁忙导致" -t ${YYYY-MM-DD} -s -1
acli task get -k "可能由于系统繁忙导致" -t ${YYYY-MM-DD} -s -1
acli task get -k "可能由于系统繁忙导致" -t ${YYYY-MM-DD} -s -1


#### redis oom
（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、检查sfvt_vtpdaemon.log日志，存在redis oom报错：OOM command not allowed when used memory > 'maxmemory'
- acli log get -k 'OOM command not allowed when used memory' -p /sf/log/${date}/ -f sfvt_vtpdaemon.logacli log get -k 'OOM command not allowed when used memory' -p /sf/log/${date}/ -f sfvt_vtpdaemon.log
```bash
acli log get -k 'OOM command not allowed when used memory' -p /sf/log/${date}/ -f sfvt_vtpdaemon.log
```


##### 解决方案
1、重启redis-server服务恢复：
680以前版本：
- /sf/etc/init.d/redis-server restart/sf/etc/init.d/redis-server restart
```bash
/sf/etc/init.d/redis-server restart
```
680及以上版本：
```bash
acli service asv redis-server restart
```

#### vn-node-agent-api内存占用过多

##### 判断方法
1、检查vn-node-agent-api.log日志，报错：Too man
```bash
acli log get -k 'Too man' -p /sf/log/${date} -f vn-node-agent-api.log
```

##### 解决方案
快速恢复方案：
1、重启vn-node-agent-api服务
- acli service anet vn-node-agent-api restartacli service anet vn-node-agent-api restart
```bash
acli service anet vn-node-agent-api restart
```
彻底解决方案：
6.8.0打最新的集和补丁
升级690及以后版本

#### 获取主机内存大小为0
(参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、执行dmidecode -t memory无法获取内存信息，提示：Invalid entry length
- acli system dmidecode -t memoryacli system dmidecode -t memory
```bash
acli system dmidecode -t memory
```


##### 解决方案
联系硬件技术支持处理

#### go-zero框架问题
（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
- 异常当天日志中包含如下日志：
- load/apaptivesshedder.go:197 dropreq，cpu：9xxxxxxxxxxxx, maxPass: xx
- handler/sheddinghandle.go:38 [http] dropped
- 说明：x86架构，部分go服务日志报错；有http dropped请求，drop原因是cpu计算异常（数值无限大）。
- acli log get -E -k 'dropreq|[http] dropped'  -p /sf/log/${date}/statuscenterd.logacli log get -E -k 'dropreq|[http] dropped'  -p /sf/log/${date}/statuscenterd.log
```bash
acli log get -E -k 'dropreq|[http] dropped'  -p /sf/log/${date}/statuscenterd.log
```


##### 解决方案
6.8.0打最新的集和补丁
升级至6.8.0R1以后LTS版本

#### 集群主机间redis 服务端口访问不通

##### 判断方法
1、检查vtpdaemon日志，报错：Cloud not connect to Redis server at .*:6379: Connection timed out
- acli log get -E -k "Cloud not connect to Redis server at .*:6379: Connection timed out" -p '/sf/log/${date}/' -f 'sfvt_vtpdaemon.log'acli log get -E -k "Cloud not connect to Redis server at .*:6379: Connection timed out" -p '/sf/log/${date}/' -f 'sfvt_vtpdaemon.log'
```bash
acli log get -E -k "Cloud not connect to Redis server at .*:6379: Connection timed out" -p '/sf/log/${date}/' -f 'sfvt_vtpdaemon.log'
```

判断主机间6379端口不通
- acli system telnet $hostip 6379acli system telnet $hostip 6379
```bash
acli system telnet $hostip 6379
```

判断集群主机是否正常运行，未运行直接提示出来
- acli  service asv redis statusacli  service asv redis status
```bash
acli  service asv redis status
```

##### 解决方案
1、检查主机间是否存在安全设备拦截端口策略；
检查管理交换机的acl策略是否拦截；
