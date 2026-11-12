package cn.schoolwatcher.app;

import android.content.ContentValues;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import java.util.ArrayList;
import java.util.List;

/** A page and its continuation commit together; unrelated refresh rounds never reset it. */
final class HistoryJournal {
    private final WatcherStore store;
    HistoryJournal(WatcherStore store){this.store=store;}
    static void create(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE IF NOT EXISTS history_sources(source_id INTEGER PRIMARY KEY REFERENCES sources(id),next_url TEXT NOT NULL,pages INTEGER NOT NULL DEFAULT 0,target INTEGER NOT NULL DEFAULT 3,exhausted INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '')");
        db.execSQL("CREATE TABLE IF NOT EXISTS history_pages(source_id INTEGER NOT NULL REFERENCES sources(id),url TEXT NOT NULL,PRIMARY KEY(source_id,url))");
    }
    void prepare() {
        store.getWritableDatabase().execSQL("INSERT OR IGNORE INTO history_sources(source_id,next_url) SELECT id,url FROM sources WHERE enabled=1");
    }
    static final class Item {
        Source source;String next,error;int pages,target;boolean exhausted;
        boolean pending(){return !exhausted && pages<target;}
    }
    List<Item> items() {
        List<Item> result=new ArrayList<>();
        try(Cursor rows=store.getReadableDatabase().rawQuery("SELECT s.id,s.school,s.name,s.url,h.next_url,h.pages,h.target,h.exhausted,h.error FROM sources s JOIN history_sources h ON h.source_id=s.id WHERE s.enabled=1 ORDER BY s.school,s.name,s.id",null)) {
            while(rows.moveToNext()) {
                Item item=new Item();item.source=new Source(rows.getString(1),rows.getString(2),rows.getString(3));item.source.id=rows.getLong(0);
                item.next=rows.getString(4);item.pages=rows.getInt(5);item.target=rows.getInt(6);item.exhausted=rows.getInt(7)!=0;item.error=rows.getString(8);result.add(item);
            }
        }
        return result;
    }
    int requestMore(NoticeFilter filter) {
        SQLiteDatabase db=store.getWritableDatabase();db.beginTransaction();
        try {
            prepare();List<Item> selected=new ArrayList<>();
            for(Item item:items())if(filter.includes(item.source) && !item.exhausted)selected.add(item);
            boolean unfinished=selected.stream().anyMatch(Item::pending);
            if(!unfinished)for(Item item:selected)db.execSQL("UPDATE history_sources SET target=pages+5,error='' WHERE source_id=?",new Object[]{item.source.id});
            db.setTransactionSuccessful();return selected.size();
        }finally{db.endTransaction();}
    }
    int commit(Item item,List<Notice> notices,String next) {
        SQLiteDatabase db=store.getWritableDatabase();db.beginTransaction();
        try {
            try(Cursor check=db.rawQuery("SELECT 1 FROM history_sources h JOIN sources s ON s.id=h.source_id WHERE s.id=? AND s.enabled=1 AND h.next_url=? AND h.pages=? AND h.exhausted=0",new String[]{Long.toString(item.source.id),item.next,Integer.toString(item.pages)})) {
                if(!check.moveToFirst())return -1;
            }
            int added=store.merge(item.source.id,notices);
            db.execSQL("INSERT OR IGNORE INTO history_pages(source_id,url) VALUES(?,?)",new Object[]{item.source.id,item.next});
            boolean loop=false;
            if(!next.isEmpty())try(Cursor seen=db.rawQuery("SELECT 1 FROM history_pages WHERE source_id=? AND url=?",new String[]{Long.toString(item.source.id),next})){loop=seen.moveToFirst();}
            ContentValues values=new ContentValues();values.put("pages",item.pages+1);values.put("next_url",next);
            values.put("exhausted",next.isEmpty() || loop?1:0);values.put("error",loop?"分页链接重复，已停止补抓":"");
            db.update("history_sources",values,"source_id=?",new String[]{Long.toString(item.source.id)});
            db.setTransactionSuccessful();item.pages++;item.next=next;item.exhausted=next.isEmpty() || loop;return added;
        }finally{db.endTransaction();}
    }
    void failed(Item item,String message) {
        ContentValues values=new ContentValues();values.put("error",message);
        store.getWritableDatabase().update("history_sources",values,"source_id=? AND next_url=?",new String[]{Long.toString(item.source.id),item.next});
    }
}
