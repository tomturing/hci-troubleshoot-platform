### 获取版本号失败

#### 现象描述
1、启动虚拟失败；描述：获取版本号失败，错误码：0x010032F5
- acli task get -k "获取版本号失败" -t ${YYYY-MM-DD} -s -l 1
acli task get -k "获取版本号失败" -t ${YYYY-MM-DD} -s -l 1
acli task get -k "获取版本号失败" -t ${YYYY-MM-DD} -s -l 1


#### boot分区未正常挂载
（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、检查vtpdaemon日志，报错：get asv controller version failed
- acli log get -k "get asv controller version failed" -p '/sf/log/${date}/' -f 'sfvt_vtpdaemon.log'
```bash
acli log get -k "get asv controller version failed" -p '/sf/log/${date}/' -f 'sfvt_vtpdaemon.log'
```

2、文件不存在则为异常
- acli system ls /boot/firmware/current/package/meta-inf/version
```bash
acli system ls /boot/firmware/current/package/meta-inf/version
```

3、无输出则为异常
- acli system df /boot/
```bash
acli system df /boot/
```

##### 解决方案
在重启一次主机，让主机自动挂载上boot分区
