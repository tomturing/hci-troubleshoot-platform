### 3D虚拟机：显卡切分方式不匹配或显卡资源不足

#### 现象描述
- 启动虚拟失败；描述：显卡切分方式不匹配或显卡资源不足！错误码：0x01002C73
- acli task get -k "显卡切分方式不匹配或显卡资源不足" -t ${YYYY-MM-DD} -s -l 1acli task get -k "显卡切分方式不匹配或显卡资源不足" -t ${YYYY-MM-DD} -s -l 1
acli task get -k "显卡切分方式不匹配或显卡资源不足" -t ${YYYY-MM-DD} -s -l 1
acli task get -k "显卡切分方式不匹配或显卡资源不足" -t ${YYYY-MM-DD} -s -l 1


#### 显卡配置文件gpu_info.ini缺少gpu_type导致初始化配置失败
（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、GPU主机没有运行任何GPU虚拟机，启动GPU虚拟机报错"显卡切分方式不匹配或显卡资源不足"

2、判断文件/sf/cfg/gpu_info.ini显卡核心缺少gpu_type字段


##### 解决方案（上升研发）
重新初始化显卡配置
若无法解决请拨打400或上升云中台技术支持
- #acli system mv /sf/cfg/gpu_info.ini /sf/data/local#acli hardware gpu ini_host_gpuacli hardware gpu config reset --file xxacli hardware gpu ini_host_gpu#acli system mv /sf/cfg/gpu_info.ini /sf/data/local#acli hardware gpu ini_host_gpuacli hardware gpu config reset --file xxacli hardware gpu ini_host_gpu
```bash
#
acli system mv /sf/cfg/gpu_info.ini /sf/data/local
#
acli hardware gpu ini_host_gpu
acli hardware gpu config reset --file xx
acli hardware gpu ini_host_gpu
#
acli system mv /sf/cfg/gpu_info.ini /sf/data/local
#
acli hardware gpu ini_host_gpu
acli hardware gpu config reset --file xx
acli hardware gpu ini_host_gpu
```


#### 显卡显示模式未关闭导致无法使用切分功能
（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、显卡驱动已安装 lsmode |grep vfio
- acli system lsmod |grep vfioacli system lsmod |grep vfio
```bash
acli system lsmod |
grep vfio
acli system lsmod |
grep vfio
```
nvidia_vgpu_vfio表示有英伟达驱动

2、显卡已经绑定nvidia驱动 lspci -nnk -d 10de:  对应的设备绑定的是nvidia驱动
- acli system lspci  -nnk -d 10de: |grep 'Kernel driver in use: nvidia'acli system lspci  -nnk -d 10de: |grep 'Kernel driver in use: nvidia'
```bash
acli system lspci  -nnk -d 10de: |
grep 'Kernel driver in use: nvidia'
acli system lspci  -nnk -d 10de: |
grep 'Kernel driver in use: nvidia'
```

3、显卡缺少切分目录：
```bash
acli log get -E -k 'ls -l /sys/bus/pci/devices/0000:xx:00.0/' -p /sf/log/{day}/sfvt_vtpdaemon.log
```

4、ls -l /sys/bus/pci/devices/0000\:xx\:00.0/ |grep virtfn  返回空
- acli system ls -l /sys/bus/pci/devices/0000\:xx\:00.0/ |grep virtfn acli system ls -l /sys/bus/pci/devices/0000\:xx\:00.0/ |grep virtfn
```bash
acli system ls -l /sys/bus/pci/devices/0000\:xx\:00.0/ |
grep virtfn
acli system ls -l /sys/bus/pci/devices/0000\:xx\:00.0/ |
grep virtfn
```

##### 解决方案（上升研发）
若无法解决请拨打400或上升云中台技术支持

#### 显卡驱动版本较低或者不兼容，显卡无法切分，日志报错GPU可提供实例数量不足

##### 判断方法
1、vtpdaemon日志，有报错"GPU可提供实例数量不足"
- acli log get -p '/sf/log/today/vt/sfvt_vtpdaemon.log' -E -k 'GPU可提供实例数量不足'acli log get -p '/sf/log/today/vt/sfvt_vtpdaemon.log' -E -k 'GPU可提供实例数量不足'
```bash
acli log get -p '/sf/log/today/vt/sfvt_vtpdaemon.log' -E -k 'GPU可提供实例数量不足'
```


##### 解决方案（上升研发）
更新高版本驱动或对应版本兼容的驱动
若无法解决请拨打400或上升云中台技术支持

#### 显卡驱动setpci版本较低，不兼容此类pci设备，无法获取到正确的pci地址，导致出现异常
（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、判断日志sfvt_vtpdaemon.log中的报错"ERR: setpci: Cannot open"
- acli log get -k "ERR: setpci: Cannot open" -p '/sf/log/${date}/' -f 'sfvt_vtpdaemon.log'acli log get -k "ERR: setpci: Cannot open" -p '/sf/log/${date}/' -f 'sfvt_vtpdaemon.log'
```bash
acli log get -k "ERR: setpci: Cannot open" -p '/sf/log/${date}/' -f 'sfvt_vtpdaemon.log'
```

- 再asv-con容器中执行 /sf/data/local/sgax/sgax-chroot.sh
- setpci -s "0000:9d:00.0" b.b
- acli hardware gpu setpci -s "0000:9d:00.0" b.b  #  b.b是否固定acli hardware gpu setpci -s "0000:9d:00.0" b.b  #  b.b是否固定
```bash
acli hardware gpu setpci -s "0000:9d:00.0" b.b  #  b.b是否固定
```


##### 解决方案（上升研发）
若无法解决请拨打400或上升云中台技术支持
