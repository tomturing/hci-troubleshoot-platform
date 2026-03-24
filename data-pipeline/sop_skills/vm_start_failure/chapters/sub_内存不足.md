### 内存不足

#### 现象描述
执行开机任务失败，报错描述：剩余可配置内存不足 或 计算内存不足
```bash
acli task get -k '剩余配置内存不足' -t ${YYYY-MM-DD} -s -1
acli task get -k '计算内存不足' -t ${YYYY-MM-DD} -s -1
```


#### 内存泄露

##### 判断方法
查看主机内存使用情况的详细统计信息
```bash
acli system memory info | grep Percpu
acli system memory dump | grep Undefined
acli system memory info | grep Percpu
acli system memory dump | grep Undefined
acli system memory info | grep Percpu
acli system memory dump | grep Undefined
acli system memory info | grep Percpu
acli system memory dump | grep Undefined
```
Percpu值大于5G疑似异常
Undefined项值大于5G疑似异常，该值过高常见为ipmi驱动导致内存异常泄露（参考KB：超融合HCI-深信服技术支持）
2、查看主机内存的SReclaimable值和slab的proc_inode_cache值
```bash
acli system memory dump | grep SReclaimable
cat /proc/slabinfo | grep proc_inode_cache | awk '{print$2}'
acli system memory dump | grep SReclaimable
cat /proc/slabinfo | grep proc_inode_cache | awk '{print$2}'
acli system memory dump | grep SReclaimable
cat /proc/slabinfo | grep proc_inode_cache | awk '{print$2}'
acli system memory dump | grep SReclaimable
cat /proc/slabinfo | grep proc_inode_cache | awk '{print$2}'
```
SReclaimable（可回收缓存）大于5G疑似异常，请拨打400或上升云BG中台技术支持
proc_inode_cache值大于1kw疑似mongodb泄露，请拨打400或上升云BG中台技术支持（参考KB：超融合HCI-深信服技术支持）
查看主机内存的VmallocUsed值
```bash
acli system memory dump | grep VmallocUsed
```
VmallocUsed大于10G疑似异常请拨打400或上升云BG中台技术支持（参考KB：超融合HCI-深信服技术支持）

4、查看主机所有进程的内存使用情况（上升研发）
```bash
acli system memory usage get
```
回显安装内存占用从小到大排序
观察占用内存较大的进程是否在正常范围内：
若占用内存高的进程是不重要的虚拟机，则关闭、迁移、或重启该虚拟机；
若占用内存高的进程为平台服务，则重启异常服务

页面内存分布说明：

硬件预留内存占用过多：内存硬件内存识别异常
预分配内存过多（大页虚拟机和内核服务占用内存）

##### 解决方案（上升研发）
临时恢复方案：
常见已知案例：
1、percpu占用过高是由于主板ipmi功能异常导致，ipmi的msghandler驱动周期性发包和主板ipmi模块通信（参考KB：超融合HCI-深信服技术支持）
- 2、slab服务占用较多内存（参考KB：超融合HCI-深信服技术支持）
- 3、外置存储异常导致内核vmalloc分配内存不断增加（参考KB：超融合HCI-深信服技术支持）
- 需要收到确认回包后才能将发包内存释放，如果长时间没有回包，而驱动内部没有超时回收包内存的机制导致内存无法回收。
- 解决方案：(上升研发)

- 找时间重启内存泄漏的主机才能回收泄漏的内存
- 4、重启平台异常服务
彻底解决方案：
升级至6.11.1及以后版本

#### 内存耗尽

##### 判断方法
在主机详细页面【主机信息】确认【计算内存】【未使用】数值小于需要开的虚拟机配置内存


##### 解决方案
- 1、关闭部分业务虚拟机
- 同客户侧确认，看是否有不重要的业务虚拟机，关闭，释放CPU/内存资源
- 2、迁移虚拟机运行位置
- a.确认集群内是否有其他主机的CPU/内存负载低，将部分虚拟机迁移运行位置到其他资源较为空闲的主机上
- b.检查集群资源调度DRS，是否启用并正常配置，如果服务器的CPU或者内存不足，需要考虑扩容CPU/内存资源
- 3、调整虚拟机配置：虚拟机【编辑】
- a.后台过滤启用虚拟机磁盘“读缓存、虚拟机内存回收、虚拟机大页内存“的虚拟机名称
- acli vm list | grep -E "vmid|pagecache=|balloon_memory: 1|hugepage: 1"
```bash
acli vm list | grep -E "vmid|pagecache=|balloon_memory: 1|hugepage: 1"
```
- b.虚拟机关闭硬盘“写缓存”

- c.虚拟机启用内存回收

- 4、增加集群资源
- a.扩容主机：增加CPU、内存
- b.扩容内存
