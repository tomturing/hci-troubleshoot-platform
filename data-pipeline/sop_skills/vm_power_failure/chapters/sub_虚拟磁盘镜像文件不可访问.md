### 虚拟磁盘镜像文件不可访问

#### 现象描述
执行开机任务失败，报错描述：镜像文件不可访问，请检查存储网络和磁盘状态！
- acli task get -k '镜像文件不可访问' -t ${YYYY-MM-DD} -s -1acli task get -k '镜像文件不可访问' -t ${YYYY-MM-DD} -s -1
```bash
acli task get -k '镜像文件不可访问' -t ${YYYY-MM-DD} -s -1
```


#### 存储离线

##### 判断方法
1、查看虚拟机存储镜像目录，获取镜像目录挂载点
```bash
acli vm disk path get -v ${vmid}
```
2、确认挂载点不在
```bash
acli system df | grep ${storageid}
```

##### 解决方案（上升研发）
涉及存储高危操作，请拨打400或上升云BG-LMT技术支持

#### vs数据双点

##### 判断方法
- 双点分片检查命令：
- acli storage asan vs_rpc_tool -c lookupacli storage asan vs_rpc_tool -c lookup
```bash
acli storage asan vs_rpc_tool -c lookup
```


##### 解决方案（上升研发）
- 双点分片恢复方案：请拨打400或上升VS技术支持-服务号
- 1、备份恢复：如对应虚拟机有备份，优先从备份拉起一个新虚拟机，验证业务正常后删除原虚拟机；
- 2、修复坏道盘：将坏道磁盘拔出，找第三方修复公司完成修复后，再将磁盘插回集群；（优点：无损修复概率较高；缺点：磁盘修复期间相关虚拟机可能无法开机使用导致业务中断，且会产生费用）
3、坏道有损修复：（研发后台操作，修复之前需要对重要虚拟机数据做备份）：将A副本（bad被指控副本）对应的偏移位置数据读出写到到坏道B副本上（ 修复量小一个坏道通常为HDD=512B,SSD=4K），读出的A副本偏移位置数据有低概率是被指控的异常数据，异常数据写到B副本分片上可能导致这个分片损坏不可用（比如：如果坏道位置数据是qemu元数据、操作系统数据、或者LUN所对应的文件系统元数据等等，可能导致虚拟机无法启动、文件系统无法挂载等等），因此手动修复的方法存在小概率数据损坏的风险；

#### 本地存储盘符变化
主机重启后本地存储的磁盘盘符发生变化（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、获取虚拟机磁盘镜像目录，回显报错输出存在：No such file or directory
```bash
acli vm disk path get -v ${vmid}
```

- 2、/sf/log/today/sfvt_vtpstatd.log过滤到doesn't mount
- acli log get -k "doesn't mount" -p '/sf/log/today/' -f 'sfvt_vtpstatd.log'acli log get -k "doesn't mount" -p '/sf/log/today/' -f 'sfvt_vtpstatd.log'
```bash
acli log get -k "doesn't mount" -p '/sf/log/today/' -f 'sfvt_vtpstatd.log'
```


##### 解决方案
在控制台【存储】-【其他存储】点击：重新发现磁盘

#### qcow2镜像损坏

##### 判断方法
1、检查存在：虚拟机镜像文件损坏告警
```bash
acli alert get -e '虚拟机镜像文件损坏告警'
```

2、获取虚拟机磁盘文件列表
```bash
acli vm disk list -v ${vmid}
```

3、检查虚拟机磁盘文件
```bash
acli vm disk check -v ${vmid} -d ${vm-disk-X}.qcow2
```
查看是否磁盘损坏，此案例损坏的是磁盘1，所以对应vm-disk-1.qcow2，如下图看确实是有磁盘损坏
(qcow2 image is good ，说明：磁盘镜像正常，其他状态均为异常，并且内核日志也无异常告警。)


##### 解决方案（上升研发）
涉及高危操作，请拨打400或上升云BG中台技术支持
（参考KB：超融合HCI-深信服技术支持）
