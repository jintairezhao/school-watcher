package cn.schoolwatcher.app;

import org.jsoup.Jsoup;
import org.jsoup.nodes.Document;
import org.jsoup.nodes.Element;
import java.net.URI;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Conservative static HTML parser, shared by foreground discovery and background collection. */
public final class NoticeParser {
    private static final Pattern DATE = Pattern.compile("(20\\d{2})\\s*[-./年]\\s*(\\d{1,2})\\s*[-./月]\\s*(\\d{1,2})");
    private static final Pattern NAV = Pattern.compile("通知|公告|公示|教务|招生|学生工作|研究生|学院|组织机构|院系|部门|新闻|资讯");
    private static final Pattern FILE = Pattern.compile("(?i).*\\.(pdf|docx?|xlsx?|zip|rar|png|jpg|jpeg|gif|mp4)(?:\\?.*)?$");

    public static String httpUrl(String raw) {
        try {
            URI url = URI.create(raw.trim()).normalize();
            if (!("https".equalsIgnoreCase(url.getScheme()) || "http".equalsIgnoreCase(url.getScheme()))
                    || url.getHost() == null || url.getUserInfo() != null) return "";
            String address=url.getScheme().toLowerCase(Locale.ROOT)+"://"+url.getRawAuthority().toLowerCase(Locale.ROOT)
                    +(url.getRawPath().isEmpty()?"/":url.getRawPath())
                    +(url.getRawQuery()==null?"":"?"+url.getRawQuery());
            return URI.create(address).toASCIIString();
        } catch (Exception ignored) { return ""; }
    }

    public static String date(String text) {
        Matcher match = DATE.matcher(text);
        if (!match.find()) return "";
        try { return LocalDate.of(Integer.parseInt(match.group(1)),Integer.parseInt(match.group(2)),Integer.parseInt(match.group(3))).toString(); }
        catch (RuntimeException ignored) { return ""; }
    }

    public static String nextPage(String html,String baseUrl) {
        Document doc=Jsoup.parse(html,baseUrl);URI base=URI.create(httpUrl(baseUrl));
        for(Element link:doc.select("a[href]")) {
            if(!link.text().replaceAll("\\s+","").matches("(?i)下一页|下页|next") && !link.attr("rel").equalsIgnoreCase("next"))continue;
            String address=httpUrl(link.absUrl("href"));if(address.isEmpty() || address.equals(httpUrl(baseUrl)))continue;
            URI next=URI.create(address);
            if(base.getScheme().equals(next.getScheme()) && java.util.Objects.equals(base.getHost(),next.getHost()) && base.getPort()==next.getPort())return address;
        }
        return "";
    }

    public static List<Notice> notices(String html, String baseUrl) {
        Document doc = Jsoup.parse(html, baseUrl);
        doc.select("script,style,nav,header,footer").remove();
        LinkedHashMap<String, Notice> result = new LinkedHashMap<>();
        for (Element link : doc.select("a[href]")) {
            String title = link.attr("title").trim();
            if (title.length() < 8) {
                Element heading=link.selectFirst("h3");
                title=heading==null?link.text().trim():heading.text().trim();
            }
            String url = httpUrl(link.absUrl("href"));
            if (title.length()<8 || title.length()>350 || url.isEmpty() || url.equals(httpUrl(baseUrl)) || FILE.matcher(url).matches()) continue;
            String published = "";
            Element region = link;
            for (int depth=0; depth<4 && region!=null; depth++,region=region.parent()) {
                // Do not borrow the date from a neighbouring article in an enclosing list.
                if (region.text().length()>700 || region.select("a[href]").stream().map(a->a.absUrl("href")).distinct().count()>1) break;
                published=splitCampusDate(region,baseUrl);
                if(published.isEmpty()) published=date(region.text());
                if (!published.isEmpty()) break;
            }
            if (published.isEmpty()) continue;
            title = title.replaceFirst("\\s*20\\d{2}[-./年]\\d{1,2}[-./月]\\d{1,2}日?\\s*$", "").trim();
            result.putIfAbsent(url,new Notice(title,url,published));
            if (result.size()>=150) break;
        }
        return new ArrayList<>(result.values());
    }

    private static String splitCampusDate(Element region,String baseUrl) {
        String host;
        try {host=URI.create(baseUrl).getHost();} catch(RuntimeException invalid) {return "";}
        if(!"www.cupk.edu.cn".equalsIgnoreCase(host) && !"cupk.edu.cn".equalsIgnoreCase(host)) return "";
        // The campus uses both year/month-day and month-day/year, even with swapped
        // CSS names. Read the date box in this single-article region, never the URL.
        Element box=region.selectFirst(".middleArticle__dates,.SecondArticle__dates");
        if(box==null) return "";
        String year="",monthDay="";
        for(Element part:box.children()) {
            String value=part.text().trim();
            if(value.matches("20\\d{2}")) year=value;
            else if(value.matches("\\d{1,2}-\\d{1,2}")) monthDay=value;
        }
        return year.isEmpty() || monthDay.isEmpty()?"":date(year+"-"+monthDay);
    }

    public static List<Source> discover(String html, String baseUrl, String school) {
        Document doc=Jsoup.parse(html,baseUrl);
        LinkedHashMap<String,Source> result=new LinkedHashMap<>();
        for (Element link:doc.select("a[href]")) {
            String name=link.text().trim(), url=httpUrl(link.absUrl("href"));
            if (name.length()<2 || name.length()>48 || !NAV.matcher(name).find() || url.isEmpty() || FILE.matcher(url).matches()) continue;
            if (url.equals(httpUrl(baseUrl))) continue;
            result.putIfAbsent(url,new Source(school,name,url));
            if(result.size()>=100) break;
        }
        return new ArrayList<>(result.values());
    }

    public static String body(String html, String baseUrl) {
        Element content=articleElement(html,baseUrl);
        for(Element block:content.select("p,div,li,tr,h1,h2,h3,br")) block.prependText("\n");
        String text=content.wholeText().replaceAll("[\\t\\x0B\\f\\r ]+", " ").replaceAll("\\n[ ]*\\n(?:[ ]*\\n)+","\n\n").trim();
        if(text.length()<30) throw new IllegalArgumentException("正文需要在官网打开");
        return text.length()>200000?text.substring(0,200000)+"\n\n内容较长，请打开原文查看完整内容。":text;
    }
    static Element articleElement(String html,String baseUrl) {
        Document doc=Jsoup.parse(html,baseUrl);
        doc.select("script,style,nav,header,footer,form,noscript,iframe").remove();
        Element content=null;
        for(String selector:new String[]{"#vsb_content","#vsb_content_2",".v_news_content",".wp_articlecontent","#article_content",".TRS_Editor",".c-right-detail__content","article",".article-content",".c-main__right","main"}) {
            Element match=doc.selectFirst(selector);
            if(match!=null && match.text().length()>=40) {content=match;break;}
        }
        if(content==null) throw new IllegalArgumentException("未识别到正文，请打开原文阅读");
        return content;
    }
}
