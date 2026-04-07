### 获取CBT信息失败

#### 现象描述
1、执行开机任务失败，报错描述：启动时获取CBT信息失败！
- acli task get -k "启动时获取CBT信息失败" -t ${YYYY-MM-DD} -s -1
```bash
acli task get -k "启动时获取CBT信息失败" -t ${YYYY-MM-DD} -s -1
```


#### 主机证书不一致
（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
1、与主控主机md5不一致则为异常
- acli platform node cert list # 获得所有的证书的绝对路径acli --cluster system md5sum -p ${绝对路径} # 获得md5值
```bash
acli platform node cert list # 获得所有的证书的绝对路径
acli --cluster system md5sum -p ${绝对路径} # 获得md5值
acli platform node cert list # 获得所有的证书的绝对路径
acli --cluster system md5sum -p ${绝对路径} # 获得md5值
```
判断存在添加或替换主机失败任务，文件内容存在添加任务记录
- acli platform node task get
```bash
acli platform node task get
```


##### 解决方案(上升研发)
- 1、如果界面存在添加主机失败的任务，则根据提示进行失败重试，确保主机能正常添加成功；重试添加失败主机任务完成后，启动虚拟机正常
- 2、同步其他正常集群证书到当前主机上：//异常主机判定方式
- scp -f IP:/sf/cfg/certs/cluster-intra/root-ca.*  /sf/cfg/certs/cluster-intra/
```bash
scp -f IP:/sf/cfg/certs/cluster-intra/root-ca.*  /sf/cfg/certs/cluster-intra/
```
- acli system file sync /sf/cfg/certs/cluster-intra/root-ca.key
```bash
acli system file sync /sf/cfg/certs/cluster-intra/root-ca.key
```
- acli system file sync /sf/cfg/certs/cluster-intra/root-ca.pem
```bash
acli system file sync /sf/cfg/certs/cluster-intra/root-ca.pem
```
- 3、重新在检查证书一致后；重新启动虚拟机正常
- acli --cluster system md5sum -p ${绝对路径} # 获得md5值
```bash
acli --cluster system md5sum -p ${绝对路径} # 获得md5值
```


#### 数据库证书不一致
（参考KB：超融合HCI-深信服技术支持）

##### 判断方法
确认数据库主库节点，mysql-master
- acli system host get
```bash
acli system host get
```
2、 与主库主机md5不一致则为异常
- acli platform node cert list # 获得所有的证书的绝对路径acli --cluster system md5sum -p ${绝对路径} # 获得md5值
```bash
acli platform node cert list # 获得所有的证书的绝对路径
acli --cluster system md5sum -p ${绝对路径} # 获得md5值
acli platform node cert list # 获得所有的证书的绝对路径
acli --cluster system md5sum -p ${绝对路径} # 获得md5值
```

##### 解决方案(上升研发)
1、从正常主机拷贝到异常主机上：//异常主机判定方式
```bash
scp -f IP:/sf/cfg/mysql/ssl/vtp-*   /sf/cfg/mysql/ssl/scp -f IP:/sf/cfg/mysql/ssl/vtp-*   /sf/cfg/mysql/ssl/
scp -f IP:/sf/cfg/mysql/ssl/vtp-*   /sf/cfg/mysql/ssl/
```
- acli system file sync /sf/cfg/mysql/ssl/vtp-mysql-ssl.key
```bash
acli system file sync /sf/cfg/mysql/ssl/vtp-mysql-ssl.key
acli system file sync /sf/cfg/mysql/ssl/vtp-mysql-ssl.pem
```
- acli system file sync /sf/cfg/mysql/ssl/vtp-root-ca.pem
```bash
acli system file sync /sf/cfg/mysql/ssl/vtp-root-ca.pem
```

