### 内部异常

#### 现象描述
执行开机任务失败，报错描述：内部异常，请稍后重试
- acli task get -k "内部异常" -t ${YYYY-MM-DD} -s -1 acli task get -k "内部异常" -t ${YYYY-MM-DD} -s -1
acli task get -k "内部异常" -t ${YYYY-MM-DD} -s -1
acli task get -k "内部异常" -t ${YYYY-MM-DD} -s -1


#### 参数异常

##### 判断方法
#todo

##### 解决方案
#todo

#### 主机缺少kvm_intel驱动
（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
- 方式1：查看问题虚拟机qemu日志，提示failed to initialize KVM: No such file or directory或failed to initialize kvm: No such file or directory
- acli log get -k 'failed to initialize KVM: No such file or directory' -t ${YYYY-MM-DD} -f /sf/log/${date}/sfvt_qemu_${vmid}.logacli log get -k 'failed to initialize KVM: No such file or directory' -t ${YYYY-MM-DD} -f /sf/log/${date}/sfvt_qemu_${vmid}.log
```bash
acli log get -k 'failed to initialize KVM: No such file or directory' -t ${YYYY-MM-DD} -f /sf/log/${date}/sfvt_qemu_${vmid}.log
```

方式2：查看kvm_intel驱动： lsmod | grep kvm_intel 无输出
```bash
acli system lsmod | grep kvm_intel
```
方式3：查看内核日志： kvm: disable by bios
- acli log get -k 'kvm: disable by bios' -t ${YYYY-MM-DD} -f /sf/log/${date}/kernel.logacli log get -k 'kvm: disable by bios' -t ${YYYY-MM-DD} -f /sf/log/${date}/kernel.log
```bash
acli log get -k 'kvm: disable by bios' -t ${YYYY-MM-DD} -f /sf/log/${date}/kernel.log
```

##### 解决方案
1、关机进BIOS，启动CPU虚拟化VT-D

#### 嵌套虚拟化未开启
6.7.0之前的版本升级到680及以后版本，升级后主机嵌套虚拟化未开启导致虚拟机开机qemu出core（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
判定标准：
只检测680及以上
集群升级了大版本，但只有部分主机重启过，部分主机未重启
- #(680之前不管)acli --cluster platform nested status get acli --cluster system uname -a#(680之前不管)acli --cluster platform nested status get acli --cluster system uname -a
```bash
#(680之前不管)
acli --cluster platform nested status get
acli --cluster system uname -a
#(680之前不管)
acli --cluster platform nested status get
acli --cluster system uname -a
```

##### 解决方案

##### 快速恢复方案：
滚动重启内核还没有生效的物理主机

##### 彻底解决方案：
TD2024122310207。需提供补丁期数
- 打补丁，自动检测虚拟机是否存在这种问题，有则迁移限制不能往670内核的主机上迁移以防止HA ；
- 打补丁，如果要迁移则可以通过重启单个虚拟机解决迁移限制；
- 打补丁，页面对老的虚拟机做升级虚拟机兼容性配置操作，然后重启虚拟机 ；

#### Cgroup组不完整

##### 判断方法
判断方法：同时命中则为异常
查看虚拟机的容器启动日志，runc_start_${vmid}.log, 报错：Cgroup does not exist
- acli log get -k 'Cgroup does not exist' -p /sf/log/${date} -f runc_start_${vmid}.logacli log get -k 'Cgroup does not exist' -p /sf/log/${date} -f runc_start_${vmid}.log
```bash
acli log get -k 'Cgroup does not exist' -p /sf/log/${date} -f runc_start_${vmid}.log
```

检查在/mnt/cgroup/cpu下没有service和compute的目录：（service给后台服务使用，compute给虚拟机使用）
```bash
acli system cgroup cpu list | grep service
acli system cgroup cpu list | grep compute
acli system cgroup cpu list | grep service
acli system cgroup cpu list | grep compute
acli system cgroup cpu list | grep service
acli system cgroup cpu list | grep compute
acli system cgroup cpu list | grep service
acli system cgroup cpu list | grep compute
```

##### 解决方案
快速恢复方案：
# 切到mgmt容器：container_exec -n mgmt-node-agent
- acli system create_base_cpu_groupsacli system cgroup cpu initacli system create_base_cpu_groupsacli system cgroup cpu init
```bash
acli system create_base_cpu_groups
acli system cgroup cpu init
acli system create_base_cpu_groups
acli system cgroup cpu init
```
# 执行/sf/bin/create_base_cpu_groups
彻底解决方案：
TD2024030600210：升级至HCI6.10.0R1及以上版本

#### SANGFOR_LINUX_UEFI文件访问异常
（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、检查qemu日志，报错：Failed to stat /sf/data/${存储id}/images/cluster/${vmid}.vm，err：No such file or directoryexclude flock pflash：/sf/data/${存储id}/images/cluster/${vmid}.vm/SANGFOR_LINUX_UEFI
- acli log get -k 'No such file or directoryexclude flock pflash' -p /sf/log/${date}/sfvt_qemu_[vmid].logacli log get -k 'No such file or directoryexclude flock pflash' -p /sf/log/${date}/sfvt_qemu_[vmid].log
```bash
acli log get -k 'No such file or directoryexclude flock pflash' -p /sf/log/${date}/sfvt_qemu_[vmid].log
```


##### 解决方案
- 快速恢复方案：
- 在页面编辑配置，使用SeaBIOS方式启动虚拟机

- 或者使用acli编辑虚拟机配置
- acli vm config set -v ${vmid} --field uefi_bios:1acli vm config set -v ${vmid} --field uefi_bios:1
```bash
acli vm config set -v ${vmid} --field uefi_bios:1
```
- 彻底解决方案：
- 1、重置虚拟机的SANGFOR_LINUX_UEFI文件
- acli vm uefi resetacli vm uefi reset
```bash
acli vm uefi reset
```
执行命令  mv  xxxx/SANGFOR_LINUX_UEFI   xxxx/SANGFOR_LINUX_UEFI.bak

#### UEFI文件双点
（参考KB：超融合HCI-深信服技术支持)

##### 判断方法
1、检查qemu日志，报错： SANGFOR_LINUX_UEFI': Input/output error
- acli log get -k "SANGFOR_LINUX_UEFI': Input/output error" -p /sf/log/${date}/sfvt_qemu_${vmid}.logacli log get -k "SANGFOR_LINUX_UEFI': Input/output error" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```bash
acli log get -k "SANGFOR_LINUX_UEFI': Input/output error" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```


##### 解决方案（上升研发）
方案一：切换为legacy启动
方案二：请拨打400或上升云BG中台技术支持

#### 低版本升级到6.11.1，vdi模板派生虚拟机pci地址冲突
（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、检查qemu日志，报错：slot 31 function 0 not available for pci-bridge
- acli log get -k "slot 31 function 0 not available for pci-bridge" -p /sf/log/${date}/sfvt_qemu_${vmid}.logacli log get -k "slot 31 function 0 not available for pci-bridge" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```bash
acli log get -k "slot 31 function 0 not available for pci-bridge" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```


##### 解决方案
快速恢复方案：
获取虚拟机配置文件路径
- find /cfs/ -name ${vmid}.conf(无法开机的虚拟机的vmid)find /cfs/ -name ${vmid}.conf(无法开机的虚拟机的vmid)
```bash
find /cfs/ -name ${vmid}.conf(无法开机的虚拟机的vmid)
```
编辑${vmid}.conf文件
```bash
vim ${path to vimd}.conf(步骤1返回的路径)vim ${path to vimd}.conf(步骤1返回的路径)
vim ${path to vimd}.conf(步骤1返回的路径)
```
3、将compatibility_version:vmx_version=vmx-3.16,src_hci_version=6.11.1中的vmx-3.16修改为vmx-3.14后重新开机
彻底解决方案：
预警：YJ20250603001
实施最新的最新集合补丁

#### 出core
qemu出core

##### 判断方法
查看/sf/data/local/dump/目录下，存在对应时间的core-kvm-xxx文件
判断core-kvm-xxx文件产生时间与虚拟机开机失败时间相近（当天）
- acli system ls -l /sf/data/local/dump acli system stat /sf/data/local/dump/core-kvm-xxxxacli system ls -l /sf/data/local/dump acli system stat /sf/data/local/dump/core-kvm-xxxx
```bash
acli system ls -l /sf/data/local/dump
acli system stat /sf/data/local/dump/core-kvm-xxxx
acli system ls -l /sf/data/local/dump
acli system stat /sf/data/local/dump/core-kvm-xxxx
```

##### 解决方案（上升研发）
请拨打400或上升云BG中台技术支持

#### 显卡异常导致创建vgpu失败
qemu创建vgpu设备失败

##### 判断方法
1、检查qemu日志，报错：qemu failed to create a virtual vfio device, there is a problem with the vfio-pci driver
- acli log get -k "qemu failed to create a virtual vfio device" -p /sf/log/${date}/sfvt_qemu_${vmid}.logacli log get -k "qemu failed to create a virtual vfio device" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```bash
acli log get -k "qemu failed to create a virtual vfio device" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```


##### 解决方案（上升研发）
方案1：显卡问题异常，nvidia-smi 无法显示显卡温度等信息 显示为： ERR。（参考KB：超融合HCI-深信服技术支持）
- 方案2：重启问题主机检查BIO设置，能找到的要全部开启，4G Abord、ACS 、SR-IOV、IOMMU、PCIe ARI Support
- 方案3：驱动兼容性问题，更新高版本驱动。
- 上诉问题都不是，上升云中台技术支持研发。

#### 显卡初始化失败导致虚拟机启动异常
显卡主机开机时，因为BAR空间不足导致显卡初始化失败（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、检查qemu日志，报错：hardware reports invalid configuration, MSIX PBA outside of specified BAR
- acli log get -k "MSIX PBA outside of specified BAR" -p /sf/log/${date}/sfvt_qemu_${vmid}.logacli log get -k "MSIX PBA outside of specified BAR" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```bash
acli log get -k "MSIX PBA outside of specified BAR" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```

2、检查kernel日志，开机初始化时显卡对应的pci地址设备初始化时报错：no space for


##### 解决方案（上升研发）
- 1、备份grub.cfg配置：
- cp /boot/boot/grub/grub.cfg /sf/data/local/
- acli system cp /boot/boot/grub/grub.cfg /sf/data/local/acli system cp /boot/boot/grub/grub.cfg /sf/data/local/
```bash
acli system cp /boot/boot/grub/grub.cfg /sf/data/local/
```
2、vim编辑grub.cfg配置文件，将pci=realloc删除；重启物理主机


#### 显卡主机BIOS设置没有开启IOMMU，导致显卡无法识别到显卡vfio设备

##### 判断方法
1、检查qemu日志，报错：failed to get group, please check /dev/vfio/0
- acli log get -k "failed to get group, please check /dev/vfio" -p /sf/log/${date}/sfvt_qemu_${vmid}.logacli log get -k "failed to get group, please check /dev/vfio" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```bash
acli log get -k "failed to get group, please check /dev/vfio" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```

2、ls -l /dev/vfio/  不存在 /dev/vfio/0设备
- acli system ls -l  /dev/vfio/0 acli system ls -l  /dev/vfio/0
```bash
acli system ls -l  /dev/vfio/0
```

##### 解决方案
1、重启问题主机，检查BIO设置，能找到的要全部开启，4G Abord、ACS 、SR-IOV、IOMMU、PCIe ARI Support
如若上述方法无法解决，请拨打400或上升云BG中台技术支持

#### 主机多张显卡其中某张显卡异常导致虚拟机开机失败

##### 判断方法
1、检查qemu日志，报错：vfio .* error getting device from group
- acli log get -E -k "vfio .* error getting device from group" -p /sf/log/${date}/sfvt_qemu_${vmid}.logacli log get -E -k "vfio .* error getting device from group" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```bash
acli log get -E -k "vfio .* error getting device from group" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```

2、检查kernel日志，内核出现显卡start failed的问题。后续显卡实例销毁有内核报错
- acli log get -E -k "nvidia-vgpu-vfio.*start failed" -p /sf/log/${date}/kernel.logacli log get -E -k "nvidia-vgpu-vfio.*start failed" -p /sf/log/${date}/kernel.log
```bash
acli log get -E -k "nvidia-vgpu-vfio.*start failed" -p /sf/log/${date}/kernel.log
```


##### 解决方案（上升研发）
1、建议可以更换下槽位观察，如果再复现观察是跟卡走还是跟槽位走，进而判断问题出在卡上还是槽位/主板上 ;
如若上述方法无法解决，请拨打400或上升云BG中台技术支持

#### 3D虚拟机开机失败：添加多张显卡且未安装系统

##### 判断方法
检查qemu日志，报错：关键报错：Bus 'pci.1' not found。
- acli log get -k "Bus 'pci.1' not found" -p /sf/log/${date}/sfvt_qemu_${vmid}.logacli log get -k "Bus 'pci.1' not found" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```bash
acli log get -k "Bus 'pci.1' not found" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```

2、检查虚拟机配置文件os_installed为0，代表未安装系统
- acli vm config get  -v $vmid|grep os_installedacli vm config get  -v $vmid|grep os_installed
```bash
acli vm config get  -v $vmid|
grep os_installed
acli vm config get  -v $vmid|
grep os_installed
```

##### 解决方案
1、创建基本虚拟机，不要先配置显卡。要先安装操作系统，然后安装vmtools重启虚拟机，最后再配置显卡
- 推升级版本611.1以后版本

#### 快速派生虚拟机创建外部快照冷迁移存储位置后无法开机

##### 判断方法
1、检查qemu日志，报错：关键报错：Could not open backing file。
- acli log get -k "Could not open backing file" -p /sf/log/${date}/sfvt_qemu_${vmid}.logacli log get -k "Could not open backing file" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```bash
acli log get -k "Could not open backing file" -p /sf/log/${date}/sfvt_qemu_${vmid}.log
```

2、检查虚拟机配置文件系统盘存在外置磁盘快照且存在backing_file
- acli vm config get -v ${vmid} | grep external_*_vm-disk-1 | grep backing_file acli vm config get -v ${vmid} | grep external_*_vm-disk-1 | grep backing_file
```bash
acli vm config get -v ${vmid} | grep external_*_vm-disk-1 | grep backing_file
```


##### 解决方案
快速恢复方案：
参考KB：临时解决超融合HCI-深信服技术支持
彻底解决方案：
- 推升级版本611.1R1及以后版本

