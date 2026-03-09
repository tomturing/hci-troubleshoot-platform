### 虚拟存储不支持在该主机上启动虚拟机

#### 现象描述

操作日志会提示：虚拟存储不支持在该主机启动虚拟机！ 可能原因：1、主机已离线：2、主机不存在虑拟机副本；3、虚拟机使用的存 储策略不支持在该主机启动：4、虚以机启用Turbo模式，主机Turbo服务异常。
- acli task get -k "虚拟存储不支持在该主机启动虚拟机" -t ${YYYY-MM-DD} -s -1acli task get -k "虚拟存储不支持在该主机启动虚拟机" -t ${YYYY-MM-DD} -s -1
acli task get -k "虚拟存储不支持在该主机启动虚拟机" -t ${YYYY-MM-DD} -s -1
acli task get -k "虚拟存储不支持在该主机启动虚拟机" -t ${YYYY-MM-DD} -s -1

#### 镜像文件不存在

##### 判断方法
判断镜像文件虚拟机系统盘对应的镜像文件不存在

```bash
vm_disk=$(
acli vm config get -v vmid|
grep ^ide0 |awk -F':' '{print $3}' |awk -F',' '{print $1}')vm_path=$(
acli vm disk path get -v vmid)
acli system ls  $vm_path/$vm_diskvm_disk=$(
acli vm config get -v vmid|
grep ^ide0 |awk -F':' '{print $3}' |awk -F',' '{print $1}')vm_path=$(
acli vm disk path get -v vmid)
acli system ls  $vm_path/$vm_disk
vm_disk=$(
acli vm config get -v vmid|
grep ^ide0 |awk -F':' '{print $3}' |awk -F',' '{print $1}')
vm_path=$(
acli vm disk path get -v vmid)
```

```bash
acli system ls  $vm_path/$vm_disk
vm_disk=$(
acli vm config get -v vmid|
grep ^ide0 |awk -F':' '{print $3}' |awk -F',' '{print $1}')
vm_path=$(
acli vm disk path get -v vmid)
```

```bash
acli system ls  $vm_path/$vm_disk
```
判断虚拟机为还原模式虚拟机,虚拟机配置文件中存在revert_mode：1，为还原虚拟机
- acli vm config get -v vmid|grep revert_modeacli vm config get -v vmid|grep revert_mode
```bash
acli vm config get -v vmid|
grep revert_mode
acli vm config get -v vmid|
grep revert_mode
```

##### 解决方案
满足判断条件1和2：（参考KB：超融合HCI-深信服技术支持）只满足条件1：
提示镜像文件xxx/xxx.qcow2不存在,请上升研发技术支持
