package cn.schoolwatcher.app;

public final class Notice {
    public long id, sourceId;
    public String title, url, date, school = "", source = "", body = "";
    public boolean read, starred;

    public Notice(String title, String url, String date) {
        this.title = title; this.url = url; this.date = date;
    }
}
