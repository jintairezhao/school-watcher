package cn.schoolwatcher.app;

import android.content.ContentValues;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

/** The archive and its checkpoint commit together, including after process death. */
final class RefreshJournal {
    static final long INTERVAL = TimeUnit.HOURS.toMillis(1);
    private final WatcherStore store;
    RefreshJournal(WatcherStore store) { this.store=store; }

    static void create(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE refresh_round(id INTEGER PRIMARY KEY CHECK(id=1),generation INTEGER NOT NULL,started INTEGER NOT NULL)");
        db.execSQL("CREATE TABLE refresh_items(source_id INTEGER PRIMARY KEY REFERENCES sources(id),done INTEGER NOT NULL DEFAULT 0,added INTEGER NOT NULL DEFAULT 0,failed INTEGER NOT NULL DEFAULT 0)");
    }
    static final class Snapshot {
        long generation, started;
        int checked, total, added, failed;
        final List<Source> pending = new ArrayList<>();
        boolean due(long now) { return started==0 || now<started || now-started>=INTERVAL; }
    }

    Snapshot snapshot() {
        SQLiteDatabase db=store.getReadableDatabase();
        Snapshot result=new Snapshot();
        try(Cursor row=db.rawQuery("SELECT generation,started FROM refresh_round WHERE id=1",null)) {
            if(row.moveToFirst()) {result.generation=row.getLong(0);result.started=row.getLong(1);}
        }
        // Include newly subscribed/resumed sources, while ignoring paused sources.
        try(Cursor rows=db.rawQuery("SELECT s.id,s.school,s.name,s.url,COALESCE(i.done,0),COALESCE(i.added,0),COALESCE(i.failed,0) FROM sources s LEFT JOIN refresh_items i ON i.source_id=s.id WHERE s.enabled=1 ORDER BY s.school,s.name,s.id",null)) {
            while(rows.moveToNext()) {
                result.total++; result.added+=rows.getInt(5);result.failed+=rows.getInt(6);
                if(rows.getInt(4)!=0) {result.checked++;continue;}
                Source source=new Source(rows.getString(1),rows.getString(2),rows.getString(3));source.id=rows.getLong(0);result.pending.add(source);
            }
        }
        return result;
    }

    Snapshot prepare(long now,long manualRequestedAt) {
        SQLiteDatabase db=store.getWritableDatabase();db.beginTransaction();
        try {
            Snapshot previous=snapshot();
            // A replayed WorkManager request must not restart a round it already finished.
            if(previous.total>0 && (previous.due(now) || (manualRequestedAt>previous.started && previous.pending.isEmpty()))) {
                db.delete("refresh_items",null,null);
                ContentValues values=new ContentValues();values.put("id",1);values.put("generation",previous.generation+1);values.put("started",now);
                db.insertWithOnConflict("refresh_round",null,values,SQLiteDatabase.CONFLICT_REPLACE);
            }
            db.execSQL("INSERT OR IGNORE INTO refresh_items(source_id) SELECT id FROM sources WHERE enabled=1");
            Snapshot result=snapshot();db.setTransactionSuccessful();return result;
        } finally {db.endTransaction();}
    }

    boolean complete(long generation,long sourceId,List<Notice> notices,String error,long now) {
        SQLiteDatabase db=store.getWritableDatabase();db.beginTransaction();
        try {
            // Reject stale workers and sources paused while a network request was in flight.
            try(Cursor row=db.rawQuery("SELECT 1 FROM refresh_round r JOIN refresh_items i JOIN sources s ON s.id=i.source_id WHERE r.id=1 AND r.generation=? AND i.source_id=? AND i.done=0 AND s.enabled=1",new String[]{Long.toString(generation),Long.toString(sourceId)})) {
                if(!row.moveToFirst()) return false;
            }
            int added=error.isEmpty()?store.merge(sourceId,notices):0;
            ContentValues check=new ContentValues();check.put("checked",now);check.put("error",error);
            db.update("sources",check,"id=?",new String[]{Long.toString(sourceId)});
            ContentValues progress=new ContentValues();progress.put("done",1);progress.put("added",added);progress.put("failed",error.isEmpty()?0:1);
            db.update("refresh_items",progress,"source_id=?",new String[]{Long.toString(sourceId)});
            db.setTransactionSuccessful();return true;
        } finally {db.endTransaction();}
    }
}
