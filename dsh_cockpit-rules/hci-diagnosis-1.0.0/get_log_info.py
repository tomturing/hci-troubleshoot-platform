#!/usr/bin/env python
# -*- coding: utf-8 -*-

import datetime
import json
import traceback
import os
import sys
import tarfile
import zipfile
import re
import pandas as pd
import numpy as np
import argparse
from pathlib import Path
from loguru import logger
from copy import deepcopy
from packaging import version
import shutil
import requests
import time
import subprocess
import signal

try:
    from llm_summarizer import LLMSummarizer
    import xmltodict
    #import xml.etree.ElementTree as ET
except Exception as e:
    pass

log_dir = os.path.join(os.path.dirname(__file__), "Logs")
g_args = None
g_sensekey_desc = None
sensekey_config = os.path.join(os.path.dirname(__file__), "sensekey.json")
g_ssd_fw = None
ssdfw_config = os.path.join(os.path.dirname(__file__), "ssdfw.json")
g_raid_fw = None
raidfw_config = os.path.join(os.path.dirname(__file__), "raidfw.json")
g_excel_columns = None
g_acli_zip_password = "sangfor.vt@aDeploy2019"

g_statistic_translation_dict = {
        'count': '计数',
        'mean': '均值',
        'std': '标准差',
        'min': '最小值',
        '25%': '第一四分位数',
        '50%': '中位数',
        '75%': '第三四分位数',
        'max': '最大值'
}

def Handle_Exceptions(func):
    def wrapper(*args, **kwargs):
        try:
            # 调用原始函数
            return func(*args, **kwargs)
        except Exception as e:
            # 处理异常
            logger.error("{0}发生异常:".format(func.__name__), e)
            # 或者进行其他处理操作
            traceback.print_exc()
    return wrapper

#读取配置
def loadConfig(filename):
    try:
        with open(filename, 'r', encoding='utf-8', errors="ignore") as f:
            return json.load(f)
    except Exception as e:
        traceback.print_exc()
        return None

#加载Senskey描述
if os.path.exists(sensekey_config):
    g_sensekey_desc = loadConfig(sensekey_config)

if os.path.exists(ssdfw_config):
    g_ssd_fw = loadConfig(ssdfw_config)

if os.path.exists(raidfw_config):
    g_raid_fw = loadConfig(raidfw_config)

#检查磁盘固件版本是否需要升级
def check_disk_fw(curfw):
    if not g_ssd_fw:
        return None
    
    for fw in g_ssd_fw:
        if fw["问题固件"] == curfw:
            return fw
    
    return None

#检查raid固件是否需要升级
def check_raid_fw(raid, curfw):
    if not g_raid_fw or not raid:
        return None
    
    if raid.find("SAS3008") != -1:
        #sas3008特殊处理
        try:
            if version.parse(curfw) < version.parse("16.00.10.00"):
                return {
                        "型号": "SAS3008",
                        "问题固件": "16.00.10.00以下",
                        "正常固件": "16.00.10.00以上",
                        "ALERT": "YJ20221014001",
                        "URL": ""
                        }
        except Exception as e:
            pass

    if raid.find("3008IMR") != -1:
        try:
            if version.parse(curfw) < version.parse("4.680.01-8582"):
                return {
                            "型号": "INSPUR 3008IMR",
                            "问题固件": "4.680.01-8582以下",
                            "正常固件": "4.680.01-8582以上",
                            "ALERT": "YJ20240820002",
                            "URL": ""
                            }
        except Exception as e:
            pass
            
    if raid.find("9361-16i") != -1:
        #
        try:
            if version.parse(curfw) < version.parse("4.740.00-8474"):
                return {
                        "型号": "9361-16i",
                        "问题固件": "4.740.00-8464",
                        "正常固件": "4.740.00-8474",
                        "ALERT": "YJ20230412002",
                        "URL": ""
                        }
        except Exception as e:
            pass

    if raid.find("PM8222-SHBA") != -1:
        #
        try:
            if version.parse(curfw) <= version.parse("4.11"):
                return {
                        "型号": "PM8222-SHBA",
                        "问题固件": "4.11及以下",
                        "正常固件": "6.22",
                        "ALERT": "YJ20241021001",
                        "URL": ""
                        }
        except Exception as e:
            pass
    
    for fw in g_raid_fw:
        if raid.find(fw["型号"]) != -1:
            #检查版本
            if fw["问题固件"] == curfw:
                return fw
            
    return None

#解析sense key说明
def decode_sensekey(sensekey):
    if sensekey.find("Sense") != -1:
        sensekey = sensekey[len("Sense")+1:]
    #以空格分割
    keys = sensekey.split()
    #第三位为Sensekey
    try:
        sensekeycode = int(keys[2])
    except Exception as e:
        sensekeycode = keys[2].lstrip('0')

    try:
        additional_sense_code = int(keys[12]) if len(keys) > 12 else 0
    except Exception as e:
        additional_sense_code = keys[12] if len(keys) > 12 else 0
    
    try:
        asc_qualifier = int(keys[13]) if len(keys) > 13 else 0
    except Exception as e:
        asc_qualifier = keys[13] if len(keys) > 13 else 0
    #查找
    for s in g_sensekey_desc:
        if str(s["Sense key"]) == str(sensekeycode) and \
            str(s["Sense code"]) == str(additional_sense_code) and \
            str(s["Sense qualifier"]) == str(asc_qualifier):
            return s
        
    return None

def is_gzip_compressed(filename):
    with open(filename, 'rb') as f:
        # 读取文件的前几个字节来检查签名
        magic = f.read(2)
        # gzip文件的签名是0x1f 0x8b
        return magic == b'\x1f\x8b'

def is_tar(filename):
    with open(filename, 'rb') as f:
        # tar文件的签名是0x75 0x73
        magic = f.read(2)
        return magic == b'uustar\x00'

# 解压zip包
def extract_zip_file(zip_path, extract_to, password):
    """
    解压加密的ZIP文件到指定目录。
 
    :param zip_path: ZIP文件的路径
    :param extract_to: 要解压到的目录路径
    :param password: ZIP文件的密码
    """
    # 确保输出目录存在
    if not os.path.exists(extract_to):
        os.makedirs(extract_to)
 
    try:
        # 打开ZIP文件
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            # 设置密码
            zip_ref.setpassword(password.encode('utf-8'))
            # 解压所有内容到指定目录
            zip_ref.extractall(extract_to)
            return True
        
    except Exception as e:
        traceback.print_exc()
        return False
    
    return False

def extract_tar_gz(file_path, destination_dir, spec_file=None):
    files = []
    mode = "r"
    if is_gzip_compressed(file_path):
        mode = "r:gz"
    try:
        with tarfile.open(file_path, mode) as tar:
            tar.errorlevel = 0
            if spec_file:
                tar.extract(spec_file, destination_dir)
                files = [os.path.join(destination_dir, f) for f in tar.getnames()]

            else:
                tar.extractall(path=destination_dir)
                files = [member.name for member in tar.getmembers() if member.isreg()]

            #logger.info("extraced {} to {}".format(file_path, destination_dir))        
            return files
    except Exception as e:
        traceback.print_exc()

def add_file_to_tar_gz(tar_gz_path, file_to_add, arcname=None):
    # 创建临时目录
    temp_dir = 'xfcloud_temp_dir_' + str(datetime.datetime.now().timestamp())
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir)

    # 提取现有的 .tar.gz 文件到临时目录
    mode = "r"
    if is_gzip_compressed(tar_gz_path):
        mode = "r:gz"
    with tarfile.open(tar_gz_path, mode) as tar:
        tar.extractall(path=temp_dir)

    # 添加新文件到临时目录
    if arcname is None:
        arcname = os.path.basename(file_to_add)
    shutil.copy(file_to_add, os.path.join(temp_dir, arcname))

    # 重新创建 .tar.gz 文件
    with tarfile.open(tar_gz_path, "w:gz") as tar:
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                file_path = os.path.join(root, file)
                tar.add(file_path, arcname=os.path.relpath(file_path, temp_dir))

    # 删除临时目录
    try:
        shutil.rmtree(temp_dir)
    except Exception as e:
        traceback.print_exc()

#向压缩包添加信息
def add_info_to_tar_gz(info, file):
    filename = "xfcloud_readme.json"
    path = Path(file)
    full_path = os.path.join(str(path.parent), filename)
    with open(full_path, "w", encoding='utf-8', errors="ignore") as f:
        f.write(json.dumps(info, sort_keys=True, indent=4, separators=(',', ': '), ensure_ascii=False))

    add_file_to_tar_gz(file, full_path)

    os.remove(full_path)

def collect_log_from_tar_gz(file):
    path = Path(file)
    #获取文件名称
    filefullname = path.name
    filename = filefullname.split(".tar.gz")[0]
    #获取文件路径
    sub_file_path = os.path.join(str(path.parent), filename)
    if not os.path.exists(sub_file_path):
        #如果文件目录不存在，则创建文件目录
        os.makedirs(sub_file_path)

    #如果指定了icare和custom参数，则向压缩包添加这两个内容
    if g_args and (g_args.icare or g_args.custom):
        add_info_to_tar_gz({ "Icare_No": g_args.icare, "Custom": g_args.custom }, file)

    #解压文件
    extract_tar_gz(file, sub_file_path)

    #从文件目录中读取raidlog日志信息
    custom_info = get_custom_info_from_log(sub_file_path)

    custom_info["客户编号"] = filename

    custom_info["vs_disk_list"] = load_vs_disks(sub_file_path)

    #检查VS踢盘日志
    analyze_vs_diskchecker(sub_file_path, custom_info)

    #更新vs隔离盘情况
    analyze_vs_recover_disk(sub_file_path, custom_info)

    #统计时延数据
    analyze_abnormal_diskstat(sub_file_path, custom_info)

    #分析虚拟机日志
    analyze_vms_log(sub_file_path, custom_info)

    #分析业务类型
    analyze_vm_type(custom_info)

    #解析kernel日志信息
    get_kernel_log(sub_file_path, custom_info)

    #解析dmesg日志(如果没有kernel日志)
    if not custom_info.get("kernel日志"):
        get_blackbox_log(sub_file_path, custom_info)

    if g_args and not g_args.save:
        #不保留
        try:
            shutil.rmtree(sub_file_path)
        except Exception as e:
            traceback.print_exc()

    return custom_info

def extract_directory(file_path):
    custom_info_list = []
    files = [os.path.join(file_path, f) for f in os.listdir(file_path)]
    tar_files = [f for f in files if f.endswith(".tar.gz")]
    logger.info(tar_files)
    for file in tar_files:
        custom_info_list.append(collect_log_from_tar_gz(file))

        if g_args.single:
            #每个文件单独输出结果
            filename = os.path.basename(file.split(".tar.gz")[0]) + ".xlsx"
            output = os.path.join(file_path, filename)
            deal_custom_info_list(custom_info_list, output)
            custom_info_list = []

    if g_args.single:
        #直接退出
        sys.exit()

    return custom_info_list

def load_vs_disks(sub_file_path):
    dir = os.path.join(sub_file_path, "sf/cfg/vs/disk")
    if not os.path.exists(dir):
        return None
    
    disks = {}
    for file in os.listdir(dir):
        if file.endswith(".json"):
            filename = file[:-5]
            with open(os.path.join(dir, file), "r") as f:
                try:
                    disks[ filename ] = json.load(f)
                except Exception as e:
                    traceback.print_exc()
    return disks

#vs隔离情况
def analyze_vs_recover_disk(sub_file_path, custom_info):
    log_path = os.path.join(sub_file_path, "sf/cfg/vs/never_recoverd_disks.json")
    if not os.path.exists(log_path):
        return custom_info
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        custom_info["VS隔离磁盘"] = json.load(f)
        if custom_info["VS隔离磁盘"] and custom_info["VS隔离磁盘"].get("disks"):
            disks = custom_info["VS隔离磁盘"].get("disks")
            for disk in disks:
                if not isinstance(disk, dict):
                    continue
                disk_id = disk.get("disk_id")
                remove_time = disk.get("remove_time")
                disk_remove = {
                    "disk_id": disk_id,
                    "remove_time": remove_time
                }
                if disk_id:
                    #读取磁盘配置
                    disk_path = os.path.join(sub_file_path, "sf/cfg/vs/disk/{}.json".format(disk_id))
                    if os.path.exists(disk_path):
                        with open(disk_path, "r", encoding="utf-8", errors="ignore") as c:
                            disk_info = json.load(c)
                            if disk_info:
                                dev = disk_info.get("dev")
                                if dev:
                                    infos = dev.split("/")
                                    if len(infos) > 1:
                                        disk_remove["dev"] = infos[1]
                                disk_sn = disk_info.get("disk_sn")
                                if disk_sn:
                                    disk_remove["disk_sn"] = disk_sn.strip()

                update_value(custom_info, "VS永久隔离磁盘列表", disk_remove)


def analyze_vs_diskchecker(file_path, custom_info):
    log_path = os.path.join(file_path, "sf/log")
    if not os.path.exists(log_path):
        return custom_info

    if not custom_info.get("内核日志"):
        custom_info["内核日志"] = {}
    
    dirs = os.listdir(log_path)
    df = pd.DataFrame()
    for dir in dirs:
        if not dir.isdigit() and dir != 'today':
            continue

        file = os.path.join(log_path, dir)
        file = os.path.join(file, "vs/scripts/vs_diskchecker.log")
        if not os.path.exists(file):
            continue
        #读取日志文件
        with open(file, "r", encoding='utf-8', errors="ignore") as f:
            #获取日志信息
            log_info = f.read()
            #brick上下线消息
            brick_log = search_section(log_info, 
                                       r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\] I (\[hostchecker.py:\d+:(handler_disk_offline|brick_dev_update)\]\[\])  (\[disk_offline\])?(recv msg data: )(\{u\'uuid\': u\'\S+\', u\'dev\': u\'\S+\'\})",
                                       re.DOTALL, group=-1, multi=1)
            if not brick_log:
                brick_log = search_section(log_info, 
                                       r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\] I (\[\<frozen vs_diskchecker\.core\.hostchecker\>:\d+:(handler_disk_offline|brick_dev_update)\]\[\])  (\[disk_offline\])?(recv msg data: )(\{u\'uuid\': u\'\S+\', u\'dev\': u\'\S+\'\})",
                                       re.DOTALL, group=-1, multi=1)
                #6.3.0_R2的日志
                #[2024-09-22 23:31:19,490] I [<frozen vs_diskchecker.core.hostchecker>:369:brick_dev_update][]  recv msg data: {u'uuid': u'XTDcZz-JIo6-MJ3b-nNNV-sHhB-Nbkd-1jsvKm', u'dev': u'sdl'}
            if brick_log:
                update_value(custom_info, "Brick上下线日志", brick_log)
                brick_ops = []
                brick_list = []
                for v in brick_log:
                    ops = {"op": v[3].replace("[", "").replace("]", "")}
                    value = v[5]
                    value = value.replace("u\'", "\'")
                    value = value.replace("\'", "\"")
                    json_value = json.loads(value)
                    ops["value"] = json_value
                    brick_ops.append(ops)
                    if not json_value["uuid"] in brick_list:
                        brick_list.append(json_value["uuid"])

                #取同个brick最早的磁盘作为故障磁盘,其他的全部作为影子盘，后续需要删除，不能作为故障盘
                for brick in brick_list:
                    find_first = None
                    for v in brick_ops:
                        if v["value"]["uuid"] == brick:
                            if not find_first:
                                update_value(custom_info["内核日志"], "故障磁盘列表", v["value"]["dev"], ignore_exists=True, type="list")
                                find_first = v["value"]["dev"]
                            elif v["value"]["dev"] != find_first:
                                #重复插拔磁盘需要最后清理掉
                                if not v["value"]["dev"] in custom_info["内核日志"]["故障磁盘列表"]:
                                    update_value(custom_info["内核日志"], "重复尝试插拔磁盘列表", v["value"]["dev"])
                            else:
                                continue
                    if len(brick_ops) > 0:
                        if brick_ops[-1]["op"] == "disk_offline":
                            #最终踢盘
                            update_value(custom_info, "VS最终处置", "隔离磁盘:{}".format(find_first))
                            update_value(custom_info, "卡慢盘分类", "踢盘")
                        elif brick_ops[-1]["op"] == "":
                            #自动恢复
                            update_value(custom_info, "VS最终处置", "自动恢复:{}".format(find_first))
                            update_value(custom_info, "卡慢盘分类", "踢盘恢复")
                        else:
                            update_value(custom_info, "VS最终处置", "未知")
            #判断是否触发VS踢盘
            #[2024-12-25 14:23:26,852] I [<frozen libcommon.log>:534:log_ratelimited][355cd2e4155ed9d78_handler_timeout_disk]  io stat disk io busy count 10, set disk (Sl1t4G-6LOn-G4fl-r3Yd-NoXj-3dLp-SULQi5-meta) to Error.
            timeout_log = search_section(log_info, r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\] I (\[\<frozen libcommon\.log\>:\d+:log_ratelimited\]\[\S+\])\s+(io stat disk io busy count 10, set disk) (\(\S+\)) (to Error).", re.DOTALL, group=-1, multi=1)
            if timeout_log:
                custom_info["VS踢盘告警日志"] = timeout_log

            #踢盘情况
            remove_log = search_section(log_info, 
                                        r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\] I (\[\<frozen vs_diskchecker\.disk_op\.scsi\>:\d+:(remove|hot_plug)\])\[(\S+)_(remove_disk|plug_disk)\]  ((start remove scsi dev)|(removed scsi dev done)|(start add scsi dev)) (\S+)\n", 
                                        re.DOTALL, group=-1, multi=1)
            if remove_log:
                custom_info["VS踢盘日志"] = remove_log
                for v in remove_log:
                    if v[5] == "start remove scsi dev":
                        update_value(custom_info, "VS是否踢盘", "是,踢盘日志:{}".format(v))

            #6.3.0_R2旧版本踢盘情况
            #[2024-09-22 23:26:07,346] I [<frozen vs_diskchecker.core.diskchecker>:1487:remove_disk][35000c500b51c0e2e_remove_disk]  removed dev done sdd,
            remove_log = search_section(log_info, 
                                        r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\] I (\[\<frozen vs_diskchecker\.core\.diskchecker\>:\d+:(remove_disk|hot_plug)\])\[(\S+)_(remove_disk|plug_disk)\]  ((I will remove the disk. dev)|(set offline done)|(removed dev done)|(start add dev)) (\S+),", 
                                        re.DOTALL, group=-1, multi=1)
            if remove_log:
                custom_info["VS踢盘日志"] = remove_log
                for v in remove_log:
                    if not custom_info.get("内核日志"):
                        custom_info["内核日志"] = {}
                    
                    if v[5] == "I will remove the disk. dev":
                        update_value(custom_info, "VS是否踢盘", "是,踢盘日志:{}".format(v))

            #VDI的踢盘日志
            remove_log = search_section(log_info, 
                                        r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\] I (\[diskchecker.py:\d+:(remove_disk|hot_plug)\])\[(\S+)_(remove_disk|plug_disk)\]  ((start remove dev)|(set offline done)|(removed dev done)|(start add dev)) (\S+),", 
                                        re.DOTALL, group=-1, multi=1)
            if remove_log:
                custom_info["VS踢盘日志"] = remove_log
                for v in remove_log:
                    if not custom_info.get("内核日志"):
                        custom_info["内核日志"] = {}
                    
                    if v[5] == "start remove dev":
                        update_value(custom_info, "VS是否踢盘", "是,踢盘日志:{}".format(v))
    return custom_info

#判断是否为drop_cache导致踢盘
def check_drop_cache(custom_info, values):
    if not custom_info:
        return
    if not values:
        return

    if not custom_info.get("VS踢盘告警日志"):
        return

    # 如果是eds直接返回
    if custom_info.get("产品线", "").upper() == "EDS":
        return

    #检查如果是凌晨00:00:00-00:30:00分之间出现的drop_cache，并且发生drop_cache之后1分钟内出现VS踢盘日志
    for v in values:
        if diff_logtime(v[0], v[0][:10] + ' 00:00:00.000000', time_format="%Y-%m-%d %H:%M:%S.%f", return_abs = True) < 1800:
            #凌晨半个小时内发生drop_cache
            if custom_info.get("VS踢盘日志"):
                for s in custom_info.get("VS踢盘日志"):
                    seconds = diff_logtime(v[0], s[0].replace(',', '.'), time_format="%Y-%m-%d %H:%M:%S.%f", return_abs = False)
                    if seconds > 0:
                        continue
                    #踢盘发生在drop_cache一分钟内
                    if seconds > -60:
                        update_value(custom_info, "问题初步结论", "内核drop_cache引起卡慢问题。请获取最新版本集合补丁包或联系技术支持。")
    
    #遍历触发踢盘告警的时间
    for v in custom_info.get("VS踢盘告警日志"):
        err_time = v[0].replace(",", ".")
        for i in range(0, len(values)):
            #取出begin配对的值
            if values[i][7] == "":
                seconds = diff_logtime(err_time, values[i][0], time_format="%Y-%m-%d %H:%M:%S.%f", return_abs = False)
                if seconds < 0:
                    #取前一个begin值
                    for j in range(0, i):
                        if values[i-1-j][7] == "begin" or values[i-i-j][6] == "3":
                            seconds_2 = diff_logtime(err_time, values[i-1-j][0], time_format="%Y-%m-%d %H:%M:%S.%f", return_abs = False)
                            if seconds_2 > 0:
                                update_value(custom_info, "问题初步结论", "内核drop_cache引起卡慢问题。请获取最新版本集合补丁包或联系技术支持。")
                            break
#分析虚拟机业务（根据虚拟机名称）
def analyze_vm_type(custom_info):
    if not custom_info.get("vmlist"):
        return

    vmlist = custom_info["vmlist"].get("ids")
    if not vmlist:
        return 
    
    for vmid, vm in vmlist.items():
        vmname = vm.get("vm_name")
        if not vmname:
            continue
        if search_section(vmname, r"数据库|DB|SQL|Oracle|RAC|Redis|Cache|Database|TiDB|达梦|瀚高|人大金仓", re.DOTALL|re.IGNORECASE, group=0):
            update_value(custom_info, "业务类型", "数据库")
        if search_section(vmname, r"大数据|BigData|Hadoop|starrocks", re.DOTALL|re.IGNORECASE, group=0):
            update_value(custom_info, "业务类型", "大数据")
        if search_section(vmname, r"MinIO", re.DOTALL|re.IGNORECASE, group=0):
            update_value(custom_info, "业务类型", "对象存储")
        if search_section(vmname, r"BI|ERP|财务|OA|办公", re.DOTALL|re.IGNORECASE, group=0):
            update_value(custom_info, "业务类型", "办公业务")
        if search_section(vmname, r"MOM|MES|WMS|QMS|PLM", re.DOTALL|re.IGNORECASE, group=0):
            update_value(custom_info, "业务类型", "制造业生产业务")
        if search_section(vmname, r"HIS|HIP|LMS", re.DOTALL|re.IGNORECASE, group=0):
            update_value(custom_info, "业务类型", "医疗业务")
        if search_section(vmname, r"媒体", re.DOTALL|re.IGNORECASE, group=0):
            update_value(custom_info, "业务类型", "媒体业务")

#分析虚拟机日志
def analyze_vms_log(sub_file_path, custom_info):
    log_path = os.path.join(sub_file_path, "sf/log")
    if not os.path.exists(log_path):
        return custom_info

    dirs = os.listdir(log_path)
    for dir in dirs:
        if not dir.isdigit() and dir != 'today':
            continue
        log_dir = os.path.join(log_path, dir)
        qemu_logs = os.listdir(log_dir)
        for log in qemu_logs:
            if not log.startswith("sfvt_qemu_"):
                continue
            vmid = search_section(log, r"sfvt_qemu_(\d+).log", re.DOTALL, group=1)
            if not vmid:
                continue

            with open(os.path.join(log_dir, log), "r", encoding="utf-8", errors="ignore") as f:
                qemu_log = f.read()
                if qemu_log:
                    error_vm = {
                        "vmid": vmid,
                        "error": 0
                    }

                    #分析running->io-error->running的时间
                    #2024-09-18 00:42:04.955921 debug [sfvt_qemu_5201066781846[446713]:] 446694 vl:838 runstate_set() vm runstate transition: 'io-error' -> 'running'
                    values = search_section(qemu_log, r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{6}) (debug \[sfvt_qemu_\d+\[\d+\]:\] \d+ vl:\d+ runstate_set\(\) vm runstate transition:) \'(io-error|running)\' -> \'(running|io-error)\'", re.DOTALL, group=-1, multi=1)
                    if values:
                        error_vm["error"] = 1
                        state_changes = [ {"time": v[0], "from": v[2], "to": v[3]} for v in values ]
                        #状态切换只需要两两统计
                        error_vm["IO状态切换间隔统计"] = statistic_times([s["time"] for s in state_changes], tag="时间间隔(秒)", time_format="%Y-%m-%d %H:%M:%S.%f", data_pick=2)
                        error_vm["IO状态切换列表"] = state_changes
                        error_vm["IO状态切换详情"] = values

                    #分析QEMU TIMEOUT超时
                    #2024-08-20 00:30:08.609083 info [sfvt_qemu_1115306628696[3565972]:] 3565972 accounting:327 QEMU_TIMEOUT: in the last 60 s, 14 io timeout happened, max timeout is 11343 ms
                    values = search_section(qemu_log, r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{6}) (info \[sfvt_qemu_\d+\[\d+\]:\] \d+ accounting:\d+) (QEMU_TIMEOUT: in the last 60 s, )(\d+) (io timeout happened, max timeout is) (\d+) ms", re.DOTALL, group=-1, multi=1)
                    if values:
                        error_vm["error"] = 1
                        timeout_list = [ {"time": v[0], "io_count": int(v[3]) ,"max_io_timeout": int(v[5])/1000} for v in values ]
                        error_vm["QEMU_IO超时时间统计"] = json.loads(pd.DataFrame([t["max_io_timeout"] for t in timeout_list], columns=["统计摘要"]).describe().rename(g_statistic_translation_dict).to_json(force_ascii=False))
                        error_vm["QEMU_IO超时间隔统计"] = statistic_times([s["time"] for s in timeout_list], tag="时间间隔(秒)")
                        error_vm["QEMU_IO超时列表"] = timeout_list
                        error_vm["QEMU_IO超时详情"] = values

                    if error_vm["error"]:
                        update_value(custom_info, "虚拟机IO影响", error_vm, type="list")

        #汇总虚拟机影响评估
        if custom_info.get("虚拟机IO影响"):
            result = {
                "本机影响数量": len(custom_info.get("虚拟机IO影响")),
                "IO错误": {
                    "最小值": 9999999999, "最大值": 0
                },
                "IO超时": {
                    "最小值": 9999999999, "最大值": 0
                }
            }
            for err_vm in custom_info.get("虚拟机IO影响"):
                #统计io error影响时间
                if err_vm.get("IO状态切换间隔统计"):
                    if err_vm["IO状态切换间隔统计"].get("统计摘要").get("最小值") and err_vm["IO状态切换间隔统计"].get("统计摘要").get("最小值") < result["IO错误"]["最小值"]:
                        result["IO错误"]["最小值"] = err_vm["IO状态切换间隔统计"].get("统计摘要").get("最小值")
                    if err_vm["IO状态切换间隔统计"].get("统计摘要").get("最大值") and err_vm["IO状态切换间隔统计"].get("统计摘要").get("最大值") > result["IO错误"]["最大值"]:
                        result["IO错误"]["最大值"] = err_vm["IO状态切换间隔统计"].get("统计摘要").get("最大值")
                if err_vm.get("QEMU_IO超时时间统计"):
                    if err_vm.get("QEMU_IO超时时间统计").get("统计摘要").get("最小值") and err_vm.get("QEMU_IO超时时间统计").get("统计摘要").get("最小值") < result["IO超时"]["最小值"]:
                        result["IO超时"]["最小值"] = err_vm.get("QEMU_IO超时时间统计").get("统计摘要").get("最小值")
                    if err_vm.get("QEMU_IO超时时间统计").get("统计摘要").get("最大值") and err_vm.get("QEMU_IO超时时间统计").get("统计摘要").get("最大值") > result["IO超时"]["最大值"]:
                        result["IO超时"]["最大值"] = err_vm.get("QEMU_IO超时时间统计").get("统计摘要").get("最大值")

            if result["IO错误"]["最小值"] == 9999999999:
                result["IO错误"]["最小值"] = 0
            if result["IO超时"]["最小值"] == 9999999999:
                result["IO超时"]["最小值"] = 0

            #超过15秒认为影响业务
            if result["IO错误"]["最大值"] >= 15 or result["IO超时"]["最大值"] >= 15:
                custom_info["是否影响客户业务"] = "是"

            custom_info["虚拟机业务影响时间"] = result

# 定义一个函数来检查窗口内是否存在连续的10个数字
def has_consecutive_numbers(series):
    if len(series) < 10:
        return False
    sorted_series = np.sort(series)
    return (sorted_series[1:] - sorted_series[:-1] == 1).all()

#分析VS异常时延检测是否存在过敏
def check_vs_abnormal_diskstat(custom_info):
    if not custom_info:
        return False
    
    if not custom_info.get("异常时延"):
        return False
    
    stats = custom_info.get("异常时延")
    json_data = [ {'time':s[0], 'dev':s[2], 'svctm':float(s[5]), 'util':float(s[7]), 'medium_cnt':int(s[9]), 'alert_cnt':int(s[11]), 'io_busy_cnt': int(s[13]) if s[13] != '' else -1} for s in stats ]
    df = pd.DataFrame(json_data)
    if df.empty:
        return False
    #判断io_busy_cnt字段是否存在，如果存在，则存在不为-1的值
    exists_io_busy_cnt = df["io_busy_cnt"] >= 0
    if exists_io_busy_cnt.any():
        #删除所有io_busy_cnt为-1的无效记录
        df = df[df['io_busy_cnt'] != -1]
    
    dfg = df.groupby("dev")
    for name, group in dfg:
        # 使用rolling和apply来检查每个窗口
        consecutive_10_number = group['alert_cnt'].rolling(window=10).apply(has_consecutive_numbers, raw=False)
        if consecutive_10_number.any():
            update_value(custom_info, "问题初步结论", "{}触发VS卡慢盘检测告警。".format(name), type='list', insert_pos=0)
            #TODO
                # 检查是否存在满足条件的行
                #if result.any():
                #    update_value(custom_info, "问题初步结论", "{}存在误判，检测过于灵敏。".format(name), type='list', insert_pos=0)
            
    return False

#统计异常时延数据
def analyze_abnormal_diskstat(file_path, custom_info):
    log_path = os.path.join(file_path, "sf/log")
    if not os.path.exists(log_path):
        return custom_info
    
    dirs = os.listdir(log_path)
    df = pd.DataFrame()
    for dir in dirs:
        if not dir.isdigit() and dir != 'today':
            continue
        
        file = os.path.join(log_path, dir)
        file = os.path.join(file, "vs/scripts/abnormal_diskstate.log")
        if not os.path.exists(file):
            continue
        #读取日志文件
        with open(file, "r", encoding='utf-8', errors="ignore") as f:
            #获取日志信息
            log_info = f.read()

            stats = search_section(log_info, 
                                   r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)\] \[\]  (dev:\s*)(\S+) (\S+)\s+(svctm:\s*)(\S+)\s+(util:) (\S+)\s+(medium_cnt:) (\d+)\s+(alert_cnt:) (\d+)(?:\s+(io_busy_cnt:) (\d+))?", 
                                   re.DOTALL, group=-1, multi=1)

            if not stats:
                return custom_info
            
            json_data = [ {'time':s[0], 'dev':s[2], 'svctm':float(s[5]), 'util':float(s[7]), 'medium_cnt':int(s[9]), 'alert_cnt':int(s[11]), 'io_busy_cnt': int(s[13]) if s[13] else -1} for s in stats ]
            custom_info["异常时延"] = stats
            df = pd.concat([df, pd.DataFrame(json_data)], ignore_index=True)
        
    update_diskstat(df, custom_info)

    return custom_info

#更新统计异常时延数据
def update_diskstat(df, custom_info, keyname="时延统计", bins = [0, 25, 60, 70, 125, 500, 1000, 10000], labels = ['<25ms', '25-60ms', '60-70ms','70-125ms', '125-500ms', '500-1000ms', '>=1000ms']):
    if df.empty:
        return
        
    df['Range'] = pd.cut(df['svctm'], bins=bins, labels=labels, right=False)
    percentage = df['Range'].value_counts(normalize=True) * 100
    #print(percentage)

    ret = {}
    if custom_info:
        ret = custom_info

    ret[keyname] = {"总数量": df.shape[0]}
    for i in range(0, len(labels)):
        ret[keyname][labels[i]] = "{:.3f}%".format(percentage[labels[i]])

    ret[keyname]["分盘统计"] = json.loads(df.groupby("dev")["svctm"].describe().rename(g_statistic_translation_dict).to_json(force_ascii=False))
    return ret


#从blackbox日期更新故障时间
def update_blackbox_log(file_path, custom_info):
    log_path = os.path.join(file_path, "sf/log/blackbox")
    if not os.path.exists(log_path):
        return
    
    dirs = os.listdir(log_path)
    for dir in dirs:
        if not dir.isdigit():
            continue

        if not "故障时间" in custom_info or not custom_info.get("故障时间"):
            custom_info["故障时间"] = dir

        blackbox_path = os.path.join(log_path, dir)

        #iostat
        iostat_path = os.path.join(blackbox_path, "LOG_iostat.txt")
        if os.path.exists(iostat_path):
            custom_info["IOSTAT"] = parse_iostat(iostat_path)

        ioslow_path = os.path.join(blackbox_path, "LOG_ioslow.txt")
        if os.path.exists(ioslow_path):
            custom_info["IOSLOW"] = parse_ioslow(ioslow_path)

        iostuck_path = os.path.join(blackbox_path, "LOG_iostuck.txt")
        if os.path.exists(iostuck_path):
            custom_info["IOSTUCK"] = parse_iostuck(iostuck_path)

#从blackbox获取日志
def get_blackbox_log(file_path, custom_info):
    log_path = os.path.join(file_path, "sf/log/blackbox")
    if not os.path.exists(log_path):
        return
    
    dirs = os.listdir(log_path)
    for dir in dirs:
        if not dir.isdigit():
            continue

        blackbox_path = os.path.join(log_path, dir)
        dmesg_path = os.path.join(blackbox_path, "LOG_dmesg.txt")
        if os.path.exists(dmesg_path):
            #读取日志文件
            with open(dmesg_path, "r", encoding='utf-8', errors="ignore") as f:
                #获取日志信息
                log_info = f.read()
                #解析日志信息
                custom_info["内核日志"].update(parse_kernel_log_info(log_info, custom_info, logtype="LOG_dmesg.txt"))
                custom_info["dmesg日志"] = "是"

def get_kernel_log(file_path, custom_info):
    kernel_log = custom_info["内核日志"] if custom_info.get("内核日志") else {}
    log_path = os.path.join(file_path, "sf/log")
    if not os.path.exists(log_path):
        return kernel_log
    
    dirs = os.listdir(log_path)
    for dir in dirs:
        if not dir.isdigit() and dir != 'today':
            continue
        #说明是日期目录
        #print("dir:{}".format(dir))
        #获取日志文件路径
        day_log_path = os.path.join(log_path, dir) + "/kernel.log"
        if not os.path.exists(day_log_path):
            continue

        #读取日志文件
        with open(day_log_path, "r", encoding='utf-8', errors="ignore") as f:
            #获取日志信息
            log_info = f.read()
            #解析日志信息
            kernel_log.update(parse_kernel_log_info(log_info, custom_info))
            custom_info["kernel日志"] = "是"

    custom_info["内核日志"] = kernel_log
    return kernel_log

def get_custom_info_from_log(file_path):
    custom_info = {}
    custom_no = file_path.split("/")[-1]
    custom_info["编号"] = custom_no

    custom_file = os.path.join(file_path, "xfcloud_readme.json")

    if os.path.exists(custom_file):
        with open(custom_file, "r", encoding='utf-8', errors="ignore") as f:
            custom = json.load(f)
            custom_info["问题单号"] = custom.get("Icare_No")
            custom_info["用户名称"] = custom.get("Custom")

    #判断file_path目录下sf/data/local/raidlog/collect_raid_info_*.log目录是否存在
    raidlog_path = os.path.join(file_path, "sf/data/local/raidlog")
    if not os.path.exists(raidlog_path):
        # 检查可能是sre改了目录
        raidlog_path = os.path.join(file_path, "sf/data/local/sre/raidlog")
        if not os.path.exists(raidlog_path):
            logger.info("not raidlog exists")
            return custom_info
    #先读取历史信息
    get_his_smartctl_lsblk_info(custom_info, raidlog_path)

    #获取文件名称
    files = os.listdir(raidlog_path)
    
    # 如果你想要逆序（最新修改的文件在前），可以添加reverse=True参数
    files = sorted(files, key=lambda x: os.path.getmtime(os.path.join(raidlog_path, x)), reverse=True)
    logger.info("raidlog {} list:{}".format(raidlog_path, files))
    analyze_collect = False
    analyze_eds_collect = False
    for file in files:
        filename = file.split("/")[-1]
        if (filename.startswith("collect_raid_info_") and not analyze_collect) or (filename.startswith("collect_eds_raid_info_") and not analyze_eds_collect) :
            #collect_info文件仅分析最新的
            #获取文件路径
            file_fullpath = os.path.join(raidlog_path, filename)
            #读取日志文件
            with open(file_fullpath, "r", encoding='utf-8', errors="ignore") as f:
                #获取日志信息
                log_info = f.read()

                #更新故障时间
                update_blackbox_log(file_path, custom_info)
                #解析日志信息
                parse_raidlog_info(custom_info, log_info)
                if filename.startswith("collect_raid_info_"):
                    analyze_collect = True
                if filename.startswith("collect_eds_raid_info_"):
                    analyze_eds_collect = True

                if custom_info.get("RAID驱动") == "smartpqi":
                    if check_smartpqi_raid_log(raidlog_path, custom_info):
                        custom_info["存在RAID日志"] = "是"

        if filename == "vmlist.json":
            #获取文件路径
            file_fullpath = os.path.join(raidlog_path, filename)
            custom_info["vmlist"] = loadConfig(file_fullpath)

        if filename == "realtime_iostat.txt":
            #解析采集的iostat
            file_fullpath = os.path.join(raidlog_path, filename)
            custom_info["实时IOSTAT"] = parse_iostat(file_fullpath)

    #读取硬件信息
    hardware_path = os.path.join(file_path, "sf/data/local/hardware_info.json")
    if os.path.exists(hardware_path):
        with open(hardware_path, "r", encoding='utf-8', errors="ignore") as f:
            #获取硬件信息
            hardware_info = f.read()

            #解析RAID，避免raid日志获取不到
            if not custom_info.get("RAID型号"):
                raidinfos = search_section(hardware_info, r"RAID卡:(.*?)\n", re.DOTALL, group=-1, multi=1)
                if raidinfos:
                    for raid in raidinfos:
                        raid_items = raid.split(";")
                        if len(raid_items) < 6:
                            continue
                        custom_info["RAID型号"] = raid_items[2]
                        if custom_info["RAID型号"] == 'N/A':
                            custom_info["RAID型号"] = raid_items[1]
                        if not "RAID固件版本" in custom_info:
                            custom_info["RAID固件版本"] = raid_items[5]
                        if not "RAID驱动" in custom_info:
                            custom_info["RAID驱动"] = raid_items[6].split()[0]
                        if not "RAID驱动版本" in custom_info or not custom_info.get("RAID驱动版本"):
                            custom_info["RAID驱动版本"] = raid_items[6].split()[1]

    #统计实时io
    statistic_realtime_iostat(custom_info)

    return custom_info

#格式化历史smartctl信息为aiops做预测
def format_smartctl_hisinfo(custom_info):
    if not custom_info.get("HIS_INFO"):
        return 
    
    data = [
        
    ]
    host_data = {
        "model_input": {

        }
    }

    times_list = [times for times, value in custom_info["HIS_INFO"].items()]
    times_list.sort(reverse=False)
    #以最后一天的磁盘为准进行检查
    if len(times_list) < 1:
        return

    last_smartctl = custom_info["HIS_INFO"][times_list[-1]].get("smartctl")
    if not last_smartctl:
        return

    hostname = "host-xxxxxxxxxxxxx"
    if custom_info.get("主机ID"):
        hostname = custom_info.get("主机ID")
    
    for disk, smartctl in last_smartctl.items():
        if not smartctl:
            continue

        disk_model = smartctl.get("Device Model")
        disk_sn = smartctl.get("Serial Number")
        keyname = "('{}', '{}_{}')".format(hostname, disk_model, disk_sn)
        host_data["model_input"][keyname] = {"date": []}
        
        for date in times_list:
            smartctls = custom_info["HIS_INFO"][date].get("smartctl")
            if not smartctls:
                continue

            host_data["model_input"][keyname]["date"].append(date)
            
            for d, smart in smartctls.items():
                if smartctl.get("Device Model") != smart.get("Device Model"):
                    continue
                if smartctl.get("Serial Number") != smart.get("Serial Number"):
                    continue

                values = smart.get("SMART Attributes List")
                if not values:
                    continue

                for v in values:
                    disk_attr_keyname = "smart_{}_raw".format(v["ID"])
                    update_value(host_data["model_input"][keyname], disk_attr_keyname, v.get("RAW_VALUE"), ignore_exists=False, type="list")
                    disk_attr_keyname = "smart_{}_normalized".format(v["ID"])
                    update_value(host_data["model_input"][keyname], disk_attr_keyname, v.get("VALUE"), ignore_exists=False, type="list")

    data.append(host_data)
    return data

#根据历史信息整理硬盘坏道增长情况
def update_disk_hisinfo(custom_info):
    if not custom_info.get("HIS_INFO"):
        return 
    times_list = [times for times, value in custom_info["HIS_INFO"].items()]
    times_list.sort(reverse=True)
    #以最后一天的磁盘为准进行检查
    if len(times_list) < 1:
        return

    if not custom_info.get("磁盘异常检查"):
        custom_info["磁盘异常检查"] = {}
    
    last_smartctl = custom_info["HIS_INFO"][times_list[0]].get("smartctl")
    if not last_smartctl:
        return 
    disk_sn = [ smartctl.get("Serial Number") for disk, smartctl in last_smartctl.items() if smartctl.get("Serial Number")]
    for date in times_list:
        smartctls = custom_info["HIS_INFO"][date].get("smartctl")
        if not smartctls:
            continue

        for sn in disk_sn:
            smartinfo = None

            for disk, smartctl in smartctls.items():
                if smartctl.get("Serial Number") == sn:
                    smartinfo = smartctl
                    break
            if not smartinfo:
                continue

            if not custom_info["磁盘异常检查"].get(sn):
                custom_info["磁盘异常检查"][sn] = {}

            value = get_smartctl_attr(smartinfo.get("SMART Attributes List"), 5)
            if not value:
                value = 0
            value = int(value)
            update_value(custom_info["磁盘异常检查"][sn], "5", value, ignore_exists=False, type="list")

            value = get_smartctl_attr(smartinfo.get("SMART Attributes List"), 197)
            if not value:
                value = 0
            value = int(value)
            update_value(custom_info["磁盘异常检查"][sn], "197", value, ignore_exists=False, type="list")
            pass
    pass

#从raidlog目录下面读取近7日的smartctl信息，和lsblk信息
def get_his_smartctl_lsblk_info(custom_info, file_path):
    custom_info["HIS_INFO"] = {}
    smart_path = os.path.join(file_path, "smartctl_info")
    if os.path.exists(smart_path):
        date_dir = os.listdir(smart_path)
        for date in date_dir:
            custom_info["HIS_INFO"][date] = {}
            subdir = os.path.join(smart_path, date)
            lsblk_file = os.path.join(subdir, "LOG_lsblk.txt")
            smartctl_file = os.path.join(subdir, "LOG_vs_smartctl.txt")
            if os.path.exists(lsblk_file):
                custom_info["HIS_INFO"][date]["lsblk"] = get_his_lsblk(lsblk_file)

            if os.path.exists(smartctl_file):
                custom_info["HIS_INFO"][date]["smartctl"] = get_his_smartctl(smartctl_file)

        update_disk_hisinfo(custom_info)

def get_his_lsblk(filename):
    lsblk_info = {}
    with open(filename, "r", encoding="utf-8", errors="ignore") as f:
        data = f.read()
        data = data.replace('[', '\n[\n[')
        data += "["
        values = search_section(data, r"\[(\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\*{10,}\n(.*?)\n\[", re.DOTALL, group=-1, multi=1)
        if values:
            for value in values:
                lsblk_info[value[0]] = parse_lsblk(value[1])

    return lsblk_info

def parse_ioslow_states(value):
    if not value:
        return None
    time = value[0]
    titles = value[1].split()
    titles = titles[:5] + ["blk:" + s for s in titles[5:15]] + titles[15:16] + ["disk:" + s for s in titles[16:]]
    iostats = []
    lines = value[2].split('\n')
    for line in lines:
        if not line.strip():
            continue

        v = {"time": time}
        sections = line.split()
        #sections长度必须和titles一样
        if len(sections) != len(titles):
            logger.error("数据长度错误")
            continue

        if len(titles) > 4:
            try:
                int(sections[4])
            except Exception as e:
                logger.error("section数据错误")
                continue

        for i in range(0, len(titles)):
            if i > 3:
                v[titles[i]] = int(sections[i])
            else:
                v[titles[i]] = sections[i]

        iostats.append(v)

    return iostats

#比较两个时间点的IO差
#iostats : Dataframe
def diff_ioslow(iostats, start_time, end_time):
    if iostats.empty:
        return None
    
    start_vals = iostats[iostats["time"] == start_time]
    end_vals = iostats[iostats["time"] == end_time]

    columes = ["blk:<1/16", "blk:<1/8", "blk:<1/4", "blk:<1/2", "blk:<1x", "blk:<2x", "blk:<4x", "blk:<8x", "blk:<16x", "blk:<32x", "blk:>32x", "disk:<1/16", "disk:<1/8", "disk:<1/4", "disk:<1/2", "disk:<1x", "disk:<2x", "disk:<4x", "disk:<8x", "disk:<16x", "disk:<32x", "disk:>32x"]
    start_vals_col = start_vals[columes]
    end_vals_col = end_vals[columes]

    prefix_col = ["major",  "minor",   "name", "op:thold"]
    prefix_df = start_vals[prefix_col]

    df = end_vals_col - start_vals_col
    df = pd.concat([prefix_df, df], axis=1)
    return df

#解析ioslow日志
def parse_ioslow(filename):
    if not os.path.exists(filename):
        return None
    
    with open(filename, "r", encoding="utf-8", errors="ignore") as f:
        data = f.read()
        if data.find("No such file or directory") != -1:
            return None
        data = data.replace('[', '\n[\n[')
        data += "["
        values = search_section(data, r"\[(\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\*{10,}\n(.*?)\n\n(.*?)\n\[", re.DOTALL, group=-1, multi=1)
        if values:
            iostats = pd.DataFrame()
            for value in values:
                iostat = parse_ioslow_states(value)
                df = pd.DataFrame(iostat)
                iostats = pd.concat([iostats, df])
            return iostats
        
    return None

def parse_iostat_states(value):
    if not value:
        return None
    
    time = value[0]
    desc = value[1].split()

    content = value[2] + '\n'

    stats = search_section(content, r"(avg-cpu:\s+.*?)\n(.*?)\n\n(Device:\s+.*?)\n(.*?)\n\n", re.DOTALL, group=-1, multi=1)
    if stats:
        iostats = []
        for s in stats:
            if not s[3].strip():
                continue

            titles = s[2].split()

            lines = s[3].split("\n")
            
            for line in lines:
                v = {"time": time}
                sections = line.split()
                #sections长度必须和titles一样
                if len(sections) != len(titles):
                    logger.error("数据错误")
                    continue

                for i in range(0, len(titles)):
                    if i > 0:
                        v[titles[i]] = float(sections[i])
                    else:
                        v[titles[i]] = sections[i]

                if v.get("avgrq-sz") > 0:
                    v["IOPS"] = (v["rkB/s"] + v["wkB/s"]) / v["avgrq-sz"]
                else:
                    v["IOPS"] = 0

                iostats.append(v)

        return iostats

    return None

#解析iostat日志
def parse_iostat(filename):
    if not os.path.exists(filename):
        return None
    
    with open(filename, "r", encoding="utf-8", errors="ignore") as f:
        data = f.read()
        if data.find("No such file or directory") != -1:
            return None
        
        data = data.replace('[', '\n[\n[')
        data += "["
        values = search_section(data, r"\[(\d{2,4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\*{10,}\n(.*?)\n\n(.*?)\n\[", re.DOTALL, group=-1, multi=1)
        if values:
            iostats = pd.DataFrame()
            for value in values:
                iostat = parse_iostat_states(value)
                df = pd.DataFrame(iostat)
                iostats = pd.concat([iostats, df])

            return iostats
        
    return None

def parse_iostuck_states(value):
    if not value:
        return None
    
    time = value[0]
    titles = value[1].split()

    iostucks = []
    lines = value[2].split('\n')
    for line in lines:
        if not line.strip():
            continue

        v = {"time": time}
        sections = line.split()
        #sections长度必须和titles一样
        if len(sections) != len(titles):
            logger.error("数据错误")
            continue

        for i in range(0, len(titles)):
            if i > 3:
                v[titles[i]] = int(sections[i])
            else:
                v[titles[i]] = sections[i]

        iostucks.append(v)

    return iostucks

#解析iostuck日志
def parse_iostuck(filename):
    if not os.path.exists(filename):
        return None
    
    with open(filename, "r", encoding="utf-8", errors="ignore") as f:
        data = f.read()
        if data.find("No such file or directory") != -1:
            return None
        data = data.replace('[', '\n[\n[')
        data += "["
        values = search_section(data, r"\[(\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\*{10,}\n(.*?)\n\n(.*?)\n\[", re.DOTALL, group=-1, multi=1)
        if values:
            iostucks = pd.DataFrame()
            for value in values:
                iostuck = parse_iostuck_states(value)
                df = pd.DataFrame(iostuck)
                iostucks = pd.concat([iostucks, df])
            return iostucks
        
    return None

def parse_lsblk(value):
    lines = value.strip().split('\n')[1:]
    # 解析每一行作为磁盘信息
    disks = []
    for line in lines:
        # 分割行，但保持MODEL和REV列的值在一起，因为MODEL可能包含空格
        parts = line.split()
        if len(parts) < 8:
            continue

        disk_info = {
            "NAME": parts[0],
            "SIZE": parts[1],
            "OWNER": parts[2],
            "GROUP": parts[3],
            "MODE": parts[4],
            "HCTL": parts[6],
            "TYPE": parts[7],
            "VENDOR": parts[8] if len(parts) > 9 else "",
            "MODEL": parts[9] if len(parts) > 9 else "",
            "REV": [-2],
            "TRAN": parts[-1]
        }
        disks.append(disk_info)

    return disks

def get_his_smartctl(filename):
    smartctl_info = {}
    with open(filename, "r", encoding="utf-8", errors="ignore") as f:
        data = f.read()
        data += "\n\n######"
        values = search_section(data, r"/dev/([sd|nvme].+?)######\n(smartctl.*?)\n\n(.*?)(?:\n\n)?######", re.DOTALL, group=-1, multi=1)
        if values:
            for value in values:
                smartctl_info[value[0]] = parse_smartctl_info(value[2]+"\n\n")
    return smartctl_info

#从lsblk -S -P的设备地址获取设备名
def get_disk_name_from_hctl(devices, hctl, custom_info):
    if not devices:
        return None
    for device in devices:
        if device.get("HCTL", "") == hctl:
            return device["NAME"]
    
    if custom_info.get("HIS_INFO"):
        #遍历历史日期
        dates = [ date for date, value in custom_info["HIS_INFO"].items()]
        #倒序排序,从最新日期开始找
        dates.sort(reverse=True)
        for date in dates:
            if custom_info["HIS_INFO"][date].get("lsblk"):
                #从最后开始查找
                lsblk_list = custom_info["HIS_INFO"][date]["lsblk"]
                times_list = [times for times, value in lsblk_list.items()]
                times_list.sort(reverse=True)
                for time in times_list:
                    time_lsblk = lsblk_list[time]
                    for disk in time_lsblk:
                        if disk["HCTL"] == hctl:
                            return disk["NAME"]
            #避免误判，仅找最后一天的
            break

    return None

#解析磁盘列表(lsblk -S -P的结果)
def parse_disk_list(disk_section):
    if not disk_section:
        return []
    
    # 正则表达式匹配键值对，考虑到值中可能包含空格
    kv_pattern = re.compile(r'(\S+)="([^"]*)"')

    # 将文本分割成行，并过滤掉空行
    lines = [line for line in disk_section.strip().split('\n') if line]

    # 解析每一行，构建字典列表
    devices = []
    for line in lines:
        device_info = {}
        # 使用正则表达式查找所有键值对
        matches = kv_pattern.findall(line)
        for key, value in matches:
            # 添加到设备信息字典中
            device_info[key] = value
        # 添加到设备列表中
        devices.append(device_info)

    return devices

#从字符串中根据正则表达式查找
def search_section(log_info, search_str, mode=re.DOTALL, group=1, multi=0):
    if not search_str or not log_info:
        return None

    if g_args and g_args.verbose:
        logger.info("search str:{}, mode:{}, group:{}, multi:{}".format(search_str, mode, group, multi))

    kv_pattern = re.compile(search_str, mode)
    if multi == 0:
        value = kv_pattern.search(log_info)
    else:
        value = kv_pattern.findall(log_info)
        if g_args and g_args.verbose:
            logger.info(value)
        return value
    if value:
        if group == -1:
            #-1返回value
            if g_args and g_args.verbose:
                logger.info(value)
            return value
        else:
            if g_args and g_args.verbose:
                logger.info(value)
            return value.group(group).strip()
    else:
        return None

def parse_smartctl_info(smartinfo):
    #解析smartctl信息
    info = {}
    info["Device Model"] = search_section(smartinfo, r'Device Model:(.*?)\n')
    info["Model Number"] = search_section(smartinfo, r'Model Number:(.*?)\n')
    if not info["Device Model"]:
        info["Device Model"] = search_section(smartinfo, r'Product:(.*?)\n')
        if not info["Device Model"]:
            info["Device Model"] = info["Model Number"]
    info["Serial Number"] = search_section(smartinfo, r'Serial Number:(.*?)\n')
    if not info["Serial Number"]:
        info["Serial Number"] = search_section(smartinfo, r'Serial number:(.*?)\n')
    info["LU WWN Device Id"] = search_section(smartinfo, r'LU WWN Device Id:(.*?)\n')
    info["Firmware Version"] = search_section(smartinfo, r'Firmware Version:(.*?)\n')
    if not info["Firmware Version"]:
        info["Firmware Version"] = search_section(smartinfo, r'Revision:(.*?)\n')
    info["User Capacity"] = search_section(smartinfo, r'User Capacity:(.*?)\n')
    #NVME容量
    info["Total NVM Capacity"] = search_section(smartinfo, r"Total NVM Capacity:(.*?)\n")
    if not info["User Capacity"]:
        info["User Capacity"] = info["Total NVM Capacity"]
    if info["Total NVM Capacity"]:
        info["NVME SMART Attributes"] = search_section(smartinfo, r'=== START OF SMART DATA SECTION ===\n(.*)', re.DOTALL, group=1)
        if info["NVME SMART Attributes"]:
            records = info["NVME SMART Attributes"].split('\n')
            record = {}
            for r in records:
                if r == "":
                    continue
                ks = r.split(':')
                k = ks[0].strip()
                v = ks[-1].strip()
                record[k] = v
            info["NVME SMART Attributes Values"] = record

    info["Sector Sizes"] = search_section(smartinfo, r'Sector Sizes:(.*?)\n')
    info["Rotation Rate"] = search_section(smartinfo, r'Rotation Rate:(.*?)\n')
    #SAS接口可以查接口协议
    info["Transport protocol"] = search_section(smartinfo, r'Transport protocol:(.*?)\n')
    if info["Transport protocol"]:
        #读取sas的属性
        info["SAS SMART Attributes"] = search_section(smartinfo, r'=== START OF READ SMART DATA SECTION ===\n(.*)', re.DOTALL, group=1)
        if info["SAS SMART Attributes"]:
            records = info["SAS SMART Attributes"].split('\n')
            record = {}
            for r in records:
                if r == "":
                    continue
                ks = re.split(r'[:=]', r)
                k = ks[0].strip()
                v = ks[-1].strip()
                record[k] = v
            info["SAS SMART Attributes Values"] = record

    info["Form Factor"] = search_section(smartinfo, r'Form Factor:(.*?)\n')
    info["Device is"] = search_section(smartinfo, r'Device is:(.*?)\n')
    info["ATA Version is"] = search_section(smartinfo, r'ATA Version is:(.*?)\n')
    info["SATA Version is"] = search_section(smartinfo, r'SATA Version is:(.*?)\n')
    info["Local Time is"] = search_section(smartinfo, r'Local Time is:(.*?)\n')
    info["SMART -H"] = search_section(smartinfo, r'SMART overall-health self-assessment test result:(.*?)\n')
    info["SSTR_RPT"] = search_section(smartinfo, r'Short self-test routine \nrecommended polling time:(.*?)\n')
    info["ESTR_RPT"] = search_section(smartinfo, r'Extended self-test routine\nrecommended polling time:(.*?)\n')
    info["SMART Attributes"] = search_section(smartinfo, r'ID#(.*?)\n\n', re.DOTALL, group=0)

    #解析smartctl属性
    if info["SMART Attributes"]:
        # 去除标题行并分割字符串为行列表
        lines = info["SMART Attributes"].strip().split('\n')[1:]

        # 解析数据并创建字典列表
        attributes = []
        for line in lines:
            parts = line.split()
            attribute = {
                'ID': parts[0],
                'ATTRIBUTE_NAME': parts[1],
                'FLAG': parts[2],
                'VALUE': parts[3],  # 假设VALUE是整数
                'WORST': parts[4],
                'THRESH': parts[5],
                'TYPE': parts[6],
                'UPDATED': parts[7],
                'WHEN_FAILED': parts[8],
                'RAW_VALUE': parts[9]  # 处理非数字RAW_VALUE
            }
            attributes.append(attribute)
        
        info["SMART Attributes List"] = attributes

    info["SMART Error Log"] = search_section(smartinfo, r'(SMART Error Log Version:)(\s+)(\d+)\n(.*?)\nSMART Self-test log structure revision number', re.DOTALL, group=0)

    values = search_section(smartinfo, r'SMART Self-test log structure revision number \d\n(.*?)SMART Selective self-test log data structure revision number \d\n(.*?)Selective self-test flags', re.DOTALL, group=-1, multi=1)
    if values:
        info["SMART Self-test"] = {
            "Self-test Record": parse_short_test(values[0][0]),
            "Self-test Data Struct": values[0][1]
        }
    return info

def parse_short_test(value):
    #解析short test
    value = value.strip()
    if search_section(value, "No self-tests have been logged.", re.DOTALL, group=0):
        return None
    #Num  Test_Description    Status                  Remaining  LifeTime(hours)  LBA_of_first_error
    # 1  Short offline       Self-test routine in progress 50%     40210         -
    # 2  Short offline       Completed without error       00%     40209 
    #
    lines = value.split('\n')
    return lines

#获取硬盘smartctl信息(从日志中)
def get_disk_smartctl_info(log_info, disk):
    value = search_section(log_info, 'smartctl(.*?) -a /dev/{} :\n(.*?)=================================================\n'.format(disk), re.DOTALL, group=-1)
    if not value:
        if disk.find("nvme") != -1:
            #nvme带n1再次查找
            value = search_section(log_info, 'smartctl(.*?) -a /dev/{}n1 :\n(.*?)=================================================\n'.format(disk), re.DOTALL, group=-1)
            if not value:
                return None
        else:
            return None
    smartinfo = value.group(2)
    return parse_smartctl_info(smartinfo)

#获取smartctl属性指定项的值
def get_smartctl_attr(smartctl_attr, index = 5, key='RAW_VALUE'):
    if smartctl_attr:
        for attr in smartctl_attr:
            if isinstance(index, int):
                if int(attr["ID"]) == index:
                    return attr[key]
            else:
                if str(attr["ID"]) == str(index):
                    return attr[key]
        
    return None

def check_raid_state_single(raid_info, custom_info):
    if not raid_info:
        return None
    
    values = search_section(raid_info, r'Drive (/c\d/e\d+/s\d+) State :\n======================\n(.*?)\n\n\n', re.DOTALL, group=-1, multi=1)
    if values:
        for v in values:
            stateinfo = parse_state_info(v[1])
            if not stateinfo:
                continue

            if stateinfo["Media Error Count"] > 0 or stateinfo["Predictive Failure Count"] > 0:
                update_value(custom_info, "RAID中异常硬盘列表", v[0], ignore_exists=True, type="list")

    values = search_section(raid_info, r'Description = Show Drive Information Failed.\n\nDetailed Status :\n\={10,20}\n\n\-{20,}\n(.*?)\n\-{20,}\n(.*?)\n\-{20,}', re.DOTALL, group=-1, multi=1)
    if values:
        for v in values:
            drive_status = parse_controller_drive_status(v)
            if not drive_status:
                continue
            
            for drive in drive_status:
                if drive.get("Status") == "Failure":
                    update_value(custom_info, "RAID中异常硬盘列表", drive.get("Drive"), ignore_exists=True, type="list")
                    update_value(custom_info, "RAID硬盘状态异常", drive.get("Drive"))
            

#检查raid中的磁盘是否存在错误
def check_raid_state(raid_info, custom_info):
    if not raid_info:
        return False
    
    if isinstance(raid_info, list):
        for rv in raid_info:
            check_raid_state_single(rv, custom_info)
    else:
        check_raid_state_single(raid_info, custom_info)
    
    return True

def parse_controller_drive_status(value):
    if not value:
        return None
    title = value[0].split()
    lines = value[1].split('\n')
    drives = []
    for line in lines:
        item = {}
        v = line.split()
        for i in range(0, len(title)):
            item[title[i]] = v[i]
        drives.append(item)
    
    return drives
    
def parse_state_info(stateinfo):
    info = {}
    try:
        info["Shield Counter"] = int(search_section(stateinfo, r'Shield Counter =(.*?)\n'))
        info["Media Error Count"] = int(search_section(stateinfo, r'Media Error Count =(.*?)\n'))
        info["Other Error Count"] = int(search_section(stateinfo, r'Other Error Count =(.*?)\n'))
        info["Drive Temperature"] = search_section(stateinfo, r'Drive Temperature =(.*?)\n')
        info["Predictive Failure Count"] = int(search_section(stateinfo, r'Predictive Failure Count =(.*?)\n'))
        info["Smart alert flag"] = search_section(stateinfo, r'S.M.A.R.T alert flagged by drive =(.*?)\n')
    except Exception as e:
        traceback.print_stack()
    return info

def get_raid_device_state_imp(raid_info, device):
    value = search_section(raid_info, device+" State :\n={20,25}\n(.*?)\n\n\n", re.DOTALL, group=-1)
    if not value:
        return None
        
    return parse_state_info(value.group(1))

#从raid信息中获取device state
def get_raid_device_state(raid_info, device):
    if not raid_info or not device:
        return None
    if isinstance(raid_info, list):
        for rv in raid_info:
            stat = get_raid_device_state_imp(rv, device)
            if stat:
                return stat
        return None
    else:
        return get_raid_device_state_imp(raid_info, device)

#解析控制器编号
def parse_controller_index(device):
    if not device:
        return
    value = search_section(device, r"/c(\d+)/", re.DOTALL, group=1)
    if value:
        return value
    return None

def parse_slot_number(device):
    if not device:
        return
    value = search_section(device, r"/s(\d+)", re.DOTALL, group=1)
    if value:
        return value
    return None

#根据SN号获取raid信息中的磁盘位置
def get_raid_location_by_sn(raid_info, sn):
    if not raid_info or not sn:
        return None
    
    values = search_section(raid_info, r'Drive (\S+) State :(.*?)\n\nDrive (\S+) Device attributes :(.*?)\n(SN =\s+\S+)', re.DOTALL, group=-1, multi=1)
    if not values:
        return None
    for value in values:
        if value[4].find(sn) != -1:
            return value[0]

#根据SN号获取raid信息中的device state信息
def get_raid_device_state_by_sn_imp(raid_info, sn):
    values = search_section(raid_info, r'Drive (\S+) State :(.*?)\n\nDrive (\S+) Device attributes :(.*?)\n(SN =\s+\S+)', re.DOTALL, group=-1, multi=1)
    if not values:
        return None

    for value in values:
        if value[4].find(sn) != -1:
            return parse_state_info(value[1])

def get_raid_device_state_by_sn(raid_info, sn):
    if not raid_info or not sn:
        return None
    
    if isinstance(raid_info, list):
        for rv in raid_info:
            info = get_raid_device_state_by_sn_imp(rv, sn)
            if info:
                return info
        return None
    else:
        return get_raid_device_state_by_sn_imp(raid_info, sn)

def get_raid_sas_address(raid_info, device):
    if not raid_info:
        return None
    if isinstance(raid_info, list):
        for rv in raid_info:
            sas_address = search_section(rv, device + ' Policies/Settings :\n(.*?)\nPort Information :\n={10,20}\n(.*?)\n\n\n\n', re.DOTALL, group=2)
            if sas_address:
                return sas_address
        return None
    else:
        return search_section(raid_info, device + ' Policies/Settings :\n(.*?)\nPort Information :\n={10,20}\n(.*?)\n\n\n\n', re.DOTALL, group=2)

def get_raid_device_attributes(raid_info, device):
    if not raid_info:
        return None
    if isinstance(raid_info, list):
        for rv in raid_info:
            info = search_section(rv, device + ' Device attributes :\n(.*?)\n={20,40}\n(.*?)\n\n\n', re.DOTALL, group=1)
            if info:
                return info
        return None
    else:
        return search_section(raid_info, device + ' Device attributes :\n(.*?)\n={20,40}\n(.*?)\n\n\n', re.DOTALL, group=1)


#从raid信息中获取Port information
def get_raid_device_port_infomation(raid_info, device):
    value = search_section(raid_info, 'Drive ' + device + ' Policies/Settings :\n(.*?)\nPort Information :\n={10,20}\n(.*?)\n\n\n\n', re.DOTALL, group=-1)
    if not value:
        return None
    info = {}
    settings = {}
    settings_value = value.group(1)
    settings["Enclosure position"] = search_section(settings_value, r'Enclosure position =(.*?)\n')
    settings["Connected Port Number"] = search_section(settings_value, r'Connected Port Number =(.*?)\n')
    settings["Sequence Number"] = search_section(settings_value, r'Sequence Number =(.*?)\n')
    settings["Commissioned Spare"] = search_section(settings_value, r'Commissioned Spare =(.*?)\n')
    settings["Emergency Spare"] = search_section(settings_value, r'Emergency Spare =(.*?)\n')
    settings["Last Predictive Failure Event Sequence Number"] = search_section(settings_value, r'Last Predictive Failure Event Sequence Number =(.*?)\n')
    settings["Successful diagnostics completion on"] = search_section(settings_value, r'Successful diagnostics completion on =(.*?)\n')
    settings["FDE Type"] = search_section(settings_value, r'FDE Type =(.*?)\n')
    settings["SED Capable"] = search_section(settings_value, r'SED Capable =(.*?)\n')
    settings["SED Enabled"] = search_section(settings_value, r'SED Enabled =(.*?)\n')
    settings["Secured"] = search_section(settings_value, r'Secured =(.*?)\n')
    settings["Cryptographic Erase Capable"] = search_section(settings_value, r'Cryptographic Erase Capable =(.*?)\n')
    settings["Sanitize Support"] = search_section(settings_value, r'Sanitize Support =(.*?)\n')
    settings["Locked"] = search_section(settings_value, r'Locked =(.*?)\n')
    settings["Needs EKM Attention"] = search_section(settings_value, r'Needs EKM Attention =(.*?)\n')
    settings["PI Eligible"] = search_section(settings_value, r'PI Eligible =(.*?)\n')
    settings["Certified"] = search_section(settings_value, r'Certified =(.*?)\n')
    settings["Wide Port Capable"] = search_section(settings_value, r'Wide Port Capable =(.*?)\n')
    settings["Multipath"] = search_section(settings_value, r'Multipath =(.*?)\n')

    info["Settings"] = settings
    info["Port information"] = value.group(2)
    
    return info

def parse_kick_kernel(custom_info, kernel_log, values, diskindex=5):
    if values:
        custom_info["内核踢盘日志"] = values
        hctl_list = [ d[diskindex] for d in values]
        hctl_list = list(set(hctl_list))
        for hctl in hctl_list:
            sd_ops = [ d for d in values if d[diskindex] == hctl ]
            sd_op = { "name": get_disk_name_from_hctl(custom_info.get("磁盘列表"), hctl, custom_info) }
            for sd in sd_ops:
                if sd[diskindex+3] == 'running' and sd[diskindex+4] == 'offline':
                    sd_op["开始离线时间"] = sd[0]
                if sd[diskindex+3] == 'offline' and sd[diskindex+4] == 'cancel':
                    sd_op["离线取消时间"] = sd[0]
                if sd[diskindex+3] == 'cancel' and sd[diskindex+4] == 'deleted':
                    sd_op["开始踢盘时间"] = sd[0]
                if sd[diskindex+3] == 'created' and sd[diskindex+4] == 'deleted':
                    sd_op["踢盘完成时间"] = sd[0]
                
                if sd_op.get("开始踢盘时间") and sd_op.get("踢盘完成时间"):
                    if sd_op.get("开始离线时间") and sd_op.get("离线取消时间"):
                        offline_time = diff_logtime(sd_op["开始离线时间"], sd_op["离线取消时间"])
                        kick_time = diff_logtime(sd_op["开始踢盘时间"], sd_op["踢盘完成时间"])
                        sd_op["离线耗时"] = offline_time
                        sd_op["踢盘耗时"] = kick_time
                        sd_op["总耗时"] = diff_logtime(sd_op["开始离线时间"], sd_op["踢盘完成时间"])
                        #踢盘时间超过3秒认为踢盘时间过长
                        if kick_time > 3:
                            sd_op["踢盘时间过长"] = "是"
                        else:
                            sd_op["踢盘时间过长"] = "否"

            update_value(kernel_log, "内核踢盘分析", sd_op)
        disknames = [get_disk_name_from_hctl(custom_info.get("磁盘列表"), d[diskindex], custom_info) for d in values]
        disknames = list(set(disknames))
        disknames = [ d for d in disknames if d ]
        if len(disknames):
            kernel_log["内核发生踢盘"] = "是"
            kernel_log["内核发生踢盘信息"] = values

            update_value(kernel_log, "故障磁盘列表", disknames, ignore_exists=True, type="list")

#解析内核日志
def parse_kernel_log_info(log_info, custom_info, logtype="kernel.log"):
    kernel_log = {}
    if g_args and g_args.verbose:
        logger.info("parse: {}".format(logtype))
    
    search_str = None
    diskindex = 0

    #判断drop_cache问题
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? \[(\d+).(\d+)\](?:\[\s{0,6}\d{0,6}\])? (?:\s{0,5}(00switch_today_\s+\S+:|\s{0,5}sh\s+\S+:)\s+)?(drop_caches): (\d+)(?:\s+(begin))?'
        diskindex = 5
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (drop_caches)'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        check_drop_cache(custom_info, values)
    
    #内核踢盘日志
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? \[(\d+).(\d+)\](?:\[\s{0,6}\d{0,6}\])? (sd|scsi) (\S+): (scsi_device_set_state\(\)) (\[(running|created|offline|cancel)\]->\[(offline|cancel|deleted)\])'
        diskindex = 5
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (sd|scsi) (\S+): (scsi_device_set_state\(\)) (\[(running|created|offline|cancel)\]->\[(offline|cancel|deleted)\])'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        parse_kick_kernel(custom_info, kernel_log, values, diskindex)
    
    #判断是否出现abort的盘

    #是否单盘卡慢引起多盘
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? \[(\d+).(\d+)\](?:\[\s{0,6}\d{0,6}\])? (scsi host\d:) (Waking error handler thread)'

    elif logtype == "LOG_dmesg.txt":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (scsi host\d:) (Waking error handler thread)'

    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        if logtype == "kernel.log":
            search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? \[(\d+).(\d+)\](?:\[\s{0,6}\d{0,6}\])? (sd|scsi) (\S+): (fast handle=1 trigger)'

        elif logtype == "LOG_dmesg.txt":
            search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (sd|scsi) (\S+): (fast handle=1 trigger)'
        
        handles = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
        if handles:
            kernel_log["单盘影响多盘"] = "否"
        else:
            kernel_log["单盘影响多盘"] = "是"
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(custom_info, "内核异常日志", handles, ignore_exists=True, type="list")

    #判断是否存在磁盘被重置
    if search_section(log_info, r'BRCM Debug mfi stat(\S+)', re.DOTALL):
        print("find BRCM")
        if logtype == "kernel.log":
            search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(warning\s+)(?:\[kernel:\])? \[(\d+).(\d+)\](?:\[\s{0,6}\d{0,6}\])? sd (\S+): (Power-on or device reset occurred)'
            diskindex = 4
        elif logtype == "LOG_dmesg.txt":
            search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] sd (\S+): (Power-on or device reset occurred)'
            diskindex = 1
        disk = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
        if disk:
            disknames = [get_disk_name_from_hctl(custom_info.get("磁盘列表"), d[diskindex], custom_info) for d in disk]
            disknames = list(set(disknames))
            disknames = [ d for d in disknames if d ]
            if len(disknames):
                kernel_log["磁盘被重置"] = "是"
                kernel_log["磁盘被重置信息"] = disk

                update_value(kernel_log, "故障磁盘列表", disknames, ignore_exists=True, type="list")

    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(warning\s+)(?:\[kernel:\])? \[(\d+).(\d+)\](?:\[\s{0,6}\d{0,6}\])? sd (\S+): (Power-on or device reset occurred)'
        diskindex = 4
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] sd (\S+): (Power-on or device reset occurred)'
        diskindex = 1
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disknames = [get_disk_name_from_hctl(custom_info.get("磁盘列表"), d[diskindex], custom_info) for d in values]
        disknames = list(set(disknames))
        disknames = [ d for d in disknames if d ]
        kernel_log["RAID重置硬盘"] = "是"
        kernel_log["RAID重置硬盘信息"] = values
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disknames, ignore_exists=True, type="list")

    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(emerg|warning|debug)\s+(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? (\[wait_for_completion_io_timeout\]) dev_name:([sd|nvme].+?),(cmd:.*?)\n'
        diskindex = 6
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (\[wait_for_completion_io_timeout\]) dev_name:([sd|nvme].+?),(cmd:.*?)\n'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disknames = [ v[diskindex] for v in values ]
        disk = list(set(disknames))
        kernel_log["磁盘IO卡死"] = "是"
        kernel_log["磁盘IO卡死信息"] = values
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk, ignore_exists=True, type="list")

    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[([sd|nvme]\S+)\] (Add. Sense: Head select fault)\n'
        diskindex = 6
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] sd (\S+): \[([sd|nvme]\S+)\] (Add. Sense: Head select fault)\n'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disknames = [ v[diskindex] for v in values ]
        disk = list(set(disknames))
        kernel_log["硬盘故障"] = "是"
        update_value(kernel_log, "硬盘故障信息", "磁头故障:{}".format(values))
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk, ignore_exists=True, type="list")

    #Medium error
    #2024-10-09 17:04:27.592164 info [kernel:] [19665.523198] sd 1:0:3:0: [sdd] tag#0 Add. Sense: Warning - background medium scan detected medium error
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[([sd|nvme]\S+)\] (tag#\d+ Add. Sense: Warning - background medium scan detected medium error)\n'
        diskindex = 6
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] sd (\S+): \[([sd|nvme]\S+)\] (tag#\d+ Add. Sense: Warning - background medium scan detected medium error)\n'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disknames = [ v[diskindex] for v in values ]
        disk = list(set(disknames))
        kernel_log["硬盘故障"] = "是"
        update_value(kernel_log, "硬盘故障信息", "硬盘内部扫描发现Medium error:{}".format(values))
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk, ignore_exists=True, type="list")

    #2025-05-17 03:14:22.555678 info [kernel:] [53528231.014921] sd 0:0:4:0: [sde] tag#208 Add. Sense: Mechanical positioning error
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[([sd|nvme]\S+)\] (tag#\d+ Add. Sense: Mechanical positioning error)\n'
        diskindex = 6
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] sd (\S+): \[([sd|nvme]\S+)\] (tag#\d Add. Sense: Mechanical positioning error)\n'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disknames = [ v[diskindex] for v in values ]
        disk = list(set(disknames))
        kernel_log["硬盘故障"] = "是"
        update_value(kernel_log, "硬盘故障信息", "硬盘出现机械定位误差:{}".format(values))
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk, ignore_exists=True, type="list")

    #Not Ready
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[([sd|nvme]\S+)\] (tag#\d+ Sense Key : Not Ready)'
        diskindex = 6
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] sd (\S+): \[([sd|nvme]\S+)\] (tag#\d+ Sense Key : Not Ready)'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disknames = [ v[diskindex] for v in values ]
        disk = list(set(disknames))
        kernel_log["硬盘故障"] = "是"
        update_value(kernel_log, "硬盘故障信息", "硬盘Not Ready:{}".format(values))
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk, ignore_exists=True, type="list")

    #Not Ready
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[([sd|nvme]\S+)\] (tag#\d Add. Sense: Logical unit not ready, cause not reportable)\n'
        diskindex = 6
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] sd (\S+): \[([sd|nvme]\S+)\] (tag#\d+ Add. Sense: Logical unit not ready, cause not reportable)\n'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disknames = [ v[diskindex] for v in values ]
        disk = list(set(disknames))
        kernel_log["硬盘故障"] = "是"
        update_value(kernel_log, "硬盘故障信息", "硬盘Not Ready:{}".format(values))
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk, ignore_exists=True, type="list")

    #链路故障
    # sd 15:0:4:0: [sdg] Add. Sense: Information unit iuCRC error detected
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[(\S+)\] (:?tag#\d+\s)?Add. Sense: Information unit iuCRC error detected'
        diskindex = 6
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] sd (\S+): \[(\S+)\] (:?tag#\d+\s)?Add. Sense: Information unit iuCRC error detected'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disknames = [ v[diskindex] for v in values ]
        disk = list(set(disknames))
        kernel_log["硬盘故障"] = "是"
        update_value(kernel_log, "硬盘故障信息", "硬盘链路故障:{}".format(values))
        update_value(custom_info, "问题初步结论", "{}存在链路故障，建议更换链路或背板。".format(disk))
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk, ignore_exists=True, type="list")

    #2024-09-06 07:18:12.706717 err [kernel:] [9164575.188352] sd 2:0:3:0: [sdd] tag#17 Medium access timeout failure. Offlining disk!
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(err\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[(\S+)\] (tag#\d+ Medium access timeout failure. Offlining disk!)'
        diskindex = 6
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] sd (\S+): \[(\S+)\] (tag#\d+ Medium access timeout failure. Offlining disk!)'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disknames = [ v[diskindex] for v in values ]
        disk = list(set(disknames))
        kernel_log["硬盘故障"] = "是"
        update_value(kernel_log, "硬盘故障信息", "硬盘故障:{}".format(values))
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk, ignore_exists=True, type="list")

    #nvme故障
    #2024-09-06 07:18:12.706717 err [kernel:] [9164575.188352] nvme nvme0: Device not ready; aborting initialisation, CSTS=0x0
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(err\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? nvme (\S+): (Device not ready; aborting initialisation), (CSTS=\S+)'
        diskindex = 5
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] nvme (\S+): (Device not ready; aborting initialisation), (CSTS=\S+)'
        diskindex = 1
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disknames = [ v[diskindex] for v in values ]
        disk = list(set(disknames))
        kernel_log["硬盘故障"] = "是"
        update_value(kernel_log, "硬盘故障信息", "NVME初始化故障:{}".format(values))
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk, ignore_exists=True, type="list")

    #nvme故障
    #2024-09-06 07:18:12.706717 warning [kernel:] [ 4575.188352] nvme nvme0: Removing after probe failure status: -19
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(warning\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? nvme (\S+): (Removing after probe failure status)'
        diskindex = 5
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] nvme (\S+): (Removing after probe failure status)'
        diskindex = 1
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disknames = [ v[diskindex] for v in values ]
        disk = list(set(disknames))
        kernel_log["硬盘故障"] = "是"
        update_value(kernel_log, "硬盘故障信息", "NVME故障:{}".format(values))
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk, ignore_exists=True, type="list")

    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? (sd \S+): \[([sd|nvme][\da-zA-Z]+)\] tag#(\d+) abort scheduled request=(0x[\da-fA-F]+), rq_timeout=(\d+) s, timeout=(\d+) s'
        diskindex = 6
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] \[([sd|nvme][\da-zA-Z]+)\] tag#(\d+) abort scheduled request=(0x[\da-fA-F]+), rq_timeout=(\d+) s, timeout=(\d+) s'
        diskindex = 1
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disk = [ v[diskindex] for v in values]
        disk = list(set(disk))
        kernel_log["磁盘IO错误"] = "是"
        kernel_log["磁盘IO错误信息"] = disk
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk, ignore_exists=True, type="list")

    #2024-11-14 10:02:21.111512 err [kernel:] [5570244.372057] nvme 10001:04:00.0: PCIe Bus Error: severity=Corrected, type=Physical Layer, (Receiver ID)
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(err\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? (nvme) (\S+): (PCIe Bus Error: severity=\S+, type=.*?)\n'
        diskindex = 0
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (nvme) (\S+): (PCIe Bus Error: severity=\S+, type=.*?)\n'
        diskindex = 0
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
    
    #megaraid_sas特征:
    #2024-09-09 06:59:19.251094 err [kernel:] [3520773.245466] megaraid_sas 0000:01:00.0: FW in FAULT state Fault code:0x10000 subcode:0x0 func:megasas_wait_for_outstanding_fusion
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(err\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? (megaraid_sas (\S+): FW in FAULT state Fault code:)(\S+) subcode:(\S+) func:(\S+)'
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (megaraid_sas (\S+): FW in FAULT state Fault code:)(\S+) subcode:(\S+) func:(\S+)'
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        update_value(kernel_log, "RAID故障", "是")
        update_value(kernel_log, "RAID故障信息", values)
        update_value(custom_info, "RAID建议", "升级固件")
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")

    #2024-10-10 07:30:32.058726 info [kernel:] [5075275.085151] megaraid_sas 0000:23:00.0: 124625 (781831032s/0x0020/DEAD) - Fatal firmware error: Line 1155 in ../../dm/src/dm.c
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? (megaraid_sas (\S+): (\d+) (\S+) - Fatal firmware error: (\S+))'
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (megaraid_sas (\S+): (\d+) (\S+) - Fatal firmware error: (\S+))'
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        update_value(kernel_log, "RAID故障", "是")
        update_value(kernel_log, "RAID故障信息", values)
        update_value(custom_info, "RAID建议", "升级固件")
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")

    #2024-09-09 06:59:59.911075 info [kernel:] [3520813.908497] megaraid_sas 0000:01:00.0: 45380 (779150973s/0x0020/CRIT) - Controller encountered a fatal error and was reset
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? (megaraid_sas (\S+): (\d+) (\S+) - Controller encountered (a fatal|an) error and was reset)'
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (megaraid_sas (\S+): (\d+) (\S+) - Controller encountered (a fatal|an) error and was reset)'
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        update_value(kernel_log, "RAID卡重置", "是")
        update_value(kernel_log, "RAID卡重置信息", values)
        update_value(custom_info, "RAID建议", "升级固件")
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")

    #mtp3sas特征：
    values = search_section(log_info, r'attempting host reset', re.DOTALL, group=-1, multi=1)
    if values:
        update_value(kernel_log, "RAID卡重置", "是")
        update_value(kernel_log, "RAID卡重置信息", values)
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")

    values = search_section(log_info, r'resetting fusion adapter scsi', re.DOTALL, group=-1, multi=1)
    if values:
        update_value(kernel_log, "RAID卡重置", "是")
        update_value(kernel_log, "RAID卡重置信息", values)

    values = search_section(log_info, r'sending diag reset', re.DOTALL, group=-1, multi=1)
    if values:
        update_value(kernel_log, "RAID卡重置", "是")
        update_value(kernel_log, "RAID卡重置信息", values)


    #megraid特征:
    values = search_section(log_info, r'OCR is requested due to IO timeout', re.DOTALL, group=-1, multi=1)
    if values:
        update_value(kernel_log, "RAID卡重置", "是")
        update_value(kernel_log, "RAID卡重置信息", values)

    values = search_section(log_info, r'mpt3sas_base_hard_reset_handler: enter', re.DOTALL, group=-1, multi=1)
    if values:
        update_value(kernel_log, "RAID卡重置", "是")
        update_value(kernel_log, "RAID卡重置信息", values)

    #smartpqi特征：
    values = search_section(log_info, r'waiting (\S+) seconds for LUN reset to complete', re.DOTALL, group=-1, multi=1)
    if values:
        update_value(kernel_log, "RAID卡重置", "是")
        update_value(kernel_log, "RAID卡重置信息", values)

    #2024-09-04 00:16:35.596405 err [kernel:] [30717627.164259][103] smartpqi 0000:63:00.0: resetting scsi 34:1:0:0 due to cmd 0x12
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(err\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? (smartpqi (\S+): resetting scsi) (\S+) (due to cmd) (\S+)'
        diskindex = 7
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (smartpqi (\S+): resetting scsi) (\S+) (due to cmd) (\S+)'
        diskindex = 3
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disk = [ d[diskindex] for d in values]
        disk = list(set(disk))
        disk_list = [get_disk_name_from_hctl(custom_info.get("磁盘列表"), d, custom_info) for d in disk]
        disk_list = [ d for d in disk_list if d ]

        update_value(kernel_log, "RAID卡重置", "是")
        update_value(kernel_log, "RAID卡重置信息", values)
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk_list, ignore_exists=True, type="list")

    #2024-09-04 00:16:35.602821 err [kernel:] [30717627.168530][103] smartpqi 0000:63:00.0: reset of scsi 34:1:0:0: SUCCESS
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(err\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? (smartpqi (\S+): reset of scsi) (\S+): (\S+)'
        diskindex = 7
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (smartpqi (\S+): reset of scsi) (\S+): (\S+)'
        diskindex = 3
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disk = [ d[diskindex] for d in values]
        disk = list(set(disk))
        disk_list = [get_disk_name_from_hctl(custom_info.get("磁盘列表"), d, custom_info) for d in disk]
        disk_list = [ d for d in disk_list if d ]

        kernel_log["RAID卡重置"] = "是"
        kernel_log["RAID卡重置信息"] = values
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk_list, ignore_exists=True, type="list")

    #aacraid
    #2024-11-19 10:58:33.535459 err [kernel:] [5863272.572726] aacraid: Host adapter abort request (0,2,2,0)
    #2024-11-19 10:58:48.598767 err [kernel:] [5863287.597408] aacraid: Host adapter abort request timed out
    #2024-11-19 11:00:31.959252 err [kernel:] [5863390.719107] aacraid: Host adapter reset request. SCSI hang ?
    #2024-11-19 11:01:17.150803 err [kernel:] [5863435.801232] aacraid 0000:5e:00.0: Controller reset type is 3
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(err\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? (aacraid: Host adapter abort request) \((\S+)\)'
        diskindex = 6
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (aacraid: Host adapter abort request) \((\S+)\)'
        diskindex = 1
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
    
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(err\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? (aacraid: Host adapter abort request timed out)'
        diskindex = 0
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (aacraid: Host adapter abort request timed out)'
        diskindex = 0
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
    
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(err\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? (aacraid: Host adapter reset request. SCSI hang)'
        diskindex = 0
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (aacraid: Host adapter reset request. SCSI hang)'
        diskindex = 0
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")

    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(err\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? (aacraid \S+: Controller reset type is \d+)'
        diskindex = 0
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (aacraid \S+: Controller reset type is \d+)'
        diskindex = 0
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")

    #RAID检测
    values = search_section(log_info, r'kernel: (.*?): Host adapter reset request. SCSI hang', re.DOTALL, group=-1, multi=1)
    if values:
        kernel_log["RAID卡响应超时"] = "是"
        kernel_log["RAID卡响应超时信息"] = values

    #超时重置特征
    values = search_section(log_info, r'Snd Host RST', re.DOTALL, group=-1 ,multi=1)
    if values and kernel_log.get("RAID卡重置") == "是":
        kernel_log["超时重置"] = "是"
        kernel_log["超时重置信息"] = values
    
    if logtype == "kernel.log":
        search_str = r'(\S+ \S+.\d+) (?:-\s+)?(info\s+)(?:\[kernel:\])? \[(\d+).(\d+)\](?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[(\S+)\](?:\stag#\d+)? (Sense Key : Medium Error )(.*?)\n'
        diskindex = 5
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] sd (\S+): \[(\S+)\](?:\stag#\d+)? (Sense Key : Medium Error )(.*?)\n'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        kernel_log["硬盘故障"] = "是"
        update_value(kernel_log, "硬盘故障信息", values)
        disk_err = [d[diskindex] for d in values]
        disk_err = list(set(disk_err))
        update_value(custom_info, "是否出现Media Error", disk_err)
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk_err, ignore_exists=True, type="list")

        #进一步分析Medium Error访问的地址分布
        sectorindex = 5
        if logtype == "kernel.log":
            search_str = r'(\S+ \S+.\d+) (?:-\s+)?(err\s+)(?:\[kernel:\])? \[(\d+).(\d+)\](?:\[\s{0,6}\d{0,6}\])? \S+: critical medium error, dev (\S+), sector (\d+)'
            diskindex = 4
            sectorindex = 5
        elif logtype == "LOG_dmesg.txt":
            search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (\S+: critical medium error), dev (\S+), sector (\d+)'
            diskindex = 2
            sectorindex = 3
        values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
        if values:
            update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
            sector_list = {}
            for err in values:
                if not sector_list.get(err[diskindex]):
                    sector_list[err[diskindex]] = {"total_count": 0, "sector":[], "statistic":{}, "sector_statistic":{}}
                sector_list[err[diskindex]]["total_count"] += 1
                update_value(sector_list[err[diskindex]], "sector", err[sectorindex])
                if not sector_list[err[diskindex]]["sector_statistic"].get(err[sectorindex]):
                    sector_list[err[diskindex]]["sector_statistic"][err[sectorindex]] = {"count":0}
                sector_list[err[diskindex]]["sector_statistic"][err[sectorindex]]["count"] += 1
                update_value(sector_list[err[diskindex]]["sector_statistic"][err[sectorindex]], "times", err[0])
            
            for key , value in sector_list.items():
                for sec, v in value["sector_statistic"].items():
                    value["statistic"][sec] = statistic_times(v["times"], "{}频率".format(sec))

            custom_info["Medium Error分析"] = sector_list
    
    # 无效指令
    # 2024-11-10 21:50:05.611559 - info  [97157432.654097] sd 14:1:0:0: [sdm] Sense Key : Illegal Request [current] 
    # 2024-11-10 21:50:05.611571 - info  [97157432.654104] sd 14:1:0:0: [sdm] Add. Sense: Invalid command operation code
    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[(\S+)\] (Add. Sense: Invalid command operation code|Sense Key : Illegal Request)'
        diskindex = 6
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] sd (\S+): \[(\S+)\] (Add. Sense: Invalid command operation code|Sense Key : Illegal Request)'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        disknames = [ v[diskindex] for v in values ]
        disk = list(set(disknames))
        update_value(kernel_log, "无效指令", values, ignore_exists=True, type="list")
        update_value(custom_info, "问题初步结论", "存在无效指令", ignore_exists=True, type="list")

    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[(\S+)\] (Sense:3/11/00)(.*?)\n'
        diskindex = 6
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] sd (\S+): \[(\S+)\] (Sense:3/11/00)(.*?)\n'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=0)
    if values:
        kernel_log["硬盘故障"] = "是"
        disk_err = [d[diskindex] for d in values]
        disk_err = list(set(disk_err))
        update_value(kernel_log, "硬盘故障信息", values, type="list", insert_pos=0)
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk_err, ignore_exists=True, type="list")

    if logtype == "kernel.log":
        search_str = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d{1,6}) (?:-\s+)?(info\s+)(?:\[kernel:\])? (\[(\d+).(\d+)\])(?:\[\s{0,6}\d{0,6}\])? sd (\S+): \[(\S+)\] (Sense Key : Hardware Error)(.*?)\n'
        diskindex = 6
    elif logtype == "LOG_dmesg.txt":
        search_str = r'\[(\w+ \w+ \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] sd (\S+): \[(\S+)\] (Sense Key : Hardware Error)(.*?)\n'
        diskindex = 2
    values = search_section(log_info, search_str, re.DOTALL, group=-1, multi=1)
    if values:
        kernel_log["硬盘故障"] = "是"
        disk_err = [d[diskindex] for d in values]
        disk_err = list(set(disk_err))
        update_value(kernel_log, "硬盘故障信息", values, type="list", insert_pos=0)
        update_value(custom_info, "内核异常日志", values, ignore_exists=True, type="list")
        update_value(kernel_log, "故障磁盘列表", disk_err, ignore_exists=True, type="list")
    
    values = search_section(log_info, r'Sense:4/(.*?)\n', re.DOTALL, group=0)
    if values:
        kernel_log["硬盘故障"] = "是"
        update_value(kernel_log, "硬盘故障信息", values, type="list", insert_pos=0)


    #if kernel_log.get("故障磁盘列表"):
    #    #去重,会导致顺序乱
    #    kernel_log["故障磁盘列表"] = list(set(kernel_log["故障磁盘列表"]))

    return kernel_log

def statistic_times(times, tag="频率", time_format="%Y-%m-%d %H:%M:%S.%f", data_pick=0):
    #将时间转为datetime,方便于做时间对比
    log_datetime = None
    #尝试转换检查是否为时间
    try:
        log_datetime = datetime.datetime.strptime(times[0], time_format)
    except Exception as e:
        try:
            log_datetime = datetime.datetime.strptime(times[0], "%y-%m-%d,%H:%M:%S")
            time_format = "%y-%m-%d,%H:%M:%S"
        except Exception as e:
            try:
                log_datetime = datetime.datetime.strptime(times[0], "%y/%m/%d,%H:%M:%S")
                time_format = "%y/%m/%d,%H:%M:%S"
            except Exception as e:
                try:
                    log_datetime = datetime.datetime.strptime(times[0], "%y/%m/%d %H:%M:%S")
                    time_format = "%y/%m/%d %H:%M:%S"
                except Exception as e:
                    try:
                        log_datetime = datetime.datetime.strptime(times[0], "%y-%m-%d %H:%M:%S")
                        time_format = "%y-%m-%d %H:%M:%S"
                    except Exception as e:
                        return None

    obj = pd.to_datetime(times, format=time_format)
    # 创建DataFrame
    df = pd.DataFrame({'DateTime': obj})
    df["DiffTime"] = df["DateTime"].diff()
    df["Diff_Seconds"] = df["DiffTime"].dt.total_seconds()
    
    #统计时间差分布情况
    statistics = {}
    statistics["TAG"] = tag
    #print(df)
    # 描述性统计
    if data_pick == 0:
        descriptive_stats = df["Diff_Seconds"].describe()
    else:
        descriptive_stats = df.loc[(df.index + 1) % data_pick == 0, "Diff_Seconds"].describe()

    statistics["统计摘要"] = json.loads(descriptive_stats.rename(g_statistic_translation_dict).to_json(force_ascii=False))
    return statistics

#解析raid日志中的磁盘位置:(e0x45/s1)
def parse_raid_disk(diskstr):
    #去掉()并转十六进制为十进制
    if not diskstr:
        return None
    disk = diskstr.replace('(', "").replace(')', "")
    num, slot = disk.split('/')
    num = int(num[1:], 16)
    return (num, slot)

def get_raid_disk_slot(diskstr):
    (num, slot) = parse_raid_disk(diskstr)
    return "/e{}/{}".format(num, slot)

#按照指定类型更新元素
def update_value(obj, key, value, ignore_exists=True, type="object", insert_pos=-1):

    if obj.get(key):
        if isinstance(obj.get(key), list):
            if isinstance(value, list):
                for v in value:
                    if ignore_exists:
                        if not v in obj[key]:
                            if insert_pos == -1:
                                obj[key].append(v)
                            else:
                                obj[key].insert(insert_pos, v)
                    else:
                        if insert_pos == -1:
                            obj[key].append(v)
                        else:
                            obj[key].insert(insert_pos, v)
            else:
                if ignore_exists:
                    if not value in obj[key]:
                        if insert_pos == -1:
                            obj[key].append(value)
                        else:
                            obj[key].insert(insert_pos, value)
                else:
                    if insert_pos == -1:
                        obj[key].append(value)
                    else:
                        obj[key].insert(insert_pos, value)
        else:
            if obj.get(key) != value:
                obj[key] = [obj.get(key), value]
    else:
        if type == "list":
            if isinstance(value, list):
                obj[key] = value
            else:
                obj[key] = [value]
        else:
            obj[key] = value

    return value

#计算缩进级别(按空格)
def get_indent_level(s, indent = 3):
    # 计算字符串前面空格的数量
    len1 = len(s)
    len2 = len(s.lstrip(' '))
    return int((len1 - len2) / indent)

#@Handle_Exceptions
def parse_smartpqi_section_json(content):
    lines = content.split('\n')
    if len(lines) == 0:
        return None
    
    data = {}
    root = data
    element = None

    section_start = False
    level_section = [data, None, None, None, None]

    listindex = 0
    line_cnt = 0
    for line in lines:
        line_cnt += 1
        if line.strip() == "":
            continue

        if line.find("| UCLI |") != -1:
            data[line.strip()] = None
            continue

        if get_indent_level(line) == 0:
            element = line.strip()
            data[element] = {}
            level_section[1] = data[element]
            root = level_section[1]
            continue

        level = get_indent_level(line)
        if element:
            level += 1

        if line.find('-------------------------') != -1:
            section_start = not section_start
            continue

        if section_start:
            root = level_section[level-1]
            if line.strip().find("Device Phy Information") != -1 or line.strip().find("Device Error Counters") != -1:
                obj = update_value(root, line.strip(), {}, ignore_exists=False)
                #if not level_section[level].get(line.strip()):
                #    level_section[level][line.strip()] = obj
                    
            else:
                if not root.get(line.strip()):
                    root[line.strip()] = {}
            level_section[level] = root[line.strip()]
            root = root[line.strip()]
            continue

        if line.find('#') != -1:
            root = level_section[level-1]
            
            if isinstance(root, list):
                if not root[len(root)-1].get(line.strip()):
                    root[len(root)-1][line.strip()] = {}

                level_section[level] = root[len(root)-1][line.strip()]
                root = root[len(root)-1][line.strip()]
                listindex += 1
            else:
                listindex = 0
                if not root.get(line.strip()):
                    root[line.strip()] = {}

                level_section[level] = root[line.strip()]
                root = root[line.strip()]
            continue

        if line.find(':') != -1:
            s = line.strip().split(':')
            key = s[0].strip()
            if len(s) > 1:
                if len(s) > 2:
                    value = ":".join(s[1:])
                else:
                    value = s[1].strip()

            if isinstance(root, list):
                update_value(root[len(root)-1], key, value)
            else:
                update_value(root, key, value)

            continue

        if isinstance(root, list):
            root[len(root)-1][line.strip()] = None
        else:
            root[line.strip()] = None

    #处理Temperature Sensors Information
    if data.get("Temperature Sensors Information"):
        tsi = []
        sid = data["Temperature Sensors Information"].get("Sensor ID")
        for i in range(0, len(sid)):
            try:
                tsi.append(
                    {
                        "Sensor ID": sid[i],
                        "Current Value": data["Temperature Sensors Information"].get("Current Value")[i],
                        "Max Value Since Powered On": data["Temperature Sensors Information"].get("Max Value Since Powered On")[i],
                        "Location": data["Temperature Sensors Information"].get("Location")[i]
                    }
                )
            except Exception as e:
                logger.error(e)

        data["Temperature Sensors Information"] = tsi
    
    #处理Connector information
    if "Connector information" in data:
        cis = []
        popkey = []
        for key, value in data.items():
            if key.find("Connector #") == -1:
                continue
            cis.append({key: value})
            popkey.append(key)

        for k in popkey:
            data.pop(k)

        data["Connector information"] = cis

    update_smartpqi_device_info(data)

    return data

def update_smartpqi_device_info(data):
    #处理Device Phy Information和Device Error Counters

    for key, value in data.items():
        if key.find("Channel #") == -1:
            continue
        
        update_smartpqi_channel_device_info(value)

def update_smartpqi_channel_device_info(channel):
    value = channel
    dpi_cnt = 0
    dec_cnt = 0
    if value.get("Device Phy Information"):
        dpi_cnt = len(value.get("Device Phy Information"))

    if value.get("Device Error Counters"):
        dec_cnt = len(value.get("Device Error Counters"))
    
    if dpi_cnt != dec_cnt:
        logger.error("smartpqi channel error:Device Phy Information count - {}, Device Error Counters - {}, info:{}".format(dpi_cnt, dec_cnt, channel))
        return False
    
    device_cnt = 0
    for key, v in channel.items():
        if key.find("Device #") == -1:
            continue
        device_cnt += 1
    
    if device_cnt != dpi_cnt:
        logger.error("smartpqi channel error:Device count - {}, Device Phy Information - {}, info:{}".format(device_cnt, dpi_cnt, channel))
        return False
    
    index = 0
    for key, v in channel.items():
        if key.find("Device #") == -1:
            continue
        v["Device Phy Information"] = value.get("Device Phy Information")[index]
        v["Device Error Counters"] = value.get("Device Error Counters")[index]
        index += 1
    
    return True
    

def parse_smartpqi_config_to_json(config_data):
    data = {}
    config_data = "----------------------------------------------------------------------\nDescription\n----------------------------------------------------------------------\n" + config_data + "\n-"
    #config_data = config_data.replace("\n\n\n","\n").replace("\n\n","\n")
    sections = search_section(config_data, r"---------------------------------------------------------------------\n(.*?)\n----------------------------------------------------------------------\n(.*?)\n-", re.DOTALL, group=-1, multi=1)
    if not sections:
        return data

    for section in sections:
        data[section[0]] = parse_smartpqi_section_json(section[1])
    
    return data

def check_smartpqi_log(custom_info, loginfo):
    if not custom_info or not loginfo:
        return False
    
    #[2024-10-09 12:08:59] ALERT: Invalid SCSI command: name=RD_DEFECTS_12 req_type=0x02000003 cmd=0x90 opcode=0xB7
    values = search_section(loginfo, r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (ALERT: Invalid SCSI command:) (name=(\S+) req_type=(\S+) cmd=(\S+) opcode=(\S+))', re.DOTALL, group=-1, multi=1)
    if values:
        for value in values:
            if (compareRAIDTimeLogTimeIndays(custom_info.get("RAID时间"), custom_info.get("故障时间"), value[0], time_format="%Y-%M-%d %H:%M:%S") and not IgnoreCheckingRAIDTime(custom_info, value[0], time_format="%Y-%m-%d %H:%M:%S")):
                update_value(custom_info, "RAID故障", "存在无效指令")
                update_value(custom_info, "RAID故障信息", value)
                update_value(custom_info, "RAID建议", "打最新集合补丁或升级版本")
                update_value(custom_info, "RAID异常日志", value, ignore_exists=False, type="list")
    

def update_smartpqi_custominfo(custom_info):
    #从firmware获取
    firmwre_lists = custom_info["smartpqi"].get("firmware")
    for key, controller in firmwre_lists.items():
        if key.find("controller") == -1:
            continue
        if controller.get("Config"):
            if controller.get("Config").get("Controller information"):
                ci = controller.get("Config").get("Controller information")
                if ci.get("Controller Version Information"):
                    if ci.get("Controller Version Information").get("Firmware"):
                        custom_info["RAID固件版本"] = ci.get("Controller Version Information").get("Firmware")

                    if ci.get("Controller Version Information").get("Driver"):
                        custom_info["RAID驱动版本"] = ci.get("Controller Version Information").get("Driver")
        if controller.get("Event_log"):
            if controller.get("Event_log").get("ControllerLog"):
                cl = controller.get("Event_log").get("ControllerLog")
                if cl.get("eventlog"):
                    eventlog = cl.get("eventlog")
                    errors = []
                    if eventlog.get('event'):
                        for event in eventlog.get('event'):
                            evmsg = event.get("@message")
                            value = search_section(evmsg, r"(Expander Link) (Down|Up), (Port=\S+ Box=\S+ Exp=\S+) (Phy=\S+) (Port on module=\S+)", re.DOTALL, group=-1)
                            if value:
                                err = {
                                    "日志": value[0],
                                    "事件": value[1],
                                    "类型": value[2],
                                    "端口": value[3],
                                    "物理": value[4],
                                    "时间": event.get("@time")
                                }
                                if compareRAIDTimeLogTimeIndays(custom_info.get("RAID时间"), custom_info.get("故障时间"), err.get("时间"), time_format="%Y-%m-%d %H:%M:%S"):
                                    errors.append(err)

                    if len(errors) > 0:
                        df = pd.DataFrame(errors)
                        # 步骤1：统计每个物理端口的类型数量
                        type_stats = df.groupby('物理').agg(
                            total_events=('类型', 'size'),  # 总事件数
                            unique_types=('类型', 'nunique')  # 唯一类型数
                        ).reset_index()
                        
                        # 步骤2：筛选出存在多种事件类型的物理设备
                        abnormal_phys = type_stats[type_stats['unique_types'] > 1]['物理'].tolist()
                        if len(abnormal_phys) > 1:
                            custom_info["RAID故障"] = "是"
                            update_value(custom_info, "RAID故障信息", errors)
                            update_value(custom_info, "RAID建议", "请确认RAID卡固件和驱动兼容性")


    #从arcconfig.xml获取
    storlib = custom_info["smartpqi"].get("storlib")
    if storlib and storlib.get("System"):
        if storlib.get("System").get("Controller"):
            if isinstance(storlib.get("System").get("Controller"), list):
                for controller in storlib.get("System").get("Controller"):
                    if controller.get("@firmwareVersion"):
                        custom_info["RAID固件版本"] = controller.get("@firmwareVersion")
                    if controller.get("@driverVersion"):
                        custom_info["RAID固件版本"] = controller.get("@driverVersion")
            else:
                if storlib.get("System").get("Controller").get("@firmwareVersion"):
                    custom_info["RAID固件版本"] = storlib.get("System").get("Controller").get("@firmwareVersion")
                if storlib.get("System").get("Controller").get("@driverVersion"):
                    custom_info["RAID驱动版本"] = storlib.get("System").get("Controller").get("@driverVersion")
    pass


#@Handle_Exceptions
def parse_smartpqi_firmware_info(custom_info, firmware_dir, controller=1):
    if not custom_info:
        return False
    
    if not custom_info.get("smartpqi"):
        custom_info["smartpqi"] = {}

    controller_key = "controller{}".format(controller)

    if not custom_info.get("smartpqi").get("firmware"):
        custom_info["smartpqi"]["firmware"] = {}

    if not custom_info.get("smartpqi").get("firmware").get(controller_key):
        custom_info["smartpqi"]["firmware"][controller_key] = {}

    #Controller_1_Config.txt
    smartconfig_path = os.path.join(firmware_dir, "Controller_{}_Config.txt".format(controller))
    if os.path.exists(smartconfig_path):
        with open(smartconfig_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            custom_info["smartpqi"]["firmware"][controller_key]["Config"] = parse_smartpqi_config_to_json(content)

    #Controller_1_SmartStats.xml
    smartstats_path = os.path.join(firmware_dir, "Controller_{}_SmartStats.xml".format(controller))
    if os.path.exists(smartstats_path):
        with open(smartstats_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            '''
            xmlroot = ET.fromstring(content)
            phydrivers = []
            for phydriver in xmlroot.findall("PhysicalDriveSmartStats"):
                phydriver_info = {}
                phydriver_info["channel"] = phydriver.get("channel")
                phydriver_info["id"] = phydriver.get("id")
                phydriver_info["nonSpinning"] = phydriver.get("nonSpinning")
                phydriver_info["isDescriptionAvailable"] = phydriver.get("isDescriptionAvailable")
                phydriver_info["smartctl_info"] = []

                for attr in phydriver.findall("Attribute"):
                    smart_attr = {}
                    smart_attr["id"] = int(attr.get("id"), base=16)
                    smart_attr["name"] = attr.get("name")
                    smart_attr["normalizedCurrent"] = attr.get("normalizedCurrent")
                    smart_attr["normalizedWorst"] = attr.get("normalizedWorst")
                    smart_attr["thresholdValue"] = attr.get("thresholdValue")
                    smart_attr["rawValue"] = attr.get("rawValue")
                    smart_attr["Status"] = attr.get("Status")
                    phydriver_info["smartctl_info"].append(smart_attr)
                
                phydrivers.append(phydriver_info)

            custom_info["smartpqi"]["firmware"][controller_key]["drivers_stats"] = phydrivers
            '''
            custom_info["smartpqi"]["firmware"][controller_key]["drivers_stats"] = xmltodict.parse(content)

    #Controller_1_Event_Log.txt
    event_log_path = os.path.join(firmware_dir, "Controller_{}_Event_Log.txt".format(controller))
    if os.path.exists(event_log_path):
        with open(event_log_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            if content:
                custom_info["smartpqi"]["firmware"][controller_key]["Event_log"] = xmltodict.parse(content)

    #update smartpqi info
    update_smartpqi_custominfo(custom_info)

    #Controller_1_Log.txt
    smartconfig_path = os.path.join(firmware_dir, "Controller_{}_Log.txt".format(controller))
    if os.path.exists(smartconfig_path):
        with open(smartconfig_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            check_smartpqi_log(custom_info, content)

    return True

def check_smartpqi_arcconfig_harddrive(custom_info, harddrive):
    sn = harddrive.get("@serialNumber")
    if sn:
        sn = sn.strip()
    disk = get_disk_name_by_sn(custom_info, sn)
    if not disk:
        logger.error("cannot find disk:{}", sn)
    
    if not custom_info.get("内核日志"):
        custom_info["内核日志"] = {}

    if harddrive.get("@mediumErrorCount"):
        if int(harddrive.get("@mediumErrorCount")) > 0:
            custom_info["内核日志"]["硬盘故障"] = "是"
            update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::mediumErrorCount:{}".format(harddrive.get("@mediumErrorCount")))
            update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")

    if harddrive.get("@mediaFailures"):
        if int(harddrive.get("@mediaFailures")) > 0:
            custom_info["内核日志"]["硬盘故障"] = "是"
            update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::mediaFailures:{}".format(harddrive.get("@mediaFailures")))
            update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")
    
    if harddrive.get("@predictiveFailures"):
        if int(harddrive.get("@predictiveFailures")) > 0:
            custom_info["内核日志"]["硬盘故障"] = "是"
            update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::predictiveFailures:{}".format(harddrive.get("@predictiveFailures")))
            update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")
    
    if harddrive.get("@hardwareErrors"):
        if int(harddrive.get("@hardwareErrors")) > 0:
            custom_info["内核日志"]["硬盘故障"] = "是"
            update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::hardwareErrors:{}".format(harddrive.get("@hardwareErrors")))
            update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")

    if harddrive.get("@hardReadErrors"):
        if int(harddrive.get("@hardReadErrors")) > 0:
            custom_info["内核日志"]["硬盘故障"] = "是"
            update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::hardReadErrors:{}".format(harddrive.get("@hardReadErrors")))
            update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")
    
    if harddrive.get("@hardWriteErrors"):
        if int(harddrive.get("@hardWriteErrors")) > 0:
            custom_info["内核日志"]["硬盘故障"] = "是"
            update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::hardWriteErrors:{}".format(harddrive.get("@hardWriteErrors")))
            update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")
    
    if harddrive.get("@scsiBusFaults"):
        if int(harddrive.get("@scsiBusFaults")) > 0:
            custom_info["内核日志"]["硬盘故障"] = "是"
            update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::scsiBusFaults:{}".format(harddrive.get("@scsiBusFaults")))
            update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")
    
    if harddrive.get("@linkFailureCount"):
        if int(harddrive.get("@linkFailureCount")) > 0:
            custom_info["内核日志"]["硬盘故障"] = "是"
            update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::linkFailureCount:{}".format(harddrive.get("@linkFailureCount")))
            update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")

    if harddrive.get("@senseKey4Count"):
        if int(harddrive.get("@senseKey4Count")) > 0:
            custom_info["内核日志"]["硬盘故障"] = "是"
            update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::senseKey4Count:{}".format(harddrive.get("@senseKey4Count")))
            update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")

def check_smartpqi_arcconfig_channel_drive_risk(custom_info, channel):
    if channel.get("HardDrive"):
        if isinstance(channel.get("HardDrive"), list):
            for hd in channel.get("HardDrive"):
                check_smartpqi_arcconfig_harddrive(custom_info, hd)
        else:
            harddrive = channel.get("HardDrive")
            if harddrive:
                check_smartpqi_arcconfig_harddrive(custom_info, harddrive)

#检查smartpqi记录
def check_smartpqi_risk(custom_info):
    #检查Storlib(arcconfig.xml)
    storlib = custom_info["smartpqi"].get("storlib")
    if storlib and storlib.get("System"):
        if storlib.get("System").get("Controller"):
            if isinstance(storlib.get("System").get("Controller"), list):
                for con in storlib.get("System").get("Controller"):
                    channel = con.get("Channel")
                    if channel:
                        if isinstance(channel, list):
                            for ch in channel:
                                check_smartpqi_arcconfig_channel_drive_risk(custom_info, ch)
                        else:
                            check_smartpqi_arcconfig_channel_drive_risk(custom_info, channel)
            else:
                channel = storlib.get("System").get("Controller").get("Channel")
                if channel:
                    if isinstance(channel, list):
                        for ch in channel:
                            check_smartpqi_arcconfig_channel_drive_risk(custom_info, ch)
                    else:
                        check_smartpqi_arcconfig_channel_drive_risk(custom_info, channel)
    
    if not custom_info.get("内核日志"):
        custom_info["内核日志"] = {}
    #检查Firmware
    firmware = custom_info["smartpqi"].get("firmware")
    if firmware:
        for key , controller in firmware.items():
            if controller.get("Config"):
                config = controller.get("Config")
                if config.get("Controller information"):
                    ci = config.get("Controller information")
                    if ci.get("Green Backup Information"):
                        if ci.get("Green Backup Information").get("Hardware Error") != "No Error":
                            update_value(custom_info, "RAID故障", "Smartpqi硬件故障")
                            update_value(custom_info, "RAID故障信息", "Hardware Error".format(ci.get("Green Backup Information").get("Hardware Error")))
                            update_value(custom_info, "RAID建议", "更换RAID卡")

                if config.get("Physical Device information"):
                    for k, channel in config.get("Physical Device information").items():
                        for kd, device in channel.items():
                            if kd.find("Device #") == -1:
                                continue

                            sn = device.get("Serial number")
                            if sn:
                                sn = sn.strip()
                            
                            disk = get_disk_name_by_sn(custom_info, sn)
                            if not disk:
                                logger.error("cannot find disk:{}".format(sn))

                            if device.get("Device Error Counters"):
                                dec = device.get("Device Error Counters")
                                if dec.get("Hardware Errors"):
                                    if int(dec.get("Hardware Errors")) > 0:
                                        custom_info["内核日志"]["硬盘故障"] = "是"
                                        update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::Config::Hardware Errors:{}".format(dec.get("Hardware Errors")))
                                        update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")

                                if dec.get("Hard Read Errors"):
                                    if int(dec.get("Hard Read Errors")) > 0:
                                        custom_info["内核日志"]["硬盘故障"] = "是"
                                        update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::Config::Hard Read Errors:{}".format(dec.get("Hard Read Errors")))
                                        update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")

                                if dec.get("Hard Write Errors"):
                                    if int(dec.get("Hard Write Errors")) > 0:
                                        custom_info["内核日志"]["硬盘故障"] = "是"
                                        update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::Config::Hard Write Errors:{}".format(dec.get("Hard Write Errors")))
                                        update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")

                                if dec.get("Media Failures"):
                                    if int(dec.get("Media Failures")) > 0:
                                        custom_info["内核日志"]["硬盘故障"] = "是"
                                        update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::Config::Media Failures:{}".format(dec.get("Media Failures")))
                                        update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")

                                if dec.get("Predictive Failures"):
                                    if int(dec.get("Predictive Failures")) > 0:
                                        custom_info["内核日志"]["硬盘故障"] = "是"
                                        update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::Config::Predictive Failures:{}".format(dec.get("Predictive Failures")))
                                        update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")

                                if dec.get("Scsi Bus Faults"):
                                    if int(dec.get("Scsi Bus Faults")) > 0:
                                        custom_info["内核日志"]["硬盘故障"] = "是"
                                        update_value(custom_info["内核日志"], "硬盘故障信息", "smartpqi::Config::Scsi Bus Faults:{}".format(dec.get("Scsi Bus Faults")))
                                        update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")

#分析smartpqi日志
def check_smartpqi_raid_log(raidlog_path, custom_info):
    if not raidlog_path:
        return False
    
    smartpqi_dir = os.path.join(raidlog_path, "Support")
    if not os.path.exists(smartpqi_dir):
        return False
    
    #Storlib目录
    storlib_dir = os.path.join(smartpqi_dir, "Storlib")
    if os.path.exists(storlib_dir):
        #arcconfig.xml
        arcconfig_path = os.path.join(storlib_dir, "arcconfig.xml")
        if os.path.exists(arcconfig_path):
            with open(arcconfig_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                #xmlroot = ET.fromstring(content)
                if not custom_info.get("smartpqi"):
                    custom_info["smartpqi"] = {}
                custom_info["smartpqi"]["storlib"] = xmltodict.parse(content)
    
    #Firmware目录
    firmware_dir = os.path.join(smartpqi_dir, "Firmware")
    if os.path.exists(firmware_dir):
        files = os.listdir(firmware_dir)
        controls = []
        for file in files:
            if file.find("Config.txt") != -1:
                control = search_section(file, r"Controller_(\d)_Config.txt", re.DOTALL, group=1, multi=0)
                if control:
                    controls.append(int(control))

        controls = list(set(controls))
        for control in controls:
            parse_smartpqi_firmware_info(custom_info, firmware_dir, controller=control)

    #检查smartpqi
    check_smartpqi_risk(custom_info)
    return True

#判断Raid日志是否存在异常
def check_raid_log(raid_log, custom_info, adapter=0):
    #判断是否发生raid卡timeout
    value = search_section(raid_log, r'(\d{2}-\d{2}-\d{2},\d{2}:\d{2}:\d{2}) (WARNING:Command timeout on PD) [0-9a-fA-Z]+(\(\S+\))', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                custom_info["RAID中硬盘是否出现超时"] = "是"
                update_value(custom_info, "RAID中硬盘是否出现超时信息", err)
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
            else:
                custom_info["RAID中硬盘是否出现超时"] = "否"

    value = search_section(raid_log, r'(\d{2}-\d{2}-\d{2},\d{2}:\d{2}:\d{2}) (WARNING:Removed: PD) [0-9a-fA-Z]+(\(\S+\))', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                custom_info["RAID卡移除硬盘"] = "是"
                update_value(custom_info, "RAID卡移除硬盘", err)
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
            else:
                custom_info["RAID卡移除硬盘"] = "否"

    value = search_section(raid_log, r'(\d{2}-\d{2}-\d{2},\d{2}:\d{2}:\d{2}) (Info:State change on PD) [0-9a-fA-Z]+\((\S+)\) (from) (JBOD|UNCONFIGURED_BAD|UNCONFIGURED_GOOD)\(\d+\) (to) (JBOD|UNCONFIGURED_BAD|UNCONFIGURED_GOOD)\(\d+\)', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")

    value = search_section(raid_log, r'(\d{2}-\d{2}-\d{2},\d{2}:\d{2}:\d{2}) (Info:Controller requests a host bus rescan)', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")

    #判断是否发生raid卡reset
    value = search_section(raid_log, r'(\d{2}-\d{2}-\d{2},\d{2}:\d{2}:\d{2}) (WARNING:PD [0-9a-fA-Z]+)(\(\S+\)) (Path\s+\S+\s+) reset \(Type 03\)\n', re.DOTALL, group = -1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                custom_info["RAID中硬盘是否出现重置"] = "是"
                update_value(custom_info, "RAID中硬盘是否出现重置信息", err)
                raid_disk = "/c{}".format(adapter) + get_raid_disk_slot(err[2])
                update_value(custom_info, "RAID中异常硬盘列表", raid_disk, ignore_exists=True, type="list")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
    else:
        custom_info["RAID中硬盘是否出现重置"] = "否"

    #博通RAID故障说明
    #24-07-20,03:26:15 FATAL:Uncorrectable medium error logged for VD 05/5 at 1579ec5 (on PD 0f(e0x08/s6) at 1579ec5)
    value = search_section(raid_log, r'(\d{2}-\d{2}-\d{2},\d{2}:\d{2}:\d{2}) (FATAL:Uncorrectable medium error logged for VD \S+ at \S+) \(on PD \S+\((\S+)\) at \S+\)\n', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                update_value(custom_info, "RAID故障", "硬盘不可修复的介质错误")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "根据PD/VD号可在LdPdInfo或PDList文件中找到实际槽位号，更换报错硬盘。")
                raid_disk = "/c{}".format(adapter) + get_raid_disk_slot(err[2])
                update_value(custom_info, "RAID中异常硬盘列表", raid_disk, ignore_exists=True, type="list")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")

    #24-08-03,04:19:22 FATAL:Bad block table on VD 05/5 is full; unable to log block 70f2116 (on PD 0f(e0x08/s6) at 70f2116)
    value = search_section(raid_log, r'(\d{2}-\d{2}-\d{2},\d{2}:\d{2}:\d{2}) (FATAL:Bad block table on VD \S+ is full; unable to log block \S+) \(on PD \S+\((\S+)\) at \S+\)\n', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                update_value(custom_info, "RAID故障", "硬盘坏块表已满")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "硬盘坏块表已满，更换报错硬盘。")
                raid_disk = "/c{}".format(adapter) + get_raid_disk_slot(err[2])
                update_value(custom_info, "RAID中异常硬盘列表", raid_disk, ignore_exists=True, type="list")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")

    value = search_section(raid_log, r'Controller temperature threshold exceeded(.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡温度过高")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "检查风扇、散热是否正常。")

    value = search_section(raid_log, r'Invalid memory configuration detected(.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡内存配置错误")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "建议更换RAID卡。")

    value = search_section(raid_log, r'(\d{2}-\d{2}-\d{2},\d{2}:\d{2}:\d{2}) (WARNING:Battery temperature is high)(.*?)\n', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                update_value(custom_info, "RAID故障", "RAID卡电池温度过高")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "检查风扇、散热是否正常。")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
    
    value = search_section(raid_log, r'Enclosure (\S+) temperature sensor (\S+) above error threshold\n', re.DOTALL, group=-1, multi=1)
    if value:
        update_value(custom_info, "RAID故障", "硬盘背板温度过高")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "检查风扇、散热是否正常。")

    value = search_section(raid_log, r'Battery charging was suspended due to high battery temperature(.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "Raid卡电池温度过高")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "检查风扇、散热是否正常。")

    value = search_section(raid_log, r'pciErrorInt: iopIntStatus(.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡异常")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "请更换raid卡。")

    value = search_section(raid_log, r'MedErr is for: cmdId=(.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "硬盘介质错误")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "更换报错硬盘。")

    value = search_section(raid_log, r'(\S+) (CRITICAL:Enclosure )(\S+) is unstable\n', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                update_value(custom_info, "RAID故障", "硬盘背板不可用")
                update_value(custom_info, "RAID故障信息", value)
                update_value(custom_info, "RAID建议", "建议更换硬盘背板。")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
    
    value = search_section(raid_log, r'(\S+) (CRITICAL:Enclosure )(\S+) hardware error\n', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                update_value(custom_info, "RAID故障", "硬盘背板不可用")
                update_value(custom_info, "RAID故障信息", value)
                update_value(custom_info, "RAID建议", "建议更换硬盘背板。")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")

    value = search_section(raid_log, r'Multibit ECC errors were detected on the RAID controller\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡内存错误")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "建议更换RAID卡。")

    value = search_section(raid_log, r'IO processor has a fault that can potentially cause data corruption\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡IO错误")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "建议更换RAID卡。")

    value = search_section(raid_log, r'Cannot communicate with iButton, possible extreme temps\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡温度过高")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "检查风扇、散热是否正常。")

    value = search_section(raid_log, r'(\S+) (DEAD:Fatal firmware error:) (.*?)\n', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                update_value(custom_info, "RAID故障", "致命错误，属于不可容忍错误")
                update_value(custom_info, "RAID故障信息", value)
                update_value(custom_info, "RAID建议", "需要具体问题具体分析")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")

    value = search_section(raid_log, r'"Controller booted to safe mode due to critical errors (.*?) Critical Error during boot', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "致命错误，属于不可容忍错误")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "需要具体问题具体分析")

    value = search_section(raid_log, r'faultcode (.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡关键报错，视具体code判定")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "需要具体问题具体分析")
    
    value = search_section(raid_log, r'(\d{2}/\d{2}/\d{2}\s+\d{1,2}:\d{2}:\d{2}): (Assertion failure) (.*?)\n', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0], time_format="%m/%d/%y %H:%M:%S")):
                update_value(custom_info, "RAID故障", "RAID卡发生断言失败")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "升级RAID固件")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")

    value = search_section(raid_log, r'Online Controller Reset(.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡发生OCR重置")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "升级RAID固件或更换RAID卡")
    
    value = search_section(raid_log, r'(\d{2}/\d{2}/\d{2}\s+\d{1,2}:\d{2}:\d{2}): (online reset - wait for Reset to occur)\n', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0], time_format="%m/%d/%y %H:%M:%S")):
                update_value(custom_info, "RAID故障", "RAID卡发生online reset重置")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "升级RAID固件或更换RAID卡")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")

    value = search_section(raid_log, r'Single-bit ECC error: (.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡的缓存颗粒故障")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "建议更换RAID卡")

    value = search_section(raid_log, r'L2/L3 Cache error was detected(.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡的芯片缓存异常")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "建议更换RAID卡")

    value = search_section(raid_log, r'pmu (.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡上硬件供电模块故障")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "建议更换RAID卡")

    value = search_section(raid_log, r'Controller temperature threshold exceeded(.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "机箱内部散热问题，导致RAID卡温度过高")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "建议调整散热策略")
    
    value = search_section(raid_log, r'(\S+) (Info:SAS wide port (.*?) lost link on PHY) (.*?)\n', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                update_value(custom_info, "RAID故障", "链路不稳")
                update_value(custom_info, "RAID故障信息", value)
                update_value(custom_info, "RAID建议", "需要具体问题具体分析")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")

    value = search_section(raid_log, r'(\S+) (FATAL:Unable to access device PD) (\S+)\((\S+)\)\n', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                update_value(custom_info, "RAID故障", "硬盘故障，重新扫描后无法访问。")
                update_value(custom_info, "RAID故障信息", err)
                raid_disk = "/c{}".format(adapter) + get_raid_disk_slot(err[3])
                
                update_value(custom_info, "RAID中异常硬盘列表", raid_disk, ignore_exists=True, type="list")
                update_value(custom_info, "RAID建议", "更换硬盘")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")

    value = search_section(raid_log, r'(\S+) (WARNING:Predictive failure: PD )(.*?)\n', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                update_value(custom_info, "RAID故障", "硬盘预失效判断")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "更换硬盘")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")

    value = search_section(raid_log, r'(\S+) (Info:Unexpected sense: PD) ([0-9a-fA-Z]+)(\(\S+\)) (Path) ([0-9a-fA-Z]+), (CDB:) ([0-9a-fA-Z\s]+), (Sense: 70 00 04) ([0-9a-fA-Z\s]+)\n', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                update_value(custom_info, "RAID故障", "硬盘故障")
                update_value(custom_info, "RAID故障信息", err)
                raid_disk = "/c{}".format(adapter) + get_raid_disk_slot(err[3])
                update_value(custom_info, "RAID中异常硬盘列表", raid_disk, ignore_exists=True, type="list")
                update_value(custom_info, "RAID建议", "更换硬盘")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")

    value = search_section(raid_log, r'(\S+) (Info:Unexpected sense: PD) ([0-9a-fA-Z]+)(\(\S+\)) (Path) ([0-9a-fA-Z]+), (CDB: [0-9a-fA-Z\s]+), (Sense: [0-9a-fA-Z\s]+)\n', re.DOTALL, group=-1, multi=1)
    if value:
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0])):
                #解析Sensekey指令
                sense_info = decode_sensekey(err[7])
                if sense_info:
                    update_value(custom_info, "RAID异常指令", sense_info)
                
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")

    value = search_section(raid_log, r'Unexpected sense: ([2-6]/.*?), (CDB:) ([0-9a-fA-Z\s]+), (Sense:) ([0-9a-fA-Z\s]+)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "硬盘故障")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "更换硬盘")

    value = search_section(raid_log, r'Single ECC error at (.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡内存错误")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "建议更换RAID卡。")

    value = search_section(raid_log, r'BLBU(.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡超级电容故障")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "建议更换RAID卡。")

    value = search_section(raid_log, r'ASSERT(.*?)\n', re.DOTALL, group=0)
    if value:
        update_value(custom_info, "RAID故障", "RAID卡内部错误")
        update_value(custom_info, "RAID故障信息", value)
        update_value(custom_info, "RAID建议", "建议更换RAID卡。")

    #判断是否存在无效指令问题
    check_fakecmd(raid_log, custom_info)

    #判断是否存在Trim问题
    check_trim(raid_log, custom_info)

def check_trim(raid_log, custom_info):
    if not raid_log:
        return False

    #检查是否有raid reset
    #02/07/25  1:24:19.039: C0:EVT#00165-02/07/25  1:24:19: 268=PD 01(e0x45/s0) Path 330c50fd2c92c009  reset (Type 03)
    values = search_section(raid_log, r'(\d{2}/\d{2}/\d{2}\s+\d{1,2}:\d{2}:\d{2}\.\d{3}): (C\d:EVT#\d+-\d{2}/\d{2}/\d{2}\s+\d{1,2}:\d{2}:\d{2}: \d+\=)(PD \S+)\((\S+)\) (Path \S+  reset) (\(Type \d+\))', re.DOTALL, group=-1, multi=1)    
    if values:
        for v in values:
            if (compareRAIDTimeLogTimeIndays(custom_info.get("RAID时间"), custom_info.get("故障时间"), v[0], time_format="%m/%d/%y %H:%M:%S.%f")):
                update_value(custom_info, "RAID异常日志", v, ignore_exists=False, type="list")
                update_value(custom_info, "RAID故障", "硬盘重置")
                update_value(custom_info, "RAID故障信息", v)
                raid_disk = get_raid_disk_slot(v[3])
                update_value(custom_info, "RAID中异常硬盘列表", raid_disk, ignore_exists=True, type="list")

        #检查是否存在42指令
        #02/07/25  1:24:19.299: C0:MptHostCmdDone cmdId=f5 lmid=546:7f8b6100 tid 01 devH 19 func f1 CDB 42 00 00 00 00 00 00 00 18 00 
        values_trim = search_section(raid_log, r'(\d{2}/\d{2}/\d{2}\s+\d{1,2}:\d{2}:\d{2}\.\d{3}): (C\d:MptHostCmdDone cmdId=\S+ lmid=\S+:)(\S+) (tid \S+) (devH \S+) (func \S+) (CDB 42 00 00 00 00 00 00 00 18 00)', re.DOTALL, group=-1, multi=1)
        if values_trim:
            for v2 in values_trim:
                if (compareRAIDTimeLogTimeIndays(custom_info.get("RAID时间"), custom_info.get("故障时间"), v2[0], time_format="%m/%d/%y %H:%M:%S.%f")):
                    update_value(custom_info, "RAID异常日志", v2, ignore_exists=False, type="list")

                #检查是不是1秒内reset前后发生trim
                for v1 in values:
                    if (diff_logtime(v1[0], v2[0], time_format="%m/%d/%y %H:%M:%S.%f") <= 1):
                        #检查是不是三星的SSD
                        raid_disk = get_raid_device_attributes(custom_info.get("RAID卡下磁盘详细信息"), get_raid_disk_slot(v1[3]))
                        if raid_disk and search_section(raid_disk, "SAMSUNG", re.DOTALL, group=0):
                            update_value(custom_info, "RAID故障", "硬盘触发三星Trim问题")
                            update_value(custom_info, "RAID建议", "联系VS技术支持关闭Trim调用")

#对比两个时间字符串的时间差
def diff_logtime(time1, time2, time_format="%Y-%m-%d %H:%M:%S.%f", return_abs=True):
    if not time1 or not time2:
        return 0
    try:
        #去掉毫秒
        #time1 = time1.split('.')[0]
        #time2 = time2.split('.')[0]
        date_time1 = datetime.datetime.strptime(time1, time_format)
        date_time2 = datetime.datetime.strptime(time2, time_format)

        if return_abs:
            return abs((date_time1-date_time2).total_seconds())
        else:
            return (date_time1-date_time2).total_seconds()

    except Exception as e:
        traceback.print_exc()
        return 0

#统计无效指令频次
def statistics_cmd(records, tag = "", time_format="%Y-%m-%d,%H:%M:%S"):
    if not isinstance(records, list):
        return None
    
    times = [r[0] for r in records]
    if len(times) == 0:
        return None
    
    #将时间转为datetime,方便于做时间对比
    log_datetime = None
    #尝试转换检查是否为时间
    try:
        log_datetime = datetime.datetime.strptime(times[0], time_format)
    except Exception as e:
        try:
            log_datetime = datetime.datetime.strptime(times[0], "%y-%m-%d,%H:%M:%S")
            time_format = "%y-%m-%d,%H:%M:%S"
        except Exception as e:
            try:
                log_datetime = datetime.datetime.strptime(times[0], "%y/%m/%d,%H:%M:%S")
                time_format = "%y/%m/%d,%H:%M:%S"
            except Exception as e:
                try:
                    log_datetime = datetime.datetime.strptime(times[0], "%y/%m/%d %H:%M:%S")
                    time_format = "%y/%m/%d %H:%M:%S"
                except Exception as e:
                    try:
                        log_datetime = datetime.datetime.strptime(times[0], "%y-%m-%d %H:%M:%S")
                        time_format = "%y-%m-%d %H:%M:%S"
                    except Exception as e:
                        return None

    obj = pd.to_datetime(times, format=time_format)
    # 创建DataFrame
    df = pd.DataFrame({'DateTime': obj})
    df["DiffTime"] = df["DateTime"].diff()
    df["Diff_Seconds"] = df["DiffTime"].dt.total_seconds()
    
    #统计时间差分布情况
    statistics = {}
    statistics["TAG"] = tag
    # 描述性统计
    descriptive_stats = df["Diff_Seconds"].describe()
    statistics["统计摘要"] = descriptive_stats.rename(g_statistic_translation_dict).to_json(force_ascii=False)

    bins = [0, 5, 10, 15, 20, 25, 30, 35, 40, 50, 60, 80, 100, 120, 100000000]
    labels = ["<5秒", "5-10秒", "10-15秒", "15-20秒", "20-25秒", "25-30秒", "30-35秒", "35-40秒", "40-50秒", "50-60秒", "60-80秒", "80-100秒", "100-120秒", ">120秒"]
    df['Range'] = pd.cut(df['Diff_Seconds'], bins=bins, labels=labels, right=False)
    percentage = df['Range'].value_counts(normalize=True) * 100
    #print(percentage)

    statistics["时间间隔百分比统计"] = {"总次数": df.shape[0]}
    for i in range(0, len(labels)):
        statistics["时间间隔百分比统计"][labels[i]] = "{:.3f}%".format(percentage[labels[i]])

    #print(statistics)
    return statistics

#判断是否存在无效指令问题
def check_fakecmd(raid_log, custom_info):
    if custom_info.get("HCI版本"):
        if custom_info.get("HCI版本").find("6.10.0") != -1:
            #6.10.0之后不检查无效指令
            return False
        if custom_info.get("HCI版本").find("EDS5") != -1:
            #EDS5之后不检查无效指令
            #EDS5.0.1.314R1、EDS5.0.1.21R2、EDS5.0.2、EDS5.0.3、EDS5.2.0、EDS5.2.1、EDS3.0.9.136 R3
            if custom_info.get("HCI版本").find("R") != -1:
                return False
            
        if custom_info.get("HCI版本").find("EDS5.0.2") != -1:
            #不检查无效指令
            return False
        if custom_info.get("HCI版本").find("EDS5.0.3") != -1:
            #不检查无效指令
            return False
            
        if custom_info.get("HCI版本").find("EDS5.2") != -1:
            return False
            
        if custom_info.get("HCI版本").find("EDS3.0.9.136 R3") != -1:
            return False
        
        
    value = search_section(raid_log, r"(\d+/\d+/\d+\s+\d+:\d+:\d+)(?:\.\d+)?: (C\d):(MptHostCmdDone\(cmdId=\S+, status=(2d|2e) extStatus=2\))", re.DOTALL, group=-1, multi=1)
    if value:
        records = []
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0], time_format="%m/%d/%y %H:%M:%S") and not IgnoreCheckingRAIDTime(custom_info, err[0])):
                update_value(custom_info, "RAID故障", "存在无效指令")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "打最新集合补丁或升级版本")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
                records.append(err)
        if len(records):
            statis = statistics_cmd(records, tag="无效指令", time_format="%m/%d/%y %H:%M:%S")
            update_value(custom_info, "无效指令统计情况", statis)

    value = search_section(raid_log, r"(\d+/\d+/\d+ \d+:\d+:\d+)(?:\.\d+)?: (C\d):(\S+: Unknown DCDB Opcode \S+)", re.DOTALL, group=-1, multi=1)
    if value:
        records = []
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0], time_format="%m/%d/%y %H:%M:%S") and not IgnoreCheckingRAIDTime(custom_info, err[0])):
                update_value(custom_info, "RAID故障", "存在未知DCDB指令")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "打最新集合补丁或升级版本")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
                records.append(err)
        if len(records):
            statis = statistics_cmd(records, tag="未知DCDB指令", time_format="%m/%d/%y %H:%M:%S")
            update_value(custom_info, "无效指令统计情况", statis)

    value = search_section(raid_log, r"(\S+) (Info:Unexpected sense: PD) ([0-9a-fA-Z]+)(\(\S+\)) (Path) (\S+), (CDB: 4d 00 40 00 00 00 00 00 04 00|CDB: 4d 00 40 00 00 00 00 00 44 00|CDB: b7 0c 00 00 00 00 00 00 00 08 00 00|CDB: 37 00 0c 00 00 00 00 00 04 00|CDB: 55 10 00 00 00 00 00 00 24 00|CDB: 1a 00 19 00 40 00|CDB: 1a e 1c ee 4e ee)(.*?), Sense:", re.DOTALL, group=-1, multi=1)
    if value:
        records = []
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0], time_format="%m/%d/%y %H:%M:%S") and not IgnoreCheckingRAIDTime(custom_info, err[0])):
                update_value(custom_info, "RAID故障", "存在异常smartctl指令")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "打最新集合补丁或升级版本")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
                records.append(err)
        if len(records):
            statis = statistics_cmd(records, tag="异常smartctl指令", time_format="%m/%d/%y %H:%M:%S")
            update_value(custom_info, "无效指令统计情况", statis)

    value = search_section(raid_log, r"(\d+/\d+/\d+\s+\d+:\d+:\d+)(?:\.\d+)?: (C\d):(MptHostCmdDone cmdId=\S+ lmid=\S+:\S+ tid \S+ devH \S+ func \S+) (CDB 4d 00 40 00 00 00 00 00 04 00|CDB 4d 00 40 00 00 00 00 00 44 00|CDB b7 0c 00 00 00 00 00 00 00 08 00 00|CDB 37 00 0c 00 00 00 00 00 04 00|CDB 55 10 00 00 00 00 00 00 24 00|CDB 1a 00 19 00 40 00|CDB 1a e 1c ee 4e ee)(.*?)\n", 
                           re.DOTALL, group=-1, multi=1)
    if value:
        records = []
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0], time_format="%m/%d/%y %H:%M:%S") and not IgnoreCheckingRAIDTime(custom_info, err[0])):
                update_value(custom_info, "RAID故障", "存在smartctl指令")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "打最新集合补丁或升级版本")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
                records.append(err)
        if len(records):
            statis = statistics_cmd(records, tag="smartctl指令", time_format="%m/%d/%y %H:%M:%S")
            update_value(custom_info, "无效指令统计情况", statis)

    #sdparm
    value = search_section(raid_log, r"(\S+) (Info:Unexpected sense: PD) ([0-9a-fA-Z]+)(\(\S+\)) (Path) (\S+), (CDB: 5a 00 c8 00 00 00 00 00 24 00|CDB: 55 10 00 00 00 00 00 00 24 00)(.*?), Sense:", re.DOTALL, group=-1, multi=1)
    if value:
        records = []
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0], time_format="%m/%d/%y %H:%M:%S") and not IgnoreCheckingRAIDTime(custom_info, err[0])):
                update_value(custom_info, "RAID故障", "存在异常sdparm指令")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "打最新集合补丁或升级版本")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
                records.append(err)
        if len(records):
            statis = statistics_cmd(records, tag="异常sdparm指令", time_format="%m/%d/%y %H:%M:%S")
            update_value(custom_info, "无效指令统计情况", statis)

    value = search_section(raid_log, r"(\d+/\d+/\d+\s+\d+:\d+:\d+)(?:\.\d+)?: (C\d):(MptHostCmdDone cmdId=\S+ lmid=\S+:\S+ tid \S+ devH \S+ func \S+) (CDB 5a 00 c8 00 00 00 00 00 24 00|CDB 55 10 00 00 00 00 00 00 24 00)(.*?)\n", 
                           re.DOTALL, group=-1, multi=1)
    if value:
        records = []
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0], time_format="%m/%d/%y %H:%M:%S") and not IgnoreCheckingRAIDTime(custom_info, err[0])):
                update_value(custom_info, "RAID故障", "存在sdparm指令")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "打最新集合补丁或升级版本")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
                records.append(err)
        if len(records):
            statis = statistics_cmd(records, tag="sdparm指令", time_format="%m/%d/%y %H:%M:%S")
            update_value(custom_info, "无效指令统计情况", statis)


    #hdparm
    value = search_section(raid_log, r"(\S+) (Info:Unexpected sense: PD) ([0-9a-fA-Z]+)(\(\S+\)) (Path) (\S+), (CDB: 85 08 0e 00 00 00 01 00 00 00 00 00 00 40 ec 00|CDB: 85 08 0e 00 00 00 01 00 00 00 00 00 00 40 a1 00)(.*?), Sense:", re.DOTALL, group=-1, multi=1)
    if value:
        records = []
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0], time_format="%m/%d/%y %H:%M:%S") and not IgnoreCheckingRAIDTime(custom_info, err[0])):
                update_value(custom_info, "RAID故障", "存在异常hdparm指令")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "打最新集合补丁或升级版本")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
                records.append(err)
        if len(records):
            statis = statistics_cmd(records, tag="异常hdparm指令", time_format="%m/%d/%y %H:%M:%S")
            update_value(custom_info, "无效指令统计情况", statis)

    value = search_section(raid_log, r"(\d+/\d+/\d+\s+\d+:\d+:\d+)(?:\.\d+)?: (C\d):(MptHostCmdDone cmdId=\S+ lmid=\S+:\S+ tid \S+ devH \S+ func \S+) (CDB 85 08 0e 00 00 00 01 00 00 00 00 00 00 40 ec 00|CDB 85 08 0e 00 00 00 01 00 00 00 00 00 00 40 a1 00)(.*?)\n", 
                           re.DOTALL, group=-1, multi=1)
    if value:
        records = []
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0], time_format="%m/%d/%y %H:%M:%S") and not IgnoreCheckingRAIDTime(custom_info, err[0])):
                update_value(custom_info, "RAID故障", "存在hdparm指令")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "打最新集合补丁或升级版本")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
                records.append(err)
        if len(records):
            statis = statistics_cmd(records, tag="hdparm指令", time_format="%m/%d/%y %H:%M:%S")
            update_value(custom_info, "无效指令统计情况", statis)

    #sgdefects
    value = search_section(raid_log, r"(\S+) (Info:Unexpected sense: PD) ([0-9a-fA-Z]+)(\(\S+\)) (Path) (\S+), (CDB: 37 00 0d 00 00 00 00 00 50 00)(.*?), Sense:", re.DOTALL, group=-1, multi=1)
    if value:
        records = []
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0], time_format="%m/%d/%y %H:%M:%S") and not IgnoreCheckingRAIDTime(custom_info, err[0])):
                update_value(custom_info, "RAID故障", "存在异常sgdefects指令")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "打最新集合补丁或升级版本")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
                records.append(err)
        if len(records):
            statis = statistics_cmd(records, tag="异常sgdefects指令", time_format="%m/%d/%y %H:%M:%S")
            update_value(custom_info, "无效指令统计情况", statis)

    value = search_section(raid_log, r"(\d+/\d+/\d+\s+\d+:\d+:\d+)(?:\.\d+)?: (C\d):(MptHostCmdDone cmdId=\S+ lmid=\S+:\S+ tid \S+ devH \S+ func \S+) (CDB 37 00 0d 00 00 00 00 00 50 00)(.*?)\n", 
                           re.DOTALL, group=-1, multi=1)
    if value:
        records = []
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0], time_format="%m/%d/%y %H:%M:%S") and not IgnoreCheckingRAIDTime(custom_info, err[0])):
                update_value(custom_info, "RAID故障", "存在sgdefects指令")
                update_value(custom_info, "RAID故障信息", err)
                update_value(custom_info, "RAID建议", "打最新集合补丁或升级版本")
                update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
                records.append(err)
        if len(records):
            statis = statistics_cmd(records, tag="sgdefects指令", time_format="%m/%d/%y %H:%M:%S")
            update_value(custom_info, "无效指令统计情况", statis)

    #希捷硬盘问题
    value = search_section(raid_log, r"(\d+/\d+/\d+\s+\d+:\d+:\d+)(?:\.\d+)?: (C\d):(MptHostCmdDone cmdId=\S+ lmid=\S+:\S+ tid \S+ devH \S+ func \S+) (CDB 12 01 c0 00 78 00)(.*?)\n", 
                           re.DOTALL, group=-1, multi=1)
    if value:
        # 检查是否存在C0:MptDirectProcessIocError: Unexpected IOC completion iocStatus: lmid=62d iocStatus=44 
        error_values = search_section(raid_log, r"(\d+/\d+/\d+\s+\d+:\d+:\d+)(?:\.\d+)?: (C\d):(MptDirectProcessIocError: Unexpected IOC completion iocStatus: lmid=\S+ iocStatus=\d+)", 
                           re.DOTALL, group=-1, multi=1)
        records = []
        for err in value:
            if (compareRAIDTimeLogTimeIndays(custom_info["RAID时间"], custom_info.get("故障时间"), err[0], time_format="%m/%d/%y %H:%M:%S") and not IgnoreCheckingRAIDTime(custom_info, err[0])):
                # 检查同一时间是否存在MptDirectProcessIocError
                for error_value in error_values:
                    if err[0] == error_value[0] and not IgnoreCheckingRAIDTime(custom_info, err[0]):
                        update_value(custom_info, "RAID故障", "存在硬盘sgdfects导致无效指令")
                        update_value(custom_info, "RAID故障信息", err)
                        update_value(custom_info, "RAID故障信息", error_value)
                        update_value(custom_info, "RAID建议", "打最新集合补丁或升级版本")
                        update_value(custom_info, "RAID异常日志", err, ignore_exists=False, type="list")
                        records.append(err)
                        break
        if len(records):
            statis = statistics_cmd(records, tag="sgdfects指令", time_format="%m/%d/%y %H:%M:%S")
            update_value(custom_info, "无效指令统计情况", statis)

def test_time_format(log_time, time_format):
    log_datetime = None
    #尝试转换检查是否为时间
    try:
        log_datetime = datetime.datetime.strptime(log_time, time_format)
    except Exception as e:
        try:
            log_datetime = datetime.datetime.strptime(log_time, "%y-%m-%d,%H:%M:%S")
            time_format = "%y-%m-%d,%H:%M:%S"
        except Exception as e:
            try:
                log_datetime = datetime.datetime.strptime(log_time, "%y/%m/%d,%H:%M:%S")
                time_format = "%y/%m/%d,%H:%M:%S"
            except Exception as e:
                try:
                    log_datetime = datetime.datetime.strptime(log_time, "%m/%d/%y %H:%M:%S")
                    time_format = "%m/%d/%y %H:%M:%S"
                except Exception as e:
                    try:
                        log_datetime = datetime.datetime.strptime(log_time, "%m-%d-%y %H:%M:%S")
                        time_format = "%m-%d-%y %H:%M:%S"
                    except Exception as e:
                        try:
                            log_datetime = datetime.datetime.strptime(log_time, "%y-%m-%d %H:%M:%S")
                            time_format = "%y-%m-%d %H:%M:%S"
                        except Exception as e:
                            try:
                                log_datetime = datetime.datetime.strptime(log_time, "%y/%m/%d %H:%M:%S")
                                time_format = "%y/%m/%d %H:%M:%S"
                            except Exception as e:
                                try:
                                    log_datetime = datetime.datetime.strptime(log_time, "%a %b %d %H:%M:%S %Y")
                                    time_format = "%a %b %d %H:%M:%S %Y"
                                except Exception as e:
                                    return None
    return time_format

# 是否忽略执行smartctl时产生的raid日志
def IgnoreCheckingRAIDTime(custom_info, log_time, time_format = "%Y-%m-%d,%H:%M:%S", seconds=5):
    if not custom_info:
        return False
    if not custom_info.get("Smartctl执行时间"):
        return False
    
    if not log_time:
        return False

    time_format = test_time_format(log_time, time_format)
    if not time_format:
        return False
    
    # 将完整时间字符串转换为 datetime 对象
    given_time = datetime.datetime.strptime(log_time, time_format)

    # 计算raid卡controller时间和系统时间差
    # 对比RAID控制时间
    control_time_format = "%m/%d/%Y %H:%M:%S"
    control_datetime = datetime.datetime.strptime(custom_info["RAID时间"], control_time_format)
    sys_datetime = datetime.datetime.strptime(custom_info["RAID系统时间"], control_time_format)
    sys_con_timedelta = sys_datetime - control_datetime
    
    for smart_time in custom_info.get("Smartctl执行时间"):
        smart_datetime = None
        try:
            smart_datetime = datetime.datetime.strptime(smart_time, "%Y-%m-%d %H:%M:%S.%f")
        except Exception as e:
            try:
                smart_datetime = datetime.datetime.strptime(smart_time, "%Y-%m-%d %H:%M:%S")
            except Exception as e:
                continue

        # 计算时间差
        time_delta = abs((smart_datetime - given_time - sys_con_timedelta).total_seconds())
        if time_delta <= seconds:
            return True

    return False

#比较RAID日志时间和RAID控制器时间是否在一天内
def compareRAIDTimeLogTimeIndays(control_time, error_time, log_time, time_format = "%Y-%m-%d,%H:%M:%S", days=1):
    if not control_time or not error_time or not log_time:
        return False
    
    time_format = test_time_format(log_time, time_format)
    if not time_format:
        return False
    
    '''
    # 获取当前时间
    current_time = datetime.datetime.now()
    cur_year = int(str(current_time.year)[-2:])
    log_year = int(log_time[0:2])
    if log_year <= cur_year:
        full_year = "20" + log_time[0:2]
    else:
        full_year = "19" + log_time[0:2]
    given_time_str_full = full_year + log_time[2:]
    '''

    # 将完整时间字符串转换为 datetime 对象
    given_time = datetime.datetime.strptime(log_time, time_format)

    # 对比RAID控制时间
    control_time_format = "%m/%d/%Y %H:%M:%S"
    control_datetime = datetime.datetime.strptime(control_time, control_time_format)

    # 计算时间差
    time_delta = abs((control_datetime - given_time).total_seconds())

    # 故障时间
    error_datetime = None
    try:
        error_time_format = "%Y%m%d"
        error_datetime = datetime.datetime.strptime(error_time, error_time_format)
    except Exception as e:
        try:
            error_datetime = datetime.datetime.strptime(error_time, control_datetime)
        except Exception as e:
            return False

    error_time_delta = abs((error_datetime - given_time).total_seconds())

    return time_delta <= days * 24 *60 * 60 or error_time_delta <= days * 24 * 60 * 60 

#解析phy error count信息
@Handle_Exceptions
def parse_raid_phy_err(text):
    if not text:
        return None
    
    # 将文本分割成行
    lines = text.strip().split('\n')

    if len(lines) < 4:
        return None

    # 忽略第一行和最后一行的标题和分隔符
    headers = lines[1].split()
    data = lines[3:-1]

    # 解析数据
    parsed_data = []
    for line in data:
        values = line.split()
        parsed_data.append({
            'Phy_No': int(values[0]),
            'Invld_Dwrd_Cnt': int(values[1]),
            'Dis_Prt_Err_Cnt': int(values[2]),
            'Ls_Dwrd_Syn_Cnt': int(values[3]),
            'Phy_Rst_Prbl_Cnt': int(values[4])
        })

    return parsed_data

#解析链路误码
def check_raid_phy_err(custom_info, values):
    if not values:
        return None
    index = 0
    for v in values:
        phy_errs = parse_raid_phy_err(v[3])
        for phy in phy_errs:
            if phy["Invld_Dwrd_Cnt"] > 0 or phy["Dis_Prt_Err_Cnt"] > 0 or phy["Ls_Dwrd_Syn_Cnt"] or phy["Phy_Rst_Prbl_Cnt"]:
                update_value(custom_info, "RAID故障", "存在RAID链路错误")
                update_value(custom_info, "RAID故障信息", { "Controller": index, "错误统计": phy })
        index += 1

#解析nvme日志
def parse_nvme_log_info(nvmeinfo):
    if not nvmeinfo:
        return None
    lines = nvmeinfo.split("\n")
    if len(lines) < 1:
        return None
    
    device = search_section(lines[0], r"nvme smart-log /dev/(\S+)", re.DOTALL)
    nvme = {
        "dev": device
    }
    for v in lines[1:]:
        parts = v.split(":")
        nvme[parts[0].strip()] = parts[1].strip()

    return nvme

def get_raid_systime_diff(custom_info, format="hour"):
    if not custom_info:
        return None
    if not custom_info.get("RAID时间") or not custom_info.get("RAID系统时间"):
        return None
    
    # 计算raid卡controller时间和系统时间差
    # 对比RAID控制时间
    control_time_format = "%m/%d/%Y %H:%M:%S"
    control_datetime = datetime.datetime.strptime(custom_info["RAID时间"], control_time_format)
    sys_datetime = datetime.datetime.strptime(custom_info["RAID系统时间"], control_time_format)
    sys_con_timedelta = sys_datetime - control_datetime
    if format == "hour":
        return sys_con_timedelta.total_seconds() / 3600
    elif format == "minute":
        return sys_con_timedelta.total_seconds() / 60
    else:
        return sys_con_timedelta.total_seconds()

#解析日志内容
def parse_raidlog_info(custom_info, log_info):
    #版本信息
    custom_info["HCI版本"] = search_section(log_info, r'HCI Version:\n(.*?)\n=================================================', re.DOTALL)
    if custom_info["HCI版本"]:
        custom_info["HCI版本"] = custom_info["HCI版本"].replace('\n', ' ')
    else:
        custom_info["HCI版本"] = ""

    #内核版本
    custom_info["内核版本"] = search_section(log_info, r"uname:\n(.*?)\n=================================================", re.DOTALL)

    #运行时长
    custom_info["运行时长"] = search_section(log_info, r"Uptime:\n(.*?)\n=================================================", re.DOTALL)

    #脚本版本
    custom_info["脚本版本"] = search_section(log_info, r"脚本版本: (.*?)\n", re.DOTALL, group=1)

    value = search_section(log_info, r'VMP: YES', re.DOTALL, group=0)
    if value:
        custom_info["产品线"] = "VDS"
    
    value = search_section(log_info, r'EDS: YES', re.DOTALL, group=0)
    if value:
        custom_info["产品线"] = "EDS"

    #默认为HCI
    if not custom_info.get("产品线"):
        custom_info["产品线"] = "HCI"
        #HCI才判断是否升级后未重启
        if custom_info.get("HCI版本"):
            version = custom_info.get("HCI版本").split()[0].split('_')[0]
            if custom_info.get("内核版本"):
                kernel_version = custom_info.get("内核版本")
                v = kernel_version.split("#")[0].split()[-1]
                if v.find('-') != -1:
                    if not version in v:
                        update_value(custom_info, "问题初步结论", "升级后未重启主机。", type="list", insert_pos=0)

    custom_info["HCI版本"] = custom_info.get("产品线") + custom_info.get("HCI版本")

    custom_info["是否影响客户业务"] = "否"   #默认不影响
    custom_info["分类"] = ""
    custom_info["卡慢盘分类"] = ""
    custom_info["问题等级"] = ""
    custom_info["硬盘延时ms"] = "NA"
    custom_info["硬盘在线时长"] = np.nan

    value = search_section(log_info, r'Cluster Members:\n(.*?)\n=================================================', re.DOTALL)
    if value:
        try:
            members = json.loads(value)
            custom_info["集群信息"] = members
            if members.get("nodename"):
                custom_info["主机ID"] = members.get("nodename")
                if members.get("nodelist", {}).get(members.get("nodename"), {}).get("ip"):
                    custom_info["主机IP"] = members.get("nodelist", {}).get(members.get("nodename"), {}).get("ip")

        except Exception as e:
            pass

    #补丁列表
    custom_info["补丁列表"] = search_section(log_info, r'SP List:\n(.*?)\n=================================================', re.DOTALL)

    #KEYINFO
    custom_info["序列号信息"] = search_section(log_info, r'KEYINFO:\n(.*?)\n=================================================', re.DOTALL)
    if custom_info["序列号信息"]:
        custom_info["序列号信息"] = json.loads(custom_info["序列号信息"])

        custom_info["KEYID"] = custom_info["序列号信息"]["asv"]["keyid"] if custom_info["序列号信息"].get("asv") else custom_info["序列号信息"].get("keyid")
        custom_info["最终用户"] = custom_info["序列号信息"]["asv"]["user_name"] if custom_info["序列号信息"].get("asv") else custom_info["序列号信息"].get("user_name")
        if not custom_info.get("用户名称"):
            custom_info["用户名称"] = custom_info["最终用户"]
    #故障时间
    failed_date = search_section(log_info, r'FAILED_DATE: (\S+)\n=================================================', re.DOTALL)
    if failed_date:
        if len(failed_date.strip()) <= 6:
            failed_date = "20" + failed_date
        custom_info["故障时间"] = failed_date

    #磁盘列表
    custom_info["磁盘列表"] = parse_disk_list(search_section(log_info, r'Disk info:\n(.*?)\n=================================================', re.DOTALL))

    #读取SMARTCTL执行时间
    custom_info["Smartctl执行时间"] = search_section(log_info, r"SMARTCTL EXECUTE TIME:\n(.*?)\n=================================================", re.DOTALL, group=-1, multi=1)

    custom_info["磁盘Smart信息列表"] = {}
    #每个磁盘详细信息
    for disk in custom_info["磁盘列表"]:
        #磁盘smart信息
        if disk:
            custom_info["磁盘Smart信息列表"][disk["NAME"]] = get_disk_smartctl_info(log_info, disk["NAME"])

    #RAID卡列表信息
    custom_info["RAID卡列表信息"] = search_section(log_info, r'RAID info:\n(.*?)\n=================================================', re.DOTALL)

    #RAID具体信息
    custom_info["RAID驱动"] = search_section(log_info, r'Kernel driver in use: (\S+)', re.DOTALL)

    if custom_info["RAID驱动"] == "megaraid_sas" or custom_info["RAID驱动"] == "pcieport":
        #RAID卡下磁盘整体信息：
        custom_info["RAID卡下磁盘整体信息"] = search_section(log_info, r'RAID卡下磁盘整体信息：\n(.*?)\n=================================================', re.DOTALL)

        custom_info["RAID型号"] = search_section(custom_info["RAID卡下磁盘整体信息"], r'(?s)Product Name = (.*?)\n', re.DOTALL)

        custom_info["RAID序列号"] = search_section(custom_info["RAID卡下磁盘整体信息"], r'Serial Number = (.*?)\n', re.DOTALL)

        custom_info["RAID时间"] = search_section(custom_info["RAID卡下磁盘整体信息"], r'Controller Time = (.*?)\n', re.DOTALL)
        
        custom_info["RAID系统时间"] = search_section(custom_info["RAID卡下磁盘整体信息"], r'System Time = (.*?)\n', re.DOTALL)

        custom_info["RAID时间和系统时间差"] = get_raid_systime_diff(custom_info)

        if not failed_date:
            custom_info["故障时间"] = custom_info["RAID系统时间"]

        custom_info["RAID固件包版本"] = search_section(custom_info["RAID卡下磁盘整体信息"], r'FW Package Build = (.*?)\n', re.DOTALL)

        custom_info["RAID固件版本"] = search_section(custom_info["RAID卡下磁盘整体信息"], r'FW Version = (.*?)\n', re.DOTALL)

        custom_info["RAID驱动版本"] = search_section(custom_info["RAID卡下磁盘整体信息"], r'Driver Version = (.*?)\n', re.DOTALL)

        #RAID卡下虚拟磁盘信息：
        custom_info["RAID卡下虚拟磁盘信息"] = search_section(log_info, r'RAID卡下虚拟磁盘信息：\n(.*?)\n=================================================', re.DOTALL, group=1, multi=1)
        
        #RAID卡下磁盘详细信息：
        custom_info["RAID卡下磁盘详细信息"] = search_section(log_info, r'RAID卡下磁盘详细信息：\n(.*?)\n=================================================', re.DOTALL, group=1, multi=1)

        #RAID卡alilog：
        custom_info["RAID卡alilog"] = search_section(log_info, r'RAID卡alilog：\n(.*?)\n=================================================', re.DOTALL)

        if custom_info.get("RAID卡alilog") and custom_info["RAID卡alilog"].strip() == "":
            custom_info["存在RAID日志"] = "否"
        else:
            custom_info["存在RAID日志"] = "是"

        #检查是否有RAID故障错误
        #多张raid卡需要区分
        raid_log = custom_info["RAID卡alilog"]
        values = search_section(raid_log, r'(SAS RAID Management Tool Command Line Utilities)(.*?)(Adapter #)(\d)\n(.*?)CLI Version =', re.DOTALL, group=-1, multi=1)
        if values:
            for v in values:
                check_raid_log(v[4], custom_info, adapter=v[3])
        else:
            check_raid_log(raid_log, custom_info)

        #匹配raid卡中是否存在介质错误
        check_raid_state(custom_info["RAID卡下磁盘详细信息"], custom_info)

        #RAID卡误码信息：
        custom_info["RAID卡误码信息"] = search_section(log_info, r'RAID卡误码信息：\n(.*?)\n=================================================', re.DOTALL)

        value = search_section(custom_info["RAID卡误码信息"], r'(PhyInfo :\n\={5,10}\n\n)(.*?)(Phy Error Counters :\n\={15,20}\n\n)(.*?)Invld_Dwrd_Cnt=', mode=re.DOTALL, group=-1, multi=1)

        custom_info["链路误码"] = value
        if value:
            check_raid_phy_err(custom_info, value)

    elif custom_info["RAID驱动"] == "mpt3sas":
        #当前获取不到raid日志
        custom_info["存在RAID日志"] = "否"
        pass
        #RAID卡下磁盘整体信息：
        custom_info["RAID卡下磁盘整体信息"] = search_section(log_info, r'RAID卡下磁盘整体信息：\n(.*?)\n=================================================', re.DOTALL, group=1, multi=1)

        #RAID卡下虚拟磁盘信息：
        custom_info["RAID卡下磁盘详细信息"] = search_section(log_info, r'RAID卡下虚拟磁盘信息：\n(.*?)\n=================================================', re.DOTALL, group=1, multi=1)

    elif custom_info["RAID驱动"] == "smartpqi":
        #暂时不获取
        custom_info["存在RAID日志"] = "否"

    else:
        custom_info["存在RAID日志"] = "否"
        pass

    #读取NVME log信息
    values = search_section(log_info, r"NVME DISK LOG:\n(.*?)\n=================================================",re.DOTALL, group=-1, multi=1)
    if values:
        custom_info["NVME日志"] = {}
        for v in values:
            nvmeinfo = parse_nvme_log_info(v)
            custom_info["NVME日志"][nvmeinfo["dev"]] = nvmeinfo

    #根据raid故障的磁盘信息查询对应的磁盘信息
    if custom_info.get("RAID中异常硬盘列表"):
        err_raid_disk = custom_info["RAID中异常硬盘列表"]
        err_raid_disk = list(set(err_raid_disk))
        for raid_disk in err_raid_disk:
            #以/eX/sN保存的数组
            
            if not custom_info.get("RAID卡下磁盘详细信息"):
                continue

            stateinfo = get_raid_device_state(custom_info["RAID卡下磁盘详细信息"], raid_disk)
            if stateinfo:
                custom_info["介质错误"] = stateinfo
                
            #获取对应的SAS地址信息
            sas = get_raid_sas_address(custom_info["RAID卡下磁盘详细信息"], raid_disk)
            if sas:
                if custom_info.get("SAS地址"):
                    custom_info["SAS地址"] = [custom_info["SAS地址"]]
                    custom_info["SAS地址"].append({ raid_disk: sas })
                else:
                    custom_info["SAS地址"] = { raid_disk: sas }
                
            #获取对应的物理磁盘盘符
            device = get_raid_device_attributes(custom_info["RAID卡下磁盘详细信息"], raid_disk)
            if device:
                disksn = search_section(device, r'SN =(.*?)\n')
                #根据sn获取盘符
                disk = get_disk_name_by_sn(custom_info, disksn)
                if disk:
                    if not custom_info.get("内核日志"):
                        custom_info["内核日志"] = {}
                    
                    update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")
                    
    #主机信息
    custom_info["主机信息"] = search_section(log_info, r'# dmidecode (.*?)\n========================================', re.DOTALL)
    custom_info["服务器厂商"] = search_section(custom_info["主机信息"], r'Manufacturer: (.*?)\n', re.DOTALL)
    if not custom_info["服务器厂商"]:
        custom_info["服务器厂商"] = ""
    custom_info["服务器SN"] = search_section(custom_info["主机信息"], r'Serial Number: (.*?)\n', re.DOTALL)
    custom_info["服务器产品名称"] = search_section(custom_info["主机信息"], r'Product Name: (.*?)\n', re.DOTALL)
    custom_info["服务器型号"] = search_section(custom_info["主机信息"], r'Family: (\S+)', re.DOTALL)

    return custom_info

#根据smartctl信息获取硬盘类型
def get_disk_type(smartctl):
    if not smartctl:
        return None

    disk_type = smartctl.get("Rotation Rate")
    if disk_type:
        if disk_type.find("Solid State Device") != -1:
            disk_type = "SSD"
        else:
            disk_type = "HDD"
            
    if smartctl.get("Total NVM Capacity"):
        #NVME一定是SSD
        disk_type = "SSD"

    return disk_type


#获取硬盘接口类型
def get_disk_protocol(smartctl):
    if not smartctl:
        return None

    proto_type = smartctl.get("Transport protocol")
    if proto_type:
        if proto_type.find("SAS") != -1:
            proto_type = "SAS"
    if smartctl.get("SATA Version is"):
        if smartctl.get("SATA Version is").find("SATA") != -1:
            proto_type = "SATA"
            
    if smartctl.get("Total NVM Capacity"):
        proto_type = "NVME"

    #默认SATA
    if not proto_type:
        proto_type = "SATA"

    return proto_type

#根据disk name获取smartctl
def get_disk_smartctl_by_name(custom_info , disk):
    #先从当前的磁盘列表查找
    if custom_info.get("磁盘Smart信息列表"):
        smartctl = custom_info["磁盘Smart信息列表"]
        for key, value in smartctl.items():
            if not key:
                continue
            if key == disk:
                return value
    
    #从历史日志查找
    if custom_info.get("HIS_INFO"):
        #遍历历史日期
        dates = [ date for date, value in custom_info["HIS_INFO"].items()]
        #倒序排序,从最新日期开始找
        dates.sort(reverse=True)
        for date in dates:
            if custom_info["HIS_INFO"][date].get("smartctl"):
                #从最后开始查找
                smartctl_list = custom_info["HIS_INFO"][date]["smartctl"]
                for d, v in smartctl_list.items():
                    if disk == d:
                        return v
    
    return None

#根据sn获取盘符
#@Handle_Exceptions
def get_disk_name_by_sn(custom_info, disksn):
    smartctl = custom_info["磁盘Smart信息列表"]
    for key, value in smartctl.items():
        if not value:
            continue
        if value["Serial Number"] == disksn:
            return key
    #如果当前找不到，尝试从历史找
    if custom_info.get("HIS_INFO"):
        #遍历历史日期
        dates = [ date for date, value in custom_info["HIS_INFO"].items()]
        #倒序排序,从最新日期开始找
        dates.sort(reverse=True)
        for date in dates:
            if custom_info["HIS_INFO"][date].get("smartctl"):
                #从最后开始查找
                smartctl_list = custom_info["HIS_INFO"][date]["smartctl"]
                for disk, smartctl in smartctl_list.items():
                    if smartctl.get("Serial Number") == disksn:
                        return disk
            #仅找最新一天
            break
    return None

#更新字段
def update_custom_info_section(custom_info):
    if not custom_info:
        return
    if not custom_info.get("RAID中硬盘是否出现重置"):
        if custom_info.get("存在RAID日志"):
            custom_info["RAID中硬盘是否出现重置"] = "否"
        else:
            custom_info["RAID中硬盘是否出现重置"] = "无RAID日志"
    
    if not custom_info.get("RAID卡重置"):
        if custom_info.get("存在RAID日志"):
            custom_info["RAID卡重置"] = "否"
        else:
            custom_info["RAID卡重置"] = "无RAID日志"
    
    if not custom_info.get("RAID中硬盘是否出现超时"):
        if custom_info.get("存在RAID日志"):
            custom_info["RAID中硬盘是否出现超时"] = "否"
        else:
            custom_info["RAID中硬盘是否出现超时"] = "无RAID日志"

    if not custom_info.get("是否出现Media Error"):
        if custom_info.get("内核日志"):
            custom_info["是否出现Media Error"] = "否"
        else:
            custom_info["是否出现Media Error"] = "无内核日志"
    
    if not custom_info.get("是否单盘卡慢引起多盘"):
        if custom_info.get("内核日志"):
            custom_info["是否单盘卡慢引起多盘"] = "否"
        else:
            custom_info["是否单盘卡慢引起多盘"] = "无内核日志"

    if not custom_info.get("是否出现踢盘时间过长或者踢盘失败影响业务"):
        if custom_info.get("内核日志"):
            custom_info["是否出现踢盘时间过长或者踢盘失败影响业务"] = "否"
        else:
            custom_info["是否出现踢盘时间过长或者踢盘失败影响业务"] = "无内核日志"

    return None

#@Handle_Exceptions
def analyze_custom_info(custom_info_list):
    analyze_custom_info = custom_info_list
    extent_custom = []

    for custom_info in analyze_custom_info:
        if not custom_info.get("服务器厂商"):
            custom_info["服务器厂商"] = ""

        if not custom_info.get("HCI版本"):
            custom_info["HCI版本"] = np.nan

        if not custom_info.get("硬盘在线时长"):
            custom_info["硬盘在线时长"] = np.nan

        if not custom_info.get("硬盘上电时间(年)"):
            custom_info["硬盘上电时间(年)"] = np.nan

        if not custom_info.get("客户编号"):
            custom_info["客户编号"] = ""

        if custom_info.get("时延统计"):
            stats = custom_info["时延统计"].get("分盘统计")
            if stats:
                #通过VS异常时延统计判断是否存在单盘影响多盘
                count = 0
                for item, v in stats["max"].items():
                    #大于500ms认为卡盘,老版本
                    if v >= 500:
                        count += 1
                #if count > 1:
                #    custom_info["是否单盘卡慢引起多盘"] = "是"

        #更新初始结论
        if "内核日志" in custom_info:
            if custom_info.get("内核日志").get("RAID重置硬盘") == "是":
                if custom_info.get("RAID中硬盘是否出现超时") == "是":
                    update_value(custom_info, "问题初步结论", "RAID超时重置硬盘。", type="list", insert_pos=0)
                    custom_info["是否出现硬盘重置"] = "是"
                else:
                    custom_info["是否出现硬盘重置"] = "否"
                    update_value(custom_info, "问题初步结论", "RAID重置硬盘。", type="list", insert_pos=0)

            if custom_info.get("内核日志").get("磁盘被重置") == "是":
                update_value(custom_info, "问题初步结论", "硬盘故障重置({})。".format(custom_info.get("内核日志").get("硬盘故障重置信息")))
                custom_info["是否出现硬盘重置"] = "是"
            else:
                custom_info["是否出现硬盘重置"] = "否"

            if custom_info.get("内核日志").get("磁盘IO卡死") == "是":
                update_value(custom_info, "问题初步结论", "磁盘IO卡死({})。".format(custom_info.get("内核日志").get("磁盘IO卡死信息")))
             
            if custom_info.get("内核日志").get("磁盘IO错误") == "是":
                update_value(custom_info, "问题初步结论", "磁盘IO错误({})。".format(custom_info.get("内核日志").get("磁盘IO错误信息")))

            if custom_info.get("内核日志").get("RAID卡重置") == "是":
                custom_info["RAID卡重置"] = "是"
                update_value(custom_info, "问题初步结论", "RAID卡重置({})。".format(custom_info.get("内核日志").get("RAID卡重置信息")))

            if custom_info.get("内核日志").get("硬盘故障") == "是":
                update_value(custom_info, "问题初步结论", "硬盘存在异常({}),请更换硬盘。如果只有Not Ready错误，请插拔硬盘观察，如果插拔后无法识别或仍然出现故障，请更换硬盘。".format(custom_info.get("内核日志").get("硬盘故障信息")), type="list", insert_pos=0)

            if custom_info.get("内核日志").get("单盘影响多盘"):
                custom_info["是否单盘卡慢引起多盘"] = "是" if custom_info.get("内核日志").get("单盘影响多盘") == "是" else "否"
                update_value(custom_info, "问题初步结论", "单盘影响多盘:{}。".format(custom_info.get("内核日志").get("单盘影响多盘")), type="list", insert_pos=0)
            
            risk_disk = check_all_disk_risk(custom_info)
            if risk_disk and len(risk_disk) > 0:
                for disk in risk_disk:
                    update_value(custom_info["内核日志"], "故障磁盘列表", disk, ignore_exists=True, type="list")

            #更新kenel日志中的故障磁盘信息
            failed_disks = custom_info["内核日志"].get("故障磁盘列表")
            if isinstance(failed_disks, str):
                failed_disks = [failed_disks]

            if failed_disks: 
                hotplus_list = custom_info["内核日志"].get("重复尝试插拔磁盘列表")
                if hotplus_list:
                    if isinstance(hotplus_list, list):
                        for i in range(1, len(hotplus_list)):
                            if hotplus_list[i] in failed_disks:
                                failed_disks.remove(hotplus_list[i])
                    else:
                        if hotplus_list in failed_disks:
                            failed_disks.remove(hotplus_list)

                #过滤查找不到的盘
                fake_disk = []
                for d in failed_disks:
                    smartctl = get_disk_smartctl_by_name(custom_info, d)
                    if not smartctl:
                        #找不到的盘
                        fake_disk.append(d)
                
                for d in fake_disk:
                    failed_disks.remove(d)

                if len(failed_disks) > 1:
                    #超过1个以上的磁盘，需要复制当前一行再更新
                    for i in range(1, len(failed_disks)):
                        new_custom = deepcopy(custom_info)
                        #清理旧磁盘信息
                        new_custom["故障时磁盘名称"] = failed_disks[i]
                        new_custom["问题初步结论"] = ""
                        new_custom["硬盘类型"] = ""
                        new_custom["硬盘延时ms"] = ""
                        new_custom["硬盘型号"] = ""
                        new_custom["硬盘序列号"] = ""
                        new_custom["硬盘固件"] = ""
                        new_custom["硬盘Smart_5"] = ""
                        new_custom["硬盘Smart_197"] = ""
                        new_custom["寿命"] = ""
                        new_custom["硬盘在线时长"] = ""
                        new_custom["硬盘上电时间(年)"] = ""
                        new_custom["硬盘已上电时间"] = ""
                        new_custom["时延统计"] = ""
                        new_custom["硬盘容量"] = ""
                        new_custom["磁盘属性检测"] = ""
                        new_custom["介质错误"] = ""
                        new_custom["SAS地址"] = ""
                        new_custom["链路误码"] = ""
                        new_custom["内核日志"] = ""
                        new_custom["VS是否踢盘"] = ""
                        new_custom["Brick上下线日志"] = ""
                        new_custom["VS最终处置"] = ""
                        new_custom["磁盘预警属性列表"] = ""
                        new_custom["磁盘Smartctl错误"] = ""
                        new_custom["RAID中硬盘是否出现重置"] = ""
                        new_custom["RAID中硬盘是否出现超时"] = ""
                        new_custom["是否出现Media Error"] = ""
                        new_custom["虚拟机IO影响"] = ""
                        new_custom["虚拟机业务影响时间"] = ""
                        disk = failed_disks[i]

                        health_check = check_disk_realtime_health(new_custom, disk)
                        if health_check:
                            #更新磁盘实时健康检查
                            update_value(new_custom, "问题初步结论", health_check, type="list", insert_pos=0)

                        if update_custom_info_disk_info(new_custom, disk, riskcheck=True):
                            #更新RAID槽位
                            if new_custom.get("RAID卡下磁盘详细信息"):
                                if isinstance(new_custom["RAID卡下磁盘详细信息"], list):
                                    for raid_info in new_custom["RAID卡下磁盘详细信息"]:
                                        location = get_raid_location_by_sn(raid_info, new_custom.get("硬盘序列号"))
                                        if location:
                                            new_custom["RAID控制器"] = parse_controller_index(location)
                                            new_custom["RAID槽位"] = parse_slot_number(location)
                                            break
                                else:
                                    location = get_raid_location_by_sn(new_custom["RAID卡下磁盘详细信息"], new_custom.get("硬盘序列号"))
                                    if location:
                                        new_custom["RAID控制器"] = parse_controller_index(location)
                                        new_custom["RAID槽位"] = parse_slot_number(location)
                            extent_custom.append(new_custom)

                disk = None
                if len(failed_disks) > 0:
                    disk = failed_disks[0]
                custom_info["故障时磁盘名称"] = disk
                
                update_custom_info_disk_info(custom_info, disk)

                #更新RAID槽位
                if custom_info.get("RAID卡下磁盘详细信息"):
                    if isinstance(custom_info["RAID卡下磁盘详细信息"], list):
                        for raid_info in custom_info["RAID卡下磁盘详细信息"]:
                            location = get_raid_location_by_sn(raid_info, custom_info.get("硬盘序列号"))
                            if location:
                                custom_info["RAID控制器"] = parse_controller_index(location)
                                custom_info["RAID槽位"] = parse_slot_number(location)
                    else:
                        location = get_raid_location_by_sn(custom_info["RAID卡下磁盘详细信息"], custom_info.get("硬盘序列号"))
                        if location:
                            custom_info["RAID控制器"] = parse_controller_index(location)
                            custom_info["RAID槽位"] = parse_slot_number(location)

        if custom_info.get("RAID故障"):
            if not custom_info.get("问题初步结论"):
                update_value(custom_info, "问题初步结论", "{}({}, 建议:{})。".format(custom_info.get("RAID故障"), custom_info.get("RAID故障信息"), custom_info.get("RAID建议")), type="list", insert_pos=0)
            else:
                raid_error = "{}({}, 建议:{})。".format(custom_info.get("RAID故障"), custom_info.get("RAID故障信息"), custom_info.get("RAID建议"), type="list", insert_pos=0)
                update_value(custom_info, "问题初步结论", "{}\n{}".format(custom_info["问题初步结论"], raid_error))

        if custom_info.get("介质错误"):
            stateinfo = custom_info["介质错误"]
            if stateinfo["Media Error Count"] > 0 or stateinfo["Predictive Failure Count"] > 0:
                update_value(custom_info, "问题初步结论", "硬盘存在介质错误{}，请按照文档【https://doc.weixin.qq.com/doc/w3_AUUAVgYhAAk10rY01QCSU2vdrlS09?scode=AI4AagdpABEpnnZVSRALkAfwYUAFU】处理。".format(stateinfo), type="list", insert_pos=0)

        if custom_info.get("磁盘预警属性列表"):
            update_value(custom_info, "问题初步结论", "磁盘存在预警属性:{}，建议联系卡慢盘服务号确认。".format(custom_info.get("磁盘预警属性列表")), type="list", insert_pos=0)

        if custom_info.get("磁盘Smartctl错误"):
            update_value(custom_info, "问题初步结论", "磁盘存在错误:{}，建议联系卡慢盘服务号确认。".format(custom_info.get("磁盘Smartctl错误")), type="list", insert_pos=0)

        raidfw_check = check_raid_fw(custom_info.get("RAID型号"), custom_info.get("RAID固件版本"))
        if raidfw_check:
            update_value(custom_info, "问题初步结论", "当前RAID卡[{}]固件版本[{}]存在问题，请升级到[{}]以上版本。预警号:[{}]，URL:[{}]".format(custom_info.get("RAID型号"), custom_info.get("RAID固件版本"), raidfw_check.get("正常固件"), raidfw_check.get("ALERT"), raidfw_check.get("URL")), type="list", insert_pos=0)

        if custom_info.get("RAID硬盘状态异常"):
            if isinstance(custom_info.get("RAID硬盘状态异常"), list):
                sn_list = []
                for disk in custom_info.get("RAID硬盘状态异常"):
                    disk_attrs = get_raid_device_attributes(custom_info["RAID卡下磁盘详细信息"], disk)
                    if disk_attrs:
                        disksn = search_section(disk_attrs, r'SN =(.*?)\n')
                        sn_list.append(disksn)
                    else:
                        sn_list.append(disk)
                update_value(custom_info, "问题初步结论", "RAID硬盘状态异常，建议更换此硬盘:{}".format(sn_list), type="list", insert_pos=0)
            else:
                disk_attrs = get_raid_device_attributes(custom_info["RAID卡下磁盘详细信息"], custom_info.get("RAID硬盘状态异常"))
                sn = custom_info.get("RAID硬盘状态异常")
                if disk_attrs:
                    sn = search_section(disk_attrs, r'SN =(.*?)\n')
                update_value(custom_info, "问题初步结论", "RAID硬盘状态异常，建议更换此硬盘:{}".format(sn), type="list", insert_pos=0)

        check_vs_abnormal_diskstat(custom_info)

        if not custom_info.get("问题初步结论"):
            update_value(custom_info, "问题初步结论", "未知原因，建议联系卡慢盘服务号确认。")
        
        update_custom_info_section(custom_info)
        #故障回溯
        update_failure_backtrace(custom_info)

        #输出iostat
        #if isinstance(custom_info.get("IOSTAT"), pd.DataFrame):
        #    custom_info.get("IOSTAT").to_excel("iostat.xlsx", sheet_name="iostat")

        #更新用户名称和icare单号
        if g_args and g_args.icare:
            custom_info["问题单号"] = g_args.icare

        if g_args and g_args.custom:
            custom_info["用户名称"] = g_args.custom

    if len(extent_custom) > 0:
        analyze_custom_info.extend(extent_custom)

    aios_data = format_smartctl_hisinfo(custom_info)
    aios_check = aiops_check_disk(aios_data)
    logger.info("aiops checking: {}".format(aios_data))
    logger.info("aiops check result:{}".format(aios_check))
    custom_info["AIOPS检测结果"] = aios_check
    aiops_check_prediction(custom_info)

    for custom_info in analyze_custom_info:
        #更新返修建议
        update_repair_sugguestion(custom_info)

    return analyze_custom_info 

#格式化日志时间
def format_error_log_time(values, source="", time_format="%Y-%m-%d %H:%M:%S"):
    if not values:
        return None
    if not isinstance(values, list):
        return None
    
    time_list = []
    for v in values:
        time = v[0]
        time_format = test_time_format(time, time_format)
        if not time_format:
            logger.error("时间格式不匹配")
            continue
        
        time = datetime.datetime.strptime(time, time_format)
        msg = " ".join(v[1:])
        time_list.append({"时间": time, "日志": msg, "来源": source})

    return time_list

#按照时间顺序回溯
#@Handle_Exceptions
def update_failure_backtrace(custom_info):
    time_df = pd.DataFrame()
    #检查vs brick上下线时间
    brick_log = custom_info.get("Brick上下线日志")
    if brick_log:
        time_list = format_error_log_time(brick_log, "VS diskchecker", "%Y-%m-%d %H:%M:%S,%f")
        df = pd.DataFrame(time_list, columns=["时间", "日志", "来源"])
        time_df = pd.concat([time_df, df], ignore_index=True, sort=True)

    #检查vs踢盘告警时间
    vs_kickalertoff = custom_info.get("VS踢盘告警日志")
    if vs_kickalertoff:
        time_list = format_error_log_time(vs_kickalertoff, "VS diskchecker","%Y-%m-%d %H:%M:%S,%f")
        df = pd.DataFrame(time_list, columns=["时间", "日志", "来源"])
        time_df = pd.concat([time_df, df], ignore_index=True, sort=True)

    #检查vs踢盘时间
    vs_kickoff = custom_info.get("VS踢盘日志")
    if vs_kickoff:
        time_list = format_error_log_time(vs_kickoff, "VS diskchecker","%Y-%m-%d %H:%M:%S,%f")
        df = pd.DataFrame(time_list, columns=["时间", "日志", "来源"])
        time_df = pd.concat([time_df, df], ignore_index=True, sort=True)

    #先检查vs时延异常时间
    abnormal_stat = custom_info.get("异常时延")
    if abnormal_stat:
        time_list = format_error_log_time(abnormal_stat, "VS abnormalstats","%Y-%m-%d %H:%M:%S,%f")
        df = pd.DataFrame(time_list, columns=["时间", "日志", "来源"])
        time_df = pd.concat([time_df, df], ignore_index=True, sort=True)

    #检查内核踢盘日志
    kernel_kickoff = custom_info.get("内核踢盘日志")
    if kernel_kickoff:
        time_list = format_error_log_time(kernel_kickoff, "内核", "%Y-%m-%d %H:%M:%S.%f")
        df = pd.DataFrame(time_list, columns=["时间", "日志", "来源"])
        time_df = pd.concat([time_df, df], ignore_index=True, sort=True)

    #检查内核异常日志
    kernel_log = custom_info.get("内核异常日志")
    if kernel_log:
        time_list = format_error_log_time(kernel_log, "内核", "%Y-%m-%d %H:%M:%S.%f")
        df = pd.DataFrame(time_list, columns=["时间", "日志", "来源"])
        time_df = pd.concat([time_df, df], ignore_index=True, sort=True)

    #检查raid故障日志时间
    raid_err = custom_info.get("RAID异常日志")
    if raid_err:
        time_list = format_error_log_time(raid_err, "RAID卡", "%m-%d-%y %H:%M:%S")
        df = pd.DataFrame(time_list, columns=["时间", "日志", "来源"])
        raid_time_diff = get_raid_systime_diff(custom_info, format="seconds")
        if raid_time_diff:
            delta = datetime.timedelta(seconds=raid_time_diff)
            df["时间"] = df["时间"].apply(lambda x: x + delta if pd.notna(x) else np.nan)
        time_df = pd.concat([time_df, df], ignore_index=True, sort=True)

    #检查虚拟机故障时间
    if custom_info.get("虚拟机IO影响"):
        for vm in custom_info.get("虚拟机IO影响"):
            vm_state_change = vm.get("IO状态切换详情")
            if vm_state_change:
                time_list = format_error_log_time(vm_state_change, "虚拟机", "%Y-%m-%d %H:%M:%S.%f")
                df = pd.DataFrame(time_list, columns=["时间", "日志", "来源"])
                time_df = pd.concat([time_df, df], ignore_index=True, sort=True)
            
            vm_io_timeout = vm.get("QEMU_IO超时详情")
            if vm_io_timeout:
                time_list = format_error_log_time(vm_state_change, "虚拟机", "%Y-%m-%d %H:%M:%S.%f")
                df = pd.DataFrame(time_list, columns=["时间", "日志", "来源"])
                time_df = pd.concat([time_df, df], ignore_index=True, sort=True)

    #时间段整理
    time_df = time_df.reindex(columns=["时间", "日志", "来源"])
    time_df = time_df.sort_values(by="时间", ignore_index=True)
    #print(time_df)

    if g_args and g_args.output:
        filename = g_args.output
        time_df.to_excel(filename, sheet_name="问题过程回溯")
        with pd.ExcelWriter(filename, mode='w') as writer:
            time_df.to_excel(writer, sheet_name="问题过程回溯")
    else:
        filename = "custom_analyze_info.xlsx"
        time_df.to_excel(filename, sheet_name="问题过程回溯")
        with pd.ExcelWriter(filename, mode='w') as writer:
            time_df.to_excel(writer, sheet_name="问题过程回溯")
    
    custom_info["故障回溯"] = json.dumps(time_df.to_json(date_format="%Y-%m-%d %H:%M:%S.%f", orient="records", force_ascii=False, indent=4), sort_keys=True, indent=4, separators=(',', ': '), ensure_ascii=False)
    return True

def aiops_check_disk(data):
    if not data:
        return None
    url = "http://192.168.8.212:8000/invocations"
    params = {"dataframe_split": pd.DataFrame(data).to_dict(orient="split")}

    # 发送POST请求
    try:
        response = requests.post(url, json=params, timeout=5)
        if response:
            # 打印响应内容
            logger.info("aiops check:")
            logger.info(response.status_code)
            ret = response.json()
            logger.info(ret)
            return ret
        else:
            logger.error("post {} : {} failed!".format(url, params))
    except Exception as e:
        logger.error(e)

    return None

def aiops_check_prediction(custom_info):
    if custom_info.get("AIOPS检测结果"):
        if custom_info["AIOPS检测结果"].get("predictions"):
            result = custom_info["AIOPS检测结果"].get("predictions")
            for dret in result:
                if dret.get("predict"):
                    if dret.get("predict") == 0:
                        update_value(custom_info, "返修建议", "AIOPS预测检查磁盘[{}]正常，无需返修。".format(dret.get("pn_sn")))
                    elif dret.get("predict") == 1:
                        update_value(custom_info, "返修建议", "AIOPS预测检查磁盘[{}]存在预警，建议返修。".format(dret.get("pn_sn")))
                    elif dret.get("predict") == 2:
                        update_value(custom_info, "返修建议", "该磁盘[{}]不支持AIOPS预测。".format(dret.get("pn_sn")))
                    else:
                        logger.error("磁盘[{}]检查返回未知状态".format(dret.get("pn_sn")))

#检查指定磁盘是否存在IO瓶颈
def check_disk_realtime_health(custom_info, disk):
    if not custom_info or not disk:
        return None
    
    if not custom_info.get("磁盘性能统计"):
        return None
    
    if not custom_info.get("磁盘性能统计").get(disk):
        return None

    statistic = custom_info.get("磁盘性能统计").get(disk)
    #判断IOPS是否合理，util均值必须大于95%
    if statistic.get("util").get("均值") >= 95:
        if statistic.get("Type") == "HDD":
            if statistic.get("Protocol") == "SATA":
                #SATA HDD的IOPS不能低于50
                if statistic.get("IOPS").get("均值") < 50:
                    return { "预警": "是", "IOPS均值": statistic.get("IOPS").get("均值"), "描述": "IOPS偏低" }
                
                #如果IOPS已经达到标准值，认为已经达到瓶颈
                if statistic.get("IOPS").get("均值") >= 120:
                    return { "预警": "是", "IOPS均值": statistic.get("IOPS").get("均值"), "描述": "IOPS已经达到SATA HDD磁盘瓶颈" }

            elif statistic.get("Protocol") == "SAS":
                #SAS HDD的IOPS不能低于80
                if statistic.get("IOPS").get("均值") < 80:
                    return { "预警": "是", "IOPS均值": statistic.get("IOPS").get("均值"), "描述": "IOPS偏低" }
                
                #如果IOPS已经达到标准值，认为已经达到瓶颈
                if statistic.get("IOPS").get("均值") >= 250:
                    return { "预警": "是", "IOPS均值": statistic.get("IOPS").get("均值"), "描述": "IOPS已经达到SAS HDD磁盘瓶颈" }

        elif statistic.get("Type") == "SSD":
            if statistic.get("Protocol") == "SATA":
                #SATA SSD的IOPS不能低于5004
                if statistic.get("IOPS").get("均值") < 500:
                    return { "预警": "是", "IOPS均值": statistic.get("IOPS").get("均值"), "描述": "IOPS偏低" }
                
                #如果IOPS已经达到标准值，认为已经达到瓶颈
                if statistic.get("IOPS").get("均值") >= 40000:
                    return { "预警": "是", "IOPS均值": statistic.get("IOPS").get("均值"), "描述": "IOPS已经达到SATA SSD磁盘瓶颈" }

            elif statistic.get("Protocol") == "SAS":
                #SAS SSD的IOPS不能低于500
                if statistic.get("IOPS").get("均值") < 500:
                    return { "预警": "是", "IOPS均值": statistic.get("IOPS").get("均值"), "描述": "IOPS偏低" }
                
                #如果IOPS已经达到标准值，认为已经达到瓶颈
                if statistic.get("IOPS").get("均值") >= 40000:
                    return { "预警": "是", "IOPS均值": statistic.get("IOPS").get("均值"), "描述": "IOPS已经达到SAS SSD磁盘瓶颈" }

            elif statistic.get("Protocol") == "NVME":
                #NVME SSD的IOPS不能低于5000
                if statistic.get("IOPS").get("均值") < 5000:
                    return { "预警": "是", "IOPS均值": statistic.get("IOPS").get("均值"), "描述": "IOPS偏低" }
                
                #如果IOPS已经达到标准值，认为已经达到瓶颈
                if statistic.get("IOPS").get("均值") >= 150000:
                    return { "预警": "是", "IOPS均值": statistic.get("IOPS").get("均值"), "描述": "IOPS已经达到NVME SSD磁盘瓶颈" }

    #判断时延是否合理
    if statistic.get("Type") == "SSD":
        #SSD时延均值不应该超过500ms
        if statistic.get("svctm").get("均值") >= 500:
            return { "预警": "是", "时延均值": statistic.get("svctm").get("均值"), "描述": "SSD平均时延过高，硬盘性能存在不足" }
        
    elif statistic.get("Type") == "HDD":
        #HDD时延均值不应该超过3秒
        if statistic.get("svctm").get("均值") >= 3000:
            return { "预警": "是", "时延均值": statistic.get("svctm").get("均值"), "描述": "HDD平均时延过高，硬盘性能存在不足" }

    #util低于50%，判断平均时延不能过高
    if statistic.get("util").get("均值") < 50:
        if statistic.get("r_await").get("均值") > 70:
            return { "预警": "是", "时延均值": statistic.get("r_await").get("均值"), "描述": "平均读时延过高，硬盘性能存在不足" }
        if statistic.get("w_await").get("均值") > 70:
            return { "预警": "是", "时延均值": statistic.get("w_await").get("均值"), "描述": "平均写时延过高，硬盘性能存在不足" }

    if not isinstance(custom_info.get("实时IOSTAT"), pd.DataFrame):
        return None
    
    df = custom_info.get("实时IOSTAT")
    if df.empty:
        return None

    disk_df = df.loc[df["Device:"] == disk]
    if disk_df.empty:
        return None
    
    # 首先，计算df中满足条件的行的布尔索引
    condition_df = (disk_df["%util"] < 50) & ((disk_df["r_await"] >= 70) | (disk_df["w_await"] >= 70))

    # 然后，使用这个布尔索引来从disk_df中选择行
    err_df = disk_df[condition_df]
    if not err_df.empty and err_df.shape[0] / disk_df.shape[0] > 1/3:
        return { "预警": "是", "描述": "[{}]出现1/3的读写时延过高，硬盘性能存在不足".format(disk) }
    
    
    return None

#统计当前磁盘的UTIL和IOPS
def statistic_realtime_iostat(custom_info):
    if not custom_info or not isinstance(custom_info.get("实时IOSTAT"), pd.DataFrame):
        return False
    
    df = custom_info.get("实时IOSTAT")
    if df.empty:
        return False
    
    custom_info["磁盘性能统计"] = {}
    #遍历所有磁盘
    for disk, group in df.groupby('Device:'):
        smartctl = get_disk_smartctl_by_name(custom_info, disk)
        utils = {
            #统计util
            "util": json.loads(group["%util"].describe().rename(g_statistic_translation_dict).to_json(force_ascii=False)),
            #统计IOPS
            "IOPS": json.loads(group["IOPS"].describe().rename(g_statistic_translation_dict).to_json(force_ascii=False)),
            "svctm": json.loads(group["svctm"].describe().rename(g_statistic_translation_dict).to_json(force_ascii=False)),
            "rkB/s": json.loads(group["rkB/s"].describe().rename(g_statistic_translation_dict).to_json(force_ascii=False)),
            "wkB/s": json.loads(group["wkB/s"].describe().rename(g_statistic_translation_dict).to_json(force_ascii=False)),
            "r_await": json.loads(group["r_await"].describe().rename(g_statistic_translation_dict).to_json(force_ascii=False)),
            "w_await": json.loads(group["w_await"].describe().rename(g_statistic_translation_dict).to_json(force_ascii=False)),
            "Type": get_disk_type(smartctl),
            "Protocol": get_disk_protocol(smartctl)
        }
        custom_info["磁盘性能统计"][disk] = utils

    if g_args and g_args.verbose:
        logger.info(custom_info["磁盘性能统计"])

    return True

#更新返修建议
#TODO
def update_repair_sugguestion(custom_info):
    err_disk = custom_info.get("故障时磁盘名称")
    #检查smartctl属性预警信息

    #Smartctl属性增加检查
    if custom_info.get("磁盘异常检查"):
        disk_sn = custom_info.get("硬盘序列号")
        if disk_sn:
            smartinfos = custom_info.get("磁盘异常检查").get(disk_sn)
            if smartinfos:
                #检查是否增加
                if smartinfos.get("5"):
                    df = pd.DataFrame(smartinfos.get("5"))
                    sub = df.diff()
                    #判断最近3天是否有增加
                    if sub.shape[0] > 4:
                        sum_of_last_three = sub.iloc[1:4].sum()
                        if not sum_of_last_three.empty and abs(sum_of_last_three[0]) > 5:
                            custom_info["返修建议"] = "该磁盘Smartctl属性5在3天内增加超过5个，建议返修。"

                    if sub.shape[0] > 1:
                        last_cnt = sub.iloc[1].sum()
                        if abs(last_cnt) > 10:
                            custom_info["返修建议"] = "该磁盘Smartctl属性5在T+1天内增加超过10个，建议返修。"
                    
    #检查Medium Error

    if custom_info.get("NVME日志"):
        for dev, v in custom_info.get("NVME日志").items():
            if not err_disk or err_disk != dev:
                continue

            if v.get("critical_warning"):
                if int(v.get("critical_warning")) > 50:
                    custom_info["返修建议"] = "该磁盘critical_warning值达到返修标准，建议返修。"
                    return
                
            if v.get("media_errors"):
                if int(v.get("media_errors")) > 50:
                    custom_info["返修建议"] = "该磁盘media_errors值达到返修标准，建议返修。"
                    return

    #检查异常时延
    if custom_info.get("时延统计"):
        stats = custom_info.get("时延统计").get("分盘统计") 
        if stats["count"].get(err_disk):
            err_cnt = stats["count"].get(err_disk)
            if err_cnt > 0:
                pass
                #TODO 待完善
            
    return

#单独检查所有磁盘是否有风险：smartctl、固件版本
@Handle_Exceptions
def check_all_disk_risk(custom_info):
    risk_disk = []

    if custom_info.get("NVME日志"):
        #大于0都认为有风险
        for dev, v in custom_info.get("NVME日志").items():
            if v.get("critical_warning"):
                if int(v.get("critical_warning")) > 0:
                    risk_disk.append(disk)
                
            if v.get("media_errors"):
                if int(v.get("media_errors")) > 0:
                    risk_disk.append(disk)

    if custom_info.get("磁盘Smart信息列表"):
        
        for disk, smartctl in custom_info["磁盘Smart信息列表"].items():
            if not smartctl:
                continue
            #检查固件风险
            errs = check_disk_fw(smartctl.get("Firmware Version"))
            if errs and not disk in risk_disk:
                risk_disk.append(disk)
            #检查属性风险

            #返修阈值检测
            alert_attrs = update_custom_info_disk_risk(None, smartctl)
            #如果达到返修阈值，加入风险磁盘
            if alert_attrs:
                for item, v in alert_attrs.items():
                    if item == "SAS" or item == "NVME":
                        if len(v.items()) > 0:
                            risk_disk.append(disk)
                            break

                    if not v.get("当前") or v.get("阈值") == '-':
                        continue
                    
                    if v.get("预警") == "是":
                        risk_disk.append(disk)
                        break

            #判断smartctl信息是否存在错误
            smartctl_error = smartctl.get("SMART Error Log")
            if smartctl_error and smartctl_error.find("No Errors Logged") == -1:
                #存在错误
                if  not disk in risk_disk:
                    risk_disk.append(disk)

        return risk_disk

    return []

#从历史smartctl信息中查找
def search_disk_his_smartctl(custom_info, disk, search_day=0):
    if not custom_info.get("HIS_INFO"):
        return None

    #遍历历史日期
    dates = [ date for date, value in custom_info["HIS_INFO"].items()]
    #倒序排序,从最新日期开始找
    dates.sort(reverse=True)
    day_cnt = 0
    smartctl = None
    for date in dates:
        day_cnt += 1
        if custom_info["HIS_INFO"][date].get("smartctl"):
            #从最后开始查找
            smartctl_list = custom_info["HIS_INFO"][date]["smartctl"]
            smartctl = smartctl_list.get(disk)
            if not smartctl and disk.find("nvme") != -1:
                smartctl = smartctl_list.get("{}n1".format(disk))
            if smartctl:
                break
        #只找最近几天的，避免时间过长找错了
        if search_day > 0 and search_day <= day_cnt:
            break

    return smartctl

#检查是否为VS使用的磁盘
def check_vs_use_disk(smartctl, custom_info):
    if not smartctl or not custom_info.get("vs_disk_list"):
        return False
    
    for diskid, value in custom_info.get("vs_disk_list").items():
        if smartctl.get("Serial Number") == value.get("disk_sn").strip():
            if g_args.verbose:
                logger.info("vs disk find: diskid:{}, disk_sn:{}, disk name:{}".format(diskid, value.get("disk_sn"), value.get("dev")))
            return True
    
    return False

def get_vs_disk_info(smartctl, custom_info):
    if not smartctl or not custom_info.get("vs_disk_list"):
        return None
    
    for diskid, value in custom_info.get("vs_disk_list").items():
        if smartctl.get("Serial Number") == value.get("disk_sn").strip():
            if g_args and g_args.verbose:
                logger.info("vs disk find: diskid:{}, disk_sn:{}, disk name:{}".format(diskid, value.get("disk_sn"), value.get("dev")))
            return value
    
    return None

def get_vs_disk_location(smartctl, custom_info):
    value = get_vs_disk_info(smartctl, custom_info)
    if value:
        return value.get("disk_location")
    
    return None

#更新指定客户的磁盘信息
#@Handle_Exceptions
def update_custom_info_disk_info(custom_info, disk, riskcheck=False):
    if not disk:
        return False

    #更新是否永久隔离
    if custom_info.get("VS永久隔离磁盘列表"):
        remove_disks = custom_info.get("VS永久隔离磁盘列表")
        if isinstance(remove_disks, list):
            for d in remove_disks:
                if d.get("dev") == disk:
                    custom_info["临时踢盘或永久踢盘"] = "永久踢盘"
                if get_disk_name_by_sn(custom_info, d.get("disk_sn")) == disk:
                    custom_info["临时踢盘或永久踢盘"] = "永久踢盘"
        else:
            if remove_disks.get("dev") == disk:
                custom_info["临时踢盘或永久踢盘"] = "永久踢盘"
            if get_disk_name_by_sn(custom_info, remove_disks.get("disk_sn")) == disk:
                custom_info["临时踢盘或永久踢盘"] = "永久踢盘"

    smartctl = None
    if custom_info.get("是否出现Media Error"):
        if disk in custom_info.get("是否出现Media Error"):
            custom_info["是否出现Media Error"] = "是"
        else:
            custom_info["是否出现Media Error"] = "否"

    #优先尝试从历史日志里查找
    if not riskcheck and custom_info.get("HIS_INFO"):
        #遍历历史日期
        smartctl = search_disk_his_smartctl(custom_info, disk, 1)

    #找不到时才用Raid日志里查找
    if not smartctl:
        if "磁盘Smart信息列表" in custom_info:
            smartctl = custom_info["磁盘Smart信息列表"].get(disk)
            if not smartctl and disk.find("nvme") != -1:
                smartctl = custom_info["磁盘Smart信息列表"].get("{}n1".format(disk))
    if not smartctl:
        #遍历历史日期
        smartctl = search_disk_his_smartctl(custom_info, disk, 3)

    if smartctl:
        custom_info["硬盘型号"] = smartctl.get("Device Model")
        custom_info["硬盘序列号"] = smartctl.get("Serial Number")
        custom_info["硬盘固件"] = smartctl.get("Firmware Version")
        custom_info["硬盘容量"] = smartctl.get("User Capacity")
        custom_info["硬盘类型"] = smartctl.get("Rotation Rate")
        custom_info["硬盘接口类型"] = smartctl.get("Transport protocol")
        if custom_info["硬盘接口类型"]:
            if custom_info["硬盘接口类型"].find("SAS") != -1:
                custom_info["硬盘接口类型"] = "SAS"
        if smartctl.get("SATA Version is"):
            if smartctl.get("SATA Version is").find("SATA") != -1:
                custom_info["硬盘接口类型"] = "SATA"

        if custom_info["硬盘类型"]:
            if custom_info["硬盘类型"].find("Solid State Device") != -1:
                custom_info["硬盘类型"] = "SSD"
            else:
                custom_info["硬盘类型"] = "HDD"
            
        if smartctl.get("Total NVM Capacity"):
            custom_info["硬盘接口类型"] = "NVME"
            #NVME一定是SSD
            custom_info["硬盘类型"] = "SSD"

        if not custom_info.get("硬盘接口类型"):
            custom_info["硬盘接口类型"] = "SATA"

        custom_info["硬盘类型"] = "{0} {1}".format(custom_info["硬盘接口类型"], custom_info["硬盘类型"])

        errs = check_disk_fw(custom_info["硬盘固件"])
        if errs:
            update_value(custom_info, "问题初步结论", "磁盘[{}]固件版本[{}]过低，请升级固件到[{}]，预警号[{}]，链接[{}]。".format(custom_info["硬盘序列号"], custom_info["硬盘固件"], errs.get("正常固件"), errs.get("ALERT"), errs.get("URL")), type="list", insert_pos=0)
        #解析Smartctl信息
        smartctl_attr = smartctl.get("SMART Attributes List")
        
        #坏道数量风险
        custom_info["硬盘Smart_5"] = get_smartctl_attr(smartctl_attr, "5")
        custom_info["硬盘Smart_197"] = get_smartctl_attr(smartctl_attr, "197")
        #在线时长
        custom_info["硬盘在线时长"] = get_smartctl_attr(smartctl_attr, "9")

        if custom_info["硬盘Smart_5"] and custom_info["硬盘在线时长"]:
            if int(custom_info["硬盘Smart_5"]) > 0  and int(custom_info["硬盘在线时长"]) > 50000:
                #使用超过5万小时并且出现坏道，做风险提示建议更换
                update_value(custom_info, "问题初步结论", "磁盘[{}]使用时间超过5万小时，并且已经出现坏道，存在较大风险，建议安排更换。".format(disk), type="list", insert_pos=0)

        alert_attr = []
        #返修阈值检测
        alert_attrs = update_custom_info_disk_risk(custom_info, smartctl)
        #如果达到返修阈值，加入风险磁盘
        if alert_attrs:
            for item, v in alert_attrs.items():
                if item == "SAS" or item == "NVME":
                    if len(v.items()) > 0:
                        alert_attr.append({item: v})
                        break

                if not v.get("当前") or v.get("阈值") == '-':
                    continue

                if v.get("预警") == "是":
                    alert_attr.append({item: v})
                    break

        if custom_info.get("NVME日志"):
            #大于0都认为有风险
            for dev, v in custom_info.get("NVME日志").items():
                if custom_info.get("故障时磁盘名称") and custom_info.get("故障时磁盘名称") == dev:
                    if v.get("critical_warning"):
                        if int(v.get("critical_warning")) > 0:
                            alert_attr.append({"critical_warning": v.get("critical_warning")})
                        
                    if v.get("media_errors"):
                        if int(v.get("media_errors")) > 0:
                            alert_attr.append({"media_errors": v.get("media_errors")})

        if len(alert_attr):
            custom_info["磁盘预警属性列表"] = alert_attr

        #判断smartctl信息是否存在错误
        smartctl_error = smartctl.get("SMART Error Log")
        if smartctl_error and smartctl_error.find("No Errors Logged") == -1:
            #存在错误
            update_value(custom_info, "磁盘Smartctl错误", smartctl_error)
            update_value(custom_info, "问题初步结论", "磁盘[{}]存在Smartctl错误，请联系厂商检查。".format(disk), type="list", insert_pos=0)

        #再次从当前收集的smartctl信息中检查有没有错误
        if "磁盘Smart信息列表" in custom_info:
            smartctl = custom_info["磁盘Smart信息列表"].get(disk)
            if not smartctl and disk.find("nvme") != -1:
                smartctl = custom_info["磁盘Smart信息列表"].get("{}n1".format(disk))
            if smartctl:
                #判断smartctl信息是否存在错误
                smartctl_error = smartctl.get("SMART Error Log")
                if smartctl_error and smartctl_error.find("No Errors Logged") == -1:
                    #存在错误
                    update_value(custom_info, "磁盘Smartctl错误", smartctl_error)

        #读取raid中的状态信息
        raid_state = get_raid_device_state_by_sn(custom_info.get("RAID卡下磁盘详细信息"), custom_info["硬盘序列号"])
        if raid_state:
            custom_info["介质错误"] = raid_state

        if custom_info["硬盘型号"] == "VIRTUAL-DISK":
            return False

        location = get_vs_disk_location(smartctl, custom_info)
        if location:
            custom_info["VS盘位"] = location
    else:
        custom_info["硬盘在线时长"] = np.nan
        update_value(custom_info, "备注", "找不到磁盘{}，可能已经被踢盘，建议联系卡慢盘服务号确认。".format(disk))
        return False
    return True

#判断磁盘返修风险
@Handle_Exceptions
def update_custom_info_disk_risk(custom_info, smartctl):
    smartctl_attr = {}
    if not smartctl:
        return smartctl_attr

    nvme_attrs = smartctl.get("NVME SMART Attributes Values")
    if nvme_attrs:
        #如果是nvme盘
        smartctl_attr["NVME"] = {}
        if nvme_attrs.get("Critical Warning"):
            if int(nvme_attrs.get("Critical Warning"), base=16) >= 50:
                smartctl_attr["NVME"]["Critical Warning"] = {"Status": nvme_attrs.get("Critical Warning"), "预警": "是"}
        if nvme_attrs.get("Media and Data Integrity Errors"):
            if int(nvme_attrs.get("Media and Data Integrity Errors")) >= 50: 
                smartctl_attr["NVME"]["Media and Data Integrity Errors"] = {"Status": nvme_attrs.get("Media and Data Integrity Errors"), "预警":"是"}

    sas_attrs = smartctl.get("SAS SMART Attributes Values")
    if sas_attrs:
        #如果是sas盘
        smartctl_attr["SAS"] = {}
        if sas_attrs.get("SMART Health Status") == "Failed":
            smartctl_attr["SAS"]["SMART Health Status"] = {"Status": "Failed", "预警": "是"}
        
        if sas_attrs.get("Elements in grown defect list"):
            if int(sas_attrs.get("Elements in grown defect list")) >= 50:
                smartctl_attr["SAS"]["Elements in grown defect list"] = {"Status": sas_attrs.get("Elements in grown defect list"), "预警": "是"}

        #if sas_attrs.get("Non-medium error count"):
            #if int(sas_attrs.get("Non-medium error count")) > 0:
            #    smartctl_attr["SAS"]["Non-medium error count"] = {"Status": sas_attrs.get("Non-medium error count"), "预警": "是"}

        '''
        Error counter log:
                Errors Corrected by           Total   Correction     Gigabytes    Total
                    ECC          rereads/    errors   algorithm      processed    uncorrected
                fast | delayed   rewrites  corrected  invocations   [10^9 bytes]  errors
        read:          0        0         0         0          0         66.632           0
        write:         0        0         0         0          0      18902.744           0
        verify:        0        0         0         0          0         74.906           0
        '''
        if sas_attrs.get("read"):
            uce_read = int(sas_attrs.get("read").split()[-1])
            if uce_read > 0:
                if not smartctl_attr["SAS"].get("Error counter log"):
                    smartctl_attr["SAS"]["Error counter log"] = {}
                smartctl_attr["SAS"]["Error counter log"]["read_Total_uncorrected_errors"] = {"Status": uce_read, "预警": "是"}

        if sas_attrs.get("write"):
            uce_write = int(sas_attrs.get("write").split()[-1])
            if uce_write > 0:
                if not smartctl_attr["SAS"].get("Error counter log"):
                    smartctl_attr["SAS"]["Error counter log"] = {}
                smartctl_attr["SAS"]["Error counter log"]["write_Total_uncorrected_errors"] = {"Status": uce_write, "预警": "是"}

        if sas_attrs.get("verify"):
            uce_verify = int(sas_attrs.get("verify").split()[-1])
            if uce_verify > 0:
                if not smartctl_attr["SAS"].get("Error counter log"):
                    smartctl_attr["SAS"]["Error counter log"] = {}
                smartctl_attr["SAS"]["Error counter log"]["verify_Total_uncorrected_errors"] = {"Status": uce_verify, "预警": "是"}
                


    smartctl_attrs = smartctl.get("SMART Attributes List")
    if not smartctl_attrs:
        return smartctl_attr
    
    smartctl_attr["1"] = {"当前" : get_smartctl_attr(smartctl_attrs, "1", "WORST"), "阈值": 10, "判定": "小于", "不适用型号": "^ST", "适用类型": "rpm"}
    smartctl_attr["5"] = {"当前" : get_smartctl_attr(smartctl_attrs, "5"), "阈值": 50, "判定": "大于"}
    smartctl_attr["7"] = {"当前" : get_smartctl_attr(smartctl_attrs, "7"), "阈值": 10, "判定": "大于", "不适用型号": "^ST", "适用类型": "rpm"}
    smartctl_attr["22"] = {"当前" : get_smartctl_attr(smartctl_attrs, "22"), "阈值": 20, "判定": "小于", "适用型号": "(^WD|^HGST)", "适用类型": "rpm"}
    smartctl_attr["200"] = {"当前" : get_smartctl_attr(smartctl_attrs, "200"), "阈值": 20, "判定": "小于", "适用型号": "^ST", "适用类型": "rpm"}
    smartctl_attr["167"] = {"当前" : get_smartctl_attr(smartctl_attrs, "167"), "阈值": 2700, "适用型号": "^FORESEE", "判定": "大于", "适用类型": "Solid State Device"}
    smartctl_attr["173"] = {"当前" : get_smartctl_attr(smartctl_attrs, "173"), "阈值": 100, "适用型号": "^TOSHIBA", "判定": "小于", "适用类型": "Solid State Device"}
    smartctl_attr["177"] = {"当前" : get_smartctl_attr(smartctl_attrs, "177", "WORST"), "阈值": 10, "判定": "小于","适用类型": "Solid State Device"}
    smartctl_attr["187"] = {"当前" : get_smartctl_attr(smartctl_attrs, "187"), "阈值": 10, "适用型号": "^ST", "判定": "大于", "适用类型": "rpm"}
    smartctl_attr["196"] = {"当前" : get_smartctl_attr(smartctl_attrs, "196"), "阈值": "-", "适用型号": "^SAMSUNG", "适用类型": "Solid State Device"}
    smartctl_attr["197"] = {"当前" : get_smartctl_attr(smartctl_attrs, "197"), "阈值": 50, "判定": "大于"}
    smartctl_attr["198"] = {"当前" : get_smartctl_attr(smartctl_attrs, "198"), "阈值": 50, "判定": "大于", "适用类型": "rpm"}
    smartctl_attr["199"] = {"当前" : get_smartctl_attr(smartctl_attrs, "199"), "阈值": "-"}
    smartctl_attr["202"] = {"当前" : get_smartctl_attr(smartctl_attrs, "202", "WORST"), "阈值": 10, "判定": "小于", "适用型号": "Micron"}
    smartctl_attr["233"] = {"当前" : get_smartctl_attr(smartctl_attrs, "233", "WORST"), "阈值": 15, "适用类型": "Solid State Device"}

    for attr in smartctl_attrs:
        if attr["TYPE"] == "Pre-fail":
            if attr["THRESH"] == "---":
                continue

            try:
                if int(attr["WORST"]) <= int(attr["THRESH"]):
                    if not smartctl_attr.get(attr["ID"]):
                        smartctl_attr[attr["ID"]] = {}
                    smartctl_attr[attr["ID"]]["预警"] = "是"
            except Exception as e:
                traceback.print_exc()

    #遍历所有属性
    if smartctl_attr:
        for key, attr in smartctl_attr.items():
            #返修阈值检测
            if not attr.get("当前") or attr.get("阈值") == '-':
                continue
            try:
                #如果不适用直接跳过
                if attr.get("不适用型号") and smartctl:
                    if smartctl.get("Device Model"):
                        if search_section(smartctl.get("Device Model"), attr.get("不适用型号"), re.DOTALL, group=0):
                            continue

                #判断是否适用
                if attr.get("适用型号") and smartctl:
                    #如果存在适用型号字段，无法判断就跳过
                    if not smartctl.get("Device Model"):
                        continue

                    if search_section(smartctl.get("Device Model"), attr.get("适用型号"), re.DOTALL, group=0):
                        if attr.get("判定") == "大于":
                            if int(attr.get("当前")) >= attr.get("阈值"):
                                attr["预警"] = "是"
                        elif attr.get("当前") == "小于":
                            if int(attr.get("当前")) <= attr.get("阈值"):
                                attr["预警"] = "是"
                    else:
                        continue

                if attr.get("适用类型") and smartctl:
                    if not smartctl.get("Rotation Rate"):
                        continue

                    if search_section(smartctl.get("Rotation Rate"), attr.get("适用类型"), re.DOTALL, group=0):
                        if attr.get("判定") == "大于":
                            if int(attr.get("当前")) >= attr.get("阈值"):
                                attr["预警"] = "是"
                        elif attr.get("当前") == "小于":
                            if int(attr.get("当前")) <= attr.get("阈值"):
                                attr["预警"] = "是"
                    else:
                        continue
                
                #判断其他通用属性
                if attr.get("判定") == "大于":
                    if int(attr.get("当前")) >= attr.get("阈值"):
                        attr["预警"] = "是"
                elif attr.get("当前") == "小于":
                    if int(attr.get("当前")) <= attr.get("阈值"):
                        attr["预警"] = "是"
                else:
                    #未明确不做判定
                    pass

            except Exception as e:
                traceback.print_exc()

    if custom_info:
        custom_info["磁盘属性检测"] = smartctl_attr

    return smartctl_attr

g_excel_columns = [
        "问题单号",	"用户名称", "产品线", "是否深信服一体机", "是否影响客户业务", "分类", "卡慢盘分类",	"硬盘类型",
        #"硬盘延时ms",	
        "软件版本",	"硬盘型号",	"硬盘序列号", "硬盘固件", "硬盘Smart_5",	"硬盘Smart_197", "寿命",
        "硬盘在线时长",	"硬盘上电时间(年)",	"硬盘已上电时间", "RAID型号", "RAID固件版本",	"RAID驱动版本",
        "卡慢盘所在主机日志", "时延统计", "RAID中硬盘是否出现重置",	"RAID卡重置", "RAID中硬盘是否出现超时",
        "是否出现Media Error",	"是否单盘卡慢引起多盘",	"是否出现踢盘时间过长或者踢盘失败影响业务", "临时踢盘或永久踢盘",	"问题等级",	"故障时间",	
        "问题现象",	"问题初步结论",	"根因分类",	"二级分类",	"VS是否介入排查", "是否查明根因",	"是否需要提单跟踪",	"TD单号", "TD最终结论",	"备注", "是否全集群巡检",
    	"是否有举措处理此类问题", "具体举措",	"处理方案",	"后续预防方案",	"最终是否返修",	"返修单号", "返修处置措施",	"多选-原因归属", 
        "IO是否录制", "业务类型", "备注信息1-文件附件",	"备注信息2-图片", "备注信息3 文字", "排查人", "最后编辑人",	"是否分析有问题",
        "客户编号", "补丁列表", "运行时长", "内核版本", "服务器厂商", "服务器SN", "服务器产品名称", "服务器型号", "硬件归属", "问题分类", "硬盘接口类型", "硬盘容量", "磁盘属性检测",
        "RAID驱动", "RAID序列号", "RAID时间", "RAID系统时间", "RAID时间和系统时间差", "介质错误", "SAS地址", "链路误码", "是否出现硬盘重置", "内核日志", "Medium Error分析",
        "RAID卡错误日志", "软件措施", "硬件措施", "RAID卡列表信息", "RAID固件包版本", "存在RAID日志", "RAID故障", 
        "RAID故障信息", "RAID建议", "RAID控制器", "RAID槽位", "VS盘位",
        "故障时磁盘名称", "返修建议",
        "VS是否踢盘", "VS最终处置", "Brick上下线日志", "最终用户", "KEYID", "磁盘预警属性列表", "磁盘Smartctl错误", "RAID异常指令", 
        "无效指令统计情况", "虚拟机业务影响时间", "脚本版本", "虚拟机IO影响", "故障回溯", "AIOPS检测结果", "AI分析总结", "主机ID", "主机IP"
    ]

g_solve_columns = ["问题单号",	"用户名称", "产品线", "是否深信服一体机", "软件版本", "AI分析总结", "问题初步结论", "RAID故障", "RAID故障信息", "RAID异常指令", "RAID建议", "返修建议", 
                   "硬盘类型", "硬盘型号", "硬盘序列号", "硬盘固件", "硬盘Smart_5", "硬盘Smart_197", "寿命", "硬盘在线时长", "硬盘上电时间(年)", 
                   "磁盘预警属性列表", "磁盘Smartctl错误", "RAID控制器", "RAID槽位", "VS盘位", "RAID型号", "RAID固件版本",	"RAID驱动版本", "服务器厂商", "服务器SN", "主机ID", "主机IP"]

#进一步整理最终输出列的数据
def format_analyze(df):
    #原输出列名
    old_columns = ["服务器厂商", "服务器SN", "服务器产品名称", "服务器型号", "客户编号", "问题单号", "等级", 
               "问题现象", "硬件归属", "问题分类", "HCI版本", 
               "硬盘序列号", "硬盘型号", "硬盘容量", "硬盘固件", "磁盘属性检测", "硬盘在线时长", "RAID型号", 
               "RAID驱动", "RAID固件版本", "RAID驱动版本", "RAID序列号", "RAID时间", "RAID系统时间", 
               "RAID中硬盘是否出现超时", "RAID中硬盘是否出现重置", "介质错误", "SAS地址", "链路误码", "RAID卡重置", 
               "是否单盘卡慢引起多盘", "是否出现踢盘时间过长或者踢盘失败影响业务", "内核日志", 
               "RAID卡错误日志", "问题初步结论", "软件措施", "硬件措施", "备注", 
               "RAID卡列表信息", "RAID固件包版本", "存在RAID日志", "RAID故障", "RAID故障信息", "RAID建议",
               #, "磁盘列表",
               "时延统计", "VS是否踢盘", "Brick上下线日志", "故障时间", "用户名称", "业务类型", "KEYID", "磁盘预警属性列表", "磁盘Smartctl错误", "RAID异常指令", "无效指令统计情况"
               ]
    #新输出列名
    new_columns = g_excel_columns

    if df.empty:
        return df

    df["是否深信服一体机"] = df["服务器厂商"].apply(lambda x: "是" if pd.notna(x) and x.find("SANGFOR") != -1 else "否")
    df["软件版本"] = df["HCI版本"].apply(lambda x: x.split()[0] if pd.notna(x) else "未获取")
    df["硬盘上电时间(年)"] = df["硬盘在线时长"].apply(lambda x: int(int(x)/8760) if pd.notna(x) else np.nan)
    df["硬盘已上电时间"] = df["硬盘上电时间(年)"].apply(lambda x: "新盘" if x == 0 else "{}年".format(int(x)) if pd.notna(x) else np.nan)

    df = df.reindex(columns=new_columns)
    return df

#合并excel到一个
def combile_excel(dir, output):
    if not os.path.exists(dir):
        logger.error("目录【{}】不存在！".format(dir))
        return False
    
    new_columns = g_excel_columns
    
    files = os.listdir(dir)
    df = pd.DataFrame()
    logger.info("files count:{}".format(len(files)))
    index = 1
    for file in files:
        if not file.endswith(".xlsx"):
            continue

        logger.info("dealing file :{}... count:{}".format(file, index))
        index += 1
        
        filename = os.path.join(dir, file)
        t = pd.read_excel(filename, sheet_name="详细内容")
        if not t.empty:
            df = pd.concat([df, t.reindex(columns=new_columns)], ignore_index=True)
    df = df.sort_values(by="客户编号", ignore_index=True)
    df.to_excel(g_args.output, columns=new_columns)

def deal_custom_info_list(custom_info_list, filename = None):
    if not custom_info_list:
        return pd.DataFrame()
    
    custom_analyze_list = analyze_custom_info(custom_info_list)

    if g_args and g_args.verbose:
        for custom_info in custom_analyze_list:
            print("客户编号:{}".format(custom_info.get("客户编号")))
            print("HCI版本:{}".format(custom_info.get("HCI版本")))
            print("服务器厂商:{}".format(custom_info.get("服务器厂商")))
            print("服务器SN:{}".format(custom_info.get("服务器SN")))
            print("服务器产品名称:{}".format(custom_info.get("服务器产品名称")))
            print("服务器型号:{}".format(custom_info.get("服务器型号")))
            print("RAID卡列表信息:{}".format(custom_info.get("RAID卡列表信息")))
            print("RAID驱动:{}".format(custom_info.get("RAID驱动")))
            print("RAID驱动版本:{}".format(custom_info.get("RAID驱动版本")))
            print("RAID型号:{}".format(custom_info.get("RAID型号")))
            print("RAID序列号:{}".format(custom_info.get("RAID序列号")))
            print("RAID时间:{}".format(custom_info.get("RAID时间")))
            print("RAID系统时间:{}".format(custom_info.get("RAID系统时间")))
            print("SAS地址:{}".format(custom_info.get("SAS地址")))
            print("链路误码:{}".format(custom_info.get("链路误码")))
            print("存在RAID日志:{}".format(custom_info.get("存在RAID日志")))
            print("RAID固件包版本:{}".format(custom_info.get("RAID固件包版本")))
            print("RAID固件版本:{}".format(custom_info.get("RAID固件版本")))
            print("RAID中硬盘是否出现超时:{}".format(custom_info.get("RAID中硬盘是否出现超时")))
            print("RAID中硬盘是否出现重置:{}".format(custom_info.get("RAID中硬盘是否出现重置")))
            print("RAID故障:{}".format(custom_info.get("RAID故障")))
            print("RAID故障信息:{}".format(custom_info.get("RAID故障信息")))
            print("RAID建议:{}".format(custom_info.get("RAID建议")))
            print("内核日志:{}".format(custom_info.get("内核日志")))
            print("问题初步结论:".format(custom_info.get("问题初步结论")))
            print("硬盘型号:{}".format(custom_info.get("硬盘型号")))
            print("硬盘序列号:{}".format(custom_info.get("硬盘序列号")))
            print("硬盘固件:{}".format(custom_info.get("硬盘固件")))
            print("硬盘容量:{}".format(custom_info.get("硬盘容量")))
            print("磁盘属性检测:{}".format(custom_info.get("磁盘属性检测")))
            print("硬盘在线时长:{}".format(custom_info.get("硬盘在线时长")))
            #print("磁盘列表:{}".format(custom_info.get("磁盘列表")))
            print("时延统计:{}".format(custom_info.get("时延统计")))

    df = pd.DataFrame(custom_analyze_list)
    '''
    culumns = ["服务器厂商", "服务器SN", "服务器产品名称", "服务器型号", "客户编号", "问题单号", "等级", 
               "问题现象", "硬件归属", "问题分类", "HCI版本", 
               "硬盘序列号", "硬盘型号", "硬盘容量", "硬盘固件", "磁盘属性检测", "硬盘在线时长", "RAID型号", 
               "RAID驱动", "RAID固件版本", "RAID驱动版本", "RAID序列号", "RAID时间", "RAID系统时间", 
               "RAID中硬盘是否出现超时", "RAID中硬盘是否出现重置", "介质错误", "SAS地址", "链路误码", "RAID卡重置", 
               "是否单盘卡慢引起多盘", "是否出现踢盘时间过长或者踢盘失败影响业务", "内核日志", 
               "RAID卡错误日志", "问题初步结论", "软件措施", "硬件措施", "备注", 
               "RAID卡列表信息", "RAID固件包版本", "存在RAID日志", "RAID故障", "RAID故障信息", "RAID建议",
               #, "磁盘列表",
               "时延统计", "VS是否踢盘", "Brick上下线日志", "故障时间", "用户名称", "业务类型", "KEYID", "磁盘预警属性列表", "磁盘Smartctl错误", "RAID异常指令", "无效指令统计情况"
               ]
    df = df.reindex(columns=culumns)
    '''

    #进一步整理最终输出列的数据
    df = format_analyze(df)
    if not df.empty:
        df = df.sort_values(by="客户编号", ignore_index=True)
        
        if g_args.ai_analyze:
            # 使用大模型生成总结
            try:
                # 获取当前工作目录
                dirname = ""
                if g_args.file:
                    workdir = os.path.dirname(g_args.file)
                    basedir = os.path.basename(g_args.file)
                    if g_args.file.endswith(".tar.gz"):
                        dirname = basedir.split(".tar.gz")[0]
                    elif g_args.file.endswith(".gz"):
                        dirname = basedir.split(".gz")[0]
                elif g_args.dir:
                    workdir = g_args.dir
                else:
                    workdir = "."

                if workdir == "":
                    workdir = "."

                workdir = os.path.join(workdir, dirname)
                
                logger.info("当前分析目标目录：{}".format(workdir))

                # 根据命令行参数确定AI提供商
                if g_args.provider == "openai":
                    summarizer = LLMSummarizer(
                        model="gpt-4" if g_args.model else "qwq:32b",
                        provider="openai",
                        openai_api_key=os.environ.get("OPENAI_API_KEY") or g_args.openai_key,
                        workdir=workdir,
                        openai_base_url="http://14.116.240.74:31301/api/v1" if not g_args.openai_base_url else g_args.openai_base_url
                    )
                else:  # 默认使用ollama
                    summarizer = LLMSummarizer(
                        model=g_args.model or "qwq:32b",
                        ollama_host=g_args.ollama_host or "192.168.8.210:11434",
                        mcp_server_url="http://localhost:8000/mcp",
                        workdir=workdir
                    )
                    
                # 为每条记录生成总结
                summaries = []
                for _, row in df.iterrows():
                    analysis_data = {
                            "conclusion": {
                                "问题初步结论": row.get("问题初步结论", "")
                            },
                            "hardware_status": {
                                "服务器信息": {
                                    "厂商": row.get("服务器厂商", ""),
                                    "型号": row.get("服务器型号", ""),
                                    "SN": row.get("服务器SN", "")
                                },
                                "RAID信息": {
                                    "型号": row.get("RAID型号", ""),
                                    "固件版本": row.get("RAID固件版本", ""),
                                    "驱动版本": row.get("RAID驱动版本", "")
                                },
                                "硬盘信息": {
                                    "型号": row.get("硬盘型号", ""),
                                    "序列号": row.get("硬盘序列号", ""),
                                    "容量": row.get("硬盘容量", ""),
                                    "固件": row.get("硬盘固件", "")
                                }
                            },
                            "errors": {
                                "RAID故障": row.get("RAID故障", ""),
                                "RAID故障信息": row.get("RAID故障信息", ""),
                                "内核日志": row.get("内核日志", ""),
                                "磁盘属性检测": row.get("磁盘属性检测", "")
                            },
                            "performance_metrics": {
                                "时延统计": row.get("时延统计", ""),
                                "RAID中硬盘是否出现超时": row.get("RAID中硬盘是否出现超时", ""),
                                "RAID中硬盘是否出现重置": row.get("RAID中硬盘是否出现重置", ""),
                                "链路误码": row.get("链路误码", "")
                            }
                    }
                        
                    summary = summarizer.generate_summary(analysis_data)
                    summaries.append(summary)
                    
                # 将总结添加到DataFrame中
                df['AI分析总结'] = summaries
            except Exception as e:
                traceback.print_exc()
                logger.error(f"生成AI分析总结时发生错误: {str(e)}")

    if g_args and g_args.verbose:
        logger.info(df)

    if filename:
        if g_args and g_args.type == "json":
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(df.to_json(orient="records", force_ascii=False, indent=4))
        else:
            with pd.ExcelWriter(filename, mode='a', if_sheet_exists='replace') as writer:
                df.to_excel(writer, sheet_name="详细内容")
                df.to_excel(writer, sheet_name='问题结论和解决方案', columns=g_solve_columns)
    else:
        if g_args and g_args.type == "json":
            with open("custom_analyze_info.json", 'w', encoding='utf-8') as f:
                f.write(df.to_json(orient="records", force_ascii=False, indent=4))
        else:
            filename = "custom_analyze_info.xlsx"
            with pd.ExcelWriter(filename, mode='a', if_sheet_exists='replace') as writer:
                df.to_excel(writer, sheet_name="详细内容")
                df.to_excel(writer, sheet_name='问题结论和解决方案', columns=g_solve_columns)

    return df

#解析ioslow文件输出
def parse_ioslow_cmd_output(content):
    if content:
        try:
            now = datetime.datetime.now()
            if content.strip() == "":
                return []

            values = content.split('\n')
            if len(values) < 2:
                time.sleep(monitor_interval)
                return []

            titles = values[0].split()

            ioslows = []
            lines = values[1:]

            for line in lines:
                if not line.strip():
                    continue

                v = {"time": now.strftime("%Y-%m-%d %H:%M:%S")}
                sections = line.split()
                #sections长度必须和titles一样
                if len(sections) != len(titles):
                    logger.error("数据错误")
                    continue

                for i in range(0, len(titles)):
                    if i > 3:
                        v[titles[i]] = int(sections[i])
                    else:
                        v[titles[i]] = sections[i]

                ioslows.append(v)
            
            return ioslows
        except Exception as e:
            logger.error(e)
    
    return []

#计算IO增量情况
def diff_all_ioslow(data):
    if data.empty:
        return data
    
    # 将Time列转换为datetime类型
    #data['time'] = pd.to_datetime(data['time'], format='%y-%m-%d %H:%M:%S')

    # 确保时间戳是按顺序排列的
    #data = data.sort_values(by='time', ignore_index=True)

    # 初始化一个空的DataFrame来存储结果
    diffs_df = pd.DataFrame()

    # 对于每个磁盘和操作类型，计算相邻两行之间的差异
    for (name, op), group in data.groupby(['name', 'op:thold']):
        # 选择第5列之后的所有列进行计算差异
        group_diff = group.iloc[1:].iloc[:, 5:].astype(int) - group.iloc[:-1].iloc[:, 5:].astype(int)
        # 将前5列和计算出的差异合并为一个新的DataFrame
        diff = pd.concat([group.iloc[:-1, :5], group_diff], axis=1)
        diff['time'] = group.iloc[1:, 0]  # 更新时间戳为后一行的时间
        diffs_df = pd.concat([diffs_df, diff], ignore_index=True)

    # 重命名列以反映它们是差异
    diffs_df.columns = ['time', 'major', 'minor', 'name', 'op:thold'] + [f'{col}' for col in diffs_df.columns[5:]]
    diffs_df.sort_values(by="time", ignore_index=True)
    logger.info(diffs_df.describe())

    return diffs_df

#统计IO分布情况
def statistic_ioslow(data):
    if data.empty:
        return data

    group = data.groupby(['name', 'op:thold'])
    # 对每个分组的每一列按行求和
    io_distribution = group.apply(lambda g: g.iloc[:, 5:].sum(axis=0))

    logger.info(io_distribution)
    return io_distribution

#监控文件输出
def monitor_file(filename, monitor_time, monitor_interval, output_file=None):
    output_f = None
    data = pd.DataFrame()
    if not os.path.exists(filename):
        logger.error("{} 不存在!".format(filename))
        return data
    
    # 记录开始时间
    start_time = time.time()

    if output_file:
        output_f = open(output_file, "w+", encoding='utf-8')

    while g_running and (time.time() - start_time) < monitor_time:
        # 读取/proc/ioslow_stats文件
        try:
            # 使用 Popen 创建一个子进程来执行 cat 命令
            cat_process = subprocess.Popen(['cat', filename], universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # 使用 Popen 创建另一个子进程来执行 grep 命令，并连接到前一个进程的输出
            grep_process = subprocess.Popen(['grep', '-vE', 'nbd|dm|loop'], universal_newlines=True, stdin=cat_process.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # 从 grep 命令获取输出
            content, error = grep_process.communicate()
            ioslows = []
            
            if filename.find("/proc/ioslow_stats") != -1:
                #解析/proc/ioslow_stats
                ioslows = parse_ioslow_cmd_output(content)

            df = pd.DataFrame(ioslows)
            if not df.empty:
                data = pd.concat([data ,df], ignore_index=True)

            #如果指定输出文件
            if output_f:
                output_f.write("[{}]***********************************\n".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                output_f.write(content)

        except subprocess.CalledProcessError as e:
            logger.info("Failed to read {}:\n{}".format(filename, e))
        
        # 等待interval秒
        time.sleep(monitor_interval)

    logger.info("Monitoring finished.")
    return data

def stop_all(signal, frame):
    g_running = False
    return g_running

g_running = True

if __name__ == "__main__":
    logger.add(
        "logs.log",
        rotation="1 week",
        compression="zip",
        level="DEBUG",
        encoding="utf-8"
    )

    parser = argparse.ArgumentParser()
    arg_analyze_group = parser.add_argument_group('analyze', 'analyze log params')
    arg_tools_group = parser.add_argument_group('tools', 'tools params')

    arg_tools_group.add_argument("-p", "--parse", help="解析ioslowstat日志")
    arg_tools_group.add_argument("-st", "--start", help="解析ioslowstat日志开始时间")
    arg_tools_group.add_argument("-e", "--end", help="解析ioslowstat日志结束时间")

    arg_tools_group.add_argument("-m", "--monitor", help="监控指定文件输出", action="store_true")
    arg_tools_group.add_argument("-mt", "--monitor_time", help="监控时长")
    arg_tools_group.add_argument("-mi", "--monitor_interval", help="监控间隔")

    arg_tools_group.add_argument("-b", "--combine", help="合并excel", action='store_true')

    arg_analyze_group.add_argument("-d", "--dir", help="日志目录路径")
    arg_analyze_group.add_argument("-g", "--single", help="目录遍历时单独输出excel", action="store_true")
    arg_analyze_group.add_argument("-f", "--file", help="指定的tar.gz/zip日志路径")
    arg_analyze_group.add_argument("-o", "--output", help="输出Excel文件路径")
    arg_analyze_group.add_argument("-t", "--type", help="输出类型，默认excel")
    arg_analyze_group.add_argument("-i", "--icare", help="icare单号")
    arg_analyze_group.add_argument("-c", "--custom", help="用户名称")
    arg_analyze_group.add_argument("-ai", "--ai_analyze", help="启用AI分析", action="store_true")
    arg_analyze_group.add_argument("-s", "--save", help="保留解压日志", action="store_true")
    
    # AI 分析相关参数
    arg_analyze_group.add_argument("--provider", help="AI提供商 (ollama 或 openai)", choices=["ollama", "openai"], default="ollama")
    arg_analyze_group.add_argument("--openai_key", help="OpenAI API密钥 (当provider=openai时需要)")
    arg_analyze_group.add_argument("--model", help="要使用的模型名称 (默认: qwq:32b 或 gpt-4)")
    arg_analyze_group.add_argument("--openai_base_url", help="MCP服务器地址 (默认: https://api.openai.com/v1)")
    arg_analyze_group.add_argument("--ollama_host", help="Ollama主机地址 (默认: localhost:11434)")

    parser.add_argument("-v", "--verbose", help="是否输出详细日志信息", action='store_true')
    parser.add_argument('--version', action='version', version='%(prog)s by hlc v1.0')

    args = parser.parse_args()
    g_args = args

    logger.info(args)

    #获取日志文件路径
    log_path = log_dir

    if g_args.monitor:
        signal.signal(signal.SIGINT, stop_all)
        if g_args.file:
            monitor_time = 60
            monitor_interval = 5
            if g_args.monitor_time:
                monitor_time = int(g_args.monitor_time)
            if g_args.monitor_interval:
                monitor_interval = int(g_args.monitor_interval)
            data = monitor_file(g_args.file, monitor_time, monitor_interval, output_file=g_args.output)
            logger.info(data)
            #输出io间隔情况
            diff_data = diff_all_ioslow(data)
            logger.info(diff_data)
            if g_args.type == "xlsx":
                diff_data.to_excel("diff_ioslow.xlsx")
            elif g_args.type == "json":
                diff_data.to_json("diff_ioslow.json", orient="records", force_ascii=False, indent=4)
            #输出IO分布统计
            io_dist = statistic_ioslow(diff_data)
            logger.info(io_dist)
            io_dist.to_excel("distribute_ioslow.xlsx")
        else:
            logger.error("请指定监控的文件")
        exit()

    #组合多个Excel为一个
    if g_args.combine and g_args.dir and g_args.output:
        combile_excel(g_args.dir, g_args.output)
        exit()

    #解析ioslow两个时间点的io差
    if g_args.parse:
        iostats = parse_ioslow(g_args.parse)
        logger.info(iostats)
        #输出所有时间之间的IO差
        diff_data = diff_all_ioslow(iostats)
        diff_data.to_excel("diff_ioslow.xlsx")
        #输出IO分布统计
        io_dist = statistic_ioslow(diff_data)
        logger.info(io_dist)
        io_dist.to_excel("distribute_ioslow.xlsx")

        #输出指定的IO差
        df = diff_ioslow(iostats, start_time=g_args.start, end_time=g_args.end)
        logger.info(df)
        if g_args.output:
            df.to_excel(g_args.output)
        else:
            df.to_excel("ioslow.xlsx")
        
        exit()

    if args.dir:
        log_path = args.dir

    custom_info_list = []
    if args.file:
        if os.path.exists(args.file):
            if args.file.endswith(".tar.gz"):
                custom_info_list.append(collect_log_from_tar_gz(args.file))
            elif args.file.endswith(".zip"):
                path = Path(args.file)
                target_dir = os.path.join(str(path.parent), args.file[:-4])
                #zip包则按照acli的格式解析
                if extract_zip_file(args.file, target_dir, g_acli_zip_password):
                    #解压成功，如果没有指定icare单号，认定文件名为icare单号
                    if not args.icare:
                        args.icare = os.path.basename(args.file)[:-4]
                    #遍历解压的文件
                    wk_dir = os.walk(target_dir)
                    for root, dirs, files in wk_dir:
                        for file in files:
                            if file.endswith(".tgz"):
                                file_path = os.path.join(root, file)
                                #找到主机日志压缩包
                                slowfiles = extract_tar_gz(file_path, file_path[:-4])
                                for f in slowfiles:
                                    if f.endswith(".tar.gz"):
                                        full_path = os.path.join(file_path[:-4], f)
                                        custom_info_list.append(collect_log_from_tar_gz(full_path))
                        #只需要遍历一次即可
                        break

                else:
                    #根据密码解压失败，按照无密码解压
                    pass
        else:
            logger.error("日志文件不存在:{}".format(args.file))
            sys.exit(-1)
    else:
        logger.info("search dir: {}".format(log_path))
        if os.path.exists(log_path):
            custom_info_list = extract_directory(log_path)

    deal_custom_info_list(custom_info_list, g_args.output)