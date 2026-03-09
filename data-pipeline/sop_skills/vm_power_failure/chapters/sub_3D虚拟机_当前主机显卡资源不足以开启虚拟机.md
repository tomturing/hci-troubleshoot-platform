### 3D虚拟机：当前主机显卡资源不足以开启虚拟机

#### 现象描述
- 启动虚拟失败；描述：当前主机显卡资源不足以开启虚拟机！
- acli task get -k "当前主机显卡资源不足以开启虚拟机" -t ${YYYY-MM-DD} -s -l 1acli task get -k "当前主机显卡资源不足以开启虚拟机" -t ${YYYY-MM-DD} -s -l 1
acli task get -k "当前主机显卡资源不足以开启虚拟机" -t ${YYYY-MM-DD} -s -l 1
acli task get -k "当前主机显卡资源不足以开启虚拟机" -t ${YYYY-MM-DD} -s -l 1


#### BIOS设置异常

##### 判断方法
场景1、查看vtpdaemon日志，报错获取主机vGPU信息失败
- acli log get -p '/sf/log/today/vt/sfvt_vtpdaemon.log' -E -k '获取主机vGPU信息失败'acli log get -p '/sf/log/today/vt/sfvt_vtpdaemon.log' -E -k '获取主机vGPU信息失败'
```bash
acli log get -p '/sf/log/today/vt/sfvt_vtpdaemon.log' -E -k '获取主机vGPU信息失败'
```

场景2、查看vtpdaemon日志，报错"get vf core failed"或者"get_vfs_cmd failed"
6.11.1及以上版本
- acli log get -p '/sf/log/today/vt/sfvt_vtpdaemon.log' -E -k 'get vf core failed'acli log get -p '/sf/log/today/vt/sfvt_vtpdaemon.log' -E -k 'get vf core failed'
```bash
acli log get -p '/sf/log/today/vt/sfvt_vtpdaemon.log' -E -k 'get vf core failed'
```
- 6.11.1以下版本
- acli log get -p '/sf/log/today/vt/sfvt_vtpdaemon.log' -E -k 'get_vfs_cmd failed'acli log get -p '/sf/log/today/vt/sfvt_vtpdaemon.log' -E -k 'get_vfs_cmd failed'
```bash
acli log get -p '/sf/log/today/vt/sfvt_vtpdaemon.log' -E -k 'get_vfs_cmd failed'
```


##### 解决方案
第三方服务器，BIOS的AMD IOMMU 或 AMD-Vi 设置没有打开。
上诉方法若没有解决，若无法解决请拨打400或上升云中台技术支持