### 虚拟机正在进行其他操作

#### 现象描述

- acli task get -k '虚拟机正在进行其他操作' -t ${YYYY-MM-DD} -s -1
```bash
acli task get -k '虚拟机正在进行其他操作' -t ${YYYY-MM-DD} -s -1
```

#### 虚拟机存在正在运行的任务

##### 判断方法
判断虚拟机是否有其他运行的操作日志，查询出虚拟机的对应操作日志，判断process不为-1或者100时，提示出来

- acli task get -v vmid -t ${YYYY-MM-DD} -s -1
```bash
acli task get -v vmid -t ${YYYY-MM-DD} -s -1
```

##### 解决方案
请拨打400或上升云BG-LMT技术支持

#### 后台残留锁文件

##### 判断方法
界面不存在运行的操作日志时，判断锁文件存在
- acli vm lock list  -v $vmid
```bash
acli vm lock list  -v $vmid
```

##### 解决方案
删除虚拟机临时状态目录（starting,reseting等）(如有对应vmid)
临时状态残留场景可以清理，再重新开机虚拟机
- # 目前不支持 -r 。考虑使用脚本？# acli system rm -rf /var/lock/flag_dir/$vmid_xxx# acli system rm -rf /cfs/vm_tmp_status/flag_dir/$vmid_xxx# acli system rm -rf /cfs/priv/lock/$vmid_vmacli vm lock list  -v $vmidacli vm lock clean -v $vmid -n $name
```bash
# 目前不支持 -r 。考虑使用脚本？
# acli system rm -rf /var/lock/flag_dir/$vmid_xxx
# acli system rm -rf /cfs/vm_tmp_status/flag_dir/$vmid_xxx
# acli system rm -rf /cfs/priv/lock/$vmid_vm
acli vm lock list  -v $vmid
acli vm lock clean -v $vmid -n $name
# 目前不支持 -r 。考虑使用脚本？
# acli system rm -rf /var/lock/flag_dir/$vmid_xxx
# acli system rm -rf /cfs/vm_tmp_status/flag_dir/$vmid_xxx
# acli system rm -rf /cfs/priv/lock/$vmid_vm
acli vm lock list  -v $vmid
acli vm lock clean -v $vmid -n $name
```
2、后台取消正在运行任务，执行kill虚拟机镜像正在执行的进程（可提供kb）
（备份、克隆、加密转换等进程可以直接kill，其他进程需评估后才可以kill）
过滤虚拟机当前进程：
- acli --cluster system ps | grep $vmid
```bash
acli --cluster system ps | grep $vmid
```
说明：虚拟机的vmid，可以在虚拟机详情页面的uri中获取，如下图
如下图图所示，经过过滤，发现有虚拟机的备份任务在执行，第二列为对应进程的ID，pid，说明该虚拟机正在执行备份，导致虚拟机无法开机
kill进程需要找到任务的父进程，进程的子进程和父进程全部都需要kill掉，一个任务链会用折线连接，如上（可以修改-A后面的参数，-A的作用是显示对应进程的上下N行，可以适当增加-A后面的数值，多打印几行找到父进程）
下图所示虽然有虚拟机vmid的进程，但这个是我们刚刚过滤的进程，不是影响虚拟机无法开机的任务

杀掉占用进程:
如图，当前任务相关的进程ID为 14572 14570 14565 48825 48806 12742 46012
- acli system kill $pid
```bash
acli system kill $pid
```
如上图：kill 14572 14570 14565 48825 48806 12742 46012
验证：操作日志的任务已经结束，虚拟机可以正常开机

