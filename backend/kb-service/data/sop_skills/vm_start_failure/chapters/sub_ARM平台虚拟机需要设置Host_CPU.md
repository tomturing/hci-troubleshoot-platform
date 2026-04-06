### ARM平台虚拟机需要设置Host CPU

#### 现象描述
启动虚拟机失败，报错：启动失败。ARM平台虚拟机需要设置HostCPU！


#### arm平台未设置HOST CPU
- (参考KB：超融合HCI-深信服技术支持)

##### 判断方法
1、获取启动虚拟机失败任务描述
- acli task get -k "ARM平台虚拟机需要设置Host CPU" -t ${YYYY-MM-DD} -s -1
acli task get -k "ARM平台虚拟机需要设置Host CPU" -t ${YYYY-MM-DD} -s -1
acli task get -k "ARM平台虚拟机需要设置Host CPU" -t ${YYYY-MM-DD} -s -1

##### 解决方案
1、手动编辑虚拟机配置文件cpu；cpu： core2duo -> cpu： host
