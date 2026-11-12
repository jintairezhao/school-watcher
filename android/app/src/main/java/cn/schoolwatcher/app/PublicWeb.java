package cn.schoolwatcher.app;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.URI;
import java.net.URL;
import org.jsoup.Jsoup;

/** Bounded, certificate-verified public-page fetches; redirects are revalidated. */
public final class PublicWeb {
    interface Route {
        InetAddress[] resolve(String host) throws IOException;
        HttpURLConnection open(URL target) throws IOException;
    }
    public static String fetch(String raw) throws IOException {
        return fetch(raw,()->true);
    }
    public static String fetch(String raw,java.util.function.BooleanSupplier permitted) throws IOException {
        return fetch(raw,permitted,new Route() {
            public InetAddress[] resolve(String host) throws IOException {return InetAddress.getAllByName(host);}
            public HttpURLConnection open(URL target) throws IOException {return (HttpURLConnection)target.openConnection();}
        });
    }
    static String fetch(String raw,java.util.function.BooleanSupplier permitted,Route route) throws IOException {
        String url=NoticeParser.httpUrl(raw);
        for(int redirect=0;redirect<6;redirect++) {
            checkPermission(permitted);
            if(url.isEmpty()) throw new IOException("请使用完整的 HTTP 或 HTTPS 官网地址");
            URL target=URI.create(url).toURL();
            for(InetAddress ip:route.resolve(target.getHost())) {
                if(ip.isAnyLocalAddress() || ip.isLoopbackAddress() || ip.isLinkLocalAddress() || ip.isSiteLocalAddress() || ip.isMulticastAddress()
                    || ((ip.getAddress()[0]&255)==0) || (ip.getAddress().length==16 && ((ip.getAddress()[0]&254)==252))) {
                    throw new IOException("仅支持公开学校网站");
                }
            }
            HttpURLConnection connection=route.open(target);
            connection.setConnectTimeout(12000); connection.setReadTimeout(18000); connection.setInstanceFollowRedirects(false);
            connection.setRequestProperty("User-Agent","SchoolWatcher-Android/0.1 (public university notices)");
            connection.setRequestProperty("Accept","text/html,application/xhtml+xml");
            try {
                checkPermission(permitted);
                int code=connection.getResponseCode();
                checkPermission(permitted);
                if(code>=300 && code<400) {
                    String location=connection.getHeaderField("Location");
                    if(location==null) throw new IOException("官网返回了无效跳转");
                    url=NoticeParser.httpUrl(new URL(target,location).toString()); continue;
                }
                if(code!=200) throw new IOException("官网暂时无法访问（HTTP "+code+"），可稍后重试或打开官网");
                String type=connection.getContentType();
                if(type!=null && !type.toLowerCase(java.util.Locale.ROOT).contains("html")) throw new IOException("该地址不是通知网页，请选择栏目列表");
                try(InputStream input=connection.getInputStream();ByteArrayOutputStream bytes=new ByteArrayOutputStream()) {
                    byte[] buffer=new byte[8192]; int count; long deadline=System.nanoTime()+30_000_000_000L;
                    while((count=input.read(buffer))!=-1) {
                        checkPermission(permitted);
                        if(System.nanoTime()>deadline) throw new IOException("官网响应较慢，请稍后重试");
                        if(bytes.size()+count>4*1024*1024) throw new IOException("网页过大，请在浏览器中查看");
                        bytes.write(buffer,0,count);
                    }
                    String charset=null;
                    if(type!=null) {
                        java.util.regex.Matcher match=java.util.regex.Pattern.compile("(?i)charset=[\"']?([a-z0-9_-]+)").matcher(type);
                        if(match.find()) charset=match.group(1);
                    }
                    // Jsoup detects a declared charset/BOM (including GBK) before normalization.
                    try {
                        checkPermission(permitted);
                        org.jsoup.nodes.Document document=Jsoup.parse(new java.io.ByteArrayInputStream(bytes.toByteArray()),charset,url);
                        for(org.jsoup.nodes.Element link:document.select("a[href]")) link.attr("href",link.absUrl("href"));
                        return document.outerHtml();
                    }
                    catch(java.nio.charset.IllegalCharsetNameException | java.nio.charset.UnsupportedCharsetException ex) { throw new IOException("官网使用了不支持的文字编码"); }
                }
            } finally {connection.disconnect();}
        }
        throw new IOException("官网跳转次数过多，请检查栏目地址");
    }
    private static void checkPermission(java.util.function.BooleanSupplier permitted) throws IOException {
        if(Thread.currentThread().isInterrupted() || !permitted.getAsBoolean()) throw new java.io.InterruptedIOException("抓取已暂停，进度已保留");
    }
}
