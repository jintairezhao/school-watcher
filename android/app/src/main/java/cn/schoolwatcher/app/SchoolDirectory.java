package cn.schoolwatcher.app;

import org.json.JSONArray;
import org.json.JSONObject;
import java.text.Normalizer;
import java.util.Locale;
import java.util.ArrayList;
import java.util.List;

final class SchoolDirectory {
    static List<Source> presets(JSONObject entry) {
        List<Source> result=new ArrayList<>();
        JSONArray presets=entry.optJSONArray("presets");
        if(presets!=null) for(int i=0;i<presets.length();i++) {
            JSONObject preset=presets.optJSONObject(i);
            if(preset!=null) result.add(new Source(entry.optString("name"),preset.optString("name"),preset.optString("url")));
        }
        return result;
    }
    static boolean matches(JSONObject entry,String query) {
        String needle=normalize(query);
        if(needle.isEmpty()) return true;
        for(String field:new String[]{"name","province","url"}) {
            if(normalize(entry.optString(field)).contains(needle)) return true;
        }
        JSONArray aliases=entry.optJSONArray("aliases");
        if(aliases!=null) for(int i=0;i<aliases.length();i++) {
            if(normalize(aliases.optString(i)).contains(needle)) return true;
        }
        return false;
    }
    private static String normalize(String value) {
        return Normalizer.normalize(value,Normalizer.Form.NFKC).toLowerCase(Locale.ROOT).replaceAll("[\\s\\p{Z}()]","");
    }
}
