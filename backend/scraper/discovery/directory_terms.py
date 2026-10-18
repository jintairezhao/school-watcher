"""Shared catalogue language for parsing and scheduling observed links.

A generic collection label is an exploration hint. It never establishes a
department, its ownership, or permission to visit an unobserved address.
"""
import re

# Match complete generic labels: a named college, a news item, or a programme
# must not become a whole-school directory because it contains 学院 or 系.
ACADEMIC_COLLECTION_PATTERN = (
    r'^(?:(?:院\s*[（(]\s*系\s*[）)]|学院\s*[（(]\s*(?:部|系|所|学部)\s*[）)]|'
    r'(?:学院|学部)\s*[、/与和及]\s*(?:学院|学部|院系|系所|系)|'
    r'(?:全部|所有|各)(?:学院|院系|学部)|学部|'
    r'(?:院系|学院|学部|系所)(?:一览|列表|目录|导航|设置))'
    r'(?:一览|列表|目录|导航|设置)?|'
    r'(?:schools|faculties|colleges)\s*(?:&|and|/)\s*(?:schools|faculties|colleges|departments)|'
    r'(?:schools|faculties|colleges|departments|academic (?:units|departments|divisions)))$'
)
_COLLECTION = re.compile(ACADEMIC_COLLECTION_PATTERN, re.I)


def academic_collection_label(label):
    return bool(isinstance(label, str) and _COLLECTION.fullmatch(label.strip()))
