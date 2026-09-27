"""Shared, explicit CMS article-address evidence for structure and title checks."""
import re

ARTICLE = re.compile(r'/(?:info/\d+/\d+|(?:article|detail|content)/|\d{4}/\d{4,})|'
                     r'/post/\d+(?:$|[?#])|/Data/View/\d+(?:$|[?#])|'
                     r'/\d{6,}\.(?:s?html?)|[?&](?:articleid|contentid|wbnewsid)=|'
                     r'/news-show-\d+\.html?(?:$|[?#])|/c\d+a\d+/page\.htm(?:$|[?#])|'
                     r'https?://mp\.weixin\.qq\.com/s(?:/|\?)|'
                     r'/recruitment/position-detail\.htm\?(?:[^#]*&)?id=[A-Za-z0-9_-]+(?:&|$)|'
                     r'#!?/(?:newsDetail\?(?:[^#]*&)?id=|digest\?(?:[^#]*&)?ArticleID=)', re.I)
