### 获取集群锁失败

#### 现象描述
控制台告警
cfs读写异常
```bash
acli platform cfs status
```
正常情况touch文件用时只有0.0x秒


#### 管理口网络丢包
集群主机管理口网络丢包（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
netdoctor检查管理口
- acli netdoctoracli netdoctor
```bash
acli netdoctor
```

##### 解决方案
1、检检查管理口IP冲突
2、检查管理口网口状态、接线、聚合配置等
