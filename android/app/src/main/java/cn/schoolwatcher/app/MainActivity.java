package cn.schoolwatcher.app;

import android.Manifest;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import androidx.activity.OnBackPressedCallback;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.appcompat.app.AppCompatDelegate;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.recyclerview.widget.LinearLayoutManager;
import androidx.recyclerview.widget.RecyclerView;
import androidx.work.WorkInfo;
import androidx.work.WorkManager;
import com.google.android.material.appbar.MaterialToolbar;
import com.google.android.material.bottomnavigation.BottomNavigationView;
import com.google.android.material.button.MaterialButton;
import com.google.android.material.color.MaterialColors;
import com.google.android.material.dialog.MaterialAlertDialogBuilder;
import com.google.android.material.navigation.NavigationBarView;
import com.google.android.material.navigationrail.NavigationRailView;
import com.google.android.material.progressindicator.LinearProgressIndicator;
import com.google.android.material.snackbar.Snackbar;
import com.google.android.material.materialswitch.MaterialSwitch;
import com.google.android.material.textfield.TextInputEditText;
import com.google.android.material.textfield.TextInputLayout;
import org.json.JSONArray;
import org.json.JSONObject;
import java.nio.charset.StandardCharsets;
import java.text.DateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.function.Consumer;

public final class MainActivity extends AppCompatActivity {
    private WatcherStore store;
    private LinearLayout root, page;
    private MaterialToolbar toolbar;
    private BottomNavigationView bottomNavigation;
    private TextView status;
    private TextView checkpoint;
    private TextView resultCount;
    private MaterialButton historyButton;
    private android.webkit.WebView readerView;
    private NoticeFilter filter=new NoticeFilter(),filterDraft=new NoticeFilter();
    private LinearProgressIndicator progress;
    private RowAdapter adapter;
    private TextView empty;
    private MaterialButton more;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final List<JSONObject> schools = new ArrayList<>();
    private final ArrayList<String> exploreHistory = new ArrayList<>();
    private int tab, generation, limit=100;
    private String route="tabs", query="", school="", sourceName="", sourceUrl="", lastFinished="";
    private boolean refreshing;
    private Notice selected;
    private long restoreNoticeId;
    private OnBackPressedCallback back;
    private boolean networkWatching,automaticNetworkReady;
    private android.net.Network lastAutomaticNetwork;
    private final android.net.ConnectivityManager.NetworkCallback networkChanges=new android.net.ConnectivityManager.NetworkCallback() {
        @Override public void onCapabilitiesChanged(@NonNull android.net.Network network,@NonNull android.net.NetworkCapabilities capabilities) {
            if(!networkWatching) return;
            boolean ready=AutomaticNetwork.accepts(prefs().getBoolean("wifi",true),capabilities);
            if(ready && (!automaticNetworkReady || !network.equals(lastAutomaticNetwork))) RefreshWorker.resume(MainActivity.this);
            automaticNetworkReady=ready;
            lastAutomaticNetwork=network;
        }
        @Override public void onLost(@NonNull android.net.Network network) {
            if(network.equals(lastAutomaticNetwork)) {automaticNetworkReady=false;lastAutomaticNetwork=null;}
        }
    };
    private final ActivityResultLauncher<String> permission = registerForActivityResult(new ActivityResultContracts.RequestPermission(),granted -> {
        prefs().edit().putBoolean("notify",granted).apply();
        if(!granted) feedback("通知权限未开启，仍可在应用内查看更新");
        render();
    });

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        store=((WatcherApplication)getApplication()).store();
        filter=NoticeFilter.restore(prefs().getString("noticeFilters","{}"));filterDraft=filter.copy();
        if(state!=null) {
            tab=state.getInt("tab"); route=state.getString("route","tabs"); query=state.getString("query","");
            school=state.getString("school",""); sourceName=state.getString("sourceName",""); sourceUrl=state.getString("sourceUrl","");
            filterDraft=NoticeFilter.restore(state.getString("filterDraft",filter.save()));lastFinished=state.getString("lastFinished",""); restoreNoticeId=state.getLong("noticeId");
            ArrayList<String> history=state.getStringArrayList("exploreHistory");if(history!=null)exploreHistory.addAll(history);
        }
        WindowCompat.setDecorFitsSystemWindows(getWindow(),false);
        back=new OnBackPressedCallback(false) { @Override public void handleOnBackPressed() {goBack();} };
        getOnBackPressedDispatcher().addCallback(this,back);
        render();
        if(restoreNoticeId>0) {
            io(() -> store.notice(restoreNoticeId),notice -> {
                if(!route.equals("reader"))return;
                if(notice!=null)selected=notice;else {restoreNoticeId=0;route="tabs";}
                render();
            });
        }
        io(() -> {
            try(java.io.InputStream input=getAssets().open("schools.json")) {
                JSONArray data=new JSONArray(readText(input));
                List<JSONObject> result=new ArrayList<>(); for(int i=0;i<data.length();i++) result.add(data.getJSONObject(i)); return result;
            }
        },result -> {schools.addAll(result);if(route.equals("directory")) showSchools(query);else if(route.equals("presets"))render();});
        WorkManager.getInstance(this).getWorkInfosForUniqueWorkLiveData(RefreshWorker.MANUAL).observe(this,work -> {
            if(work.isEmpty()) return;
            WorkInfo current=java.util.Collections.max(work,java.util.Comparator.comparingLong(this::requestedAt));
            refreshing=!current.getState().isFinished();
            if(refreshing) {
                int total=current.getProgress().getInt("total",0), checked=current.getProgress().getInt("checked",0);
                setStatus(current.getState()==WorkInfo.State.ENQUEUED?"准备刷新…":"history".equals(current.getProgress().getString("phase"))?"正在补抓历史 · 已保存 "+current.getProgress().getInt("pages",0)+" 页":total==0?"正在连接订阅栏目…":"正在刷新 "+checked+" / "+total,true);
            } else {
                String id=current.getId().toString();
                if(!lastFinished.equals(id)) {
                    lastFinished=id;
                    if(current.getState()==WorkInfo.State.SUCCEEDED) {
                        int failed=current.getOutputData().getInt("failed",0), added=current.getOutputData().getInt("added",0);
                        setStatus(current.getOutputData().getBoolean("paused",false)?current.getOutputData().getString("pauseMessage"):failed>0?"新增 "+added+" 条 · "+failed+" 个栏目读取失败，详情见订阅":"检查完成 · 新增 "+added+" 条",false);
                    } else setStatus("刷新未完成，可再次点击刷新",false);
                    if(route.equals("tabs") && tab!=3) loadTab();
                    else if(route.equals("tabs")) updateCheckpoint();
                }
            }
        });
    }

    private long requestedAt(WorkInfo work) {
        for(String tag:work.getTags()) if(tag.startsWith("requested:")) return Long.parseLong(tag.substring(10));
        return 0;
    }
    @Override protected void onResume() {
        super.onResume();
        RefreshWorker.configure(this);
        RefreshWorker.resume(this);
        android.net.ConnectivityManager network=getSystemService(android.net.ConnectivityManager.class);
        if(network!=null && !networkWatching) {
            automaticNetworkReady=AutomaticNetwork.allowed(this);lastAutomaticNetwork=network.getActiveNetwork();networkWatching=true;
            network.registerDefaultNetworkCallback(networkChanges,handler);
        }
        if(route.equals("tabs") && tab==3) render();
    }
    @Override protected void onPause() {
        if(networkWatching) {
            networkWatching=false;
            getSystemService(android.net.ConnectivityManager.class).unregisterNetworkCallback(networkChanges);
        }
        super.onPause();
    }

    @Override protected void onSaveInstanceState(@NonNull Bundle state) {
        state.putInt("tab",tab);state.putString("route",route);state.putString("query",query);
        state.putString("school",school);state.putString("sourceName",sourceName);state.putString("sourceUrl",sourceUrl);
        state.putString("filterDraft",filterDraft.save());state.putString("lastFinished",lastFinished);
        state.putStringArrayList("exploreHistory",exploreHistory);
        if(route.equals("reader")) state.putLong("noticeId",selected!=null?selected.id:restoreNoticeId);
        super.onSaveInstanceState(state);
    }

    private SharedPreferences prefs() {return getSharedPreferences("settings",0);}
    private int dp(int value) {return Math.round(value*getResources().getDisplayMetrics().density);}
    private int color(int attr) {return MaterialColors.getColor(this,attr,0);}
    private TextView text(String value,int size,boolean bold) {
        TextView view=new TextView(this);view.setText(value);view.setTextSize(size);
        view.setTextColor(color(com.google.android.material.R.attr.colorOnSurface));
        if(bold) view.setTypeface(null,Typeface.BOLD);
        view.setLineSpacing(dp(3),1);return view;
    }
    private LinearLayout column() {LinearLayout layout=new LinearLayout(this);layout.setOrientation(LinearLayout.VERTICAL);return layout;}
    private MaterialButton button(String label,Runnable action) {
        MaterialButton button=new MaterialButton(this,null,com.google.android.material.R.attr.materialButtonOutlinedStyle);button.setText(label);button.setMinHeight(dp(48));
        button.setOnClickListener(v -> action.run());return button;
    }
    private MaterialButton primaryButton(String label,Runnable action) {
        MaterialButton button=new MaterialButton(this);button.setText(label);button.setMinHeight(dp(48));
        button.setOnClickListener(v -> action.run());return button;
    }
    private void feedback(String message) {Snackbar.make(root,message,Snackbar.LENGTH_LONG).show();}
    private void setStatus(String message,boolean busy) {
        status.setText(message);progress.setVisibility(busy?View.VISIBLE:View.GONE);
        if(historyButton!=null)historyButton.setEnabled(!busy);
    }

    private void render() {
        generation++;
        disposeReader();resultCount=null;historyButton=null;
        bottomNavigation=null;
        checkpoint=null;
        root=column();root.setBackgroundColor(color(com.google.android.material.R.attr.colorSurface));
        // New content views need the already-dispatched insets as well as future updates.
        WindowInsetsCompat currentInsets=ViewCompat.getRootWindowInsets(getWindow().getDecorView());
        ViewCompat.setOnApplyWindowInsetsListener(root,(view,insets)-> {
            applyInsets(view,insets);
            // Material navigation must not add a second copy of the same system-bar padding.
            return WindowInsetsCompat.CONSUMED;
        });
        toolbar=new MaterialToolbar(this);
        toolbar.setTitleTextColor(color(com.google.android.material.R.attr.colorOnSurface));
        root.addView(toolbar,new LinearLayout.LayoutParams(-1,dp(64)));
        boolean home=route.equals("tabs");back.setEnabled(!home || tab!=0);
        if(!home) {
            toolbar.setNavigationIcon(R.drawable.ic_back);toolbar.setNavigationIconTint(color(com.google.android.material.R.attr.colorOnSurface));
            toolbar.setNavigationContentDescription("返回");toolbar.setNavigationOnClickListener(v->goBack());
        }
        status=text(refreshing?"正在刷新订阅栏目…":"数据保存在此设备",12,false);
        status.setTextColor(color(com.google.android.material.R.attr.colorOnSurfaceVariant));
        status.setPadding(dp(20),0,dp(20),dp(8));status.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        root.addView(status);
        progress=new LinearProgressIndicator(this);progress.setIndeterminate(true);progress.setVisibility(refreshing?View.VISIBLE:View.GONE);
        root.addView(progress,new LinearLayout.LayoutParams(-1,dp(3)));
        LinearLayout main=new LinearLayout(this);root.addView(main,new LinearLayout.LayoutParams(-1,0,1));
        boolean wide=getResources().getConfiguration().screenWidthDp>=600;
        NavigationBarView nav=wide?new NavigationRailView(this):new BottomNavigationView(this);
        int[] icons={R.drawable.ic_notice,R.drawable.ic_school,R.drawable.ic_bookmark,R.drawable.ic_settings};
        String[] labels={"通知","订阅","收藏","设置"};
        for(int i=0;i<labels.length;i++) nav.getMenu().add(0,i+1,i,labels[i]).setIcon(icons[i]);
        nav.setLabelVisibilityMode(NavigationBarView.LABEL_VISIBILITY_LABELED);nav.setSelectedItemId(tab+1);
        nav.setOnItemSelectedListener(item->{int next=item.getItemId()-1;if(next!=tab || !route.equals("tabs")){tab=next;route="tabs";query="";limit=100;selected=null;render();}return true;});
        if(wide && home) main.addView(nav,new LinearLayout.LayoutParams(dp(96),-1));
        page=column();page.setPadding(dp(20),dp(8),dp(20),0);main.addView(page,new LinearLayout.LayoutParams(0,-1,1));
        if(!wide && home) {
            bottomNavigation=(BottomNavigationView)nav;
            root.addView(nav,new LinearLayout.LayoutParams(-1,-2));
        }
        if(currentInsets!=null) applyInsets(root,currentInsets);
        setContentView(root);
        ViewCompat.requestApplyInsets(root);
        boolean light=(getResources().getConfiguration().uiMode & android.content.res.Configuration.UI_MODE_NIGHT_MASK)!=android.content.res.Configuration.UI_MODE_NIGHT_YES;
        WindowCompat.getInsetsController(getWindow(),root).setAppearanceLightStatusBars(light);
        WindowCompat.getInsetsController(getWindow(),root).setAppearanceLightNavigationBars(light);
        if(Build.VERSION.SDK_INT<35) {
            getWindow().setStatusBarColor(android.graphics.Color.TRANSPARENT);
            getWindow().setNavigationBarColor(Build.VERSION.SDK_INT==26?android.graphics.Color.rgb(32,32,32):color(com.google.android.material.R.attr.colorSurface));
        }
        switch(route) {
            case "directory": directory();break;
            case "presets": presets();break;
            case "filters": filters();break;
            case "explore": explore();break;
            case "add": addForm();break;
            case "reader": if(selected!=null) reader();else {toolbar.setTitle("通知详情");page.addView(text("正在恢复通知…",16,false));}break;
            case "licenses": licenses();break;
            default: tabs();
        }
    }

    private void tabs() {
        toolbar.setTitle(new String[]{"学校通知","我的订阅","收藏","设置"}[tab]);
        if(tab==3) {settings();return;}
        if(tab==1) {
            page.addView(primaryButton("添加订阅",()->{route="directory";query="";render();}));
            page.addView(text("选择关心的学校与栏目，暂停订阅会保留已有通知。",14,false));
        } else {
            toolbar.getMenu().add("刷新").setShowAsAction(android.view.MenuItem.SHOW_AS_ACTION_IF_ROOM);
            toolbar.setOnMenuItemClickListener(item->{RefreshWorker.refresh(this);setStatus("准备刷新…",true);return true;});
            searchField("搜索通知、学校或栏目",query,value->{query=value;limit=100;loadTab();});
            LinearLayout actions=new LinearLayout(this);
            actions.addView(button(filter.active()?"筛选 · 已启用":"筛选",()->{filterDraft=filter.copy();route="filters";render();}),new LinearLayout.LayoutParams(0,-2,1));
            historyButton=button("补抓历史",this::requestHistory);historyButton.setEnabled(!refreshing);
            LinearLayout.LayoutParams historySpace=new LinearLayout.LayoutParams(0,-2,1);historySpace.setMarginStart(dp(8));actions.addView(historyButton,historySpace);page.addView(actions);
            if(filter.active()){TextView summary=text(filter.summary(),12,false);summary.setMaxLines(2);summary.setEllipsize(android.text.TextUtils.TruncateAt.END);page.addView(summary);}
            resultCount=text("正在读取通知数量…",13,false);page.addView(resultCount);
        }
        prepareList();more=button("显示更多已下载通知",()->{limit+=100;loadTab();});more.setVisibility(View.GONE);page.addView(more);
        loadTab();
    }

    private void loadTab() {
        if(!route.equals("tabs") || tab==3 || adapter==null) return;
        int token=++generation;
        if(tab==1) io(()->{
            List<Source> sources=store.sources();
            for(HistoryJournal.Item item:new HistoryJournal(store).items())if(!item.error.isEmpty())
                for(Source source:sources)if(source.id==item.source.id)source.error+=(source.error.isEmpty()?"":"；")+"历史："+item.error;
            return sources;
        }, sources->{
            if(token!=generation) return;
            List<Row> rows=new ArrayList<>();
            for(Source source:sources) {
                String detail=source.school+"\n"+(source.enabled?"订阅中":"已暂停");
                if(!source.error.isEmpty()) detail+=" · "+source.error;
                else if(source.checked>0) detail+=" · "+DateFormat.getDateTimeInstance(DateFormat.SHORT,DateFormat.SHORT).format(new Date(source.checked));
                else detail+=" · 等待首次刷新";
                rows.add(new Row(source.name,detail,()->sourceActions(source),false));
            }
            populate(rows,"还没有订阅\n点击「添加订阅」，找到学校的通知栏目。");
        });
        else {
            String search=query;boolean starred=tab==2;int count=limit;NoticeFilter selectedFilter=filter.copy();
            io(()->new NoticeResults(store.notices(search,starred,selectedFilter,count),store.countNotices(search,starred,selectedFilter)),result->{
                if(token!=generation) return;
                List<Row> rows=new ArrayList<>();
                for(Notice notice:result.rows) rows.add(new Row(notice.title,notice.school+" · "+notice.source+"\n"+notice.date+(notice.read?" · 已读":" · 未读"),()->openNotice(notice),!notice.read));
                populate(rows,selectedFilter.active() || !search.isEmpty()?"没有符合条件的已下载通知\n可调整筛选；较早的内容可点击「补抓历史」。":starred?"还没有收藏\n阅读通知时，点击「收藏」即可保存在这里。":"通知会出现在这里\n先到「订阅」添加栏目，再刷新通知。");
                resultCount.setText((selectedFilter.active() || !search.isEmpty()?"符合条件 ":"已下载 ")+result.total+" 条 · 显示 "+result.rows.size()+" 条");
                more.setVisibility(result.rows.size()<result.total?View.VISIBLE:View.GONE);
            });
        }
    }
    private static final class NoticeResults {
        final List<Notice> rows;final int total;
        NoticeResults(List<Notice> rows,int total){this.rows=rows;this.total=total;}
    }
    private void requestHistory() {
        NoticeFilter selection=filter.copy();
        io(()->new HistoryJournal(store).requestMore(selection),count->{
            if(count==0){feedback("所选范围没有可继续补抓的启用栏目，可调整学校或栏目筛选");return;}
            RefreshWorker.history(this);setStatus("准备补抓历史，使用当前网络…",true);
        });
    }
    private void filters() {
        toolbar.setTitle("筛选通知");LinearLayout content=scrollColumn();
        content.addView(text("筛选已下载的通知。较早的内容可回到列表补抓历史。",14,false));
        content.addView(text("发布日期",18,true));
        MaterialButton start=button("开始日期："+(filterDraft.from.isEmpty()?"不限":filterDraft.from),()->chooseDate(true));
        MaterialButton end=button("结束日期："+(filterDraft.to.isEmpty()?"不限":filterDraft.to),()->chooseDate(false));
        content.addView(start);content.addView(end);
        content.addView(button("选择常用日期范围",()->new MaterialAlertDialogBuilder(this).setTitle("日期范围")
                .setItems(new String[]{"不限日期","最近 7 天","最近 30 天","最近 90 天","本年"},(dialog,which)->{
                    java.time.LocalDate today=java.time.LocalDate.now();
                    filterDraft.to=which==0?"":today.toString();
                    filterDraft.from=which==0?"":which==4?today.withDayOfYear(1).toString():today.minusDays(new int[]{0,6,29,89}[which]).toString();render();
                }).setNegativeButton("取消",null).show()));
        content.addView(button("学校："+(filterDraft.school.isEmpty()?"全部学校":filterDraft.school),this::chooseFilterSchool));
        content.addView(button("栏目："+(filterDraft.sourceId==0?"全部栏目":filterDraft.sourceName),this::chooseFilterSource));
        String[] readOptions={"全部阅读状态","未读","已读"};
        content.addView(button("阅读状态："+readOptions[filterDraft.read],()->new MaterialAlertDialogBuilder(this).setTitle("阅读状态")
                .setSingleChoiceItems(readOptions,filterDraft.read,(dialog,which)->{filterDraft.read=which;dialog.dismiss();render();}).setNegativeButton("取消",null).show()));
        content.addView(button("排序："+(filterDraft.oldestFirst?"最早优先":"最新优先"),()->new MaterialAlertDialogBuilder(this).setTitle("时间排序")
                .setSingleChoiceItems(new String[]{"最新优先","最早优先"},filterDraft.oldestFirst?1:0,(dialog,which)->{filterDraft.oldestFirst=which==1;dialog.dismiss();render();}).setNegativeButton("取消",null).show()));
        content.addView(button("重置全部筛选",()->{filterDraft=new NoticeFilter();render();}));
        page.addView(primaryButton("应用筛选",()->{
            if(!filterDraft.valid()){feedback("开始日期不能晚于结束日期");return;}
            filter=filterDraft.copy();prefs().edit().putString("noticeFilters",filter.save()).apply();limit=100;route="tabs";render();
        }));
    }
    private void chooseDate(boolean start) {
        String saved=start?filterDraft.from:filterDraft.to;
        java.time.LocalDate selected=saved.isEmpty()?java.time.LocalDate.now():java.time.LocalDate.parse(saved);
        android.app.DatePickerDialog picker=new android.app.DatePickerDialog(this,(view,year,month,day)->{
            String chosen=java.time.LocalDate.of(year,month+1,day).toString();
            if(start)filterDraft.from=chosen;else filterDraft.to=chosen;render();
        },selected.getYear(),selected.getMonthValue()-1,selected.getDayOfMonth());
        picker.setButton(android.app.Dialog.BUTTON_NEUTRAL,"不限",(dialog,which)->{if(start)filterDraft.from="";else filterDraft.to="";render();});picker.show();
    }
    private void chooseFilterSchool() {
        io(store::sources,sources->{
            if(!route.equals("filters"))return;
            List<String> options=new ArrayList<>();options.add("全部学校");
            for(Source source:sources)if(!options.contains(source.school))options.add(source.school);
            new MaterialAlertDialogBuilder(this).setTitle("选择学校").setItems(options.toArray(new String[0]),(dialog,which)->{
                filterDraft.school=which==0?"":options.get(which);filterDraft.sourceId=0;filterDraft.sourceName="";render();
            }).setNegativeButton("取消",null).show();
        });
    }
    private void chooseFilterSource() {
        io(store::sources,sources->{
            if(!route.equals("filters"))return;
            List<Source> choices=new ArrayList<>();List<String> labels=new ArrayList<>();labels.add("全部栏目");
            for(Source source:sources)if(filterDraft.school.isEmpty() || filterDraft.school.equals(source.school)){
                choices.add(source);labels.add((filterDraft.school.isEmpty()?source.school+" · ":"")+source.name);
            }
            new MaterialAlertDialogBuilder(this).setTitle("选择栏目").setItems(labels.toArray(new String[0]),(dialog,which)->{
                filterDraft.sourceId=which==0?0:choices.get(which-1).id;filterDraft.sourceName=which==0?"":choices.get(which-1).name;render();
            }).setNegativeButton("取消",null).show();
        });
    }

    private void sourceActions(Source source) {
        new MaterialAlertDialogBuilder(this).setTitle(source.name).setItems(new String[]{source.enabled?"暂停订阅（保留通知）":"恢复订阅","查找下级栏目","打开官网"},(dialog,which)->{
            if(which==0) io(()->{store.setEnabled(source.id,!source.enabled);return true;},done->{loadTab();feedback(source.enabled?"已暂停，历史通知保留":"已恢复订阅");});
            if(which==1) openExplore(source.school,source.name,source.url);
            if(which==2) openUrl(source.url);
        }).setNegativeButton("取消",null).show();
    }

    private void directory() {
        toolbar.setTitle("添加订阅");
        searchField("搜索学校、简称或省份",query,value->{query=value;showSchools(value);});
        page.addView(button("输入学校与栏目地址",()->{route="add";render();}));
        page.addView(text("带内置栏目的学校可直接订阅，其他学校可从官网查找。",14,false));
        prepareList();showSchools(query);
    }
    private void showSchools(String filter) {
        List<Row> rows=new ArrayList<>();
        List<JSONObject> ordered=new ArrayList<>(schools);
        ordered.sort(java.util.Comparator.comparing(entry->SchoolDirectory.presets(entry).isEmpty()));
        for(JSONObject entry:ordered) {
            String name=entry.optString("name"),province=entry.optString("province"),url=entry.optString("url");
            if(!SchoolDirectory.matches(entry,filter)) continue;
            int count=SchoolDirectory.presets(entry).size();
            String detail=(province.isEmpty()?Uri.parse(url).getHost():province)+(count==0?"":" · 内置 "+count+" 个栏目");
            rows.add(new Row(name,detail,()->{
                if(count==0)openExplore(name,"学校官网",url);
                else {school=name;sourceUrl=url;query="";route="presets";render();}
            },false));
        }
        populate(rows,schools.isEmpty()?"正在读取学校目录…":"未找到学校\n可以直接输入学校名称和栏目地址。");
    }
    private void presets() {
        toolbar.setTitle("内置栏目");page.addView(text(school,20,true));
        List<Source> sources=new ArrayList<>();
        for(JSONObject entry:schools) if(entry.optString("name").equals(school)) sources.addAll(SchoolDirectory.presets(entry));
        page.addView(text("可单独订阅，也可一次添加全部栏目。订阅后立即抓取，使用当前网络。",14,false));
        MaterialButton all=primaryButton("订阅全部 "+sources.size()+" 个栏目并抓取",()->subscribeAll(sources));
        all.setEnabled(!sources.isEmpty());page.addView(all);
        page.addView(button("从官网查找更多栏目",()->openExplore(school,"学校官网",sourceUrl)));
        prepareList();List<Row> rows=new ArrayList<>();
        for(Source source:sources) rows.add(new Row(source.name,"官方栏目 · 点击订阅",()->
            new MaterialAlertDialogBuilder(this).setTitle(source.name).setMessage("订阅后立即读取此栏目的最新列表。已有通知和收藏会保留。")
                .setPositiveButton("订阅并抓取",(dialog,which)->subscribe(source))
                .setNeutralButton("打开官网",(dialog,which)->openUrl(source.url)).setNegativeButton("取消",null).show(),false));
        populate(rows,"正在读取内置栏目…");
    }
    private void openExplore(String schoolName,String name,String url) {
        if(route.equals("explore")) {exploreHistory.add(school);exploreHistory.add(sourceName);exploreHistory.add(sourceUrl);}
        else exploreHistory.clear();
        school=schoolName;sourceName=name;sourceUrl=url;query="";route="explore";render();
    }
    private void explore() {
        toolbar.setTitle(sourceName);page.addView(text(school,20,true));
        TextView address=text(sourceUrl,12,false);address.setTextIsSelectable(true);page.addView(address);
        page.addView(primaryButton("订阅当前栏目",()->subscribe(new Source(school,sourceName,sourceUrl))));
        page.addView(button("在浏览器打开",()->openUrl(sourceUrl)));
        prepareList();empty.setText("正在查找通知与下级栏目…");
        int token=generation;String url=sourceUrl,schoolLabel=school;
        io(()->PublicWeb.fetch(url),html->{
            if(token!=generation) return;
            List<Source> candidates=NoticeParser.discover(html,url,schoolLabel);List<Row> rows=new ArrayList<>();
            for(Source candidate:candidates) rows.add(new Row(candidate.name,Uri.parse(candidate.url).getHost(),()->openExplore(candidate.school,candidate.name,candidate.url),false));
            int found=NoticeParser.notices(html,url).size();
            setStatus(found>0?"当前页面识别到 "+found+" 条通知 · 可订阅":"选择下级通知栏目后订阅；未识别到通知的页面可能需要动态加载",false);
            populate(rows,found>0?"可订阅当前页面\n未发现其他下级栏目。":"没有找到可识别栏目\n可打开官网查找通知列表，再输入该栏目地址。");
        },error->{if(token==generation){empty.setText(RefreshWorker.friendlyError(error));setStatus("官网读取失败，可重试或在浏览器打开",false);page.addView(button("重试",this::render));}});
    }
    private void subscribe(Source source) {
        io(()->store.subscribe(source),id->{tab=1;route="tabs";query="";render();RefreshWorker.refresh(this);feedback("已订阅，正在准备刷新");});
    }
    private void subscribeAll(List<Source> sources) {
        io(()->store.subscribeAll(sources),count->{tab=1;route="tabs";query="";render();RefreshWorker.refresh(this);feedback("已订阅 "+count+" 个栏目，正在准备刷新");});
    }
    private void addForm() {
        toolbar.setTitle("添加栏目");LinearLayout form=scrollColumn();
        form.addView(text("填写公开的通知列表地址",22,true));
        form.addView(text("可从学校官网复制通知公告、教务或学院栏目的网址。",15,false));
        TextInputEditText schoolInput=input(form,"学校名称",school);
        TextInputEditText nameInput=input(form,"栏目名称","");
        TextInputEditText urlInput=input(form,"栏目地址（https://…）","");urlInput.setInputType(android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_VARIATION_URI);
        form.addView(primaryButton("保存并刷新",()->{
            String schoolName=schoolInput.getText().toString().trim(),name=nameInput.getText().toString().trim(),url=NoticeParser.httpUrl(urlInput.getText().toString());
            if(schoolName.isEmpty()){schoolInput.setError("请输入学校名称");return;}
            if(name.isEmpty()){nameInput.setError("请输入栏目名称");return;}
            if(url.isEmpty()){urlInput.setError("请输入完整的 HTTP 或 HTTPS 地址");return;}
            subscribe(new Source(schoolName,name,url));
        }));
    }

    private void openNotice(Notice notice) {selected=notice;route="reader";render();io(()->{store.setRead(notice.id,true);notice.read=true;return true;},done->{});}
    private void reader() {
        toolbar.setTitle("通知详情");Notice notice=selected;
        LinearLayout actions=new LinearLayout(this);
        MaterialButton bookmark=button(notice.starred?"取消收藏":"收藏",()->{
            boolean next=!notice.starred;io(()->{store.setStarred(notice.id,next);return next;},value->{notice.starred=value;updateBookmarkLabel(actions,value);});
        });
        actions.addView(bookmark,new LinearLayout.LayoutParams(0,-2,1));
        MaterialButton original=button("原文",()->openUrl(notice.url));
        LinearLayout.LayoutParams other=new LinearLayout.LayoutParams(0,-2,1);other.setMarginStart(dp(8));actions.addView(original,other);
        MaterialButton share=button("分享",()->{
            Intent shareIntent=new Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT,notice.title+"\n"+notice.url);
            startActivity(Intent.createChooser(shareIntent,"分享通知"));
        });LinearLayout.LayoutParams shareSpace=new LinearLayout.LayoutParams(0,-2,1);shareSpace.setMarginStart(dp(8));actions.addView(share,shareSpace);
        for(MaterialButton action:new MaterialButton[]{bookmark,original,share})action.setPadding(dp(8),0,dp(8),0);
        page.addView(actions);
        android.webkit.WebView web=new android.webkit.WebView(this);readerView=web;
        android.webkit.WebSettings settings=web.getSettings();settings.setJavaScriptEnabled(false);settings.setAllowFileAccess(false);settings.setAllowContentAccess(false);
        settings.setBlockNetworkLoads(true);settings.setDomStorageEnabled(false);settings.setCacheMode(android.webkit.WebSettings.LOAD_NO_CACHE);
        settings.setTextZoom(Math.round(getResources().getConfiguration().fontScale*100));
        web.setBackgroundColor(color(com.google.android.material.R.attr.colorSurface));
        if((getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE)!=0)android.webkit.WebView.setWebContentsDebuggingEnabled(true);
        web.setWebViewClient(new android.webkit.WebViewClient(){
            @Override public boolean shouldOverrideUrlLoading(android.webkit.WebView view,android.webkit.WebResourceRequest request){openUrl(request.getUrl().toString());return true;}
        });
        page.addView(web,new LinearLayout.LayoutParams(-1,0,1));showArticle(web,notice,"<p>正在读取正文…</p>");
        int token=generation;
        io(()->{
            String cached=store.articleHtml(notice.id);if(!cached.isEmpty())return cached;
            try {
                String fetched=PublicWeb.fetch(notice.url),formatted=ArticleFormatter.fragment(fetched,notice.url);
                store.saveArticle(notice.id,formatted,NoticeParser.body(fetched,notice.url));return formatted;
            } catch(Exception failure) {
                String legacy=store.body(notice.id);
                if(legacy.isEmpty())throw failure;
                return "<p class='tip'>当前显示旧版离线正文。联网后重新打开，可恢复表格排版。</p>"+ArticleFormatter.legacy(legacy);
            }
        },value->{if(token==generation)showArticle(web,notice,value);},error->{if(token==generation){showArticle(web,notice,ArticleFormatter.legacy(RefreshWorker.friendlyError(error)));page.addView(button("重新读取正文",this::render));}});
    }
    private void updateBookmarkLabel(LinearLayout actions,boolean saved) {((MaterialButton)actions.getChildAt(0)).setText(saved?"取消收藏":"收藏");}
    private String cssColor(int attr){return String.format(java.util.Locale.ROOT,"#%06X",color(attr)&0xffffff);}
    private void showArticle(android.webkit.WebView web,Notice notice,String fragment) {
        String document=ArticleFormatter.document(notice,fragment,cssColor(com.google.android.material.R.attr.colorSurface),cssColor(com.google.android.material.R.attr.colorOnSurface),
                cssColor(com.google.android.material.R.attr.colorOnSurfaceVariant),cssColor(com.google.android.material.R.attr.colorOutline),cssColor(com.google.android.material.R.attr.colorPrimary));
        web.loadDataWithBaseURL("https://reader.schoolwatcher.invalid/",document,"text/html","UTF-8",null);
    }
    private void disposeReader(){if(readerView!=null){readerView.stopLoading();readerView.destroy();readerView=null;}}
    @Override protected void onDestroy(){disposeReader();super.onDestroy();}

    private void settings() {
        LinearLayout content=scrollColumn();content.addView(text("更新与提醒",22,true));
        toggle(content,"后台刷新","每小时尝试检查；关闭后保留进度，下次打开应用时恢复。","background",false,()->{
            RefreshWorker.configure(this);render();
            if(prefs().getBoolean("background",false) && !BackgroundAccess.allowed(this)) explainBackground();
        });
        content.addView(text(BackgroundAccess.status(this),14,false));
        content.addView(button("系统后台与电池设置",this::explainBackground));
        checkpoint=text("正在读取抓取进度…",14,false);content.addView(checkpoint);updateCheckpoint();
        toggle(content,"自动抓取仅用 Wi-Fi","开启：只用 Wi-Fi 自动抓取。关闭：Wi-Fi 或手机流量均可自动抓取。手动刷新使用当前网络。","wifi",true,()->{
            automaticNetworkReady=AutomaticNetwork.allowed(this);
            RefreshWorker.configure(this);RefreshWorker.resume(this);
        });
        MaterialSwitch notifications=new MaterialSwitch(this);notifications.setText("新通知提醒");notifications.setMinHeight(dp(56));
        boolean allowed=Build.VERSION.SDK_INT<33 || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)==PackageManager.PERMISSION_GRANTED;
        notifications.setChecked(prefs().getBoolean("notify",false)&&allowed);
        notifications.setOnCheckedChangeListener((button,checked)->{
            if(checked && Build.VERSION.SDK_INT>=33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED) permission.launch(Manifest.permission.POST_NOTIFICATIONS);
            else prefs().edit().putBoolean("notify",checked).apply();
        });content.addView(notifications);
        TextView appearance=text("外观与存储",22,true);appearance.setPadding(0,dp(24),0,dp(8));content.addView(appearance);
        content.addView(button("主题："+themeName(),()->{
            int[] modes={AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM,AppCompatDelegate.MODE_NIGHT_NO,AppCompatDelegate.MODE_NIGHT_YES};
            new MaterialAlertDialogBuilder(this).setTitle("选择主题").setItems(new String[]{"跟随系统","浅色","深色"},(dialog,which)->{
                prefs().edit().putInt("theme",modes[which]).apply();AppCompatDelegate.setDefaultNightMode(modes[which]);
            }).setNegativeButton("取消",null).show();
        }));
        content.addView(button("清理离线正文",()->new MaterialAlertDialogBuilder(this).setTitle("清理离线正文？").setMessage("通知标题、订阅、已读和收藏都会保留。下次阅读时需要联网重新获取正文。")
                .setNegativeButton("取消",null).setPositiveButton("清理",(dialog,which)->io(()->{store.clearBodies();return true;},done->feedback("离线正文已清理"))).show()));
        TextView about=text("学校通知 · Android 0.1.4",20,true);about.setPadding(0,dp(24),0,dp(12));content.addView(about);
        content.addView(text("无需账号，数据仅存于此设备。卸载会移除本机数据。\n\n此版本支持公开静态栏目、离线文字正文和本机提醒。复杂动态页面、验证码、AI 摘要及桌面数据同步暂未接入。\n\n内置学校目录不代表所有栏目已适配。通知内容以学校官网为准。",15,false));
        content.addView(button("许可与第三方声明",()->{route="licenses";render();}));
    }
    private void updateCheckpoint() {
        TextView target=checkpoint;if(target==null)return;
        io(()->new RefreshJournal(store).snapshot(),state->{
            if(!target.isAttachedToWindow())return;
            String time=state.started==0?"尚未开始抓取":"本轮开始："+DateFormat.getDateTimeInstance(DateFormat.SHORT,DateFormat.SHORT).format(new Date(state.started));
            target.setText(time+"\n已检查 "+state.checked+" / "+state.total+" 个栏目。重新打开时，满一小时开始新一轮，未满则继续剩余栏目。");
        });
    }
    private void explainBackground() {
        new MaterialAlertDialogBuilder(this).setTitle("允许后台刷新")
                .setMessage("可在系统电池优化中找到「学校通知」，选择不优化或不受限制。vivo、OPPO 等手机还可能需要在应用信息中允许后台活动或自启动，具体名称以手机为准。\n\n撤销授权后会保留进度，回到应用时继续。强行停止后，需再次打开应用才会恢复。授权也不能保证系统始终保持运行。")
                .setPositiveButton("电池优化设置",(dialog,which)->openSystemSettings(new Intent(android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)))
                .setNeutralButton("应用信息",(dialog,which)->openSystemSettings(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,Uri.parse("package:"+getPackageName()))))
                .setNegativeButton("暂不开启",null).show();
    }
    private void openSystemSettings(Intent intent) {
        try {startActivity(intent);}
        catch(android.content.ActivityNotFoundException | SecurityException error) {
            try {startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,Uri.parse("package:"+getPackageName())));}
            catch(android.content.ActivityNotFoundException | SecurityException unavailable) {feedback("请在手机设置的应用管理中找到「学校通知」，查看后台与电池选项");}
        }
    }
    private void toggle(LinearLayout parent,String label,String explanation,String key,boolean fallback,Runnable action) {
        MaterialSwitch toggle=new MaterialSwitch(this);toggle.setText(label);toggle.setMinHeight(dp(56));toggle.setChecked(prefs().getBoolean(key,fallback));
        toggle.setOnCheckedChangeListener((button,checked)->{prefs().edit().putBoolean(key,checked).apply();action.run();});parent.addView(toggle);
        TextView detail=text(explanation,13,false);detail.setPadding(0,0,0,dp(12));parent.addView(detail);
    }
    private String themeName() {int mode=prefs().getInt("theme",-1);return mode==1?"浅色":mode==2?"深色":"跟随系统";}
    private void licenses() {
        toolbar.setTitle("许可与第三方声明");LinearLayout content=scrollColumn();TextView legal=text("正在读取…",14,false);legal.setTextIsSelectable(true);content.addView(legal);
        io(()->{
            StringBuilder value=new StringBuilder();
            for(String file:new String[]{"LICENSE.txt","THIRD_PARTY_NOTICES.txt","Apache-2.0.txt","jsoup-LICENSE.txt"}) {
                try(java.io.InputStream input=getAssets().open(file)){value.append(readText(input)).append("\n\n");}
            }
            return value.toString();
        },legal::setText);
    }
    private LinearLayout scrollColumn() {
        ScrollView scroll=new ScrollView(this);scroll.setFillViewport(true);page.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));
        LinearLayout column=column();int gutter=dp(Math.max(0,(getResources().getConfiguration().screenWidthDp-800)/2));
        column.setPadding(gutter,dp(8),gutter,dp(32));scroll.addView(column,new ScrollView.LayoutParams(-1,-2));return column;
    }
    private TextInputEditText input(LinearLayout parent,String hint,String initial) {
        TextInputLayout layout=new TextInputLayout(this);layout.setHint(hint);layout.setBoxBackgroundMode(TextInputLayout.BOX_BACKGROUND_OUTLINE);
        TextInputEditText field=new TextInputEditText(layout.getContext());field.setSingleLine(true);field.setText(initial);field.setTextSize(16);layout.addView(field);
        LinearLayout.LayoutParams params=new LinearLayout.LayoutParams(-1,-2);params.topMargin=dp(12);params.bottomMargin=dp(4);parent.addView(layout,params);return field;
    }
    private void searchField(String hint,String initial,Consumer<String> change) {
        TextInputEditText field=input(page,hint,initial);
        field.addTextChangedListener(new TextWatcher() {
            private Runnable pending;
            public void beforeTextChanged(CharSequence value,int start,int count,int after) {}
            public void onTextChanged(CharSequence value,int start,int before,int count) {
                if(pending!=null)handler.removeCallbacks(pending);
                String next=value.toString().trim();pending=()->{if(field.isAttachedToWindow())change.accept(next);};handler.postDelayed(pending,180);
            }
            public void afterTextChanged(Editable value) {}
        });
    }
    private void prepareList() {
        empty=text("正在读取…",16,false);empty.setGravity(Gravity.CENTER);empty.setPadding(dp(12),dp(24),dp(12),dp(24));page.addView(empty,new LinearLayout.LayoutParams(-1,-2));
        RecyclerView list=new RecyclerView(this);list.setLayoutManager(new LinearLayoutManager(this));list.setClipToPadding(false);list.setPadding(0,dp(8),0,dp(16));
        adapter=new RowAdapter();list.setAdapter(adapter);page.addView(list,new LinearLayout.LayoutParams(-1,0,1));
    }
    private void populate(List<Row> rows,String message) {adapter.rows=rows;adapter.notifyDataSetChanged();empty.setText(message);empty.setVisibility(rows.isEmpty()?View.VISIBLE:View.GONE);}
    private void goBack() {
        if(route.equals("tabs")){tab=0;query="";}
        else if(route.equals("explore") && exploreHistory.size()>=3) {
            sourceUrl=exploreHistory.remove(exploreHistory.size()-1);sourceName=exploreHistory.remove(exploreHistory.size()-1);school=exploreHistory.remove(exploreHistory.size()-1);
        }
        else if(route.equals("explore") || route.equals("add") || route.equals("presets")){route="directory";query="";}
        else route="tabs";
        selected=null;render();
    }
    private void applyInsets(View view,WindowInsetsCompat insets) {
        Insets bars=insets.getInsets(WindowInsetsCompat.Type.systemBars()|WindowInsetsCompat.Type.displayCutout());
        Insets keyboard=insets.getInsets(WindowInsetsCompat.Type.ime());
        // A docked keyboard needs the compact navigation's space for search results.
        // Keep the expanded side rail, and keep navigation with a floating keyboard.
        if(bottomNavigation!=null) {
            boolean dockedKeyboard=insets.isVisible(WindowInsetsCompat.Type.ime()) && keyboard.bottom>bars.bottom;
            bottomNavigation.setVisibility(dockedKeyboard?View.GONE:View.VISIBLE);
        }
        view.setPadding(bars.left,bars.top,bars.right,Math.max(bars.bottom,keyboard.bottom));
    }
    private void openUrl(String value) {
        String url=NoticeParser.httpUrl(value);if(url.isEmpty()){feedback("网页地址无效");return;}
        try {startActivity(new Intent(Intent.ACTION_VIEW,Uri.parse(url)));}
        catch(android.content.ActivityNotFoundException error) {feedback("未找到浏览器，请先安装浏览器");}
    }
    private <T> void io(Callable<T> task,Consumer<T> success) {io(task,success,error->feedback(RefreshWorker.friendlyError(error)));}
    private static String readText(java.io.InputStream input) throws java.io.IOException {
        java.io.ByteArrayOutputStream bytes=new java.io.ByteArrayOutputStream();byte[] buffer=new byte[8192];int count;
        while((count=input.read(buffer))!=-1)bytes.write(buffer,0,count);
        return bytes.toString(StandardCharsets.UTF_8.name());
    }
    private <T> void io(Callable<T> task,Consumer<T> success,Consumer<Exception> failure) {
        WatcherApplication.IO.execute(()->{
            try {T value=task.call();runOnUiThread(()->{if(!isFinishing()&&!isDestroyed())success.accept(value);});}
            catch(Exception error){runOnUiThread(()->{if(!isFinishing()&&!isDestroyed())failure.accept(error);});}
        });
    }
    private static final class Row {
        final String title,detail;final Runnable action;final boolean unread;
        Row(String title,String detail,Runnable action,boolean unread){this.title=title;this.detail=detail;this.action=action;this.unread=unread;}
    }
    private final class RowAdapter extends RecyclerView.Adapter<RowHolder> {
        List<Row> rows=new ArrayList<>();
        @NonNull public RowHolder onCreateViewHolder(@NonNull ViewGroup parent,int type) {
            LinearLayout container=column();container.setMinimumHeight(dp(88));container.setPadding(dp(4),dp(16),dp(4),dp(16));
            android.util.TypedValue background=new android.util.TypedValue();getTheme().resolveAttribute(android.R.attr.selectableItemBackground,background,true);container.setBackgroundResource(background.resourceId);
            container.setFocusable(true);container.setClickable(true);container.setLayoutParams(new RecyclerView.LayoutParams(-1,-2));
            TextView title=text("",16,false),detail=text("",13,false);title.setMaxLines(4);title.setEllipsize(android.text.TextUtils.TruncateAt.END);
            detail.setTextColor(color(com.google.android.material.R.attr.colorOnSurfaceVariant));detail.setPadding(0,dp(7),0,0);
            container.addView(title);container.addView(detail);return new RowHolder(container,title,detail);
        }
        public void onBindViewHolder(@NonNull RowHolder holder,int position) {
            Row row=rows.get(position);holder.title.setText(row.title);holder.title.setTypeface(null,row.unread?Typeface.BOLD:Typeface.NORMAL);holder.detail.setText(row.detail);
            holder.itemView.setOnClickListener(view->row.action.run());
        }
        public int getItemCount(){return rows.size();}
    }
    private static final class RowHolder extends RecyclerView.ViewHolder {
        final TextView title,detail;
        RowHolder(View view,TextView title,TextView detail){super(view);this.title=title;this.detail=detail;}
    }
}
