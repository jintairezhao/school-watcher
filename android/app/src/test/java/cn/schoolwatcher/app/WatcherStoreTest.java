package cn.schoolwatcher.app;

import android.app.Application;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;
import java.util.List;
import static org.junit.Assert.*;

@RunWith(RobolectricTestRunner.class)
@Config(sdk=28,application=Application.class)
public class WatcherStoreTest {
    private WatcherStore store;
    private long source;
    @Before public void setup() {
        RuntimeEnvironment.getApplication().deleteDatabase("watcher.db");
        store=new WatcherStore(RuntimeEnvironment.getApplication());
        source=store.subscribe(new Source("测试大学","教务通知","https://example.edu.cn/notices/"));
    }
    @After public void teardown(){store.close();}
    @Test public void refreshPreservesFlagsBodyAndRemovedNotices() {
        Notice a=new Notice("关于测试课程安排的通知","https://example.edu.cn/a.html","2026-09-30");
        Notice b=new Notice("关于奖学金申请事项的通知","https://example.edu.cn/b.html","2026-10-01");
        assertEquals(2,store.merge(source,List.of(a,b)));
        long id=store.notices("课程",false,false,100).get(0).id;
        store.setRead(id,true);store.setStarred(id,true);store.saveBody(id,"已缓存正文");
        assertEquals(0,store.merge(source,List.of(a)));
        assertEquals(2,store.notices("",false,false,100).size());
        Notice saved=store.notice(id);assertTrue(saved.read);assertTrue(saved.starred);assertEquals("已缓存正文",store.body(id));
        assertEquals(0,store.merge(source,List.of()));
        assertEquals(2,store.notices("",false,false,100).size());
    }
    @Test public void pauseAndResubscribeDoNotDeleteOrDuplicateAnything() {
        store.merge(source,List.of(new Notice("关于测试课程安排的通知","https://example.edu.cn/a.html","2026-10-01")));
        store.setEnabled(source,false);assertTrue(store.sources(true).isEmpty());
        assertEquals(1,store.notices("",false,false,100).size());
        assertEquals(source,store.subscribe(new Source("测试大学","教务通知","https://example.edu.cn/notices/")));
        assertEquals(1,store.sources(true).size());assertEquals(1,store.sources().size());
    }
    @Test public void cleanupAndSearchRespectPersonalDataAndLiteralWildcards() {
        store.merge(source,List.of(new Notice("完成率达到100%的课程通知","https://example.edu.cn/a.html","2026-10-01"),new Notice("普通课程通知内容","https://example.edu.cn/b.html","2026-10-02")));
        Notice notice=store.notices("%",false,false,100).get(0);assertEquals(1,store.notices("%",false,false,100).size());
        store.setStarred(notice.id,true);store.setRead(notice.id,true);store.saveBody(notice.id,"缓存");store.clearBodies();
        assertEquals("",store.body(notice.id));assertTrue(store.notice(notice.id).starred);assertTrue(store.notice(notice.id).read);
        assertEquals(1,store.notices("",false,true,100).size());
    }
    @Test public void subscribingPresetBatchTwicePreservesBookmarksAndHasNoDuplicateSources() {
        store.merge(source,List.of(new Notice("关于测试课程安排的通知","https://example.edu.cn/a.html","2026-10-01")));
        Notice saved=store.notices("",false,false,100).get(0);
        store.setStarred(saved.id,true);store.saveBody(saved.id,"保留的离线正文");store.setEnabled(source,false);
        List<Source> batch=List.of(new Source("测试大学","教务通知","https://example.edu.cn/notices/"),
                new Source("测试大学","学工通知","https://example.edu.cn/student/"));
        assertEquals(2,store.subscribeAll(batch));assertEquals(2,store.subscribeAll(batch));
        assertEquals(2,store.sources(true).size());assertEquals(2,store.sources().size());
        assertEquals(1,store.notices("",true,false,100).size());assertEquals("保留的离线正文",store.body(saved.id));
    }
}
