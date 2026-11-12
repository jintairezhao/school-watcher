package cn.schoolwatcher.app;

import org.junit.Test;
import static org.junit.Assert.*;
import java.util.List;

public class NoticeParserTest {
    @Test public void followsOnlyExplicitSafeNextPageLinks() {
        String base="https://www.cupk.edu.cn/jwb/tzgg/";
        assertEquals(base+"index_2.shtml",NoticeParser.nextPage("<a href='index_2.shtml'><span>下一页</span></a>",base));
        for(String href:new String[]{"#","javascript:next()","https://evil.example/page2","http://www.cupk.edu.cn/page2"})
            assertEquals("",NoticeParser.nextPage("<a href='"+href+"'>下一页</a>",base));
        assertEquals("",NoticeParser.nextPage("<a href='/article/2'>下一篇</a>",base));
    }
    @Test public void campusSplitDatesUseDisplayedPublicationDateInEitherOrder() {
        String html="<div class='SecondArticle__art--art'><div class='SecondArticle__dates'><span>2026</span><br><span>08-08</span></div>"
                +"<a href='/sgxy/c/2026-08-07/123.shtml'><h3>关于举办学术研讨会的正式通知</h3></a></div>"
                +"<div class='middleArticle__art--art'><div class='middleArticle__dates'><span>09-15</span><br><span>2026</span></div>"
                +"<a href='/xgybw/c/2026-09-15/456.shtml'><h3>关于办理新生户口迁移的通知</h3></a></div>";
        List<Notice> notices=NoticeParser.notices(html,"https://www.cupk.edu.cn/tzgg/");
        assertEquals(2,notices.size());assertEquals("2026-08-08",notices.get(0).date);
        assertEquals("2026-09-15",notices.get(1).date);
    }
    @Test public void splitDateCannotLeakIntoUndatedNeighboursOrInvalidCalendarDates() {
        String html="<main><div><a href='/undated'>关于本年度学生选课的重要通知</a></div>"
                +"<div><div class='middleArticle__dates'><span>2026</span><br><span>02-30</span></div>"
                +"<a href='/invalid'>关于本年度学生评奖的重要通知</a></div>"
                +"<div><div class='middleArticle__dates'><span>2026</span><br><span>09-30</span></div>"
                +"<a href='/valid'>关于本年度学生奖学金的结果公示</a></div></main>";
        List<Notice> notices=NoticeParser.notices(html,"https://www.cupk.edu.cn/tzgg/");
        assertEquals(1,notices.size());assertTrue(notices.get(0).url.endsWith("/valid"));
    }
    @Test public void parsesDatedRelativeLinksAndDeduplicates() {
        String html="<ul><li><a href='/notice/1.html' title='关于新学期课程安排的通知'>课程安排</a><span>2026年9月30日</span></li>"
                +"<li><a href='/notice/1.html'>关于新学期课程安排的通知</a>2026-09-30</li></ul>";
        List<Notice> result=NoticeParser.notices(html,"https://example.edu.cn/notices/");
        assertEquals(1,result.size());assertEquals("https://example.edu.cn/notice/1.html",result.get(0).url);
        assertEquals("2026-09-30",result.get(0).date);
    }
    @Test public void navigationAndUndatedLinksAreNotNotices() {
        String html="<nav><a href='/home'>学校通知栏目导航入口</a>2026-10-01</nav>"
                +"<ul><li><a href='/about'>学校简介以及组织机构</a></li>"
                +"<li><a href='/notice/2.html'>关于新学期选课有关事项的通知</a>2026-10-02</li></ul>";
        List<Notice> result=NoticeParser.notices(html,"https://example.edu.cn/");
        assertEquals(1,result.size());assertTrue(result.get(0).url.endsWith("2.html"));
    }
    @Test public void rejectsExecutableCredentialAndInvalidAddresses() {
        for(String url:new String[]{"javascript:alert(1)","file:///etc/test","intent://test","https://user:secret@example.edu.cn/","not a url"}) assertEquals("",NoticeParser.httpUrl(url));
        assertEquals("https://example.edu.cn/a",NoticeParser.httpUrl("https://EXAMPLE.edu.cn/a#anchor"));
        assertEquals("https://example.edu.cn/view?id=a%26b",NoticeParser.httpUrl("https://example.edu.cn/view?id=a%26b"));
    }
    @Test public void validatesCalendarDates() {
        assertEquals("",NoticeParser.date("2026年2月30日"));assertEquals("2024-02-29",NoticeParser.date("2024.2.29"));
    }
    @Test public void discoversNamedColumnsWithoutJavascriptOrFiles() {
        String html="<a href='/jwb/'>教务部</a><a href='/notice/'>通知公告</a><a href='javascript:void(0)'>通知公告</a><a href='/a.pdf'>通知文件</a>";
        assertEquals(2,NoticeParser.discover(html,"https://example.edu.cn/","测试大学").size());
    }
    @Test public void bodyIsPlainTextWithoutActiveContent() {
        String html="<article><h1>课程安排通知</h1><p>请同学们及时查看本学期课程安排，并根据学校要求完成选课和信息核对。</p><script>alert('unsafe')</script></article>";
        String body=NoticeParser.body(html,"https://example.edu.cn/");
        assertTrue(body.contains("课程安排通知"));assertFalse(body.contains("unsafe"));assertFalse(body.contains("<script"));
    }
    @Test(expected=IllegalArgumentException.class) public void missingArticleDoesNotCacheSiteNavigation() {
        NoticeParser.body("<body><nav>学校官网首页导航</nav><div>请启用 JavaScript 查看内容</div></body>","https://example.edu.cn/");
    }
}
