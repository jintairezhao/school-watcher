package cn.schoolwatcher.app;

import android.Manifest;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import androidx.annotation.NonNull;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;
import androidx.work.Constraints;
import androidx.work.Data;
import androidx.work.ExistingPeriodicWorkPolicy;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.PeriodicWorkRequest;
import androidx.work.WorkManager;
import androidx.work.Worker;
import androidx.work.WorkerParameters;
import java.util.concurrent.TimeUnit;

public final class RefreshWorker extends Worker {
    public static final String MANUAL = "refresh-manual", PERIODIC = "refresh-periodic";
    private static final Object COLLECTION_LOCK = new Object();
    private android.net.Network automaticRoute;
    private boolean networkInterrupted;
    public RefreshWorker(@NonNull Context context,@NonNull WorkerParameters params) { super(context,params); }

    public static void refresh(Context context) {
        // A user-requested check must work when the school is reachable even if Android's
        // external connectivity probe is blocked. The fetch reports actual network errors.
        enqueue(context,true);
    }
    public static void resume(Context context) { enqueue(context,false); }
    public static void history(Context context) {enqueue(context,true,true);}
    private static void enqueue(Context context,boolean manual) {
        enqueue(context,manual,false);
    }
    private static void enqueue(Context context,boolean manual,boolean historyOnly) {
        long requestedAt=System.currentTimeMillis();
        WorkManager.getInstance(context).enqueueUniqueWork(MANUAL,ExistingWorkPolicy.APPEND_OR_REPLACE,
                new OneTimeWorkRequest.Builder(RefreshWorker.class)
                        .addTag("requested:"+requestedAt)
                        .setInputData(new Data.Builder().putLong("manualRequestedAt",manual?requestedAt:0).putBoolean("historyOnly",historyOnly).build()).build());
    }
    public static void configure(Context context) {
        WorkManager work=WorkManager.getInstance(context);
        if(!BackgroundAccess.allowed(context)) {work.cancelUniqueWork(PERIODIC);return;}
        // WorkManager has no WIFI constraint. Enforce the actual transport before any
        // automatic request, including foreground recovery, and bind its connection.
        Constraints constraints=new Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).setRequiresBatteryNotLow(true).build();
        work.enqueueUniquePeriodicWork(PERIODIC,ExistingPeriodicWorkPolicy.UPDATE,
                new PeriodicWorkRequest.Builder(RefreshWorker.class,1,TimeUnit.HOURS).setConstraints(constraints)
                        .setInputData(new Data.Builder().putBoolean("background",true).build()).build());
    }

    @NonNull @Override public Result doWork() {
        synchronized (COLLECTION_LOCK) {return collect();}
    }
    private Result collect() {
        WatcherStore store=((WatcherApplication)getApplicationContext()).store();
        RefreshCollection.Outcome outcome=getInputData().getBoolean("historyOnly",false)
                ?new RefreshCollection.Outcome(new RefreshJournal(store).snapshot(),false,0)
                :RefreshCollection.run(new RefreshJournal(store),getInputData().getLong("manualRequestedAt",0),
                System::currentTimeMillis,this::permitted,
                source->NoticeParser.notices(fetch(source.url),source.url),
                state->setProgressAsync(new Data.Builder().putInt("checked",state.checked).putInt("total",state.total).build()));
        HistoryCollection.Outcome history=outcome.paused?new HistoryCollection.Outcome():HistoryCollection.run(new HistoryJournal(store),this::permitted,this::fetch,
                pages->setProgressAsync(new Data.Builder().putString("phase","history").putInt("pages",pages).build()));
        boolean paused=outcome.paused || history.paused || history.remaining;
        if(!paused && outcome.added>0 && getApplicationContext().getSharedPreferences("settings",0).getBoolean("notify",false)) notifyNew(outcome.added);
        return Result.success(new Data.Builder().putInt("added",Math.max(0,outcome.added)+history.added).putInt("failed",outcome.snapshot.failed+history.failed)
                .putInt("checked",outcome.snapshot.checked).putBoolean("paused",paused)
                .putString("pauseMessage",history.remaining?"本批历史已保存，点击补抓历史继续":isAutomatic() && (!AutomaticNetwork.allowed(getApplicationContext()) || networkInterrupted)
                        ?AutomaticNetwork.waitingMessage(getApplicationContext()):"抓取已暂停，打开应用后继续").build());
    }
    private boolean isAutomatic() {return getInputData().getLong("manualRequestedAt",0)==0;}
    private String fetch(String url) throws java.io.IOException {
        if(!isAutomatic()) return PublicWeb.fetch(url,this::permitted);
        automaticRoute=AutomaticNetwork.selected(getApplicationContext());
        if(automaticRoute==null) {networkInterrupted=true;throw new java.io.InterruptedIOException("等待符合设置的网络");}
        android.net.Network route=automaticRoute;
        return PublicWeb.fetch(url,this::permitted,new PublicWeb.Route() {
            public java.net.InetAddress[] resolve(String host) throws java.io.IOException {return route.getAllByName(host);}
            public java.net.HttpURLConnection open(java.net.URL target) throws java.io.IOException {return (java.net.HttpURLConnection)route.openConnection(target);}
        });
    }
    private boolean permitted() {
        if(isStopped() || Thread.currentThread().isInterrupted()) return false;
        WatcherApplication app=(WatcherApplication)getApplicationContext();
        if((getInputData().getBoolean("background",false) || !app.isVisible()) && !BackgroundAccess.allowed(app)) return false;
        return !isAutomatic() || (!networkInterrupted && AutomaticNetwork.allowed(app)
                && (automaticRoute==null || AutomaticNetwork.allowed(app,automaticRoute)));
    }
    public static String friendlyError(Exception error) {
        if(error instanceof java.net.UnknownHostException) return "无法连接官网，请检查网络后重试";
        if(error instanceof java.net.SocketTimeoutException) return "官网响应超时，请稍后重试";
        if(error instanceof javax.net.ssl.SSLException) return "官网安全证书异常，请在浏览器中核实";
        String message=error.getMessage();
        return message==null?"读取失败，请稍后重试或打开官网":message;
    }
    private void notifyNew(int count) {
        Context context=getApplicationContext();
        if(android.os.Build.VERSION.SDK_INT>=33 && ContextCompat.checkSelfPermission(context,Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED) return;
        PendingIntent intent=PendingIntent.getActivity(context,0,new Intent(context,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        try {
            NotificationManagerCompat.from(context).notify(1,new NotificationCompat.Builder(context,"notices")
                    .setSmallIcon(R.drawable.ic_notice).setContentTitle("发现 "+count+" 条新通知")
                    .setContentText("打开学校通知查看订阅栏目的更新").setContentIntent(intent).setAutoCancel(true).build());
        } catch(SecurityException ignored) { /* User may revoke permission while work is running. */ }
    }
}
