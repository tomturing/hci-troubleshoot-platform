### 虚拟机未运行

#### 现象描述
虚拟机（虚拟机名）未运行


#### exporter服务未运行

##### 判断方法
判断exporter服务未运行
- acli service asv exporter statusacli service asv exporter status
```bash
acli service asv exporter status
```
- 判断exporter服务文件不存在,再asv-con容器里边查看
- acli system ls /sf/bin/exporteracli system ls /sf/bin/exporter
```bash
acli system ls /sf/bin/exporter
```

##### 解决方案
1、手动拉起服务（参考KB：超融合HCI-深信服技术支持）
- acli service asv exporter startacli service asv exporter start
```bash
acli service asv exporter start
```
- 2、界面重试开机

#### prometheus服务未运行

##### 判断方法
判断主控 prometheus 服务未运行
- acli service asv prometheus statusacli service asv prometheus status
```bash
acli service asv prometheus status
```
- 2、判断主控/sf/data/local/ 分区使用率满
- acli system df -h /sf/data/localacli system df -h /sf/data/local
```bash
acli system df -h /sf/data/local
```

##### 解决方案
清理/sf/data/local分区（参考KB：超融合HCI-深信服技术支持）
界面重试开机
