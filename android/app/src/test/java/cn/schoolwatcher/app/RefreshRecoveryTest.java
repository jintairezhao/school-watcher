package cn.schoolwatcher.app;

import android.app.Application;
import android.database.sqlite.SQLiteException;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;
import static org.junit.Assert.*;

@RunWith(RobolectricTestRunner.class)
@Config(sdk=28,application=Application.class)
public class RefreshRecoveryTest {
    private WatcherStore store;
    private RefreshJournal journal;
    private final long now=1_800_000_000_000L;
    private long first,second;
    @Before public void setup() {
        RuntimeEnvironment.getApplication().deleteDatabase("watcher.db");
        store=new WatcherStore(RuntimeEnvironment.getApplication());journal=new RefreshJournal(store);
        first=store.subscribe(new Source("测试大学","A 教务","https://example.edu.cn/a/"));
        second=store.subscribe(new Source("测试大学","B 学生","https://example.edu.cn/b/"));
    }
    @After public void teardown() {store.close();}
    private List<Notice> notices(long id) {return List.of(new Notice("关于课程安排的测试通知","https://example.edu.cn/notice/"+id,"2026-10-03"));}
    private void checkpointFirst() {
        RefreshJournal.Snapshot round=journal.prepare(now,0);
        assertTrue(journal.complete(round.generation,first,notices(first),"",now+1));
    }
    private void reopen() {store.close();store=new WatcherStore(RuntimeEnvironment.getApplication());journal=new RefreshJournal(store);}
    @Test public void restartBeforeIntervalFetchesOnlyUnfinishedSources() {
        checkpointFirst();reopen();List<Long> visited=new ArrayList<>();
        RefreshCollection.Outcome outcome=RefreshCollection.run(journal,0,()->now+1000,()->true,source->{visited.add(source.id);return notices(source.id);},state->{});
        assertEquals(List.of(second),visited);assertEquals(now,outcome.snapshot.started);
        assertEquals(2,outcome.snapshot.checked);assertEquals(1,outcome.added);assertFalse(outcome.paused);
    }
    @Test public void expiredRoundStartsAllSourcesAndPreservesArchiveFlags() {
        checkpointFirst();Notice saved=store.notices("",false,false,100).get(0);store.setStarred(saved.id,true);store.setRead(saved.id,true);store.saveBody(saved.id,"缓存正文");
        reopen();List<Long> visited=new ArrayList<>();long later=now+RefreshJournal.INTERVAL;
        RefreshCollection.Outcome outcome=RefreshCollection.run(journal,0,()->later,()->true,source->{visited.add(source.id);return notices(source.id);},state->{});
        assertEquals(List.of(first,second),visited);assertEquals(later,outcome.snapshot.started);assertEquals(2,outcome.snapshot.generation);
        assertTrue(store.notice(saved.id).read);assertTrue(store.notice(saved.id).starred);assertEquals("缓存正文",store.body(saved.id));assertEquals(2,store.notices("",false,false,100).size());
    }
    @Test public void permissionRevokedDuringRequestLeavesThatSourcePending() {
        AtomicBoolean permitted=new AtomicBoolean(true);
        RefreshCollection.Outcome stopped=RefreshCollection.run(journal,0,()->now,permitted::get,source->{permitted.set(false);return notices(source.id);},state->{});
        assertTrue(stopped.paused);assertEquals(0,stopped.snapshot.checked);assertTrue(store.notices("",false,false,100).isEmpty());
        reopen();List<Long> visited=new ArrayList<>();
        RefreshCollection.run(journal,0,()->now+1,()->true,source->{visited.add(source.id);return notices(source.id);},state->{});
        assertEquals(List.of(first,second),visited);
    }
    @Test public void noAuthorizationDoesNotEvenStartRound() {
        RefreshCollection.Outcome stopped=RefreshCollection.run(journal,0,()->now,()->false,source->{fail("Network should not run");return notices(source.id);},state->{});
        assertTrue(stopped.paused);assertEquals(0,journal.snapshot().started);
    }
    @Test public void finishedRoundWaitsUntilDueButManualRefreshCanStartAgain() {
        RefreshCollection.run(journal,0,()->now,()->true,source->notices(source.id),state->{});
        RefreshCollection.run(journal,0,()->now+1,()->true,source->{fail("Already complete");return notices(source.id);},state->{});
        assertEquals(1,journal.snapshot().generation);
        List<Long> visited=new ArrayList<>();
        RefreshCollection.run(journal,now+2,()->now+2,()->true,source->{visited.add(source.id);return notices(source.id);},state->{});
        assertEquals(List.of(first,second),visited);assertEquals(2,journal.snapshot().generation);
    }
    @Test public void failedSourceIsRecordedAndDoesNotBlockRemainingSources() {
        RefreshCollection.Outcome result=RefreshCollection.run(journal,0,()->now,()->true,source->{if(source.id==first)throw new java.net.SocketTimeoutException();return notices(source.id);},state->{});
        assertEquals(2,result.snapshot.checked);assertEquals(1,result.snapshot.failed);assertEquals(1,result.added);
        assertTrue(store.sources().get(0).error.contains("超时"));
    }
    @Test public void pausedAndNewSubscriptionsAreReconciledOnResume() {
        checkpointFirst();store.setEnabled(second,false);
        long third=store.subscribe(new Source("测试大学","C 新栏目","https://example.edu.cn/c/"));
        List<Long> visited=new ArrayList<>();
        RefreshCollection.run(journal,0,()->now+1,()->true,source->{visited.add(source.id);return notices(source.id);},state->{});
        assertEquals(List.of(third),visited);store.setEnabled(second,true);
        assertEquals(second,journal.prepare(now+2,0).pending.get(0).id);
    }
    @Test public void sourcePausedDuringRequestCannotCommit() {
        RefreshCollection.run(journal,0,()->now,()->true,source->{store.setEnabled(source.id,false);return notices(source.id);},state->{});
        assertTrue(store.notices("",false,false,100).isEmpty());store.setEnabled(first,true);
        assertEquals(first,journal.snapshot().pending.get(0).id);
    }
    @Test public void staleCompletionCannotModifyNewRound() {
        long old=journal.prepare(now,0).generation;
        journal.prepare(now+RefreshJournal.INTERVAL,0);
        assertFalse(journal.complete(old,first,notices(first),"",now+RefreshJournal.INTERVAL+1));
        assertTrue(store.notices("",false,false,100).isEmpty());assertEquals(0,journal.snapshot().checked);
    }
    @Test public void archiveAndProgressRollbackTogetherOnStorageFailure() {
        long round=journal.prepare(now,0).generation;
        store.getWritableDatabase().execSQL("CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON refresh_items BEGIN SELECT RAISE(ABORT,'simulated disk failure'); END");
        assertThrows(SQLiteException.class,()->journal.complete(round,first,notices(first),"",now));
        assertTrue(store.notices("",false,false,100).isEmpty());assertEquals(0,store.sources().get(0).checked);assertEquals(0,journal.snapshot().checked);
    }
    @Test public void versionOneUpgradePreservesPersonalData() {
        store.merge(first,notices(first));long id=store.notices("",false,false,100).get(0).id;store.setStarred(id,true);store.saveBody(id,"旧版正文");
        store.getWritableDatabase().execSQL("DROP TABLE refresh_items");store.getWritableDatabase().execSQL("DROP TABLE refresh_round");store.getWritableDatabase().setVersion(1);
        reopen();assertEquals(3,store.getReadableDatabase().getVersion());assertEquals(2,store.sources().size());assertTrue(store.notice(id).starred);assertEquals("旧版正文",store.body(id));
        assertEquals(2,journal.prepare(now,0).pending.size());
    }
    @Test public void clockMovingBackwardsStartsNewRound() {
        checkpointFirst();assertEquals(2,journal.prepare(now-1000,0).generation);
    }
    @Test public void replayAfterFinalCommitDoesNotRestartCompletedManualRequest() {
        RefreshCollection.run(journal,now,()->now+1,()->true,source->notices(source.id),state->{});
        reopen();
        RefreshCollection.run(journal,now,()->now+1000,()->true,source->{fail("Replayed request must retain completed checkpoints");return notices(source.id);},state->{});
        assertEquals(1,journal.snapshot().generation);
    }
}
