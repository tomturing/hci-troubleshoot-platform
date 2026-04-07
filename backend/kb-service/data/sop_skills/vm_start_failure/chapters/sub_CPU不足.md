### CPU不足

#### 现象描述
执行开机任务失败，报错描述：此主机剩余可配置CPU不足
```bash
acli task get -k 'CPU不足' -t ${YYYY-MM-DD} -s -1
```


#### CPU不足

##### 判断方法
如何检查CPU
命令一：使用  acli system top  检查是否有占CPU核数较多的资源
- acli system top
```bash
acli system top
```
#PID对应的值为进程IP，如图，进程id为23557的一个kvm进程占用了201%的CPU资源（2核）

命令二：使用  acli system ps auxf | grep ${PID}  找出详细进程（PID为实际进程ID、通过top命令确认）
- acli system ps axuf | grep ${PID}
acli system ps axuf | grep ${PID}
acli system ps axuf | grep ${PID}
a.通过ps可以发现，占用2核的进程，是虚拟机为“应用交付1”的设备
b.若占用CPU高的进程是不重要的虚拟机，则关闭、迁移、或重启该虚拟机
c.若占用CPU高的进程为平台服务，则重启异常服务（无法评估是什么服务占用，可以跟专家确认后拉通研发确认）


##### 解决方案
- 关闭部分业务虚拟机
- 同客户侧确认，看是否有不重要的业务虚拟机，关闭，释放CPU/内存资源。
- 迁移虚拟机运行位置
- a）确认集群内是否有其他主机的CPU负载低，将部分虚拟机迁移运行位置到其他资源较为空闲的主机上
b）检查集群资源调度DRS，是否启用并正常配置，如果服务器的CPU不足，需要考虑扩容CPU资源
- 增加集群资源
- a）扩容主机：增加CPU、内存
- b）扩容CPU
- 重启平台异常服务
- //如检查为CPU耗尽导致，无法评估是什么服务占用，可以跟专家确认后拉通研发确认
