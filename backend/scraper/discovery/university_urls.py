"""中国高校名称 → 官网URL 映射表

用于管理界面「添加学校」时根据校名自动填入URL。
数据来源：各高校官网公开信息。
"""

# 格式：{学校全称: 官网首页URL}
UNIVERSITY_URL_MAP = {
    # ============ 985 高校 ============
    "北京大学": "https://www.pku.edu.cn",
    "清华大学": "https://www.tsinghua.edu.cn",
    "中国人民大学": "https://www.ruc.edu.cn",
    "北京师范大学": "https://www.bnu.edu.cn",
    "北京航空航天大学": "https://www.buaa.edu.cn",
    "北京理工大学": "https://www.bit.edu.cn",
    "中国农业大学": "https://www.cau.edu.cn",
    "中央民族大学": "https://www.muc.edu.cn",
    "复旦大学": "https://www.fudan.edu.cn",
    "上海交通大学": "https://www.sjtu.edu.cn",
    "同济大学": "https://www.tongji.edu.cn",
    "华东师范大学": "https://www.ecnu.edu.cn",
    "南京大学": "https://www.nju.edu.cn",
    "东南大学": "https://www.seu.edu.cn",
    "浙江大学": "https://www.zju.edu.cn",
    "中国科学技术大学": "https://www.ustc.edu.cn",
    "武汉大学": "https://www.whu.edu.cn",
    "华中科技大学": "https://www.hust.edu.cn",
    "国防科技大学": "https://www.nudt.edu.cn",
    "中山大学": "https://www.sysu.edu.cn",
    "华南理工大学": "https://www.scut.edu.cn",
    "四川大学": "https://www.scu.edu.cn",
    "电子科技大学": "https://www.uestc.edu.cn",
    "重庆大学": "https://www.cqu.edu.cn",
    "西安交通大学": "https://www.xjtu.edu.cn",
    "西北工业大学": "https://www.nwpu.edu.cn",
    "西北农林科技大学": "https://www.nwafu.edu.cn",
    "兰州大学": "https://www.lzu.edu.cn",
    "南开大学": "https://www.nankai.edu.cn",
    "天津大学": "https://www.tju.edu.cn",
    "大连理工大学": "https://www.dlut.edu.cn",
    "东北大学": "https://www.neu.edu.cn",
    "吉林大学": "https://www.jlu.edu.cn",
    "哈尔滨工业大学": "https://www.hit.edu.cn",
    "厦门大学": "https://www.xmu.edu.cn",
    "山东大学": "https://www.sdu.edu.cn",
    "中国海洋大学": "https://www.ouc.edu.cn",
    "湖南大学": "https://www.hnu.edu.cn",
    "中南大学": "https://www.csu.edu.cn",

    # ============ 211 高校（部分，不含985） ============
    "北京交通大学": "https://www.bjtu.edu.cn",
    "北京工业大学": "https://www.bjut.edu.cn",
    "北京科技大学": "https://www.ustb.edu.cn",
    "北京化工大学": "https://www.buct.edu.cn",
    "北京邮电大学": "https://www.bupt.edu.cn",
    "北京林业大学": "https://www.bjfu.edu.cn",
    "北京协和医学院": "https://www.pumc.edu.cn",
    "北京中医药大学": "https://www.bucm.edu.cn",
    "北京外国语大学": "https://www.bfsu.edu.cn",
    "中国传媒大学": "https://www.cuc.edu.cn",
    "中央财经大学": "https://www.cufe.edu.cn",
    "对外经济贸易大学": "https://www.uibe.edu.cn",
    "北京体育大学": "https://www.bsu.edu.cn",
    "中央音乐学院": "https://www.ccom.edu.cn",
    "中国政法大学": "https://www.cupl.edu.cn",
    "华北电力大学": "https://www.ncepu.edu.cn",
    "中国矿业大学（北京）": "https://www.cumtb.edu.cn",
    "中国石油大学（北京）": "https://www.cup.edu.cn",
    "中国地质大学（北京）": "https://www.cugb.edu.cn",
    "河北工业大学": "https://www.hebut.edu.cn",
    "太原理工大学": "https://www.tyut.edu.cn",
    "内蒙古大学": "https://www.imu.edu.cn",
    "辽宁大学": "https://www.lnu.edu.cn",
    "大连海事大学": "https://www.dlmu.edu.cn",
    "延边大学": "https://www.ybu.edu.cn",
    "东北师范大学": "https://www.nenu.edu.cn",
    "哈尔滨工程大学": "https://www.hrbeu.edu.cn",
    "东北林业大学": "https://www.nefu.edu.cn",
    "东北农业大学": "https://www.neau.edu.cn",
    "华东理工大学": "https://www.ecust.edu.cn",
    "东华大学": "https://www.dhu.edu.cn",
    "上海外国语大学": "https://www.shisu.edu.cn",
    "上海财经大学": "https://www.sufe.edu.cn",
    "上海大学": "https://www.shu.edu.cn",
    "苏州大学": "https://www.suda.edu.cn",
    "南京航空航天大学": "https://www.nuaa.edu.cn",
    "南京理工大学": "https://www.njust.edu.cn",
    "中国矿业大学": "https://www.cumt.edu.cn",
    "河海大学": "https://www.hhu.edu.cn",
    "江南大学": "https://www.jiangnan.edu.cn",
    "南京农业大学": "https://www.njau.edu.cn",
    "中国药科大学": "https://www.cpu.edu.cn",
    "南京师范大学": "https://www.njnu.edu.cn",
    "安徽大学": "https://www.ahu.edu.cn",
    "合肥工业大学": "https://www.hfut.edu.cn",
    "福州大学": "https://www.fzu.edu.cn",
    "南昌大学": "https://www.ncu.edu.cn",
    "郑州大学": "https://www.zzu.edu.cn",
    "中国地质大学（武汉）": "https://www.cug.edu.cn",
    "武汉理工大学": "https://www.whut.edu.cn",
    "华中农业大学": "https://www.hzau.edu.cn",
    "华中师范大学": "https://www.ccnu.edu.cn",
    "中南财经政法大学": "https://www.zuel.edu.cn",
    "湖南师范大学": "https://www.hunnu.edu.cn",
    "暨南大学": "https://www.jnu.edu.cn",
    "华南农业大学": "https://www.scau.edu.cn",
    "华南师范大学": "https://www.scnu.edu.cn",
    "广西大学": "https://www.gxu.edu.cn",
    "海南大学": "https://www.hainanu.edu.cn",
    "西南交通大学": "https://www.swjtu.edu.cn",
    "四川农业大学": "https://www.sicau.edu.cn",
    "西南大学": "https://www.swu.edu.cn",
    "西南财经大学": "https://www.swufe.edu.cn",
    "贵州大学": "https://www.gzu.edu.cn",
    "云南大学": "https://www.ynu.edu.cn",
    "西藏大学": "https://www.utibet.edu.cn",
    "西北大学": "https://www.nwu.edu.cn",
    "西安电子科技大学": "https://www.xidian.edu.cn",
    "长安大学": "https://www.chd.edu.cn",
    "陕西师范大学": "https://www.snnu.edu.cn",
    "青海大学": "https://www.qhu.edu.cn",
    "宁夏大学": "https://www.nxu.edu.cn",
    "新疆大学": "https://www.xju.edu.cn",
    "石河子大学": "https://www.shzu.edu.cn",

    # ============ 其他知名高校 ============
    "中国科学院大学": "https://www.ucas.ac.cn",
    "南方科技大学": "https://www.sustech.edu.cn",
    "上海科技大学": "https://www.shanghaitech.edu.cn",
    "西湖大学": "https://www.westlake.edu.cn",
    "深圳大学": "https://www.szu.edu.cn",
    "首都师范大学": "https://www.cnu.edu.cn",
    "中国社会科学院大学": "https://www.ucass.edu.cn",
    "外交学院": "https://www.cfau.edu.cn",
    "中国人民公安大学": "https://www.ppsuc.edu.cn",
    "中央戏剧学院": "https://www.zhongxi.cn",
    "中央美术学院": "https://www.cafa.edu.cn",
    "北京电影学院": "https://www.bfa.edu.cn",
    "中国音乐学院": "https://www.ccmusic.edu.cn",
    "上海音乐学院": "https://www.shcmusic.edu.cn",
    "上海戏剧学院": "https://www.sta.edu.cn",
    "中国美术学院": "https://www.caa.edu.cn",
    "南京艺术学院": "https://www.nua.edu.cn",

    # ============ 石油/地质/矿业类高校 ============
    "中国石油大学（华东）": "https://www.upc.edu.cn",
    "西南石油大学": "https://www.swpu.edu.cn",
    "东北石油大学": "https://www.nepu.edu.cn",
    "西安石油大学": "https://www.xsyu.edu.cn",
    "辽宁石油化工大学": "https://www.lnpu.edu.cn",
    "北京石油化工学院": "https://www.bipt.edu.cn",
    "中国地质大学": "https://www.cug.edu.cn",
    "中国矿业大学（徐州）": "https://www.cumt.edu.cn",
}

# 短名称/别名映射（用于模糊匹配回退）
ALIAS_MAP = {
    "北大": "北京大学",
    "清华": "清华大学",
    "人大": "中国人民大学",
    "北师大": "北京师范大学",
    "北航": "北京航空航天大学",
    "北理工": "北京理工大学",
    "中农": "中国农业大学",
    "复旦": "复旦大学",
    "上交": "上海交通大学",
    "上海交大": "上海交通大学",
    "同济": "同济大学",
    "华师大": "华东师范大学",
    "南大": "南京大学",
    "东大": "东南大学",
    "浙大": "浙江大学",
    "中科大": "中国科学技术大学",
    "武大": "武汉大学",
    "华科": "华中科技大学",
    "中大": "中山大学",
    "华工": "华南理工大学",
    "川大": "四川大学",
    "电子科大": "电子科技大学",
    "重大": "重庆大学",
    "西交": "西安交通大学",
    "西工大": "西北工业大学",
    "南开": "南开大学",
    "天大": "天津大学",
    "大工": "大连理工大学",
    "吉大": "吉林大学",
    "哈工大": "哈尔滨工业大学",
    "厦大": "厦门大学",
    "山大": "山东大学",
    "湖大": "湖南大学",
    "中南": "中南大学",
    "兰大": "兰州大学",
    "石大": "中国石油大学（北京）",
    "北邮": "北京邮电大学",
    "北交": "北京交通大学",
    "北科": "北京科技大学",
    "北化": "北京化工大学",
    "北林": "北京林业大学",
    "中传": "中国传媒大学",
    "央财": "中央财经大学",
    "外经贸": "对外经济贸易大学",
    "法大": "中国政法大学",
    "华电": "华北电力大学",
    "华理": "华东理工大学",
    "上财": "上海财经大学",
    "南航": "南京航空航天大学",
    "南理工": "南京理工大学",
    "河海": "河海大学",
    "合工大": "合肥工业大学",
    "郑大": "郑州大学",
    "武理工": "武汉理工大学",
    "华农": "华中农业大学",
    "暨大": "暨南大学",
    "西电": "西安电子科技大学",
    "国科大": "中国科学院大学",
    "南科大": "南方科技大学",
    "深大": "深圳大学",
    "上科大": "上海科技大学",
}


def suggest_url(name: str) -> str | None:
    """根据学校名称查找官网URL。

    查找策略（按优先级）：
    1. 精确匹配全称
    2. 别名匹配 (ALIAS_MAP)
    3. 子串模糊匹配（名称包含输入的学校名）

    Returns:
        找到的 URL 字符串，未找到返回 None
    """
    if not name or not name.strip():
        return None

    name = name.strip()

    # Step 1: 精确匹配
    if name in UNIVERSITY_URL_MAP:
        return UNIVERSITY_URL_MAP[name]

    # Step 2: 别名
    if name in ALIAS_MAP:
        full_name = ALIAS_MAP[name]
        if full_name in UNIVERSITY_URL_MAP:
            return UNIVERSITY_URL_MAP[full_name]

    # Step 3: 子串模糊匹配（输入是大学全称的子串）
    # 例：输入"华中科技大学"匹配"华中科技大学"
    for full_name, url in UNIVERSITY_URL_MAP.items():
        if name in full_name:
            return url

    # Step 4: 反向子串匹配（全称包含输入）
    # 例：输入"北京大学医学部"匹配"北京大学"
    for full_name, url in UNIVERSITY_URL_MAP.items():
        if full_name in name:
            return url

    return None
