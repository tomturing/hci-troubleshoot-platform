### 更新网卡设备信息失败：数据处理失败

#### 现象描述
启动虚拟机失败，报错：更新网卡设备信息失败：数据处理失败；错误码：0x0100186D


#### 数据库只读
（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、ssh 数据库主库执行
- ssh mysql-master
```bash
ssh mysql-master
```
2、数据库状态确认：
- acli service asv mysql status
```bash
acli service asv mysql status
```
3、数据库只读状态检查
```bash
acli platform mysql-manager-cli status writable --get
```
返回R表示只读，W表示可写（异常状态为只读）


##### 解决方案（上升研发）
检查/sf/data/platform_database/分区容量满
- acli system df -h  /sf/data/platform_database/
```bash
acli system df -h  /sf/data/platform_database/
```
2、若是方案1不符合，请拨打400或上升云BG-LMT技术支持

#### 数据库配置异常无法运行
(参考KB：超融合HCI-深信服技术支持)

##### 判断方法
1、查看mysqld日志；有报错日志则为异常
- acli log get -E -k "err.*mysqld.*provided the mandatory server-id.*" -p "/sf/log/${date}/mysqld.log"
```bash
acli log get -E -k "err.*mysqld.*provided the mandatory server-id.*" -p "/sf/log/${date}/mysqld.log"
```


##### 解决方案（上升研发）
若无法解决请拨打400或上升云BG-LMT技术支持
