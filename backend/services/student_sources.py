"""Student discovery order only; these hints never establish unit ownership."""
import json
import re

ACADEMIC = re.compile(r'院系|学院|学部|系部|专业|教学单位|本科生院|研究生院')
TEACHING = re.compile(r'教务|教学|本科生院|研究生院|培养|选课|课程|考务|考试|学籍|学位|'
                      r'转专业|推免|保研|招生|夏令营|毕业|答辩|实践教学|实验教学|专业设置|专业介绍|专业建设|培养方案')
NON_STUDENT = re.compile(r'招聘|招标|采购|校友|捐赠|宣传部|纪委|纪检|审计|离退休|'
                         r'人事处|人力资源|教师工作部|组织部|统战部|保卫处|后勤|财务处')
NOTICES = re.compile(r'通知|公告|办事指南|下载|学生处|学生工作|学工|学生事务')


def display_priority(label):
    """Order choices for readers, never filter or infer institutional ownership."""
    label = label or ''
    if re.search(r'推免|保研|招生|夏令营', label):
        return 1
    if re.search(r'教学发展|教师|教职工|师资', label):
        return 6
    if TEACHING.search(label) or re.search(r'本科|研究生|学生|奖助|奖学金|资助', label):
        return 2
    if NON_STUDENT.search(label):
        return 7
    if NOTICES.search(label):
        return 3
    if re.search(r'新闻|动态|资讯|要闻|媒体', label):
        return 5
    if re.search(r'学术|科研|研究院|研究所|实验室', label):
        return 4
    if re.search(r'院系|学院|学部|教学单位|书院', label):
        return 0
    return 6


def student_priority(kind, label, path_json='[]', school_name=''):
    """None retains an entrance outside this crawl; it does not reject the source."""
    if kind == 'root':
        return 0
    # A reviewed programme such as 人力资源管理 is not the personnel office.
    if kind == 'major':
        return 3
    label = label or ''
    try:
        path = json.loads(path_json or '[]')
    except (TypeError, ValueError):
        path = []
    path = path if isinstance(path, list) else []
    trail = ' / '.join(p for p in path if isinstance(p, str) and p != school_name)
    if NON_STUDENT.search(label):
        return None
    if kind == 'directory':
        if NON_STUDENT.search(trail) and not ACADEMIC.search(label):
            return None
        return 1
    if TEACHING.search(label):
        return 2
    if NON_STUDENT.search(trail):
        return None
    if kind == 'unit' and ACADEMIC.search(label) and len(label) <= 50:
        return 3
    if NOTICES.search(label):
        return 4
    # A college's mixed news column can carry course notices. It remains a
    # lower-priority lead; central publicity has no such academic context.
    if ACADEMIC.search(trail) or TEACHING.search(trail):
        return 5
    return None


COLLEGE = re.compile(r'(?:学院|学部|书院|学系|系)(?:[（(].{1,25}[）)])?$')
COLLEGE_DIRECTORY = re.compile(r'院系|学院设置|教学单位|教学机构|学部设置|二级学院|教学科研单位')
PROFILE_DIRECTORY = re.compile(r'导师|师资|教师名录|师资力量|人物')


def college_priority(kind, label, path_json='[]'):
    """Order the college -> notices route; never infer a source's owner."""
    label = label or ''
    try:
        path = json.loads(path_json or '[]')
    except (TypeError, ValueError):
        path = []
    path = [part for part in path if isinstance(part, str)] if isinstance(path, list) else []
    if kind == 'directory' and COLLEGE_DIRECTORY.search(label):
        return 0
    if kind == 'unit' and COLLEGE.search(label) and not any(PROFILE_DIRECTORY.search(p) for p in path):
        return 1
    if kind == 'gateway' and any(COLLEGE.search(p) for p in path):
        return 1
    if kind == 'channel' and re.search(r'通知|公告|公示', label) and any(COLLEGE.search(p) for p in path):
        return 2
    return None
