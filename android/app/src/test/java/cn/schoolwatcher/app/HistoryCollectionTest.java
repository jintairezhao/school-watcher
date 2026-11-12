package cn.schoolwatcher.app;
import android.app.Application;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;
import java.util.List;
import java.util.ArrayList;
import java.util.concurrent.atomic.AtomicBoolean;
import static org.junit.Assert.*;

@RunWith(RobolectricTestRunner.class) @Config(sdk=28,application=Application.class)
public class HistoryCollectionTest {
    WatcherStore store;HistoryJournal journal;long source;
    String base="https://example.edu.cn/notices/";
    @Before public void setup(){RuntimeEnvironment.getApplication().deleteDatabase("watcher.db");reopen();source=store.subscribe(new Source("测试大学","教务通知",base));}
    void reopen(){if(store!=null)store.close();store=new WatcherStore(RuntimeEnvironment.getApplication());journal=new HistoryJournal(store);}
    @After public void close(){store.close();}
    String page(String url) {
        int p=url.endsWith("/")?1:Integer.parseInt(url.substring(url.lastIndexOf('_')+1,url.lastIndexOf('.')));
        return "<ul><li><a href='/notice/"+p+"'>关于历史课程安排的通知第"+p+"号</a><span>2026-09-"+String.format("%02d",20-p)+"</span></li></ul>"
                +(p<6?"<a href='index_"+(p+1)+".shtml'>下一页</a>":"");
    }
    @Test public void firstCollectionLoadsThreePagesThenMoreContinuesWithoutDuplicates() {
        HistoryCollection.Outcome first=HistoryCollection.run(journal,()->true,this::page,p->{});
        assertEquals(3,first.pages);assertEquals(3,store.countNotices("",false,new NoticeFilter()));
        Notice saved=store.notices("",false,false,100).get(0);store.setStarred(saved.id,true);store.saveArticle(saved.id,"<p>已保存正文</p>","正文");
        assertEquals(0,HistoryCollection.run(journal,()->true,url->{fail("Bootstrap must not repeat");return "";},p->{}).pages);
        journal.requestMore(new NoticeFilter());journal.requestMore(new NoticeFilter());assertEquals(8,journal.items().get(0).target);
        HistoryCollection.Outcome more=HistoryCollection.run(journal,()->true,this::page,p->{});
        assertEquals(3,more.pages);assertEquals(6,store.countNotices("",false,new NoticeFilter()));assertTrue(journal.items().get(0).exhausted);
        assertTrue(store.notice(saved.id).starred);assertEquals("<p>已保存正文</p>",store.articleHtml(saved.id));
    }
    @Test public void interruptionReplaysOnlyUncommittedPageAfterDatabaseReopen() {
        AtomicBoolean allowed=new AtomicBoolean(true);
        HistoryCollection.Outcome partial=HistoryCollection.run(journal,allowed::get,url->{if(url.contains("index_2"))allowed.set(false);return page(url);},p->{});
        assertTrue(partial.paused);assertEquals(1,journal.items().get(0).pages);reopen();List<String> requested=new ArrayList<>();
        HistoryCollection.run(journal,()->true,url->{requested.add(url);return page(url);},p->{});
        assertEquals(List.of(base+"index_2.shtml",base+"index_3.shtml"),requested);assertEquals(3,store.countNotices("",false,new NoticeFilter()));
    }
    @Test public void failureDoesNotSkipPageAndRetryKeepsSavedNotifications() {
        HistoryCollection.Outcome result=HistoryCollection.run(journal,()->true,url->{if(url.contains("index_2"))throw new java.io.IOException("测试断网");return page(url);},p->{});
        assertEquals(1,result.failed);assertEquals(1,journal.items().get(0).pages);assertFalse(journal.items().get(0).error.isEmpty());
        HistoryCollection.run(journal,()->true,this::page,p->{});assertEquals(3,journal.items().get(0).pages);assertEquals("",journal.items().get(0).error);
    }
    @Test public void noNetworkPermissionAndPausedSourcesCannotAdvanceHistory() {
        HistoryCollection.run(journal,()->false,url->{fail();return "";},p->{});assertTrue(journal.items().isEmpty());
        HistoryCollection.run(journal,()->true,url->{store.setEnabled(source,false);return page(url);},p->{});
        assertEquals(0,store.countNotices("",false,new NoticeFilter()));
    }
    @Test public void repeatedPaginationLinkStopsWithoutLooping() {
        HistoryCollection.Outcome result=HistoryCollection.run(journal,()->true,url->page(url).replace("index_3.shtml","https://example.edu.cn/notices/"),p->{});
        assertEquals(2,result.pages);assertTrue(journal.items().get(0).exhausted);
    }
    @Test public void archiveAndPageCursorRollbackTogetherOnStorageFailure() {
        journal.prepare();HistoryJournal.Item item=journal.items().get(0);
        store.getWritableDatabase().execSQL("CREATE TRIGGER fail_history BEFORE UPDATE ON history_sources BEGIN SELECT RAISE(ABORT,'test disk failure'); END");
        assertThrows(android.database.sqlite.SQLiteException.class,()->journal.commit(item,NoticeParser.notices(page(base),base),base+"index_2.shtml"));
        assertEquals(0,store.countNotices("",false,new NoticeFilter()));assertEquals(0,journal.items().get(0).pages);
    }
}
