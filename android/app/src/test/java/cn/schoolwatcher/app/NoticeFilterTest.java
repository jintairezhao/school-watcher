package cn.schoolwatcher.app;
import android.app.Application;
import org.junit.*;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;
import java.util.List;
import static org.junit.Assert.*;

@RunWith(RobolectricTestRunner.class) @Config(sdk=28,application=Application.class)
public class NoticeFilterTest {
    WatcherStore store;long a,b,c;
    @Before public void setup() {
        RuntimeEnvironment.getApplication().deleteDatabase("watcher.db");store=new WatcherStore(RuntimeEnvironment.getApplication());
        a=store.subscribe(new Source("甲大学","教务","https://a.example.edu.cn/"));
        b=store.subscribe(new Source("甲大学","学工","https://b.example.edu.cn/"));
        c=store.subscribe(new Source("乙大学","教务","https://c.example.edu.cn/"));
        for(long id:new long[]{a,b,c})store.merge(id,List.of(
            new Notice("课程通知一","https://example.edu.cn/1","2026-09-01"),
            new Notice("课程通知二","https://example.edu.cn/2","2026-09-30"),
            new Notice("课程通知三","https://example.edu.cn/3","2026-10-01")));
    }
    @After public void close(){store.close();}
    @Test public void combinesInclusiveDatesSchoolColumnAndReadState() {
        NoticeFilter f=new NoticeFilter();f.school="甲大学";f.sourceId=b;f.from="2026-09-01";f.to="2026-09-30";
        List<Notice> rows=store.notices("课程",false,f,100);assertEquals(2,rows.size());
        store.setRead(rows.get(0).id,true);store.setStarred(rows.get(0).id,true);f.read=2;
        assertEquals(1,store.countNotices("课程",true,f));assertEquals("2026-09-30",store.notices("",true,f,100).get(0).date);
        f.read=1;assertEquals(1,store.countNotices("",false,f));
    }
    @Test public void countIsIndependentOfDisplayLimitAndOldestSortWorks() {
        NoticeFilter f=new NoticeFilter();f.oldestFirst=true;
        assertEquals(9,store.countNotices("",false,f));assertEquals(2,store.notices("",false,f,2).size());
        assertEquals("2026-09-01",store.notices("",false,f,2).get(0).date);
        assertEquals(0,store.countNotices("%",false,f));
    }
    @Test public void invalidDatesRejectedAndSavedChoicesRestore() {
        NoticeFilter f=new NoticeFilter();f.from="2026-09-30";f.to="2026-09-01";assertFalse(f.valid());
        f.to="2026-10-01";f.school="甲大学";f.sourceId=b;f.read=2;f.oldestFirst=true;
        assertEquals(f.save(),NoticeFilter.restore(f.save()).save());f.from="2026-02-30";assertFalse(f.valid());
    }
}
