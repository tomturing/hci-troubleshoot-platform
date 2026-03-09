### AMD和海光CPU不支持使用嵌套虚拟化

#### 现象描述
启动虚拟机失败，报错：AMD和海光CPU不支持使用嵌套虚拟化！错误码：0x010028D4


#### 嵌套虚拟机化
- (参考KB：超融合HCI-深信服技术支持)

##### 判断方法
1、获取启动虚拟机失败任务描述
- acli task get -k "AMD和海光CPU不支持使用嵌套虚拟化" -t ${YYYY-MM-DD} -s -1acli task get -k "AMD和海光CPU不支持使用嵌套虚拟化" -t ${YYYY-MM-DD} -s -1
acli task get -k "AMD和海光CPU不支持使用嵌套虚拟化" -t ${YYYY-MM-DD} -s -1
acli task get -k "AMD和海光CPU不支持使用嵌套虚拟化" -t ${YYYY-MM-DD} -s -1

##### 解决方案
在vdc关闭该虚拟机的嵌套虚拟化功能，
或编辑虚拟机配置文件删除nested_virtualization字段
