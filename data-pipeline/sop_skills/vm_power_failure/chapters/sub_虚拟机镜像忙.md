### 虚拟机镜像忙

#### 现象描述
执行开机任务失败，报错描述：虚拟机镜像忙，正在执行其他操作！
```bash
acli task get -v ${vmid} -t ${YYYY-MM-DD} -k '虚拟机镜像忙' -s 'failed'
```


#### 进程残留

##### 判断方法
查看异常虚拟机的KVM进程，有输出则为异常（vmid为异常虚拟机的id）
```bash
acli --cluster system ps axuf | grep ${vmid} | grep '.qcow2'
```
获取异常虚拟机的镜像所在目录

查看异常虚拟机的镜像是否被打开，${path}参考上个命令输出


##### 解决方案
若存在进程占用虚拟机存储，联系专家或研发评估后，kill掉对应进程（如有备份进程等可以直接kill掉）
```bash
acli system kill ${pid}
```

#### 加锁失败

##### 判断方法
1、日志路径：虚拟机运行主机：/sf/log/today/sfvt_qemu_vmid.log  (vmid替换为对应虚拟机的vmid)
```bash
acli log get -E -k "Unknown error 208|ret = -11" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```
说明：虚拟机运行在外置存储上报错"Unknown error 208"，则为外置存储异常。解决方案参考情况一
说明：虚拟机运行在虚拟存储上报错 "Failed to lock file xxxx, ret = -11"，则为虚拟存储异常。解决方案参考情况二


##### 解决方案（上升研发）
情况一：若存在外置存储208错误，则参考KB外置存储208错误处理锁（参考KB：超融合HCI-深信服技术支持）
- 情况二：若存在虚拟存储加锁失败 -11错误，则重启NFS服务【！！！重启NFS服务会中断对应主机的虚拟机IO，影响时间30s左右，请在业务允许的情况下执行】
- 高危操作，请拨打400或上升VS技术支持-服务号
- acli storage asan vs_update_nfs restart recoveracli storage asan vs_update_nfs restart recover
```bash
acli storage asan vs_update_nfs restart recover
```
- 6.8.0及以下版本：/sf/vs/bin/vs_update_nfs.sh


#### 镜像目录同名

##### 判断方法
从587升级上来的虚拟机历史上多个虚拟机名称一样，导致虚拟机的镜像目录也是同名的，但是再不同分组下不会出问题，编辑移动到同一分组出异常（参考KB：超融合HCI-深信服技术支持）
虚拟机conf里的name的值，存在2个及以上虚拟机配置文件相同，提示出来相同的虚拟机名称
```bash
acli vm config get -v ${vmid}
```
用第一步查询出来的多个vmid，查看这些虚拟机镜像目录的都相同
```bash
acli vm disk path get -v ${vmid} acli vm disk path get -v ${vmid}
acli vm disk path get -v ${vmid}
```

##### 解决方案（上升研发）
- 高危操作，请拨打400或上升云BG-LMT技术支持
- 修改虚拟机配置文件
- acli vm config set -v ${vmid} --field name:xxxacli vm config set -v ${vmid} --field name:xxx
```bash
acli vm config set -v ${vmid} --field name:xxx
```
- 将name字段xxx修改为具体的虚拟机vmid
- 修改虚拟机镜像目录名称，改为vmid
- acli vm disk path get -v ${vmid} # 获得地址acli vm disk path get -v ${vmid} # 获得地址
```bash
acli vm disk path get -v ${vmid} # 获得地址
```
- mv /sf/data/xxx/imgages/cluster/xxx.vm  /sf/data/xxx/imgages/cluster/$vmid.vm
- 界面开机验证

#### 系统盘backing file指向旧的存储路径

##### 判断方法
检查qemu日志，报错：关键报错：Could not open backing file: Failed to mount nfs share: mount/mnt call。
- acli log get -k "Could not open backing file: Failed to mount nfs share: mount/mnt call" -p /sf/log/${date}/sfvt_qemu_${vmid}.logacli log get -k "Could not open backing file: Failed to mount nfs share: mount/mnt call" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```bash
acli log get -k "Could not open backing file: Failed to mount nfs share: mount/mnt call" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```


##### 解决方案
参考KB：超融合HCI-深信服技术支持
KB案例无法解决请上升研发技术支持
