package cn.schoolwatcher.app;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;
import java.util.ArrayList;
import java.util.List;

/** Additive local archive: refresh never replaces personal flags or deletes old notices. */
public final class WatcherStore extends SQLiteOpenHelper {
    public WatcherStore(Context context) { super(context, "watcher.db", null, 3); }

    @Override public void onConfigure(SQLiteDatabase db) { db.setForeignKeyConstraintsEnabled(true); }
    @Override public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE sources(id INTEGER PRIMARY KEY,school TEXT NOT NULL,name TEXT NOT NULL,url TEXT NOT NULL UNIQUE,enabled INTEGER NOT NULL DEFAULT 1,checked INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '')");
        db.execSQL("CREATE TABLE notices(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL REFERENCES sources(id),title TEXT NOT NULL,url TEXT NOT NULL,date TEXT NOT NULL DEFAULT '',body TEXT NOT NULL DEFAULT '',is_read INTEGER NOT NULL DEFAULT 0,starred INTEGER NOT NULL DEFAULT 0,created INTEGER NOT NULL,UNIQUE(source_id,url))");
        db.execSQL("CREATE INDEX notices_order ON notices(date DESC,created DESC)");
        RefreshJournal.create(db);
        HistoryJournal.create(db);
        db.execSQL("ALTER TABLE notices ADD COLUMN article_html TEXT NOT NULL DEFAULT ''");
    }
    @Override public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        if (oldVersion < 2) RefreshJournal.create(db);
        if (oldVersion < 3) {
            HistoryJournal.create(db);
            boolean present=false;
            try(Cursor columns=db.rawQuery("PRAGMA table_info(notices)",null)) {
                while(columns.moveToNext())if("article_html".equals(columns.getString(1)))present=true;
            }
            if(!present)db.execSQL("ALTER TABLE notices ADD COLUMN article_html TEXT NOT NULL DEFAULT ''");
        }
    }

    public long subscribe(Source source) {
        ContentValues values = new ContentValues();
        values.put("school", source.school); values.put("name", source.name); values.put("url", source.url);
        SQLiteDatabase db = getWritableDatabase();
        long id = db.insertWithOnConflict("sources", null, values, SQLiteDatabase.CONFLICT_IGNORE);
        if (id != -1) return id;
        try (Cursor row = db.rawQuery("SELECT id FROM sources WHERE url=?", new String[]{source.url})) {
            if (!row.moveToFirst()) throw new IllegalStateException("Source could not be saved");
            id = row.getLong(0);
        }
        setEnabled(id, true);
        return id;
    }

    public int subscribeAll(List<Source> sources) {
        SQLiteDatabase db=getWritableDatabase();
        db.beginTransaction();
        try {
            for(Source source:sources) subscribe(source);
            db.setTransactionSuccessful();
            return sources.size();
        } finally {db.endTransaction();}
    }

    public List<Source> sources() { return sources(false); }

    public List<Source> sources(boolean enabledOnly) {
        List<Source> result = new ArrayList<>();
        try (Cursor rows = getReadableDatabase().rawQuery("SELECT id,school,name,url,enabled,checked,error FROM sources" + (enabledOnly ? " WHERE enabled=1" : "") + " ORDER BY school,name", null)) {
            while (rows.moveToNext()) {
                Source source = new Source(rows.getString(1), rows.getString(2), rows.getString(3));
                source.id = rows.getLong(0); source.enabled = rows.getInt(4) != 0;
                source.checked = rows.getLong(5); source.error = rows.getString(6); result.add(source);
            }
        }
        return result;
    }

    public void setEnabled(long id, boolean enabled) {
        ContentValues values = new ContentValues(); values.put("enabled", enabled ? 1 : 0);
        getWritableDatabase().update("sources", values, "id=?", new String[]{Long.toString(id)});
    }

    public void recordCheck(long id, String error) {
        ContentValues values = new ContentValues(); values.put("checked", System.currentTimeMillis()); values.put("error", error);
        getWritableDatabase().update("sources", values, "id=?", new String[]{Long.toString(id)});
    }

    public int merge(long sourceId, List<Notice> notices) {
        SQLiteDatabase db = getWritableDatabase(); int added = 0;
        db.beginTransaction();
        try {
            for (Notice notice : notices) {
                ContentValues values = new ContentValues();
                values.put("source_id", sourceId); values.put("title", notice.title); values.put("url", notice.url);
                values.put("date", notice.date); values.put("created", System.currentTimeMillis());
                if (db.insertWithOnConflict("notices", null, values, SQLiteDatabase.CONFLICT_IGNORE) != -1) added++;
                else {
                    // Update publisher metadata only; never overwrite content, read or bookmark state.
                    ContentValues metadata = new ContentValues(); metadata.put("title", notice.title);
                    if (!notice.date.isEmpty()) metadata.put("date", notice.date);
                    db.update("notices", metadata, "source_id=? AND url=?", new String[]{Long.toString(sourceId), notice.url});
                }
            }
            db.setTransactionSuccessful();
        } finally { db.endTransaction(); }
        return added;
    }

    public List<Notice> notices(String query, boolean starred, boolean unread, int limit) {
        NoticeFilter filter=new NoticeFilter();filter.read=unread?1:0;
        return notices(query,starred,filter,limit);
    }
    public int countNotices(String query,boolean starred,NoticeFilter filter) {
        NoticeFilter.Selection selection=filter.selection(query,starred);
        try(Cursor row=getReadableDatabase().rawQuery("SELECT COUNT(*) FROM notices n JOIN sources s ON s.id=n.source_id WHERE "+selection.sql,selection.args)) {
            row.moveToFirst();return row.getInt(0);
        }
    }
    public List<Notice> notices(String query,boolean starred,NoticeFilter filter,int limit) {
        NoticeFilter.Selection selection=filter.selection(query,starred);
        String sql = "SELECT n.id,n.source_id,n.title,n.url,n.date,n.is_read,n.starred,s.school,s.name FROM notices n JOIN sources s ON s.id=n.source_id WHERE "+selection.sql
                + " ORDER BY n.date "+(filter.oldestFirst?"ASC":"DESC")+",n.created DESC,n.id DESC LIMIT ?";
        String[] args=java.util.Arrays.copyOf(selection.args,selection.args.length+1);args[args.length-1]=Integer.toString(limit);
        List<Notice> result = new ArrayList<>();
        try (Cursor rows = getReadableDatabase().rawQuery(sql, args)) {
            while (rows.moveToNext()) {
                Notice notice = new Notice(rows.getString(2), rows.getString(3), rows.getString(4));
                notice.id=rows.getLong(0); notice.sourceId=rows.getLong(1); notice.read=rows.getInt(5)!=0;
                notice.starred=rows.getInt(6)!=0; notice.school=rows.getString(7); notice.source=rows.getString(8);
                result.add(notice);
            }
        }
        return result;
    }

    public String body(long id) {
        try (Cursor row = getReadableDatabase().rawQuery("SELECT body FROM notices WHERE id=?", new String[]{Long.toString(id)})) {
            return row.moveToFirst() ? row.getString(0) : "";
        }
    }

    public Notice notice(long id) {
        try (Cursor row=getReadableDatabase().rawQuery("SELECT n.id,n.source_id,n.title,n.url,n.date,n.is_read,n.starred,s.school,s.name FROM notices n JOIN sources s ON s.id=n.source_id WHERE n.id=?",new String[]{Long.toString(id)})) {
            if(!row.moveToFirst()) return null;
            Notice notice=new Notice(row.getString(2),row.getString(3),row.getString(4));
            notice.id=row.getLong(0);notice.sourceId=row.getLong(1);notice.read=row.getInt(5)!=0;notice.starred=row.getInt(6)!=0;
            notice.school=row.getString(7);notice.source=row.getString(8);return notice;
        }
    }

    public void saveBody(long id, String body) {
        ContentValues values = new ContentValues(); values.put("body", body);
        getWritableDatabase().update("notices", values, "id=?", new String[]{Long.toString(id)});
    }
    public String articleHtml(long id) {
        try(Cursor row=getReadableDatabase().rawQuery("SELECT article_html FROM notices WHERE id=?",new String[]{Long.toString(id)})) {
            return row.moveToFirst()?row.getString(0):"";
        }
    }
    public void saveArticle(long id,String html,String plain) {
        ContentValues values=new ContentValues();values.put("article_html",html);values.put("body",plain);
        getWritableDatabase().update("notices",values,"id=?",new String[]{Long.toString(id)});
    }

    public void setRead(long id, boolean read) { flag(id, "is_read", read); }
    public void setStarred(long id, boolean starred) { flag(id, "starred", starred); }
    private void flag(long id, String column, boolean value) {
        ContentValues values = new ContentValues(); values.put(column, value ? 1 : 0);
        getWritableDatabase().update("notices", values, "id=?", new String[]{Long.toString(id)});
    }
    public void clearBodies() { getWritableDatabase().execSQL("UPDATE notices SET body='',article_html='' WHERE body<>'' OR article_html<>''"); }
}
