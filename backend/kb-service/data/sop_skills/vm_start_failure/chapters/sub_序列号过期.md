### 序列号过期

#### 现象描述
启动虚拟机失败，报错：序列号过期

- 存在序列号过期告警


#### asv授权到期

##### 判断方法
1、获取启动虚拟机失败描述信息，报错：序列号过期
- acli task get -k '序列号过期' -t ${YYYY-MM-DD} -s -1
```bash
acli task get -k '序列号过期' -t ${YYYY-MM-DD} -s -1
```
2、获取序列号告警
- acli alert get -k '序列号过期'
```bash
acli alert get -k '序列号过期'
```

##### 解决方案（上升研发）
- 涉及授权，请拨打400或上升云BG-LMT技术支持
