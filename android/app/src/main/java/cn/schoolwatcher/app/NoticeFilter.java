package cn.schoolwatcher.app;

import org.json.JSONObject;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.List;

final class NoticeFilter {
    String from="",to="",school="",sourceName="";
    long sourceId;
    int read; // 0 all, 1 unread, 2 read
    boolean oldestFirst;
    NoticeFilter copy() {return restore(save());}
    String save() {
        try {return new JSONObject().put("from",from).put("to",to).put("school",school)
                .put("sourceId",sourceId).put("sourceName",sourceName).put("read",read).put("oldestFirst",oldestFirst).toString();}
        catch(Exception error) {throw new IllegalStateException(error);}
    }
    static NoticeFilter restore(String json) {
        NoticeFilter filter=new NoticeFilter();
        try {
            JSONObject value=new JSONObject(json);
            filter.from=value.optString("from");filter.to=value.optString("to");filter.school=value.optString("school");
            filter.sourceId=value.optLong("sourceId");filter.sourceName=value.optString("sourceName");
            filter.read=value.optInt("read");filter.oldestFirst=value.optBoolean("oldestFirst");
            if(!filter.valid()) return new NoticeFilter();
        } catch(Exception ignored) {return new NoticeFilter();}
        return filter;
    }
    boolean valid() {
        try {
            if(!from.isEmpty() && !LocalDate.parse(from).toString().equals(from))return false;
            if(!to.isEmpty() && !LocalDate.parse(to).toString().equals(to))return false;
            return read>=0 && read<=2 && sourceId>=0 && (from.isEmpty() || to.isEmpty() || from.compareTo(to)<=0);
        } catch(RuntimeException invalid) {return false;}
    }
    boolean active() {return !from.isEmpty() || !to.isEmpty() || !school.isEmpty() || sourceId>0 || read>0 || oldestFirst;}
    boolean includes(Source source) {return (school.isEmpty() || school.equals(source.school)) && (sourceId==0 || sourceId==source.id);}
    String summary() {
        List<String> parts=new ArrayList<>();
        if(!from.isEmpty() || !to.isEmpty())parts.add((from.isEmpty()?"最早":from)+" 至 "+(to.isEmpty()?"最新":to));
        if(!school.isEmpty())parts.add(school);
        if(sourceId>0)parts.add(sourceName);
        if(read>0)parts.add(read==1?"未读":"已读");
        if(oldestFirst)parts.add("最早优先");
        return parts.isEmpty()?"全部日期 · 全部学校与栏目":String.join(" · ",parts);
    }
    static final class Selection {
        final String sql;final String[] args;
        Selection(String sql,List<String> args){this.sql=sql;this.args=args.toArray(new String[0]);}
    }
    Selection selection(String query,boolean starred) {
        if(!valid())throw new IllegalArgumentException("日期范围无效");
        String term="%"+query.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")+"%";
        StringBuilder sql=new StringBuilder("(n.title LIKE ? ESCAPE '\\' OR s.school LIKE ? ESCAPE '\\' OR s.name LIKE ? ESCAPE '\\')");
        List<String> args=new ArrayList<>(java.util.Arrays.asList(term,term,term));
        if(starred)sql.append(" AND n.starred=1");
        if(read>0)sql.append(read==1?" AND n.is_read=0":" AND n.is_read=1");
        if(!from.isEmpty()){sql.append(" AND n.date>=?");args.add(from);}
        if(!to.isEmpty()){sql.append(" AND n.date<>'' AND n.date<=?");args.add(to);}
        if(!school.isEmpty()){sql.append(" AND s.school=?");args.add(school);}
        if(sourceId>0){sql.append(" AND s.id=?");args.add(Long.toString(sourceId));}
        return new Selection(sql.toString(),args);
    }
}
