package cn.schoolwatcher.app;

import org.jsoup.Jsoup;
import org.jsoup.nodes.Document;
import org.jsoup.nodes.Element;
import org.jsoup.safety.Safelist;

/** Offline HTML with publisher layout and executable content removed. */
final class ArticleFormatter {
    static String escape(String text){return new Element("span").text(text).html();}
    static String legacy(String text){return "<p>"+escape(text).replace("\n","<br>")+"</p>";}
    static String fragment(String html,String url) {
        Element content=NoticeParser.articleElement(html,url);
        content.select("script,style,iframe,object,embed,form,input,button,video,audio,svg,canvas").remove();
        for(Element picture:content.select("img")) {
            String address=NoticeParser.httpUrl(picture.absUrl("src"));
            if(address.isEmpty())picture.remove();
            else picture.replaceWith(new Element("a").attr("href",address).text(picture.attr("alt").trim().isEmpty()?"在浏览器查看图片":"查看图片："+picture.attr("alt")));
        }
        for(Element link:content.select("a[href]"))link.attr("href",NoticeParser.httpUrl(link.absUrl("href")));
        Safelist safe=new Safelist().addTags("p","div","span","br","h1","h2","h3","h4","h5","h6","strong","b","em","i","u","s","sub","sup","ul","ol","li","blockquote","pre","code","hr","a","table","thead","tbody","tfoot","tr","th","td","caption")
                .addAttributes("a","href").addProtocols("a","href","https","http")
                .addAttributes("th","colspan","rowspan","scope").addAttributes("td","colspan","rowspan");
        Document clean=Jsoup.parseBodyFragment(Jsoup.clean(content.html(),url,safe),url);
        for(Element empty:clean.select("p"))if(empty.text().replace('\u00a0',' ').trim().isEmpty() && empty.select("a,table").isEmpty())empty.remove();
        for(Element cell:clean.select("td,th"))for(String attr:new String[]{"colspan","rowspan"}) {
            if(cell.hasAttr(attr))try {int count=Integer.parseInt(cell.attr(attr));if(count<1 || count>100)cell.removeAttr(attr);}catch(NumberFormatException invalid){cell.removeAttr(attr);}
        }
        for(Element table:clean.select("table"))table.wrap("<div class='table-scroll' role='region' aria-label='表格，可左右滑动' tabindex='0'></div>");
        return clean.body().html();
    }
    static String document(Notice notice,String fragment,String background,String foreground,String muted,String border,String accent) {
        return "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                +"<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; frame-src 'none'; form-action 'none'\">"
                +"<style>:root{color-scheme:light dark}*{box-sizing:border-box}html,body{margin:0;padding:0;width:100%;background:"+background+";color:"+foreground+"}"
                +"body{font:17px/1.75 sans-serif;padding:12px 0 40px;overflow-wrap:anywhere}article{width:100%;min-width:0}h1{font-size:24px;line-height:1.45;margin:0 0 14px}"
                +"h2,h3,h4,h5,h6{font-size:20px;line-height:1.5;margin:22px 0 12px}p,div{max-width:100%}p{margin:0 0 16px}ul,ol{padding-left:1.5em}li{margin:6px 0}"
                +".meta,.tip{font-size:13px;color:"+muted+";line-height:1.6}.tip{margin:16px 0}a{color:"+accent+"}pre{white-space:pre-wrap}"
                +".table-scroll{width:100%;overflow-x:auto;margin:20px 0;-webkit-overflow-scrolling:touch}table{border-collapse:collapse;min-width:100%;width:max-content;font-size:15px;line-height:1.6}"
                +"th,td{min-width:5em;max-width:18em;padding:10px 12px;border:1px solid "+border+";vertical-align:top;white-space:normal;overflow-wrap:anywhere}th{font-weight:700}"
                +"td p,th p{margin:0 0 4px}td p:last-child,th p:last-child{margin-bottom:0}caption{text-align:left;font-weight:700}blockquote{margin:16px 0;padding:0 16px}"
                +"</style></head><body><h1>"+escape(notice.title)+"</h1><p class='meta'>"+escape(notice.school)+"<br>"+escape(notice.source+" · "+notice.date)+"</p>"
                +"<p class='tip'>宽表格可左右滑动；图片和附件可打开原文查看。</p><article>"+fragment+"</article></body></html>";
    }
}
