package cn.schoolwatcher.app;
import org.junit.Test;
import org.jsoup.Jsoup;
import static org.junit.Assert.*;
public class ArticleFormatterTest {
    @Test public void campusArticleDoesNotRepeatPublisherNavigationAndHeader() {
        String html="<div class='c-main__right'><div class='c-right-detail'><div class='c-right-detail__header'>重复的通知标题 返回官网</div>"
                +"<div class='c-right-detail__sub-title'>发布日期 作者 浏览次数</div>"
                +"<div class='c-right-detail__content'><p>这是需要阅读的正式通知正文，请同学们认真查看课程安排以及有关要求，及时核对信息并按时完成相关事项。</p><table><tr><th>项目</th><th>内容</th></tr><tr><td>课程</td><td>测试课程</td></tr></table></div></div></div>";
        String result=ArticleFormatter.fragment(html,"https://www.cupk.edu.cn/c/notice.shtml");
        assertTrue(result.contains("正式通知正文"));assertTrue(result.contains("<table>"));
        assertFalse(result.contains("返回官网"));assertFalse(result.contains("浏览次数"));assertFalse(result.contains("重复的通知标题"));
    }
    @Test public void keepsTableRelationshipsButRemovesDesktopWidthsAndActiveContent() {
        String html="<article style='width:1800px'><h2>课程考核结果公示，以下为本次测试表格</h2><p>请同学们核对相关信息，如有疑问请联系学院。</p>"
                +"<table width='1800'><tr><th colspan='2'>学生信息</th><th>成绩</th></tr><tr><td>测试甲</td><td><p>环境工程测试班</p></td><td>27.5</td></tr></table>"
                +"<script>alert(1)</script><iframe src='https://evil.example'></iframe><a href='javascript:alert(1)' onclick='bad()'>危险链接</a><img src='/a.png' onerror='bad()'></article>";
        String output=ArticleFormatter.fragment(html,"https://example.edu.cn/notice/");var doc=Jsoup.parse(output);
        assertEquals(2,doc.select("table tr").size());assertEquals(3,doc.select("tr").get(1).childrenSize());
        assertEquals("2",doc.selectFirst("th").attr("colspan"));assertEquals(1,doc.select(".table-scroll").size());
        assertFalse(output.contains("1800"));assertFalse(output.contains("javascript:"));assertFalse(output.contains("onclick"));assertFalse(output.contains("onerror"));
        assertTrue(doc.select("script,iframe,img").isEmpty());assertTrue(output.contains("https://example.edu.cn/a.png"));
    }
    @Test public void metadataAndLegacyCacheAreEscapedRatherThanInterpreted() {
        Notice n=new Notice("<script>alert(1)</script>","https://example.edu.cn/","2026-10-03");n.school="学校";n.source="栏目";
        String document=ArticleFormatter.document(n,ArticleFormatter.legacy("<img src=x>\n正文"),"#ffffff","#111111","#444444","#777777","#0066cc");
        assertTrue(Jsoup.parse(document).select("script,img").isEmpty());assertTrue(document.contains("width=device-width"));assertTrue(document.contains("overflow-x:auto"));
    }
}
