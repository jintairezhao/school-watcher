"""Local directory; listing a school never initiates a network crawl.

Double First Class membership: Ministry of Education, second round (2022).
https://www.moe.gov.cn/srcsite/A22/s7065/202202/t20220211_598710.html
Names checked against the published attachment; URLs are connection candidates,
not a claim that every site has passed a live crawl.
"""
from functools import lru_cache
from urllib.parse import urlsplit

from backend.scraper.discovery.university_urls import UNIVERSITY_URL_MAP, ALIAS_MAP

MEMBERS_BY_PROVINCE = {
    '北京': '北京大学 中国人民大学 清华大学 北京交通大学 北京工业大学 北京航空航天大学 北京理工大学 北京科技大学 北京化工大学 北京邮电大学 中国农业大学 北京林业大学 北京协和医学院 北京中医药大学 北京师范大学 首都师范大学 北京外国语大学 中国传媒大学 中央财经大学 对外经济贸易大学 外交学院 中国人民公安大学 北京体育大学 中央音乐学院 中国音乐学院 中央美术学院 中央戏剧学院 中央民族大学 中国政法大学 华北电力大学 中国矿业大学（北京） 中国石油大学（北京） 中国地质大学（北京） 中国科学院大学',
    '天津': '南开大学 天津大学 天津工业大学 天津医科大学 天津中医药大学 河北工业大学',
    '山西': '山西大学 太原理工大学',
    '内蒙古': '内蒙古大学',
    '辽宁': '辽宁大学 大连理工大学 东北大学 大连海事大学',
    '吉林': '吉林大学 延边大学 东北师范大学',
    '黑龙江': '哈尔滨工业大学 哈尔滨工程大学 东北农业大学 东北林业大学',
    '上海': '复旦大学 同济大学 上海交通大学 华东理工大学 东华大学 上海海洋大学 上海中医药大学 华东师范大学 上海外国语大学 上海财经大学 上海体育大学 上海音乐学院 上海大学 上海科技大学 海军军医大学',
    '江苏': '南京大学 苏州大学 东南大学 南京航空航天大学 南京理工大学 中国矿业大学 南京邮电大学 河海大学 江南大学 南京林业大学 南京信息工程大学 南京农业大学 南京医科大学 南京中医药大学 中国药科大学 南京师范大学',
    '浙江': '浙江大学 中国美术学院 宁波大学',
    '安徽': '安徽大学 中国科学技术大学 合肥工业大学',
    '福建': '厦门大学 福州大学',
    '江西': '南昌大学',
    '山东': '山东大学 中国海洋大学 中国石油大学（华东）',
    '河南': '郑州大学 河南大学',
    '湖北': '武汉大学 华中科技大学 中国地质大学（武汉） 武汉理工大学 华中农业大学 华中师范大学 中南财经政法大学',
    '湖南': '湘潭大学 湖南大学 中南大学 湖南师范大学 国防科技大学',
    '广东': '中山大学 暨南大学 华南理工大学 华南农业大学 广州医科大学 广州中医药大学 华南师范大学 南方科技大学',
    '海南': '海南大学',
    '广西': '广西大学',
    '四川': '四川大学 西南交通大学 电子科技大学 西南石油大学 成都理工大学 四川农业大学 成都中医药大学 西南财经大学',
    '重庆': '重庆大学 西南大学',
    '贵州': '贵州大学',
    '云南': '云南大学',
    '西藏': '西藏大学',
    '陕西': '西北大学 西安交通大学 西北工业大学 西安电子科技大学 长安大学 西北农林科技大学 陕西师范大学 空军军医大学',
    '甘肃': '兰州大学',
    '青海': '青海大学',
    '宁夏': '宁夏大学',
    '新疆': '新疆大学 石河子大学',
}

EXTRA_URLS = {
    '天津工业大学': 'https://www.tiangong.edu.cn',
    '天津医科大学': 'https://www.tmu.edu.cn',
    '天津中医药大学': 'https://www.tjutcm.edu.cn',
    '山西大学': 'https://www.sxu.edu.cn',
    '上海海洋大学': 'https://www.shou.edu.cn',
    '上海中医药大学': 'https://www.shutcm.edu.cn',
    '上海体育大学': 'https://www.sus.edu.cn',
    '南京邮电大学': 'https://www.njupt.edu.cn',
    '南京林业大学': 'https://www.njfu.edu.cn',
    '南京信息工程大学': 'https://www.nuist.edu.cn',
    '南京医科大学': 'https://www.njmu.edu.cn',
    '南京中医药大学': 'https://www.njucm.edu.cn',
    '河南大学': 'https://www.henu.edu.cn',
    '湘潭大学': 'https://www.xtu.edu.cn',
    '华南农业大学': 'https://www.scau.edu.cn',
    '广州医科大学': 'https://www.gzhmu.edu.cn',
    '广州中医药大学': 'https://www.gzucm.edu.cn',
    '成都理工大学': 'https://www.cdut.edu.cn',
    '成都中医药大学': 'https://www.cdutcm.edu.cn',
    '宁波大学': 'https://www.nbu.edu.cn',
    '海军军医大学': 'https://www.smmu.edu.cn',
    '空军军医大学': 'https://www.fmmu.edu.cn',
    '浙江工业大学': 'https://www.zjut.edu.cn',
    '杭州电子科技大学': 'https://www.hdu.edu.cn',
    '浙江理工大学': 'https://www.zstu.edu.cn',
    '江苏大学': 'https://www.ujs.edu.cn',
    '扬州大学': 'https://www.yzu.edu.cn',
    '南京工业大学': 'https://www.njtech.edu.cn',
    '南京审计大学': 'https://www.nau.edu.cn',
    '河北大学': 'https://www.hbu.edu.cn',
    '燕山大学': 'https://www.ysu.edu.cn',
    '山西医科大学': 'https://www.sxmu.edu.cn',
    '中北大学': 'https://www.nuc.edu.cn',
    '沈阳工业大学': 'https://www.sut.edu.cn',
    '东北财经大学': 'https://www.dufe.edu.cn',
    '中国医科大学': 'https://www.cmu.edu.cn',
    '黑龙江大学': 'https://www.hlju.edu.cn',
    '哈尔滨医科大学': 'https://www.hrbmu.edu.cn',
    '安徽师范大学': 'https://www.ahnu.edu.cn',
    '安徽医科大学': 'https://www.ahmu.edu.cn',
    '福建师范大学': 'https://www.fjnu.edu.cn',
    '华侨大学': 'https://www.hqu.edu.cn',
    '江西财经大学': 'https://www.jxufe.edu.cn',
    '山东师范大学': 'https://www.sdnu.edu.cn',
    '山东科技大学': 'https://www.sdust.edu.cn',
    '青岛大学': 'https://www.qdu.edu.cn',
    '河南师范大学': 'https://www.htu.edu.cn',
    '河南理工大学': 'https://www.hpu.edu.cn',
    '武汉科技大学': 'https://www.wust.edu.cn',
    '湖北大学': 'https://www.hubu.edu.cn',
    '长沙理工大学': 'https://www.csust.edu.cn',
    '湖南科技大学': 'https://www.hnust.edu.cn',
    '广东工业大学': 'https://www.gdut.edu.cn',
    '广州大学': 'https://www.gzhu.edu.cn',
    '广东外语外贸大学': 'https://www.gdufs.edu.cn',
    '南方医科大学': 'https://www.smu.edu.cn',
    '广西师范大学': 'https://www.gxnu.edu.cn',
    '西南科技大学': 'https://www.swust.edu.cn',
    '重庆邮电大学': 'https://www.cqupt.edu.cn',
    '重庆医科大学': 'https://www.cqmu.edu.cn',
    '昆明理工大学': 'https://www.kmust.edu.cn',
    '西安理工大学': 'https://www.xaut.edu.cn',
    '西安建筑科技大学': 'https://www.xauat.edu.cn',
    '西北师范大学': 'https://www.nwnu.edu.cn',
    '新疆医科大学': 'https://www.xjmu.edu.cn',
}

NAME_ALIASES = {'上海体育学院': '上海体育大学', '中国地质大学': '中国地质大学（武汉）',
                '中国矿业大学（徐州）': '中国矿业大学'}

OTHER_PROVINCES = {
    '北京': '中国社会科学院大学 北京电影学院 北京石油化工学院',
    '上海': '上海戏剧学院',
    '浙江': '西湖大学 浙江工业大学 杭州电子科技大学 浙江理工大学',
    '江苏': '南京艺术学院 江苏大学 扬州大学 南京工业大学 南京审计大学',
    '河北': '河北大学 燕山大学',
    '山西': '山西医科大学 中北大学',
    '辽宁': '辽宁石油化工大学 沈阳工业大学 东北财经大学 中国医科大学',
    '黑龙江': '东北石油大学 黑龙江大学 哈尔滨医科大学',
    '安徽': '安徽师范大学 安徽医科大学',
    '福建': '福建师范大学 华侨大学',
    '江西': '江西财经大学',
    '山东': '山东师范大学 山东科技大学 青岛大学',
    '河南': '河南师范大学 河南理工大学',
    '湖北': '武汉科技大学 湖北大学',
    '湖南': '长沙理工大学 湖南科技大学',
    '广东': '深圳大学 广东工业大学 广州大学 广东外语外贸大学 南方医科大学',
    '广西': '广西师范大学',
    '四川': '西南科技大学',
    '重庆': '重庆邮电大学 重庆医科大学',
    '云南': '昆明理工大学',
    '陕西': '西安石油大学 西安理工大学 西安建筑科技大学',
    '甘肃': '西北师范大学',
    '新疆': '新疆医科大学',
}


def normalize_name(name):
    name = name.strip().replace('(', '（').replace(')', '）')
    return NAME_ALIASES.get(name, ALIAS_MAP.get(name, name))


@lru_cache(maxsize=1)
def catalog_entries():
    provinces = {name: province for province, names in MEMBERS_BY_PROVINCE.items() for name in names.split()}
    other_provinces = {name: province for province, names in OTHER_PROVINCES.items() for name in names.split()}
    urls = {**UNIVERSITY_URL_MAP, **EXTRA_URLS}
    entries = {}
    for name, url in urls.items():
        name = normalize_name(name)
        entries[name] = dict(name=name, url=url, province=provinces.get(name, other_provinces.get(name, '')),
                             double_first_class=name in provinces)
    return tuple(entries.values())


def find_entry(name):
    name = normalize_name(name)
    return next((item for item in catalog_entries() if item['name'] == name), None)


def host_key(url):
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')
