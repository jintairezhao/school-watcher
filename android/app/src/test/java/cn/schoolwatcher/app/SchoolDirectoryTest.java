package cn.schoolwatcher.app;

import android.app.Application;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import static org.junit.Assert.*;

@RunWith(RobolectricTestRunner.class)
@Config(sdk=28,application=Application.class)
public class SchoolDirectoryTest {
    private JSONArray schools;
    private final String campus="中国石油大学（北京）克拉玛依校区";
    @Before public void loadPackagedDirectory() throws Exception {
        try(java.io.InputStream input=RuntimeEnvironment.getApplication().getAssets().open("schools.json")) {
            schools=new JSONArray(new String(input.readAllBytes(),StandardCharsets.UTF_8));
        }
    }
    private List<JSONObject> find(String query) throws Exception {
        List<JSONObject> matches=new ArrayList<>();
        for(int i=0;i<schools.length();i++) if(SchoolDirectory.matches(schools.getJSONObject(i),query)) matches.add(schools.getJSONObject(i));
        return matches;
    }
    @Test public void campusAndUserAbbreviationArePresentInPackagedDirectory() throws Exception {
        for(String query:new String[]{"中石克","克拉玛依","中石大克拉玛依","CUPK.EDU.CN"}) {
            List<JSONObject> results=find(query);assertEquals(query,1,results.size());
            assertEquals(campus,results.get(0).getString("name"));assertEquals("https://www.cupk.edu.cn/",results.get(0).getString("url"));
            assertEquals("新疆",results.get(0).getString("province"));
        }
    }
    @Test public void namesAcceptBothParenthesesStylesAndSpacing() throws Exception {
        for(String query:new String[]{campus,"中国石油大学(北京)克拉玛依校区"," 中国石油大学 北京 克拉玛依校区 ","中国石油大学克拉玛依校区"}) {
            assertEquals(campus,find(query).get(0).getString("name"));
        }
    }
    @Test public void campusDoesNotReplaceMainUniversityAndExistingAbbreviationsWork() throws Exception {
        List<JSONObject> results=find("中国石油大学");
        JSONObject main=results.stream().filter(entry->entry.optString("name").equals("中国石油大学（北京）")).findFirst().orElseThrow();
        assertEquals("https://www.cup.edu.cn",main.getString("url"));
        assertTrue(results.stream().anyMatch(entry->entry.optString("name").equals(campus)));
        assertTrue(find("北大").stream().anyMatch(entry->entry.optString("name").equals("北京大学")));
    }
    @Test public void officialPresetColumnsShipInTheActualAssetWithoutDuplicateUrls() throws Exception {
        List<Source> presets=SchoolDirectory.presets(find("中石克").get(0));
        assertEquals(42,presets.size());
        java.util.Set<String> urls=new java.util.HashSet<>();
        for(Source source:presets) {
            assertEquals(campus,source.school);assertFalse(source.name.isBlank());
            assertTrue(source.url.startsWith("https://www.cupk.edu.cn/"));assertTrue(urls.add(source.url));
        }
        for(String path:new String[]{"tzgg/","jwb/tzgg/","xgybw/tzgg/tzgg/","xszz/jzxj/","yjsb/tzgg/","jdxy/tzgg/"})
            assertTrue(urls.contains("https://www.cupk.edu.cn/"+path));
    }
}
