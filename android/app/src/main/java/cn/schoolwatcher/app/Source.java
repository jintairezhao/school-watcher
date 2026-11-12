package cn.schoolwatcher.app;

public final class Source {
    public long id;
    public String school, name, url, error = "";
    public boolean enabled = true;
    public long checked;

    public Source(String school, String name, String url) {
        this.school = school; this.name = name; this.url = url;
    }
}
