### 存储ID不可访问

#### 现象描述
执行开机任务失败，控制台页面右下角弹框报错：未指定存储ID


#### 存储离线

##### 判断方法
1、检查控制台告警：存储掉线告警
```bash
acli alert get -e '存储掉线告警'
```
2、查看虚拟机存储镜像目录，获取镜像目录挂载点
- acli vm disk path get -v ${vmid}
```bash
acli vm disk path get -v ${vmid}
```
3、确认挂载点不在
- acli system df | grep ${storageid}
acli system df | grep ${storageid}
acli system df | grep ${storageid}

##### 解决方案（上升研发）
涉及存储高危操作，请拨打400或上升云BG-LMT技术支持

#### 虚拟机配置异常

##### 判断方法
- 获取所有虚拟机的配置信息，正常一个vmid对应一个cfgstorage，返回无cfgstorage字段或者返回为空
- acli vm list | grep -E  'vmid|cfgstorage' | grep -v cfgstorageshared
```bash
acli vm list | grep -E  'vmid|cfgstorage' | grep -v cfgstorageshared
```

- 获取虚拟机配置信息，返回无cfgstorage字段或者返回为空
- acli vm config get -v ${vmid}
```bash
acli vm config get -v ${vmid}
```


##### 解决方案（上升研发）
涉及存储高危操作，请拨打400或上升云BG-LMT技术支持
将虚拟机镜像目录下将配置文件重新拷贝一份正常的配置覆盖为空的配置
